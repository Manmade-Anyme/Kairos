# Kairos Discord Delivery Audit — 5 October 2026

> Historical pre-repair snapshot. The authorized repair and subsequent production checks are recorded in [TASK-138 recovery audit](TASK-138_recovery-audit.md). Findings below describe the earlier read-only audit, not the restored state.

**Verdict:** Monitoring is idle; supporting services are reachable. Two database problems and a health-reporting gap require attention.
**Scope:** Read-only production audit of Fly, Supabase, Dhan data access, Discord webhook metadata, and repository tests. No production session, schema, deployment, or Discord message was changed.
**Observed window:** Approximately 12:30–12:36 IST. Historical API evidence covers the preceding 24 hours.

## Main finding

The Fly worker is running, but `public.session_config` contains **zero rows**, including zero ACTIVE sessions. `public.environment_log` also contains zero rows. The worker reads the session table successfully every minute and returns before data fetching, scoring, startup notifications, or environment alerts when no active session exists.

Earlier today, its session reads failed with `PGRST205`: “Could not find the table 'public.session_config' in the schema cache”. Access subsequently recovered before this audit. Recovery did not supply an active monitoring session.

Neither the database nor the retained logs establish who changed the schema, why the tables were unavailable, or whether historical data was removed. An unavailable PostgREST table is not proof that a SQL table was physically absent.

## Feedback loop and hypotheses

The pass/fail seam is: an ACTIVE session must be visible to the deployed worker before any monitoring notification is possible. Queries used the actual Fly environment credentials; secret values were never printed.

| Hypothesis | Falsifiable signal | Result |
|---|---|---|
| Worker did not start | Fly machine stopped or scheduler absent | Rejected for current state: machine started, scheduler initialized, requests continue every minute |
| Session bridge unavailable or empty | Live session read fails or returns no ACTIVE rows | Confirmed: historical 404/PGRST205 failures; current HTTP 200 with an empty table |
| Dhan token or market-data access broken | Profile/data requests fail or return stale data | Rejected for current credentials: profile and data checks succeeded |
| Discord webhook removed or inaccessible | Read-only webhook lookup fails | Rejected for URL validity: both lookups return HTTP 200; message delivery itself was not tested |
| Alert policy silences normal activity | No session or routine heartbeat suppression prevents notifications | Confirmed: no-session early return and intentional routine-heartbeat suppression |

## Service checks

| Component | Evidence | Assessment |
|---|---|---|
| Fly app | `kairos-yxfydw`, machine `7847455a5e5d78`, state `started`, region `sin`, 1 shared CPU / 1 GB | Running; no configured Fly health checks, so state alone does not prove monitoring health |
| Scheduler | Latest start at 11:29:41 IST; scoring interval 60 seconds, heartbeat interval 300 seconds | Alive, polling an empty session table |
| Supabase | Project `mgenubvjbatpcpntlgav` (“Trading signal data”), ACTIVE_HEALTHY; session and score reads from Fly return HTTP 200 | Reachable; current Fly key has `service_role` privileges |
| Dhan credentials | Credential row refreshed at 11:01:27 IST; profile HTTP 200; token validity reported as 6 October, 11:01 | Current credentials accepted |
| Dhan data plan | Profile reports `Active`, validity through 7 September 2027 | Data entitlement active |
| Dhan market data | Deployed client fetched 488 NIFTY option-chain rows for 6 October; completed candle timestamp 12:34 IST at 12:35:11 IST | Data fetching works independently of the idle scheduler |
| Environment webhook | HTTP 200; name `KAIROS bot`; channel ID `1546193679964962977` | URL valid and reachable; verify this is the channel you expect |
| Health webhook | HTTP 200; name `KAIROS system health`; channel ID `1545448133012496475` | URL valid and reachable; verify this is the channel you expect |
| Local tests | `PYTHONPATH=src python3 -m pytest tests/ -q` | **211 passed**, two Supabase client deprecation warnings |

Webhook GET checks validate the configured webhook and destination metadata; they do not establish that Discord accepted or displayed a POST. No test message was sent.

## Incident timeline

All times below are IST, converted from Supabase/Fly UTC logs.

- **09:12:09–11:20:09:** Supabase logs contain 150 HTTP 404 session-table reads. Fly records repeated `PGRST205` errors. The worker was awake by 09:12, so a missed wake-up is not the demonstrated cause today.
- **11:13:44:** An environment-table GET also returned HTTP 404.
- **11:21:09 onward:** Session-table reads return HTTP 200.
- **11:22:35:** Environment-table GET returns HTTP 200.
- **11:23 and 11:29:** Retained Fly logs show restarts; subsequent startup retrieves Dhan credentials and starts the scheduler successfully.
- **During this audit:** Direct SQL and Fly-authenticated REST both confirm empty session and score tables. Available-expiry data and previous-day levels were last refreshed on 1 October. No POST/PATCH/DELETE requests to the session table appear in the queried 24-hour API window.

Absence of REST writes does not exclude direct SQL changes. The audit made none.

## Findings requiring remediation

### 1. No active session — immediate monitoring blocker

