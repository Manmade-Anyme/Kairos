# ADR-024: Trend-Aware VWAP Bands & Measured-Distribution Calibration

**Date:** 2026-07-24
**Decision Status:** APPROVED

## Problem Statement

A full 8/8 score had never been observed in live operation. An audit of the scoring engine,
live cycle output, and the repository's own ADR history identified three compounding causes:

1. **Structural conflict — C7 vs the trend cluster.** Condition 7 scored GREEN only when price
   sat within 0.20% of session VWAP, while Conditions 2/5/6 (momentum thrust, PDH/PDL breakout,
   realized-beats-implied) all require a directional move that, outside the opening minutes,
   necessarily pulls price away from VWAP. Any genuine trend therefore sacrificed the C7 point,
   capping the practical maximum at 7/8.
2. **C4 (Gamma/Theta) was a free point.** The DTE-scaled thresholds (`0.00008`–`0.0002`) assumed
   the ratio lives in the `0.0000xx` range, but real Dhan payloads produce ratios in the ~0.5–3.0
   range (live sample: `1.989` at DTE=6). Every reading cleared every bar, so C4 was
   unconditionally GREEN and its DTE scaling was dead logic. This inflated all scores by one
   unearned point while masking the strictness elsewhere.
3. **Duplicated / unreachable cap logic.** The engine-level `iv_capped` GO→CAUTION downgrade in
   `_determine_status` was unreachable from the engine path (a RED IV contributes 0 of 2 points,
   capping the remaining total at 6, below the GO threshold of 7). The scheduler's hysteresis
   HOLD path — the only place the downgrade can actually bite — re-implemented it inline.

A meta-pattern was also identified: ADR-010, ADR-011 (both), and ADR-004 each fixed a condition
that had been "permanently RED" because thresholds were derived from *assumed* units rather than
*measured* live distributions. C4 is the same failure mode in the opposite direction.

## Decision Made

**1. Trend-aware VWAP bands (C7).** `score_vwap_distance` accepts a `breakout` parameter
(`"bullish"` / `"bearish"` / `None`), derived by the engine from the same PDH/PDL levels C5
scores against. When a breakout is active **and** price is on the breakout side of VWAP, widened
bands apply: GREEN < 0.50%, YELLOW ≤ 0.75% (`vwap_trend_green` / `vwap_trend_yellow`). Riding
away from VWAP inside a confirmed breakout is healthy trend behavior — the genuine reversal
signal is a deep adverse crossing back through VWAP, which (like the no-breakout case) keeps the
original strict 0.20/0.40 bands.

**2. Recalibrated C4 thresholds.** Bars raised to the measured order of magnitude, preserving
the original tier proportions: DTE≥3 green > 1.5 / yellow > 0.75; DTE=2 green > 2.25 /
yellow > 1.10; DTE≤1 green > 3.75 / yellow > 1.50. These are provisional and marked for
empirical refinement.

**3. Unified cap downgrade.** The scheduler's hysteresis HOLD path now calls the engine's
`_determine_status(score, iv_capped=True)` instead of duplicating the downgrade, making the
engine implementation the single, reachable source of truth.

**4. Calibration instrumentation.** New `execution/score_audit.py` pulls `environment_log`
history and reports the score histogram, per-condition status rates, pairwise green
co-occurrence, and raw metric distributions parsed from `summary_raw`. **Process rule going
forward: scoring thresholds are calibrated from these measured distributions, never from assumed
API units.**

## Rationale

- The 0–8 scale and GO/CAUTION/AVOID semantics are preserved — no downstream Discord/DB changes.
- C2 and C3 remain strict by design; they are the conviction gates protecting an options buyer.
  This ADR removes *contradictions* and *dead thresholds*, not strictness.
- An honest 8/8 is now attainable on a textbook trend day and is covered by a regression test
  that reaches 8/8 through real scoring paths (no hand-fabricated consensus buffers).

## Alternatives Considered & Rejected

1. **Rescale to a 7-point maximum (drop C7 in trends)** — rejected: changes score semantics
   across DB, Discord, and docs for no behavioral gain over trend-aware bands.
2. **Partial credit (YELLOW = 0.5) across C2–C7** — rejected for now: `environment_log.score`
   is an integer column and the orchestrator parses integer scores; revisit only if the
   co-occurrence data shows near-misses matter.
3. **VWAP-slope/side-persistence trend detection inside C7** — rejected: the PDH/PDL breakout
   state already exists, is already what C5 anchors on, and requires no new buffers.

## Component Boundaries

| File | Change |
|------|--------|
| `src/kairos/processor.py` | `score_vwap_distance(…, breakout)` trend-mode bands |
| `src/kairos/engine.py` | Derives breakout direction from PDH/PDL, passes to C7 |
| `src/kairos/config.py` | `vwap_trend_green/yellow`; recalibrated `gamma_theta_*` |
| `src/kairos/scheduler.py` | HOLD path delegates to `_determine_status` |
| `src/kairos/db.py` | `fetch_environment_log(days)` read method |
| `execution/score_audit.py` | New distribution-audit CLI |
| `supabase_schema.sql` | `ce_oi_change` / `pe_oi_change` columns documented |

## Definition of Done

- [x] Honest 8/8 regression test passes through real code paths (`test_evaluate_honest_8_of_8`)
- [x] `breakout=None` path byte-identical to previous behavior
- [x] Recalibrated C4 tiers discriminate at realistic Dhan magnitudes
- [x] Full pytest suite green
- [ ] `score_audit.py` run against ≥2 weeks of live data; thresholds refined from the report

## Known Risks & Mitigation

- **Provisional C4 bars may mis-grade some regimes.** Mitigation: `score_audit.py` reports the
  measured gamma/theta ratio distribution; refine bars from percentiles after live accumulation.
- **Trend-mode C7 slightly increases ≥6 scores**, so more unsilenced alerts. Accepted — this is
  the intended effect of unblocking legitimate trend scores.
