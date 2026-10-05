# TASK-138 — Kairos Monitoring Recovery Audit

**Verdict:** PASS — monitoring and automatic Discord delivery restored.
**Date:** 5 October 2026 (IST); final SQL snapshot at 13:29:16.
**Scope:** Authorized repair of missing monitoring state, score-log schema mismatch, and silent session-read failures, followed by production verification.
**Branch:** `codex/TASK-138-restore-monitoring`.

## Proven causes and repair

The initial audit found no ACTIVE session, so the worker returned before market fetching or notifications. Historical session-table reads also failed with PGRST205, while the database wrapper returned the same value as normal inactivity. Two missing OI columns would prevent score persistence when monitoring resumed.

- Applied Supabase migration `20261005130239_kairos_restore_environment_log_oi_columns.sql`: additive `bigint NOT NULL DEFAULT 0` columns `ce_oi_change` and `pe_oi_change`, guarded with `IF NOT EXISTS`. The fresh-install schema now matches the writer.
- Restored one ACTIVE NIFTY WEEKLY session for 6 October 2026 at 13:03:07 IST, using a currently available expiry verified with Dhan. The insert was guarded against an existing ACTIVE session.
- Database session failures now raise `SessionReadError`; a valid empty result still returns `None`. Both scheduled readers use a serialized health helper: one successfully delivered warning per outage, retry after failed delivery, recovery notification after successful reads, and a fresh warning for a subsequent outage even if the preceding recovery message failed.
- Reconciled the checkout with merged MANM-137 at `4a7446b9394d54e801305ff0b24284d7a0f9df7c` before making changes. Its alert debouncing remains intact.
- Deployed Fly release 56 to existing machine `7847455a5e5d78` in `sin` at 13:14:27 IST; worker initialization and startup checks completed by 13:14:30. The machine identifier used by the existing wake-up integration was retained.

## Production verification

The worker completed its normal 15-reading IV warmup at 13:28 IST. Both WARMUP COMPLETE in the health channel and the first automatic ENVIRONMENT: AVOID alert (3/8) in the environment channel were visible in the native Discord UI on 5 October. This proves delivery through the actual scheduler; no warmup bypass or fabricated market result was used.

| Check | Evidence | Result |
|---|---|---|
| Fly rollout | Release 56, image `deployment-01M45G7YRNQP5TDRK9KXT1TBR9`; machine started | PASS |
| Deployed source | SHA-256 of scheduler, database wrapper, and notifier matches repaired local files | PASS |
| Supabase access | Actual Fly credentials read session and score tables with HTTP 200; key role `service_role` | PASS |
| Session recovery | One ACTIVE NIFTY WEEKLY session, expiry 2026-10-06, survives deployment | PASS |
| Score persistence | At 13:29:16, 26 total rows including 15 since deployment; latest row 13:28:30, CE delta -188377 / PE delta 420429; zero null OI rows | PASS |
| Supabase API logs | 13:14:30–13:29:00: 18 session GETs returned 200 and 14 score POSTs returned 201; all returned events for the audited bridge/data tables were successful | PASS |
| Startup data | PDH/PDL and 18 NIFTY expiries refreshed at 13:14:30 | PASS |
| Dhan credentials | Profile HTTP 200; token validity 6 October 11:01; data plan Active through 7 September 2027 | PASS |
| Discord health receipt | Native UI shows ENGINE STARTING and SESSION ACTIVE at 13:14, plus WARMUP COMPLETE at 13:28 in #system-health-🍎 | PASS |
| Environment receipt | Native UI shows KAIROS bot ENVIRONMENT: AVOID, score 3/8, NIFTY, DTE 1, expiry 6 October, time 13:28 IST in #kairos-⛅ | PASS |

Source hashes verified in the running image:

```text
scheduler e5b71a8a687b4748223fc5fbaf466f510f6b57ba83b347efd1cbb8d9025a6304
db        835ef8b94b7f80ffb060250f7e4745963c7ba8c5cc0b2a6fc6d7411e8b631129
notifier  dfe7d9801c23b67a17be256354d8b3a41bddeec9e782b6f463bf434afec52c49
```

