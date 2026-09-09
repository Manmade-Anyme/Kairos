# TASK-103: Audited OI Flow Filter Remediation

**Task ID:** MANM-103  
**Date:** 2026-09-08  
**Status:** Proposed amendment; implementation must wait for human approval  
**Supersedes/amends:** ADR-018, ADR-021, ADR-012, ADR-015, ADR-017, ADR-023

## Problem statement

At audit baseline `3950e7108f1dbec107980e30727ae4dd8f195d1d`, Condition 3 can
remain inactive or report a false positive because:

- PCR is still a required green gate and is included in conviction text.
- `consolidate_oi_flow` counts historical green votes before evaluating the
  current reading, so a current trap, pin, contradiction, ambiguity, or
  ineligible phase can be masked by seven earlier green readings.
- Bullish and bearish green votes are pooled into one count.
- A zero or unusable Greek denominator is rendered as valid neutral, allowing
  incomplete data to qualify for green.
- The near-ATM Vega numerator and denominator share the same ±3 window, making
  the exposure share reach 100% for positive Vega inputs.
- The scheduler suppresses important OI/trap events when total score is below
  six, and the environment status / hysteresis path can still allow an OI Vega
  block to report `GO`.

The change must preserve OI calculations, the 16-cycle OI lookback, existing
phase eligibility (including the current Short Covering / Long Unwinding
behavior), rolling consensus dimensions, 40% Vega exposure threshold, and
zero IV-change threshold.

## Decision

Keep the existing pipeline and data ownership, but make Condition 3 a
directional, validity-aware safety gate. The flow is:

```text
option-chain + candle/IV buffers
        → engine aggregation (main ±7; near-ATM ±3)
        → raw OI scorer (current validity and vetoes)
        → rolling consensus (8 readings, directional 5/8)
        → environment total/status (OI veto caps GO)
        → scheduler event fingerprint and notifier
```

### 1. Current-reading failures have precedence

`consolidate_oi_flow` must inspect the newest raw reading before any historical
vote or recovery rule. A current reading immediately produces a red OI result
when any of these holds:

- current Vega trap;
- GEX pin;
- NDE contradiction or ambiguity/non-confirmation for a directional phase;
- neutral/ineligible phase;
- invalid or missing Greek aggregates, unusable denominator, insufficient
  history, or stale observation.

The existing three-trap history threshold remains a recovery hold: once the
window contains at least three trap readings, a later clean reading remains
blocked until the retained history falls below the configured threshold. It is
not a delay before blocking a newly observed trap. A current trap therefore
blocks even when the history count is zero or one.

The consolidated result must expose an effective OI veto (or equivalent
unambiguous state) so a retained Vega block cannot be lost between Condition 3
and environment status. Its reason must distinguish `Current ...` from
`Historical recovery hold ...`.

### 2. Directional consensus

Retain `oi_consensus_window = 8` and
`oi_consensus_green_threshold = 5`. Define a phase-to-direction helper with
the existing eligibility mapping:

| Phase | Direction for consensus |
|---|---|
| Long Buildup, Short Covering | bullish |
| Short Buildup, Long Unwinding | bearish |
| Neutral | none |

The current/latest directional phase is the reference direction. Count only
raw readings that are green, valid, NDE-confirming, and map to that same
direction. Never add bullish and bearish counts. Green requires at least five
qualifying votes matching the current direction; the displayed phase, NDE
state, and current price direction must agree. A direction reversal must
invalidate the old side immediately rather than inherit its vote count.

The latest valid snapshot remains the source for displayed GEX/NDE/Vega/wall
fields. A historical recovery hold may override the score, but must not make
the display claim that the current snapshot itself has a trap.

### 3. Vega exposure normalization

In `compute_greeks_aggregates`, use one deterministic weight function for both
windows (ATM weight 1.0; existing distance weights for shared strikes):

- numerator: absolute weighted Vega exposure within `oi_flow_vega_window`
  (default ±3 strikes);
- denominator: absolute weighted Vega exposure within
  `oi_flow_strike_window` (default ±7 strikes).

The trap condition is `denominator > 0`,
`numerator / denominator > settings.vega_high_pct`, and
`iv_change_rate < settings.vega_trap_iv_threshold`. Keep the 40% and 0.0
defaults unchanged. Missing/non-finite Greeks or a non-positive denominator
are invalid data, not a neutral-green result. Preserve IV delta units in the
existing units and label the reason consistently (do not silently append a
percent sign to an absolute IV-point delta).

### 4. Validity, warmup, and staleness

Use explicit validity in the internal/result contract. Recommended minimal
fields are `data_valid`, `stale`, and an effective-veto/reason field on
`OIFlowResult`; defaults must preserve compatibility for existing fixtures.
The raw scorer must mark the result invalid when required Greeks are absent,
non-finite, or have unusable denominators, or when the input observation is
stale. `gex_state="neutral"` is green-eligible only when GEX data and its
denominator are valid. `nde_state="neutral"` is ambiguous and never green.

