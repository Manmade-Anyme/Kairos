# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **Docstring Coverage Gate (100%)**: documented every module, class, function, and fixture across
  `src/`, `execution/`, and `tests/` — coverage rose from 44.7% to **100%**, clearing the 80%
  review threshold.
  - Production code gained docs for the `Settings` model, Pydantic field validators, client
    lifecycle/guard methods, and the nested weighting helpers in `engine.py`.
  - Test modules gained module-level summaries plus per-test descriptions stating the behaviour
    each case pins, rather than restating the test name.
  - `interrogate` is now a dev dependency with a `[tool.interrogate]` block in `pyproject.toml`
    (`fail-under = 80`), so the threshold is enforceable locally via
    `interrogate -v src execution tests` instead of living only in review tooling.
  - Declared the previously missing `respx` dev dependency — `tests/test_fetcher.py` and
    `tests/test_notifier.py` import it, so a clean `pip install -e ".[dev]"` could not run the
    full suite without it.

### Fixed
- **OI Flow Self-Contradictory Green Gate (ADR-025)**: Condition 3 required GEX="trend",
  NDE="confirms", and PCR alignment simultaneously — three correlated reads of the same call/put
  OI imbalance that can never all confirm one phase (bearish blocked by PCR, bullish by NDE);
  live tape showed GEX "trend" 0/14 cycles and NDE "ambiguous" 13/14.
  - RULE D is now `nde_state == "confirms"`: direction is carried by the price×OI phase plus NDE
    conviction. GEX participates only as the pin veto ("trend" is a reported strengthener); PCR is
    demoted to descriptive context ("PCR aligned"/"PCR divergent").
  - `nde_pct_threshold` relaxed 0.20 → 0.10 (provisional) — near-ATM call/put delta exposure
    cancels in the net while adding in the gross, so 20% was rarely reachable.
  - All safety vetoes unchanged: vega trap, NDE contradiction, GEX pin, neutral phase, theta
    dominance, and the 5-of-8 consensus filter.
  - Added `📐 c3_raw: gex/nde/pcr` telemetry to `summary_raw` each cycle; `score_audit.py` parses
    it (`c3_gex_ratio`, `c3_nde_ratio`, `c3_pcr`) so the provisional bars get data-driven refinement.
- **8/8 Score Ceiling (ADR-024)**: Audit found a perfect score was structurally unreachable —
  Condition 7's strict VWAP bands contradicted the trend conditions (C2/C5/C6), capping any
  genuine trend day at 7/8, while Condition 4's thresholds were ~4–5 orders of magnitude below
  real Dhan-scale ratios, making it an unconditional free point.
  - `score_vwap_distance` is now **trend-aware**: during a confirmed PDH/PDL breakout with price
    on the breakout side of VWAP, widened bands apply (🟢 < 0.50%, 🟡 ≤ 0.75%); the strict
    0.20/0.40 bands still govern range days and adverse VWAP crossings.
  - Recalibrated `gamma_theta_*` thresholds to the measured ratio scale (DTE≥3: 🟢 > 1.5;
    DTE=2: 🟢 > 2.25; DTE≤1: 🟢 > 3.75), restoring real discrimination to Condition 4.
  - Unified the IV-cap GO→CAUTION downgrade: the scheduler's hysteresis HOLD path now delegates
    to `engine._determine_status` instead of duplicating the logic inline.
  - Documented the live `ce_oi_change` / `pe_oi_change` columns in `supabase_schema.sql`
    (previously inserted by `db.py` but missing from the reference schema).