Health channel: [#system-health-🍎](https://discord.com/channels/1545250495386484796/1545448133012496475). Environment channel: [#kairos-⛅](https://discord.com/channels/1545250495386484796/1546193679964962977). Both webhook metadata requests returned HTTP 200. Health and environment receipts were verified separately in the UI; metadata alone was not treated as delivery proof. No manual diagnostic Discord message was sent. The API snapshot contains 14 POST events versus SQL's 15 post-deployment rows, consistent with log ingestion lag; SQL and actual UI receipt are the latest direct evidence.

## Regression and independent review

- Full suite: **229 passed**, compared with **216 passed** on the reconciled baseline; two unchanged Supabase SDK deprecation warnings.
- Changed executable Python lines **32/32** and changed branch exits **8/8** covered. Whole-application coverage remains 88.12% statements and 82.61% branches.
- Tests cover pre-first-cycle failure, inactive silence, repeated/concurrent outage deduplication, failed delivery retries, recovery without an active session, a subsequent outage following failed recovery delivery, and exclusion of raw database error details from Discord.
- A reviewer reproduced a second-outage suppression bug in the first patch. Separate pending-recovery state fixed it; the failing regression and independent replay now pass.
- [QA report](../qa/TASK-138_qa-report.md): PASS. [PR review report](TASK-138_review-report.md): APPROVE. Documentation updated on the same branch. No temporary debug instrumentation remains; `git diff --check` passes.
- The checked-in GitHub workflow deploys main and does not run unit tests. Its earlier successful baseline run is not evidence for this repair; this repair was directly deployed and verified in the running image.

## Residual findings and limits

These findings remain separate from the repaired notification outage:

- Shared Supabase project: ten public tables have RLS disabled, including `available_expiries` and `previous_day_levels`; two functions have mutable search paths. RLS-enabled Kairos service tables have no client policies. The worker uses server-only service-role access. Broad policy changes require review of the other applications sharing this project and the external orchestrator's credential contract.
- `pg_cron` is not installed; the documented retention jobs are absent. Retention remains unconfigured.
- Dhan first-attempt option-chain requests received HTTP 429 from 13:15 through 13:23. Existing retries succeeded about five seconds later, with a new score persisted each minute. Recent cycles from 13:24 through 13:28 show no such errors. Investigate shared-account request timing if throttling recurs; the audit did not increase polling or bypass limits.
- The origin of the morning table unavailability remains unknown. Retained REST logs cannot establish direct SQL activity, deletion, or the actor responsible. Restoring a session does not prove the external orchestrator can create the next session; its deployment and credentials were unavailable in this repository.
- Bootstrap credential loading occurs before notifier initialization; the new session-read alerts cover scheduled jobs, not a complete credential-loading failure before startup. There are no configured Fly health checks; fresh rows and Discord receipts provide the runtime evidence here.
- Routine five-minute Discord heartbeats remain intentionally suppressed under ADR-006. Identical low-score results can remain silent under MANM-137; those are expected behaviors.

The schema migration and restored session are already live. The code is live on Fly; the feature branch will be presented in one PR for review and merge. No automatic PR merge is performed.

## PR #18 follow-up — preserve state during a transient outage

The [P1 review comment](https://github.com/Manmade-Anyme/Kairos/pull/18#discussion_r4181870721) identified that the health helper still returns `None` on a read failure. The scoring cycle incorrectly processed this as a confirmed inactive session and cleared `in_session`; the next successful read reset the candle, IV, and OI buffers and restarted warmup.

A new cycle-level regression reproduced `in_session=False` after a transient error in a warm session. The cycle now returns immediately when the bridge health check fails, before inactive-session handling. Regression coverage verifies preserved history, warmup, and session membership; no fetch or environment alert during the outage; and a score write plus qualifying environment alert on the first successful recovery cycle, without session-entry or warmup notifications. A separate test confirms a successfully read empty session still ends monitoring normally.

The follow-up suite passes **232 tests**, with the same two dependency deprecation warnings. This addendum describes the PR follow-up, not a new production rollout. Fly release 56 and source hashes above remain the initial repair's production evidence; the subsequent guard will be deployed through the existing main-branch workflow after PR merge.
