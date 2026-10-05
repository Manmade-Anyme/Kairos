# QA Report — TASK-138

**Date:** 2026-10-05  
**Verdict:** PASS — local test and changed-code coverage gate cleared. Production recovery verification remains the coordinator's separate audit.  
**Branch:** `codex/TASK-138-restore-monitoring`  
**Baseline:** `origin/main` at `4a7446b9394d54e801305ff0b24284d7a0f9df7c`

## Scope and result

Audited the uncommitted repair to `src/kairos/db.py`, `src/kairos/notifier.py`, `src/kairos/scheduler.py`, `supabase_schema.sql`, `tests/test_db.py`, `tests/test_notifier.py`, the new `tests/test_supabase_health.py`, and `supabase/migrations/20261005130239_kairos_restore_environment_log_oi_columns.sql`.

The complete suite passes **229 tests**. An isolated archive of the baseline passes **216 tests** under the same Python runtime and dummy service settings. No baseline test failures or removals were found. The repair adds 13 test cases, including the parametrized cases. Both runs emit the same two Supabase SDK deprecation warnings for the `timeout` and `verify` parameters; these are unchanged dependency warnings.

No `reports/qa/baseline.json` exists. The isolated baseline run provides a direct comparison instead. Initial invocation required `PYTHONPATH=src`; the isolated archive also required the four mandatory service settings. Successful verification used dummy localhost service URLs and a dummy Supabase key, without contacting production services.

## Changed-code coverage

Coverage.py branch instrumentation was intersected with new line ranges from `git diff --unified=0 origin/main`. Only executable statements and branch exits originating on changed lines count toward this gate. Comments, docstrings, SQL, and type annotations do not inflate the Python denominator.

| File | Changed executable lines covered | Changed branch exits covered | Missing changed lines or branches |
|---|---:|---:|---|
| `src/kairos/db.py` | 2 / 2 | No changed decision branches | None |
| `src/kairos/notifier.py` | 4 / 4 | No changed decision branches | None |
| `src/kairos/scheduler.py` | 26 / 26 | 8 / 8 | None |
| **Total** | **32 / 32 — 100%** | **8 / 8 — 100%** | **None** |

Covered scheduler decision exits are `305 → 306/313`, `316 → 317/319`, `319 → 320/323`, and `321 → 322/323`. There are **no uncovered changed executable lines**.

For context, the whole application has 88.12% statement coverage and 82.61% branch coverage. The uncovered legacy code is outside this repair; these figures are not represented as 100% application coverage.

Reproduction command, with dummy service settings supplied through the environment:

```sh
PYTHONPATH=src COVERAGE_FILE=/tmp/kairos-task138-qa.coverage \
  python -m coverage run --branch --source=src/kairos -m pytest tests -q
COVERAGE_FILE=/tmp/kairos-task138-qa.coverage \
  python -m coverage json -o /tmp/kairos-task138-qa-coverage.json
```

## Assertion quality and regression findings

The new tests assert externally meaningful behavior: a failed bridge read raises rather than returning inactivity; either scheduled job warns before any successful scoring cycle; repeated and simultaneous jobs deliver one warning; failed warning and recovery deliveries retry; a valid empty session causes no health messages; recovery can be reported without an active trading session; and raw database error details are excluded from Discord. Real notifier HTTP mocks cover recovery delivery succeeding with 204 and failing with 500, checking the returned boolean and recovery content.

A second-outage regression was reproduced during this audit: after successful database recovery but failed recovery-message delivery, the original warning flag suppressed the next outage's warning. `test_new_outage_warns_when_previous_recovery_message_failed` initially failed at its warning-count assertion with actual 1 versus expected 2. The final implementation separates pending recovery delivery from the per-outage warning state. The regression now passes, as do all other tests; all new state decisions are covered.

The migration is additive and guards both OI column additions with `IF NOT EXISTS`. Both columns use `bigint NOT NULL DEFAULT 0`, matching the persisted score contract and the fresh-install schema. Static inspection found no unrelated destructive SQL. This QA agent did not execute SQL or mutate Supabase/Fly; the coordinator's production audit must provide the applied-migration and actual row-write evidence.

`git diff --check` passes. No temporary `[DEBUG-` tags were found in `src` or `tests`.

## CI evidence and limits

The latest completed [Fly Deploy run](https://github.com/Manmade-Anyme/Kairos/actions/runs/34473813099) succeeded for the baseline SHA `4a7446b9394d54e801305ff0b24284d7a0f9df7c`; the previous deployment run also succeeded. This does **not** verify deployment of the uncommitted TASK-138 repair. The repository's checked-in GitHub workflow deploys `main` and does not run pytest or a lint gate. No new-branch CI result was available during this audit.

## Recommendation

Local QA approves the final repair. Continue with the separate PR review and production recovery audit. Do not infer production code rollout or Discord receipt from this local QA result. No external reviews, comments, commits, deployments, or service mutations were performed by this QA agent.

## PR #18 P1 follow-up — preserve a warm session during bridge outages

**Date:** 2026-10-05
**Verdict:** PASS — follow-up regression and changed-code coverage gate cleared.
**Comparison point:** `f46f7dd`, the initial repair commit. The evidence above describes the original audit and is retained unchanged.

The follow-up adds an early return at `src/kairos/scheduler.py:334–335` when the session bridge is unreadable. A transient outage now pauses scoring while preserving the existing warm session, instead of marking it stopped and forcing another warmup after recovery. A successful read that confirms inactivity still ends the session.

QA independently reproduced the reported failure by running the three new test cases against an isolated copy of the `f46f7dd` source. The cycle-outage case failed at `tests/test_scheduler.py:114`: `session_state.in_session` became `False` and the old scheduler logged `Session stopped`. The heartbeat-outage case and confirmed-inactivity case passed, yielding **1 failed / 2 passed** before the fix.

With the current source, the complete suite passes **232 / 232 tests**, including all three new cases. The same two existing Supabase SDK deprecation warnings remain. The parametrized warm-session test verifies preserved candle, IV and OI history, cycle count, and OI flow buffer; no fetching or environment alert during the outage; and an immediate environment-log write and Discord alert on the next successful cycle. It also asserts that recovery does not reset buffers or emit new session-boundary/warmup notifications. The separate confirmed-inactivity test proves legitimate session termination remains functional. These assertions exercise the reported runtime behavior rather than mirroring the new conditional.

| Follow-up changed-code metric | Result |
|---|---:|
| Executable lines `334`, `335` | 2 / 2 — 100% |
| Branch exits `334 → 335`, `334 → 336` | 2 / 2 — 100% |
| Uncovered changed lines or branches | None |

Branch coverage was collected with the same full-suite command shown above, using `/tmp/kairos-task138-followup-qa.coverage`; the JSON output is `/tmp/kairos-task138-followup-qa-coverage.json`. `git diff --check` passes. QA made no source/test changes, network calls, commits, deployments, or external comments during this follow-up. Local QA clears the follow-up; deployment and production delivery evidence remain separate coordinator checks.
