# TASK-127: Momentum Filter Audit Remediation

**Task ID:** MANM-127  
**Date:** 2026-09-09  
**Status:** Proposed amendment; implementation starts after human ADR approval  
**Baseline:** `3950e7108f1dbec107980e30727ae4dd8f195d1d` and the current feature branch tip  
**Amends:** ADR-007, ADR-015, and the shared OI alerting rules in TASK-103  
**Related:** ADR-001, ADR-008, ADR-013, ADR-021, ADR-023, TASK-103

## Problem statement

The momentum path can score the wrong candle and the wrong window. The fetcher
currently chooses the final row returned by Dhan's 1-minute intraday response,
even when that row is still forming. The scheduler appends every poll, so the
same timestamp can occupy several buffer entries and revised candles are not
represented. The scorer then evaluates four close-to-close changes while
displaying `4/5`, and its volume average includes the candle being tested.

The notification path has a second, independent defect: a meaningful condition
transition can be suppressed when the aggregate score is below six. A recovery
and a deterioration must both remain visible without turning a momentum update
into an overall trade-entry claim.

The implementation must preserve the OI decisions already approved in TASK-103:
PCR is telemetry only; eligible neutral/trend GEX remains eligible; NDE must
confirm direction and price action; the existing OI calculations and eligibility
remain intact; and an active Vega trap/effective OI veto blocks trade eligibility.

## Decision

Keep the existing Dhan → ingestion buffer → pure scoring → aggregate →
Supabase → Discord pipeline, but make candle readiness, momentum math, and
condition alerting explicit contracts. No new technical indicator or trading
gate is introduced.

```text
Dhan 1-minute arrays
  -> timezone-normalized, validated completed bars
  -> timestamp-keyed ordered candle buffer (upsert revisions)
  -> readiness gate (fresh, contiguous, in-session, 16+ bars)
  -> momentum scorer (6 closes, 5 deltas, latest-5 range, preceding-15 volume)
  -> EnvironmentScore + structured diagnostics
  -> persistence and shared transition fingerprint
  -> Discord alert (independent of aggregate score threshold)
```

### 1. Candle completion and buffer contract

Dhan's intraday endpoint is the minute-candle source (`interval: "1"`), and the
repository already receives epoch timestamps and converts them to IST. For this
system each timestamp is the opening boundary of a one-minute candle:

```text
completed(timestamp, now) := timestamp + 1 minute <= now
```

The comparison uses timezone-aware datetimes in `Asia/Kolkata`; a naive or
unparseable timestamp is invalid data. At the exact boundary the candle is
eligible. The current minute (`timestamp + 1 minute > now`) is always rejected
from scoring and remains a readiness reason, never a bearish signal. The
implementation must inject `now` into the readiness helper so boundary tests do
not depend on wall-clock timing.

Each session buffer is keyed by `(symbol, candle_timestamp)` and remains ordered
by timestamp. Ingestion rules are:

1. A repeated timestamp with identical fields is a no-op.
2. A repeated timestamp with changed OHLCV/VWAP replaces the existing bar at
   that timestamp; it never appends a second bar.
3. An out-of-order completed bar is inserted at its timestamp, then the buffer
   is re-ordered before evaluation.
4. Invalid OHLC values are rejected: all OHLC values and volume must be finite,
   `high >= max(open, close)`, `low <= min(open, close)`, `high >= low`, and
   volume must be non-negative when present. Missing volume is distinct from a
   genuine zero.
5. Timestamps must be minute-aligned after normalization. No synthetic bar is
   created for a missing interval.
6. A required evaluation window must contain consecutive one-minute timestamps
   within the configured exchange session. A gap, stale latest bar, or session
   boundary crossing makes the momentum result data-unavailable. The session
   boundary is checked before adjacency so lunch/session rollover cannot be
   mistaken for a market gap.
7. The latest completed bar must be no older than two one-minute intervals at
   evaluation (`age <= 120 seconds`). This is the freshness limit for the
   one-minute polling cadence and is configurable only if the polling cadence
   changes with it.