### Added
- **Score Distribution Audit CLI (`execution/score_audit.py`)**: pulls `environment_log` history
  and reports the score histogram (empirical ceiling), per-condition status rates, pairwise green
  co-occurrence (structural-conflict detection), and raw metric distributions parsed from
  `summary_raw` — the calibration inputs mandated by ADR-024 ("thresholds come from measured
  distributions, never assumed units"). Backed by `db.fetch_environment_log(days)`.
- **Honest 8/8 regression test** (`test_evaluate_honest_8_of_8`): reaches a perfect score through
  real scoring paths — OI Flow consensus earned by repeated `evaluate()` cycles on aligned chain
  data rather than hand-fabricated buffer objects.

### Changed
- **OI Flow Calculation Window**:
  - Migrated from a 15-minute anchored block window to a true **15-minute rolling window** to avoid zero-delta resets at block boundaries.
  - Configured `oi_lookback_cycles = 16` (15-minute lookback) and expanded `candle_buffer_size = 20` to support the larger lookback.
  - Removed the strict 15-minute interval alert gating for OI Phase changes in `scheduler.py` to allow consensus-smoothed alerts to trigger in real-time.

### Added
- **Alert Silencing for Low Scores (`scheduler.py`)**:
  - Implemented noise reduction logic: Discord `#environment` alerts are suppressed when the score is below 6.
  - When the score drops below 6, the system alerts once, then remains silent until the score reaches 6 or above again.
- **Fly.io Deployment Setup**:
  - Added `Dockerfile` allowing for reliable explicit builds on Fly.io using the Hatchling `pyproject.toml` configuration.
  - Added `.dockerignore` to secure image creation and prevent `.env` secrets leakage.
  - Documented deployment process in `README.md` and added architectural decision in `ADR-019`.
- **Fly.io Scale-to-Zero & Automatic Shutdown**:
  - Implemented automatic process termination in `scheduler.py` explicitly checking `session_2_end` (extended to 15:25 IST) to allow graceful exit.
  - Added GitHub Actions cron workflow (`.github/workflows/market-hours.yml`) to scale the Fly.io machine to 1 before market open and down to 0 after market close.
  - Removed persistent `http_service` from `fly.toml` to support the scale-to-zero behaviour.
  - Documented decision in `directives/adr/ADR-020_fly_io_scale_to_zero_schedule.md`.
- **Greeks-Aware OI Flow Scoring Engine (`processor.py`, `engine.py`):**
  - Upgraded Condition 3 from a simple delta classifier to a professional-grade scoring system.
  - Integrated **GEX (Gamma Exposure)** and **NDE (Net Delta Exposure)** to capture institutional hedging and conviction.
  - Implemented **Vega Trap detection** to prevent aggressive entries into contracting IV (ADR-018).
  - Added priority-based rule ordering: Vega Trap > NDE Alignment > GEX Pin > Combined Conviction.
  - Integrated **PCR (Put-Call Ratio)** and **Theta Dominance** as secondary conviction filters.
  - Updated Discord notifier to report these advanced metrics in detail strings.
- **15-Minute Post-Lunch Warmup Window (`scheduler.py`):**
  - The system now automatically starts fetching and buffering data 15 minutes before the official start of Session 2 (at 12:45 IST).
  - This warmup window is **exclusive to Session 2**; Session 1 continues to start exactly at the official market open (09:15 IST) as pre-market data is unavailable.
  - During this post-lunch warmup, environment alerts are suppressed to prevent noisy or inaccurate notifications while buffers populate.
  - The first official Session 2 signal is delivered exactly at 13:00 IST with fully populated 15-minute data buffers.
  - Updated `is_active_session` and `is_lunch_break` helpers to handle this specific transition.
  - Documented via updates to `directives/adr/ADR-013_lunch_break_alert.md`.
- **Granular Condition & Status Alerting (`scheduler.py`, `notifier.py`):**
  - Replaced the noise-reduction strategy (ADR-007) with a granular alerting system (ADR-015) per user request.
  - The system now alerts on *every* status transition (GO ↔ CAUTION ↔ AVOID), ensuring full visibility into environment changes.
  - Implemented cycle-by-cycle tracking of all 7 scoring conditions; any change in a condition's status or human-readable detail triggers a Discord notification.
  - This ensures the trader is aware of all market fluctuations and value changes every minute.
  - Documented in `directives/adr/ADR-015_granular_condition_alerting.md`.
- **Trend Phase-Driven OI Flow Scoring (`processor.py`):**
  - Refactored `score_oi_flow` to derive status and points directly from calculated Trend Phases rather than strict opposing-side unwinding.
  - "Long Buildup" and "Short Buildup" now score **GREEN (1 pt)**, providing consistent points for stable trend continuations even if both sides build OI.
  - "Short Covering" and "Long Unwinding" now score **RED (0 pts)** and are labeled as "Pullbacks" per user request to avoid these trades.
  - Added specific detection for "Straddle Writing" in neutral markets where both sides build OI without price movement.
  - Documented in `directives/adr/ADR-016_trend_phase_oi_scoring.md`.
- **Sustained OI Window & Noise-Reduction Alerting (`scheduler.py`, `config.py`):**
  - Implemented a **5-minute rolling window** for all OI calculations (T vs T-5) to filter out 1-minute market jitter and capture institutional conviction.
  - Redesigned alerting logic to eliminate "digit jitter" noise. Discord notifications now only trigger when:
    - Overall **Environment Status** changes (GO ↔ CAUTION ↔ AVOID).
    - A **Condition Color/Emoji** changes.
    - An **OI Trend Phase** changes (e.g., Neutral → Short Buildup).
  - This ensures that every alert represents a significant market shift or trade opportunity.
  - Documented in `directives/adr/ADR-017_sustained_oi_window.md`.
- **Lunch Break No-Trading Alert (`scheduler.py`, `notifier.py`):**
  - New Discord alert fires once when the engine enters the dead zone between session 1 (ends 11:45 IST) and session 2 (starts 13:00 IST).
  - Posts to both `#environment` and `#system-check` channels, explicitly warning the trader not to act on stale signals during the 75-minute gap.
  - Uses a one-shot flag (`lunch_break_alert_sent`) to prevent duplicate messages; resets automatically each day.
  - Added `is_lunch_break()` helper to `scheduler.py` for clean time-window detection.
  - Documented in `directives/adr/ADR-013_lunch_break_alert.md`.

### Fixed
- **IV Flickering — Anti-Flap Hysteresis (`scheduler.py`, `config.py`):**
  - Root cause: Tiny fluctuations in IV change around zero caused the IV cap to toggle on/off every minute, resulting in an alternating `AVOID`→`CAUTION` status loop.
  - Fix: Implemented hysteresis logic where the IV cap, once triggered, is only released if IV recovers by `>= 0.30`.
  - Added `iv_cap_active` state to `SessionState` to track cross-cycle cap status.
  - Documented in `directives/adr/ADR-012_iv_cap_hysteresis.md`.

- **Move Ratio — Time-Horizon Scaling (`processor.py`, `engine.py`):**
  - Root cause: The implied move (straddle price) represented the expected move over the remaining life of the option (e.g., 6 days), while the realized move was measured over 15 minutes. This structural mismatch caused the ratio to be permanently RED (0.06–0.18 range).
  - Fix: Applied square-root-of-time scaling to the implied move to bring it down to a 15-minute equivalent before computing the ratio.
  - Added `max(dte, 1)` guard for division safety on expiry day.
  - Documented in `directives/adr/ADR-011_move_ratio_time_scaling.md`.
- **OI Flow — Cycle-Level Delta Implementation (`scheduler.py`, `fetcher.py`):**
  - Root cause: `fetcher.py` was returning a day-level OI delta (cumulative build since yesterday's close). This caused both legs (CE and PE) to always show positive build during market hours, leading to a permanent "Confused" (RED) scoring state.
  - Fix: Moved delta calculation to `scheduler.py`. The scheduler now maintains an in-memory `prev_oi_snapshot` and calculates the delta relative to the previous 1-minute cycle.
  - Removed day-level delta calculation from `fetcher.py` to ensure it remains a stateless API proxy.
  - Documented decision in `directives/adr/ADR-010_cycle_level_oi_delta.md`.
- **Initial Session Alert & Boundary Reset (`scheduler.py`):**
  - Root cause: The "proof of life" alert (introduced in ADR-008) only triggered if `previous_status` was `None`. However, the system failed to reset this status when crossing the lunch break boundary, causing the 1:00 PM session to start without an alert if the market status was unchanged from the morning.
  - Fix: Implemented `state.reset_buffers()` call within the session entry gate. This ensures `previous_status` is cleared, triggering the initial alert, and also flushes stale buffers between the morning and afternoon sessions.
  - Added regression test `test_run_cycle_session_transition_resets_state` to `tests/test_scheduler.py`.
- **Gamma/Theta Ratio — Theta Normalization (`processor.py`):**
  - Root cause: Dhan returns `theta` as an absolute ₹-per-day decay value (e.g. `-29.3`) while `gamma` is expressed per point² (e.g. `0.00047`). These are dimensionally incompatible, causing the raw `gamma / abs(theta)` ratio to always be near zero and score permanently RED.
  - Fix: `theta` is now normalized by the spot price (`theta_normalized = abs(theta) / spot_price`) before the ratio is computed. This converts theta to `₹-decay / point` (matching gamma's per-point² scale), yielding a meaningful ratio in the correct order of magnitude.
  - Updated `docs/scoring_architecture.md` and `directives/adr/ADR-004` to reflect the normalization step.
- **Scheduler Heartbeat:**
  - Suppressed heartbeats when no active session exists (returns `None` or status `STOPPED` from db).
  - Updated `run_heartbeat()` to call `db.get_active_session()` directly for live status.
- **Scoring Engine Calibration:**
  - Recalibrated `score_gamma_theta` thresholds by 1000x to match Dhan API's per-point-squared index terms.
  - Implemented CE/PE Greek averaging for robustness and added a session-open guard for `gamma=0` cases.
  - Increased reporting precision for ratios to 6 decimal places.
- **Dhan API Integration:** Resolved critical data fetching issues:
  - Corrected `/optionchain` endpoint payload (changed `UnderlyingSegment` to `UnderlyingSeg`) and response parsing for `data.oc` structure.
  - Fixed `/charts/intraday` by ensuring `securityId` is an integer and handling root-level OHLCV array responses.
  - Implemented smart index logic for `/charts/historical` to correctly identify the previous trading day when today's candle is (or isn't) present.
  - Corrected `/optionchain/expirylist` endpoint path and method.
- **Dhan Debugging:** Added detailed raw response logging for 4xx/5xx errors to speed up future troubleshooting.
- **Session Logic:** Removed temporary debug bypass flags and cleaned up verbose execution logging.
- **Test Suite:** 
  - Fixed `tests/test_fetcher.py` by aligning mock payloads with the actual nested `data.oc` API structure.
  - Updated `tests/test_processor.py` to use realistic Dhan-scale Greeks and added DTE-scaled boundary tests.
  - Built an exhaustive 49-assertion cumulative `pytest` suite ensuring all mathematical thresholds map to the correct 🟢/🟡/🔴 point system.
- **Documentation:** Added `ADR-004` documenting Gamma/Theta calibration decisions.
- **Documentation:** Updated `docs/scoring_architecture.md` with recalibrated thresholds.
- **Architecture Records:** Initialized `directives/adr/` capturing critical system design choices (Supabase Bridge, IV Cap, Greek Scaling).
- **Repo Tooling:** Included standard Python `.gitignore` and `.env.example`.

### Changed
- **Structure:** Relocated core execution modules into `src/kairos/` aligning strictly with `pyproject.toml` packaging standards.
