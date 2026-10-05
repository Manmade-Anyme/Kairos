# TASK-138 Review Report

**Verdict:** APPROVE — initial P2 finding corrected and re-reviewed; no remaining blockers.
**Review date:** 5 October 2026 (IST).
**Scope:** Current uncommitted diff against `origin/main`, including the new migration and outage tests. Reviewed against `2026-10-05_discord-delivery-audit.md`, ADR-006, and the supplied recovery scope. No source/test edits or production operations performed by this reviewer.

## Resolved P2 — A failed recovery delivery suppressed the next outage warning

**Axis:** Logic/runtime safety; grounded by a deterministic, network-free execution.

**Location:** [scheduler.py](/Users/manmadeanyme/Documents/Work/Kairos/src/kairos/scheduler.py:302), final helper at lines 297–323. The excerpt below shows the superseded implementation reviewed initially.

```python
if not state.supabase_warning_sent:
    sent = await notifier.post_critical_alert(...)
    state.supabase_warning_sent = bool(sent)
...
state.supabase_ok = True
if state.supabase_warning_sent:
    sent = await notifier.post_supabase_recovered()
    if sent:
        state.supabase_warning_sent = False
return config
```

**Trigger:** Outage A's critical warning is delivered. A later read succeeds, but Discord rejects or times out the recovery POST. The bridge then fails again during outage B.

**Execution:** The successful read sets `supabase_ok=True` and returns the active configuration, permitting scoring and environment notifications to resume. Failed recovery delivery leaves `supabase_warning_sent=True`. On outage B the flag suppresses `post_critical_alert`, even though this is a new failure after proven bridge recovery. If a fresh GO arrived during the healthy interval, the original outage warning predates that signal and no new pause warning follows it.

**Reproduction:** Ran the real `read_session_with_health_check` with reads `[SessionReadError('outage 1'), None, SessionReadError('outage 2')]`, a successful critical POST, and a failed recovery POST. After the healthy read: `supabase_ok=True`, `supabase_warning_sent=True`. After outage B: `supabase_ok=False`, total critical POST count **1**, recovery count **1**. This control-flow failure also occurs with an ACTIVE configuration because the helper returns it without another guard.

**Verified correction:** The final helper tracks `supabase_recovery_pending` separately from the per-outage `supabase_warning_sent`. A successful read clears the outage warning state immediately and marks recovery delivery pending. A new failed read cancels pending recovery and permits a fresh warning. The new `test_new_outage_warns_when_previous_recovery_message_failed` passes. Independently replayed two outages interleaved with healthy reads and failed recovery deliveries: two critical warnings, three recovery delivery attempts, final health state healthy with both flags clear. Existing uninterrupted healthy-read retry coverage still passes.

## Other review results

- **Spec/schema:** Additive bigint OI columns match the integer fields written by `SupabaseDB.write_environment_log`; fresh schema and tracked migration agree. Production application and restored session details remain subject to the parent agent's live re-audit.
- **Callers:** Both runtime session readers use the helper. Existing critical-alert callers ignore the new boolean result safely. Recovery POST returns `_post`'s delivery result.
- **Concurrency:** An asyncio lock serializes cycle and heartbeat session reads and notification-state transitions. Concurrent outage jobs have regression coverage.
- **Delivery failures:** Failed initial warnings retry; failed recovery messages retry during uninterrupted healthy reads. Recovery occurs even without an ACTIVE session. The re-review verifies that a subsequent outage produces a fresh warning and cancels obsolete recovery delivery.
- **Startup:** The patch does not alter startup checks or process initialization. Existing credential loading precedes notifier initialization; this helper covers session-read outages once scheduled jobs are running, not credential/bootstrap failures. This is a pre-existing boundary, not an additional patch blocker.
- **Notification policy:** MANM-137 scoring/debounce code has no diff against the pulled `origin/main`. Routine Discord heartbeats remain suppressed under ADR-006. Recovery text explicitly advises waiting for a fresh environment alert.
- **Scope/simplicity:** Changes remain focused on the demonstrated schema mismatch and session-read alert gap, plus relevant documentation and regression coverage.

## Validation

- `PYTHONPATH=src python3 -m pytest tests/ -q`: **229 passed**, two existing Supabase client deprecation warnings.
- `git diff --check`: passed.
- Network-free recurring-outage reproduction: initially confirmed the P2 finding; independent final replay passed after correction.

All four review axes pass for the final patch. The local approval does not establish live Supabase/Fly/Discord verification; the parent agent's re-audit remains necessary. No external review, comment, commit, deployment, or production mutation was made.

## Follow-up review — Preserve the warm session during bridge outages

**Verdict:** APPROVE for the follow-up diff against `f46f7dd`; no remaining blockers found.
**Trigger:** PR 18's later P1 finding identified an additional failure missed by the initial review: a session-read outage returned `None` and entered the genuine inactive-session branch. This cleared `state.in_session`; the next readable ACTIVE session consequently reset buffers and repeated warmup. The prior approval above records the initial review, not this subsequently discovered issue.

**Verified correction:** [scheduler.py](/Users/manmadeanyme/Documents/Work/Kairos/src/kairos/scheduler.py:333) now exits before inactive-session handling when the helper has established an unreadable bridge:

```python
config = await read_session_with_health_check()
if not state.supabase_ok:
    return  # Pause on an unreadable bridge without treating it as a stopped session.
```

The helper sets `supabase_ok=False` on `SessionReadError`, so the guard preserves session membership, completed warmup, buffers, and notification references while preventing market fetching or scoring. A successful read sets it `True` before returning, including when no ACTIVE session exists; genuine inactivity therefore still clears `in_session`. The unchanged configuration and schedule gates execute normally once the bridge is readable.

**Concurrency:** Both scheduled jobs serialize the helper under its existing asyncio lock. Returning from the helper and executing this immediate guard introduces no intervening await; the heartbeat cannot replace the observed health state between these steps in the supported single event loop. The heartbeat already returns on an outage's `None`, so preserving session membership does not introduce an additional stale-signal notification during that failure.

**Independent replay:** Confirmed that both readable `None` and explicit STOPPED configurations end an existing session. Concurrent cycle/heartbeat outage checks preserved a warm session, delivered one critical warning, and sent no stale-signal warning despite an old last-cycle timestamp. Recovery into the midday gap applied the usual exit boundary. Changed-configuration and session-transition regression tests also pass in the full suite.

**Regression coverage:** `test_bridge_outage_preserves_warm_session_and_resumes_alerts` exercises an outage through each scheduled job, then verifies same-session recovery scores and sends the next environment alert without resets or repeated boundary/warmup notifications. `test_confirmed_inactive_session_ends_existing_session` covers the distinction from genuine inactivity. Existing completed-candle freshness and gap validation remain unchanged.

**Validation:** Full suite **232 passed**, two existing Supabase deprecation warnings; `git diff --check` passed. The independent network-free replay passed. Reviewed the matching integration/changelog text. No source/test changes, network operations, commits, deployment, or external comments were performed by this reviewer.