The buffer must retain at least `max(momentum_volume_lookback + 1, 6)` completed
bars for momentum evaluation. With the approved defaults this is 16 bars: the
latest six supply the trend window and the latest five supply the price range;
the immediately preceding 15 bars for the volume baseline include the evaluated
bar's preceding history and exclude the evaluated bar itself. Historical fetch
or response parsing may fill the buffer, but it must not bypass the completion,
validation, session, or gap rules.

If readiness fails, return `ConditionResult(name="momentum", status="YELLOW",
points=0, max_points=1)` with a structured `data_unavailable` diagnostic and a
clear reason. Missing data must never be converted into a bearish momentum
reading.

### 2. Four-of-five trend and range contract

Let the six newest completed, contiguous closes be `c0 ... c5` in ascending
timestamp order. Compute exactly five deltas:

```text
deltas = [c1-c0, c2-c1, c3-c2, c4-c3, c5-c4]
up_count   = count(delta > 0)
down_count = count(delta < 0)
flat_count = count(delta == 0)
dominant_count = max(up_count, down_count)
```

Flat deltas count toward neither side. A tie is `mixed` when both directions
are present and `flat` when all five deltas are flat; it is never automatically
classified as down. The detail and diagnostics must display the actual counts
and denominator, for example `up 4/5, down 1/5, flat 0/5`.

The high-low range is calculated only over candles `c1 ... c5` (the latest five
completed candles); `c0` supplies only the preceding close for the first delta:

```text
range_pct = (max(high[c1..c5]) - min(low[c1..c5])) / c5 * 100
```

Preserve the existing strict thresholds:

- GREEN range eligibility requires `range_pct > 0.30`.
- RED range condition requires `range_pct < 0.15`.
- Exactly `0.15` is neither RED by range nor GREEN; exactly `0.30` is not
  GREEN by range. Other failing gates still determine the final status.

Use configuration values, not literals, for trend cutoffs. GREEN requires
`dominant_count >= momentum_trend_count_green` (default 4). RED by directional
chop requires `dominant_count < momentum_trend_count_yellow` (default 3).
Therefore 3/5 is insufficient for GREEN but is not RED by trend alone; with
otherwise passing price checks it is YELLOW. Validate settings so the yellow
cutoff is below the green cutoff and both are within the five-delta denominator;
reject impossible configurations at startup rather than making GREEN silently
unreachable.

GREEN requires all three independent gates: range, four-of-five direction, and
valid volume spike. A valid price/trend pass with a volume gate failure is
YELLOW, not RED. RED is reserved for the existing small-range or configured
insufficient-direction conditions, not for missing data.

### 3. Volume confirmation contract

The evaluated candle is `c5`, the latest completed candle. Let `b` be the 15
completed candles immediately preceding `c5`:

```text
baseline_count = len(b)                         # must equal 15
baseline_avg   = sum(volume[x] for x in b) / 15
volume_ratio   = volume[c5] / baseline_avg
volume_spike   = volume[c5] > 1.5 * baseline_avg
```

The multiplier remains `settings.momentum_volume_multiplier` (default 1.5),
and the lookback remains `settings.momentum_volume_lookback` (default 15).
The current volume is never included in `baseline_avg`; the comparison is
strict, so exactly `1.5 * baseline_avg` does not pass.

The volume gate is `data_unavailable` and returns YELLOW/zero points when the
full baseline is absent, any required volume is missing/non-finite/negative, or
the baseline is all zero (`baseline_avg == 0`). An all-zero baseline is not a
valid spike denominator and must not let a tiny non-zero current reading pass.
When the baseline is valid and current volume is genuinely zero, the gate is a
valid failure and produces YELLOW when price checks pass. The diagnostic must
distinguish unavailable volume from a valid no-spike market observation.

The provider's index-volume behavior remains an empirical integration concern.
Do not change the instrument segment, assume that index volume is always zero,
or introduce a futures/options proxy. Fixtures and diagnostics are sufficient
when live credentials are unavailable; the implementation report must state
that limitation.

### 4. Structured diagnostics and persistence

`score_momentum(candle_buffer: Sequence[OHLCVCandle]) -> ConditionResult`
remains pure and side-effect free. Its result must expose a structured momentum
diagnostic (either a typed model or an equivalent validated mapping) containing:

