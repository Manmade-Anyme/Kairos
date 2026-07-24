# ADR-025: OI Flow Decoupled Gates (GEX as Veto, NDE as Conviction, PCR as Context)

**Date:** 2026-07-24
**Decision Status:** APPROVED

## Problem Statement

Condition 3 (OI Flow) almost never scored GREEN in production. Across 14 live alerts
(2026-07-21/22): GEX read "trend" **0** times, NDE read "ambiguous" **13 of 14** times, and the
single NDE-"confirms" cycle was vega-trapped. The green gate (old RULE D) required
`gex_state == "trend"` AND `nde_state == "confirms"` AND `pcr_confirms` simultaneously,
sustained in 5 of 8 consecutive cycles.

Code verification showed this gate is **self-contradictory**, because GEX sign, NDE sign, and
PCR are three differently-weighted reads of the *same* underlying quantity — the call/put OI
imbalance of the chain:

| Chain state | PCR | NDE sign | net GEX |
|---|---|---|---|
| Call-heavy | < 1 | positive | positive → "pin" |
| Put-heavy | > 1 | negative | negative → "trend" |

Given a phase, the old gate always demanded two lenses put-heavy plus one call-heavy at the
same instant:

| Phase | GEX "trend" wants | NDE "confirms" wants | PCR "confirms" wants | Blocked by |
|---|---|---|---|---|
| Bullish | put-heavy | **call-heavy** | put-heavy | NDE (RULE C fires) |
| Bearish | put-heavy | put-heavy | **call-heavy** (< 0.95) | PCR |

A bearish green required a chain that is simultaneously call-heavy (PCR < 0.95) and
put-gamma-heavy (net_gex < −20% of gross) — effectively impossible; a bullish green required a
put-heavy chain whose net delta is nonetheless positive, which instead triggers the NDE
*contradiction* veto. Compounding this, the NDE 20%-of-gross bar was rarely reachable because
near-ATM call (+δ) and put (−δ) exposure cancel in the net while adding in the gross.

Conceptually, the design **triple-counted one signal as three independent confirmations**, and
misused GEX — a *volatility-regime* indicator (dealers pinning vs amplifying) — as a
*directional* gate.

## Decision Made

Decouple the three lenses so each carries the meaning it actually measures:

1. **GEX → regime veto only.** The GEX **pin** hard override (RULE A) is unchanged — a pinned
   chain still forces 0, and ≥3 pin cycles still force the consensus RED. But `gex_state ==
   "trend"` is **no longer required** for green; when present it is reported in the reason
   string ("GEX amplifying") as a strengthener.
2. **Direction = phase + NDE.** The trend phase (price Δ × OI Δ — independent of the OI
   imbalance) selects the direction; NDE **confirms** it. New RULE D: a **buildup** phase
   (Long/Short Buildup only — pullback phases Short Covering / Long Unwinding remain excluded
   as exit flows, per the scoring contract) with `nde_state == "confirms"` → score 1.
   `nde_pct_threshold` relaxed **0.20 → 0.10** (provisional) to account for the ATM
   delta-cancellation effect.
3. **PCR → dropped as a gate** (user decision). It remains in `OIFlowResult`/alerts as
   descriptive context ("PCR aligned" / "PCR divergent") and is no longer able to block a green.
4. **All safety vetoes unchanged**: vega trap (RULE B), NDE contradiction (RULE C), GEX pin
   (RULE A), neutral phase, theta dominance, and the 5-of-8 consensus filter (ADR-021).

Green now means: **directional phase (weighted OI change > 8000) + net-delta positioning
confirming that direction + no pin + no vega trap + no theta dominance, sustained 5/8 cycles.**
Achievable in both directions; still multi-condition strict.

## Calibration Instrumentation

Every cycle now appends a machine-readable telemetry line to `summary_raw`:
`📐 c3_raw: gex=<net/gross> nde=<net/gross> pcr=<pcr>`. `execution/score_audit.py` parses these
(`c3_gex_ratio`, `c3_nde_ratio`, `c3_pcr`) into its distribution report, so the provisional
`nde_pct_threshold=0.10` and `gex_pin_pct=0.20` bars can be refined from measured live
distributions (per the ADR-024 calibration rule).

## Alternatives Considered & Rejected

1. **Keep PCR as a soft veto (block only on gross contradiction)** — rejected by user in favor
   of full removal; phase + NDE already encode direction with less redundancy.
2. **Invert the PCR thresholds for the contrarian reading** (low PCR = bearish *fuel*) —
   rejected: keeps a third correlated lens inside the gate and just relocates the contradiction.
3. **Require GEX "trend" for bullish only** (where it agrees with PCR) — rejected: asymmetric
   logic, and bullish was still blocked by the NDE clash.

## Component Boundaries

| File | Change |
|------|--------|
| `src/kairos/processor.py` | New RULE D (`nde_state == "confirms"`), PCR demoted to context, updated reason strings |
| `src/kairos/config.py` | `nde_pct_threshold` 0.20 → 0.10 (provisional, documented) |
| `src/kairos/engine.py` | `c3_raw` telemetry line appended to `summary_raw` |
| `execution/score_audit.py` | Parses `c3_gex_ratio` / `c3_nde_ratio` / `c3_pcr` |
| `tests/test_oi_flow.py` | Call-heavy bearish green regression, PCR-divergent green, NDE-neutral red |

## Definition of Done

- [x] Bearish green reachable in a call-heavy chain (the live 2026-07-22 regime) — regression test
- [x] Bullish green reachable — existing conviction test still passes
- [x] GEX pin and vega trap still force 0 (tests unchanged)
- [x] Full pytest suite green
- [ ] After ~2 weeks live: refine `nde_pct_threshold` / `gex_pin_pct` from `score_audit.py` c3_raw distributions

## Known Risks & Mitigation

- **Green frequency rises** (that is the point) — the 5/8 consensus filter and unchanged vetoes
  bound the increase; monitor via `score_audit.py` oi_flow green-rate.
- **NDE 0.10 bar is provisional** — could be too loose/tight for real chains; the `c3_nde_ratio`
  distribution answers this empirically.