Warmup remains `YELLOW/0` until the intended OI lookback and other required
buffers are populated. Repeated candles with the same observation timestamp
or otherwise stale input must not create independent confirmation votes.
Do not shorten `oi_lookback_cycles = 16` or alter phase classification.

### 5. Environment veto and hysteresis ordering

Apply OI effective vetoes after summing condition points and before returning
`EnvironmentScore`. The status rule is:

```text
raw status from total score
  → preserve AVOID
  → if effective Vega/OI veto and raw status == GO: CAUTION
  → attach explicit NO TRADE reason to OI condition/summary
```

The scheduler's IV hysteresis may keep an IV cap active, but must not clear or
raise a status constrained by the OI veto. The `GO 7/8` case with
`vega_trap=true` and `IV=YELLOW` must return at most `CAUTION` and contain an
explicit no-trade reason. A subsequent IV recovery must not undo an active or
retained OI veto.

### 6. Alert and explanation contract

Keep alert deduplication, but remove the total-score-below-six prerequisite for
these OI events:

- OI condition color/status changes;
- phase or direction changes;
- GEX state changes;
- NDE state changes;
- trap activation and clearance/hold transitions;
- a changed OI blocking reason.

Use a stable OI event fingerprint made from semantic fields (not arbitrary
numeric jitter). Post once per new fingerprint, including when overall
environment status is unchanged and total score is below six. Identical
fingerprints remain suppressed. The notification must show the actual current
OI blocking reason and clearly label a historical recovery hold. Rendered
GEX/NDE tokens must be derived from the same state used for scoring.

PCR may remain in `OIFlowResult` and the Discord payload as passive telemetry
for compatibility, but it must not affect scoring, vetoes, consensus, green
text, or the required trading checklist. Update active README/checklist text;
do not rewrite historical ADR decisions. Record this document as the
amendment. The existing code's ability to award green to Short Covering and
Long Unwinding remains unchanged in this task; the discrepancy with the old
checklist is documented, not silently corrected.

## Alternatives considered

- **Delete PCR and the field:** rejected; compatibility consumers may still
  need the passive metric. Decoupling it from decisions meets the requirement
  without an unnecessary schema break.
- **Let the 8-reading majority decide all failures:** rejected; current risk
  must override historical conviction for capital protection.
- **Keep one combined green count:** rejected; mixed-direction votes are not
  conviction in one direction.
- **Treat zero-denominator Greeks as neutral:** rejected; missing data cannot
  qualify a trade.
- **Raise IV tolerance or disable Vega detection:** rejected; it manufactures
  green readings and violates the protection requirement.
- **Global alert bypass for every low-score cycle:** rejected; bypass only the
  semantic OI event gate and retain fingerprint deduplication.

## Component boundaries and file assignments

Implementation is assigned to the Code Generator after human ADR approval.

| File | Boundary and required changes |
|---|---|
| `src/kairos/models.py` | Extend `OIFlowResult` only as needed for validity/effective-veto semantics; keep PCR/wall fields backward compatible. Document raw vs consolidated trap meaning. |
| `src/kairos/engine.py` | Update `compute_greeks_aggregates` for separate ±3 numerator and ±7 denominator with shared weights. Validate finite inputs and return explicit validity. In `evaluate`, apply the effective OI veto before status is returned; keep total scoring and lookbacks otherwise unchanged. |
| `src/kairos/processor.py` | Update `score_oi_flow` to remove PCR gating/text, reject invalid/ambiguous data, allow valid GEX neutral, and enforce current-failure precedence. Update `consolidate_oi_flow` with phase direction matching, current-first vetoes, five-of-eight directional consensus, and recovery-hold reasons. Preserve current phase eligibility. |
| `src/kairos/scheduler.py` | Preserve 16-cycle snapshot/candle behavior. Ensure IV hysteresis cannot undo an OI veto. Replace score-based OI alert suppression with semantic event fingerprinting and reset the fingerprint at session boundaries. |
| `src/kairos/notifier.py` | Keep PCR as passive telemetry only if compatibility needs it. Render state tokens from scored fields and always include the OI blocking reason for an OI event, including unchanged overall status; distinguish current failures and historical holds. |
| `src/kairos/config.py` | Preserve existing OI, Vega, IV, consensus, and phase defaults; only add a named staleness/validity setting if required by the implementation contract. |
| `tests/test_oi_flow.py` | Add focused raw scorer and consensus tests using valid/invalid fixtures, both directions, neutral GEX, all vetoes, reversals, recovery holds, and varied passive PCR. |
| `tests/test_scheduler.py` / `tests/test_scheduler_extra.py` | Add end-to-end status-cap, hysteresis, event/fingerprint, trap activation/clearance, stale observation, and low-score OI alert regressions. |
| `tests/test_engine.py` or a new engine regression module | Add realistic multi-strike chain aggregation tests for Vega numerator/denominator and missing/non-finite Greeks. Do not rely exclusively on independently supplied aggregate totals. |
| `tests/test_notifier.py` | Assert rendered GEX/NDE/phase/reason consistency and passive PCR behavior. |
| `pyproject.toml` | Add `respx` to the dev dependencies because existing fetcher/notifier tests import it. |
| `README.md` and active trading checklist documentation | Remove PCR as a decision requirement and align displayed OI rules; preserve historical ADR files and link this amendment. |
| `directives/adr/INDEX.md` | Add this amendment to the active ADR index. |