**Category:** STATE/ASYNC. `src/kairos/db.py:65` queries ACTIVE sessions and returns `None` for an empty result. `src/kairos/scheduler.py:295` then exits the cycle immediately.

The empty database is independently confirmed with privileged SQL and the deployed worker's credentials. Restarts cannot supply the missing session. Start the intended monitoring session through the Discord orchestrator, or use the existing control panel with the intended symbol and a currently valid expiry. Dhan confirmed that 6 October 2026 is an available NIFTY expiry.

The deployed worker uses `service_role`, so missing RLS policies do not prevent its current read. However, `session_config`, `environment_log`, and `api_keys` have RLS enabled and **no policies**. An orchestrator using an anon/authenticated key cannot insert a session without an appropriate policy. Its credentials and deployment were outside the accessible repository; verify that integration rather than making these tables publicly writable.

### 2. Score-log schema does not match the deployed writer

**Category:** LOGIC / integration contract. `src/kairos/db.py:258` writes `ce_oi_change` and `pe_oi_change`; both columns are absent from the production `environment_log` table. The checked-in `supabase_schema.sql` omits them too.

Production and local `db.py` have identical SHA-256 hashes. This is a proven schema mismatch, although an actual INSERT was not attempted during the read-only audit. Once scoring resumes, the current payload cannot be persisted successfully until the contract is repaired. `write_environment_log` catches the failure and allows scoring and direct webhook delivery to continue, so this mismatch is an additional persistence failure rather than the explanation for today's initial absence of all messages.

Concrete proposed migration, **not applied**:

```sql
alter table public.environment_log
    add column if not exists ce_oi_change bigint not null default 0,
    add column if not exists pe_oi_change bigint not null default 0;
```

Track the same fields in the repository schema. Verify a real score insert/read after applying the migration through the project's migration workflow.

### 3. Database-read failures become silent inactivity

**Category:** LOGIC / observability. `src/kairos/db.py:89` catches every session read exception and returns the same `None` value used for normal inactivity. Both the scoring cycle and heartbeat return before notifying Discord when this happens. The stale-cycle check also requires a prior successful cycle, so it cannot detect this failure before monitoring has ever started.

A network-free reproduction injected the logged session-read failure at the database boundary and ran the real `run_cycle` and `run_heartbeat`: **neither called the notifier**. This explains why the morning database outage stayed confined to Fly logs.

Recommended surgical repair: distinguish failed session reads from an empty result; send a throttled Supabase health alert and a recovery notice while retaining issue-only routine reporting. Add a regression test for a database failure before the first successful cycle.

### 4. Production scheduler differs from local main

Production `scheduler.py` contains MANM-137 notification debouncing changes absent from this checkout. Its `db.py` and `notifier.py` match local files. The Obsidian MANM-137 directive/changelog documents this work, so the difference is consistent with later alert-policy development; it is not evidence of a rogue modification.

The local 211-test result therefore validates the current checkout, not the exact deployed scheduler. Reconcile the deployed revision before making notification changes. The no-session early return is present in both versions and is independent of MANM-137 deduplication.

### 5. Supabase access and maintenance drift

Ten public tables have RLS disabled, including Kairos's `available_expiries` and `previous_day_levels`. This is a separate security finding; permissions should be reviewed with the other applications sharing this project. Do not enable RLS indiscriminately without the required policies.

`pg_cron` is not installed, so the cleanup jobs in `supabase_schema.sql` are not active. This is not the cause of current inactivity, but scheduled retention is not configured as documented. The project also reports no tracked migrations.

References: [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security), [Supabase monitoring and debugging](https://supabase.com/docs/guides/monitoring-and-debugging).

## Expected notification behavior

- Routine five-minute Discord heartbeats are intentionally suppressed under ADR-006; they only log locally. `docs/integration.md` still describes the old behavior.
- At the 12:35 IST live check, Kairos was in its midday gap. Official scoring sessions are 09:15–11:45 and 13:00–15:25; afternoon warmup starts earlier according to buffer settings.
- The midday gap does not explain the morning failures, and an empty session suppresses all cycle activity regardless of time.
- The deployed MANM-137 policy also deliberately suppresses repeated low-score diagnostic changes. A valid active session is still required for startup, warmup, boundary, and environment notifications.

## Recovery and verification sequence

1. Restore the two missing score-log columns and track the schema change.
2. Verify the orchestrator's Supabase authorization, then create the intended ACTIVE session. Do not insert an arbitrary trading configuration.
3. Confirm the Fly worker detects that session, passes startup checks, and refreshes expiry and previous-day-level data.
4. Confirm actual score rows are persisted during an active window. The local warmup logic requires sufficient completed candle/IV history before environment notifications.
5. Verify startup/health and subsequent environment messages in the two configured Discord channels.
6. Repair the session-read failure alert gap with regression coverage and deploy the reconciled scheduler revision.
7. Separately address shared-project access policies and retention jobs.

**Remaining limits:** The audit did not reactivate monitoring, modify production, send Discord messages, inspect the external orchestrator code, or prove end-to-end delivery. Cause of the morning table unavailability remains unknown. Recovery steps are concrete but have not been executed.
