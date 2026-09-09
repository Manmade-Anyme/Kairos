# TASK-127 Fix Momentum Filter
**Date:** 2026-09-09
**Status:** ready

## Goal
Make momentum scoring accurate, explainable, and capable of turning GREEN when its intended conditions pass, correcting data ingestion, "4/5 trend" calculation, volume gate reliability, alert suppression, and diagnostic reporting without forcing green or adding new trading filters.

## Inputs
- Issue MANM-127 audit specification
- Existing codebase in `src/kairos/` (specifically `processor.py`, `fetcher.py`, `notifier.py`, `scheduler.py`)
- Dhan API candle contracts & 1-minute candle ingestion buffer
- Prior OI remediation ADR (`directives/adr/TASK-103_oi-flow-filter-audit-remediation.md`) for shared alert/suppression patterns
- Test fixtures in `tests/`

## Tools / Scripts to Use
- `pytest` for test suite execution
- `git` / `gh` for version control and PR creation

## Expected Output
1. **Software Architect**: Architecture Decision Record (`directives/adr/TASK-127_momentum-filter-audit-remediation.md`) defining explicit candle completion/deduplication rules, 4-of-5 trend evaluation semantics, volume baseline lookback formula, and transition alerting decoupled from total score.
2. **Code Generator**: Robust implementation in `src/kairos/` strictly adhering to ADR.
3. **Code Generator**: Comprehensive regression tests in `tests/` covering all audit scenarios.
4. **Documentation Agent**: Updated `docs/scoring_architecture.md`, `CHANGELOG.md`, and Obsidian sync.
5. **PR Reviewer & QA**: Independent code/spec review and test/coverage verification on Pull Request.

## Acceptance Criteria
1. **Candle Ingestion & Readiness**:
   - Score only completed one-minute candles using explicit timestamp and timezone handling.
   - Maintain unique, ordered bars. Repeated polling of the same timestamp must not add another candle.
   - Replace revised candles at their existing timestamps rather than appending duplicates.
   - Detect missing intervals, stale data, invalid OHLC values, and session boundaries.
   - Fetch enough completed historical bars for the price window and full volume baseline where supported.
   - Return YELLOW with zero momentum points and a clear data-readiness reason when required window is unavailable.
2. **Resolve "4/5 Trend" Calculation**:
   - Evaluate five close-to-close changes using six consecutive completed closes.
   - GREEN trend eligibility requires at least 4 of those 5 changes in the same direction.
   - High-low range evaluated over latest 5 completed candles; the 6th candle supplies only the preceding close.
   - Flat changes count toward neither direction. Tied counts labeled mixed/flat (never automatically down).
   - Display actual numerator and denominator (e.g., 4/5).
   - Preserve existing range thresholds: GREEN requires range >0.30%, RED requires range <0.15%. Exact boundaries retain current non-passing behavior.
   - Use `momentum_trend_count_yellow` consistently. With defaults, dominant counts below 3 are RED, counts of 3 are insufficient for GREEN (YELLOW).
3. **Make Volume Gate Reliable**:
   - Keep volume confirmation. Compare latest completed candle volume with mean of preceding 15 completed candles (excluding evaluated candle).
   - Require current volume > 1.5 × baseline.
   - Require full 15-bar baseline before awarding GREEN.
   - Validate finite, non-negative values; distinguish missing volume from genuine zero volume.
   - All-zero baseline must not allow tiny non-zero reading to trigger a false spike.
   - Report unavailable or unusable volume as YELLOW/data unavailable with zero points.
   - When price checks pass, volume spike gate failure alone produces YELLOW.
4. **Fix Suppressed Recovery Alerts**:
   - Meaningful condition-status changes (both recovery and deterioration) must alert even when total score < 6.
   - Deduplicate identical repeated states.
   - Distinguish latest evaluated state from last successfully notified state.
   - Do not present a momentum recovery as an overall trade-entry signal unless overall entry rules pass.
5. **Explain Every Result**:
   - Expose structured diagnostics and concise Discord explanation: evaluated candle timestamp, range %, up/down/flat counts, volume ratio, failing gates, and whether result reflects market conditions or data readiness.
6. **Preserve Scope & Existing Decisions**:
   - Do not add RSI, ADX, EMA, net-displacement, or candle-body gates.
   - Do not alter agreed OI requirements.

## Edge Cases
- Session open: fewer than 15 historical bars available. Must report YELLOW (warmup/insufficient baseline), never RED or false GREEN.
- Dhan API returning current incomplete minute candle. Must be discarded.
- Duplicate or revising candle payloads. Must be deduplicated/overwritten by timestamp.
- Steady volume with high price trend: volume gate fails, must result in YELLOW (not RED).
- Total score 5 transition (RED -> GREEN): must trigger recovery alert despite being below 6 score threshold.