### API contracts

The exact names may be adapted to local style, but behavior must match these
contracts:

```python
def compute_greeks_aggregates(
    option_chain: list[OptionChainRow],
    atm_strike: int,
    spot_price: float,
    strike_step: float,
    lot_size: int,
    strike_window: int = 7,
    vega_window: int = 3,
) -> dict:
    """Return main-window aggregates plus separately validatable Vega share."""

def score_oi_flow(
    cluster: StrikeCluster,
    iv_change_rate: float,
    candle_buffer: deque,
) -> tuple[ConditionResult, OIFlowResult]:
    """Return one current, validity-aware OI reading; current vetoes win."""

def consolidate_oi_flow(
    buffer: deque[OIFlowResult],
) -> tuple[ConditionResult, OIFlowResult]:
    """Return current-first, direction-matched 5/8 consensus or a veto/hold."""
```

The consolidated result's `ConditionResult.detail` and `OIFlowResult.reason`
are the source of truth for the blocking explanation. `EnvironmentScore.status`
must never be `GO` when the consolidated result carries an effective Vega/OI
veto.

## Performance and security

- Aggregation remains O(number of chain rows) per cycle; two bounded windows
  reuse one row lookup and add no network calls or unbounded storage.
- Consensus remains O(8); event fingerprints are bounded to the current
  session. Keep the 16-cycle snapshot deque and existing candle limits.
- Validate finite numeric inputs before arithmetic to avoid NaN comparisons
  silently bypassing gates. Do not log credentials, raw broker payloads, or
  production data in new diagnostics.
- Preserve the existing in-memory-only evaluation boundary and Supabase/
  Discord interfaces. No new external endpoint, credential, or schema write
  is authorized by this amendment.

## Regression matrix

| Area | Required examples and assertion |
|---|---|
| PCR decoupling | Bullish and bearish valid readings with varied PCR, including contradictory PCR; same OI/GEX/NDE outcome and no PCR alignment text. |
| GEX/NDE | Valid GEX trend and valid GEX neutral can qualify; GEX pin, NDE contradiction, NDE ambiguity, and direction mismatch are red. |
| Current veto precedence | Seven prior green readings followed by current Vega trap, GEX pin, NDE contradiction/ambiguity, invalid data, and ineligible OI; each blocks immediately. |
| Environment cap | Reproduce `GO 7/8` with current `vega_trap=true`, `IV=YELLOW`; result is at most CAUTION with explicit NO TRADE. Verify active and retained historical trap cannot be cleared by IV hysteresis. |
| Directional consensus | Five matching bullish, five matching bearish, four bullish/four bearish, reversal, same-direction recovery, and three-trap retention. Assert no mixed-direction `8/8 unified conviction`. |
| Vega normalization | Realistic chain with ±3 and ±7 strikes proves low near-ATM share is not 100%; a genuinely >40% share with IV contraction still vetoes immediately. Test zero/invalid denominators and IV units. |
| Validity/warmup | Missing/non-finite Greeks, missing sides, stale repeated timestamps, insufficient history, and warmup remain YELLOW/0 and cannot vote green. |
| Alerts | OI RED→GREEN, trap activation, trap clearance/hold, changed blocking reason, and phase/state changes alert below total score six; identical fingerprints deduplicate. |
| Preservation | Existing OI lookback (16), phase mapping/eligibility, thresholds, rolling baseline, and unrelated condition scores remain unchanged. |
| Dependencies | Full test environment installs and imports `respx`; separate dependency failures from product failures. |

## Definition of done

- [ ] Human approves this amendment before implementation starts.
- [ ] Code Generator changes only the assigned boundaries and preserves the
      stated defaults and phase policy.
- [ ] PCR is passive compatibility telemetry only; active docs/checklist are
      updated and historical ADRs remain intact.
- [ ] Current failures override history; directional consensus is 5/8 over an
      8-reading window; the three-trap rule remains a recovery hold.
- [ ] Vega share uses a weighted ±3 numerator and weighted ±7 denominator;
      invalid data never becomes neutral-green; IV threshold remains 0.0.
- [ ] OI veto caps environment GO and survives IV hysteresis.
- [ ] OI alerts are semantic and deduplicated, not hidden by total score.
- [ ] Required focused, realistic-chain, end-to-end, notifier, and full-suite
      tests pass, with `respx` declared in dev dependencies.
- [ ] Reviewable PR links MANM-103 and includes test evidence, before/after
      examples, and the Short Covering / Long Unwinding policy discrepancy.