- evaluated candle timestamp and readiness/data-availability state;
- range percentage plus the 0.15% and 0.30% strict thresholds;
- up, down, flat counts and denominator 5, dominant direction/count;
- current volume, baseline average, baseline count, multiplier, and ratio when
  valid;
- failed gates and whether each failure is market-condition or data-readiness;
- the timestamp/gap/session reason when the window cannot be evaluated.

`ConditionResult.detail` is the concise human-readable rendering. The complete
diagnostic is retained in `EnvironmentScore.summary_raw`/the existing
environment-log write path and is rendered by `Notifier.post_environment_alert`.
No credentials or raw sensitive API payloads may enter diagnostics.

### 5. Decoupled transition alerting

Amend ADR-007 and ADR-015 so the shared scheduler alert path has two distinct
state concepts:

- `latest_evaluated_fingerprint`: the most recent valid score/condition state,
  updated after every evaluation, regardless of send success;
- `last_successfully_notified_fingerprint`: the fingerprint for which
  `post_environment_alert` completed successfully.

The fingerprint must include each condition's name and status plus the semantic
OI event fingerprint already used by TASK-103. For momentum it must include the
readiness state, direction/counts, range gate, volume gate, and evaluated
timestamp when those are part of the displayed explanation. Exact repeated
states deduplicate. A status transition in either direction (RED→YELLOW/GREEN
or GREEN/YELLOW→RED), including a momentum recovery with total score below 6,
is independently alertable. The aggregate score threshold is not a veto for a
meaningful condition transition.

Only set `last_successfully_notified_fingerprint` after the notifier returns
success. If Discord fails, retain the last successful fingerprint so the same
transition is retried on the next cycle; a failed send must not swallow a
recovery or deterioration. Persistence of the score/log and alert state must
remain consistent with the evaluated candle timestamp.

An alert describing momentum recovery must state the momentum condition and
overall score separately. It must not call the recovery an overall `GO` or trade
entry unless the aggregate entry rules independently pass. OI event alerting is
implemented once in this shared path; do not add a momentum-only bypass that
duplicates TASK-103 behavior. Active OI Vega traps/effective vetoes continue to
block trade eligibility and PCR remains decoupled from OI scoring.

### 6. Configuration invariants

Keep these defaults unless a separate approved decision changes them:

```text
momentum_candle_window       = 5       # range candles; trend requires +1 close
momentum_range_green         = 0.30    # strict percent comparison
momentum_range_yellow        = 0.15    # strict percent comparison
momentum_volume_multiplier   = 1.5
momentum_volume_lookback     = 15
momentum_trend_count_green   = 4       # out of 5 deltas
momentum_trend_count_yellow  = 3       # below this is RED
```

Startup validation must enforce positive lookbacks, `momentum_candle_window ==
5` (or a coordinated change to the six-close contract), green/yellow range
ordering, `0 < momentum_trend_count_yellow < momentum_trend_count_green <= 5`,
and a positive finite volume multiplier. The validation must fail closed.

## Component boundaries and implementation assignment

The Code Generator owns implementation only after human approval of this ADR.
The following files are the assigned boundary; do not move business logic into
the fetcher or notifier:

| File | Required responsibility and contract |
|---|---|
| `src/kairos/models.py` | Add/extend the validated momentum diagnostic contract without breaking existing `OHLCVCandle`, `ConditionResult`, `EnvironmentScore`, or OI models. Preserve timezone-aware timestamps and a distinct missing-volume representation. |
| `src/kairos/fetcher.py` | Normalize Dhan epoch timestamps to IST, validate response arrays/OHLCV, reject malformed rows, and return enough completed historical bars where the endpoint supports it. `get_latest_candle` or its replacement must never claim an unfinished row is completed. Do not change `IDX_I`/`INDEX` or invent a volume proxy. |
| `src/kairos/scheduler.py` | Own timestamp-keyed ordered upsert/revision handling, readiness/gap/session/freshness checks, shared buffer consumers, and the two alert fingerprints. `run_cycle` must persist/evaluate only accepted completed bars and retry unsent transitions. |
| `src/kairos/processor.py` | Implement pure `score_momentum(candle_buffer)` using six closes, five deltas, latest-five range, and preceding-15 volume. Return YELLOW/zero for readiness failures and emit complete diagnostics. No RSI/ADX/EMA/body/displacement gates. |
| `src/kairos/engine.py` | Pass the accepted candle window through unchanged, preserve total scoring and IV cap behavior, and preserve TASK-103 OI effective-veto/eligibility decisions. Do not make momentum recovery a trade signal. |
| `src/kairos/notifier.py` | Render the structured momentum diagnostics concisely, including candle timestamp/readiness, range, counts, volume baseline/ratio, and every failing gate. Keep PCR display-only and OI trap reasons intact. Return/propagate send success so scheduler state is correct. |
| `tests/test_fetcher.py` | Add completion, timezone, malformed OHLCV, historical-window, and index-volume fixture coverage. |
| `tests/test_processor.py`, `tests/test_processor_extra.py` | Add bullish/bearish 4/5, 3/5, ties/flats, range boundaries, volume baseline/exclusion/spike boundary, invalid/all-zero/incomplete data, and diagnostics coverage. |
| `tests/test_scheduler.py`, `tests/test_scheduler_extra.py` | Add deduplication, revision, out-of-order, gaps, stale/session rollover, shared-buffer, RED→GREEN and GREEN→RED below-six alerts, identical-state deduplication, and failed-send retry coverage. |
| `docs/scoring_architecture.md`, `CHANGELOG.md` | Document the corrected six-close/4-of-5 behavior, preceding-15 volume formula, readiness semantics, and decoupled alerts. Documentation Agent owns the prose update after implementation. |

## Alternatives considered

1. **Keep selecting the final API row and rely on polling cadence.** Rejected:
   polling cadence does not prove candle completion and caused current-minute
   prices to enter scoring.
2. **Keep five closes and require all four comparisons.** Rejected: it is not
   the specified 4-of-5 behavior and cannot represent one counter-move.
3. **Average the latest 15 volumes including the evaluated candle.** Rejected:
   the tested observation contaminates its own baseline and suppresses genuine
   spikes.
4. **Retain the aggregate score `< 6` suppression gate.** Rejected: it hides
   both recovery and deterioration transitions. Deduplication belongs to the
   condition fingerprint, not a total-score shortcut.
5. **Fork a separate momentum notification path.** Rejected: it would duplicate
   OI event behavior and reintroduce inconsistent persistence/send semantics.
6. **Add a new indicator or lower range thresholds to make GREEN easier.**
   Rejected: this changes trading strategy and violates task scope.

## Performance and security considerations

- Use bounded buffers and timestamp-keyed upsert; the default working set is at
  most the required 16 momentum bars plus existing OI/IV buffers. Evaluation is
  O(n) over a small fixed window.
- Historical candle requests must be bounded to the required warmup window and
  respect Dhan data rate limits. Do not add a per-cycle full-day request unless
  the existing endpoint contract requires it.
- All numeric and timestamp inputs are validated before arithmetic. Fail closed
  to YELLOW/data-unavailable rather than infer bearishness from malformed data.
- Diagnostics may include market values and timestamps only. Never log access
  tokens, webhook URLs, or raw authenticated payloads.
- Discord failure must not mutate the successful-notification fingerprint;
  retrying is safer than losing an alert, while fingerprint deduplication avoids
  repeated successful posts.

## Definition of done

- [ ] Human approves this ADR before implementation begins.
- [ ] Completed one-minute candles are timezone-normalized, validated, fresh,
      contiguous, session-safe, unique, and revision-aware.
- [ ] Momentum uses six completed closes, five deltas, latest-five range, and a
      preceding-15 completed-candle volume baseline with strict `> 1.5x`.
- [ ] Missing/unusable data yields YELLOW with zero momentum points and a
      readiness reason; valid no-spike volume yields YELLOW when price passes.
- [ ] Condition transitions alert in both directions below total score six,
      identical states deduplicate, and failed sends are retried.
- [ ] Structured diagnostics and concise Discord explanations cover every
      required value and failing gate.
- [ ] PCR remains decoupled; eligible neutral/trend GEX and NDE confirmation
      remain intact; active Vega traps/effective OI vetoes still block entry.
- [ ] Focused regression tests and repository quality checks pass, with live
      Dhan/index-volume limitations explicitly reported if unverified.
- [ ] Documentation and Obsidian note are synchronized by the assigned agents.

