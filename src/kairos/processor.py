"""
processor.py — Pure functions for all 7 scoring conditions.
No I/O, no side effects. Each function takes data, returns a ConditionResult.
All data comes from in-memory buffers or the current cycle's fetched objects.
"""

from collections import deque
import math
from typing import Optional

from kairos.config import settings
from kairos.models import (
    ATMStrikes,
    ConditionResult,
    OHLCVCandle,
    OIFlowResult,
    PreviousDayLevels,
    StrikeCluster,
    TrendPhase,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _result(name: str, status: str, points: int, max_points: int, detail: str) -> ConditionResult:
    return ConditionResult(
        name=name,
        status=status,
        points=points,
        max_points=max_points,
        detail=detail,
    )


def _caution(name: str, max_points: int, reason: str = "Warming up") -> ConditionResult:
    """Default CAUTION result used during buffer warmup."""
    return _result(name, "YELLOW", max_points // 2, max_points, reason)


# ─────────────────────────────────────────────────────────────────────────────
# Condition 1 — IV Change Rate (max 2 points)
# ─────────────────────────────────────────────────────────────────────────────

def score_iv_change(iv_buffer: deque, dte: int = 0) -> ConditionResult:
    """
    Measures whether At-The-Money (ATM) Implied Volatility (IV) is expanding or contracting.
    
    Compares current IV against IV from `iv_change_lookback` (default: 15) periods ago.
    Thresholds are DTE-scaled (ADR-011): expiry-day accepts normal theta-drift as YELLOW,
    while early-week demands genuine expansion before scoring GREEN.
    
    :param iv_buffer: A rolling in-memory deque containing the recent history of IV floats.
    :type iv_buffer: collections.deque
    :param dte: Days To Expiry — used to select the appropriate threshold tier.
    :type dte: int
    :return: A ConditionResult mapping the points (Max: 2) and the status. 
             Returns RED (0 pts) if IV is contracting beyond the DTE-scaled threshold,
             which triggers the systemic IV Cap.
    :rtype: ConditionResult
    """
    lookback = settings.iv_change_lookback

    if len(iv_buffer) < lookback:
        remaining = lookback - len(iv_buffer)
        return _caution("iv_trend", 2, f"Warming up — {remaining} readings needed")

    iv_now = iv_buffer[-1]
    iv_then = iv_buffer[-lookback]
    change = round(iv_now - iv_then, 3)

    # Select thresholds based on DTE (ADR-011 — mirrors gamma/theta DTE-scaling)
    if dte >= 3:
        green_thresh = settings.iv_change_dte_high_green
        yellow_thresh = settings.iv_change_dte_high_yellow
        scale_label = "DTE≥3"
    elif dte == 2:
        green_thresh = settings.iv_change_dte_mid_green
        yellow_thresh = settings.iv_change_dte_mid_yellow
        scale_label = "DTE=2"
    else:
        green_thresh = settings.iv_change_dte_low_green
        yellow_thresh = settings.iv_change_dte_low_yellow
        scale_label = "DTE≤1"

    if change > green_thresh:
        return _result("iv_trend", "GREEN", 2, 2, f"+{change:.2f} — IV expanding ({scale_label})")
    elif change >= yellow_thresh:
        return _result("iv_trend", "YELLOW", 1, 2, f"{change:+.2f} — mild expansion ({scale_label})")
    else:
        return _result("iv_trend", "RED", 0, 2, f"{change:+.2f} — IV contracting ({scale_label})")


# ─────────────────────────────────────────────────────────────────────────────
# Condition 2 — Underlying Momentum + Directional Consistency (max 1 point)
# ─────────────────────────────────────────────────────────────────────────────

def score_momentum(candle_buffer: deque) -> ConditionResult:
    """
    Evaluates underlying momentum using price range, volume spikes, and directional consistency.
    
    Extracts the last 5 candles to determine if the price is trending cleanly or violently chopping.
    Checks volume against a 20-candle moving average to confirm institutional participation.
    
    :param candle_buffer: A rolling deque of the last 15 1-minute OHLCVCandle objects.
    :type candle_buffer: collections.deque
    :return: A ConditionResult object mapping status (GREEN requires Range > 0.3%, Volume Spike, and >=4/5 trend). Max points: 1.
    :rtype: ConditionResult
    """
    window = settings.momentum_candle_window
    vol_lookback = settings.momentum_volume_lookback

    if len(candle_buffer) < window:
        return _caution("momentum", 1, f"Warming up — need {window} candles")

    recent: list[OHLCVCandle] = list(candle_buffer)[-window:]
    all_candles: list[OHLCVCandle] = list(candle_buffer)

    # Part A — Range and volume
    price_high = max(c.high for c in recent)
    price_low = min(c.low for c in recent)
    current_close = recent[-1].close

    price_range = price_high - price_low
    range_pct = (price_range / current_close) * 100

    # Volume spike: current vs average of last N candles
    if len(all_candles) >= vol_lookback:
        avg_vol = sum(c.volume for c in all_candles[-vol_lookback:]) / vol_lookback
    else:
        avg_vol = sum(c.volume for c in all_candles) / len(all_candles)

    current_vol = recent[-1].volume
    volume_spike = current_vol > (avg_vol * settings.momentum_volume_multiplier)

    # Part B — Directional consistency (how many candles trend in same direction)
    closes = [c.close for c in recent]
    up_count = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    down_count = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])
    trend_count = max(up_count, down_count)

    direction = "up" if up_count > down_count else "down"

    # Score
    detail = (
        f"{range_pct:.2f}% range, "
        f"{'vol spike' if volume_spike else 'no vol spike'}, "
        f"trend {trend_count}/5 {direction}"
    )

    if (
        range_pct > settings.momentum_range_green
        and volume_spike
        and trend_count >= settings.momentum_trend_count_green
    ):
        return _result("momentum", "GREEN", 1, 1, detail)
    elif (
        range_pct < settings.momentum_range_yellow
        or trend_count <= 2
    ):
        return _result("momentum", "RED", 0, 1, detail)
    else:
        return _result("momentum", "YELLOW", 0, 1, detail)


# ─────────────────────────────────────────────────────────────────────────────
# Condition 3 — OI Flow at ATM (max 1 point) — Greeks-Aware Scoring Engine
# ─────────────────────────────────────────────────────────────────────────────

def _classify_phase(
    total_ce_oi_change: int,
    total_pe_oi_change: int,
    price_change: float,
) -> TrendPhase:
    """
    Classify the current market trend phase from price delta vs OI flow.
    Unchanged from original logic — just extracted to a named helper.
    """
    total_oi_change = total_ce_oi_change + total_pe_oi_change
    oi_thresh = settings.trend_phase_oi_threshold

    if price_change > 0:
        if total_oi_change > oi_thresh:
            return TrendPhase.LONG_BUILDUP
        elif total_oi_change < -oi_thresh:
            return TrendPhase.SHORT_COVERING
    elif price_change < 0:
        if total_oi_change > oi_thresh:
            return TrendPhase.SHORT_BUILDUP
        elif total_oi_change < -oi_thresh:
            return TrendPhase.LONG_UNWINDING
    return TrendPhase.NEUTRAL


def score_oi_flow(
    cluster: StrikeCluster,
    iv_change_rate: float,
    candle_buffer: deque,
) -> tuple[ConditionResult, OIFlowResult]:
    """
    Upgraded Condition 3: Full-chain Greeks-aware OI Flow scoring engine.

    Evaluates GEX (gamma exposure), NDE (net delta exposure), vega trap,
    PCR, and theta dominance alongside the existing phase classification
    to produce a conviction score.

    Returns:
        tuple of (ConditionResult for score aggregation, OIFlowResult for Discord formatting)
    """
    lookback = settings.oi_lookback_cycles
    if len(candle_buffer) < lookback:
        warmup_result = _caution("oi_flow", 1, f"Warming up — {lookback} candles needed for trend phase")
        warmup_oi = OIFlowResult(
            score=0,
            phase=TrendPhase.NEUTRAL,
            reason="Warming up",
            gex_state="neutral",
            nde_state="neutral",
            vega_trap=False,
            pcr=cluster.pcr,
            iv_skew=cluster.iv_skew,
            ce_wall_strike=cluster.ce_wall_strike,
            ce_wall_oi_cr=cluster.ce_wall_oi_cr,
            pe_wall_strike=cluster.pe_wall_strike,
            pe_wall_oi_cr=cluster.pe_wall_oi_cr,
            pe_unwind_above_spot=cluster.pe_unwind_above_spot,
            ce_unwind_below_spot=cluster.ce_unwind_below_spot,
        )
        return warmup_result, warmup_oi

    # ── STEP 1: Phase classification (UNCHANGED) ────────────────────────
    phase = _classify_phase(
        cluster.total_ce_oi_change,
        cluster.total_pe_oi_change,
        cluster.price_change,
    )
    phase_is_bearish = phase in (TrendPhase.SHORT_BUILDUP, TrendPhase.LONG_UNWINDING)
    phase_is_bullish = phase in (TrendPhase.LONG_BUILDUP, TrendPhase.SHORT_COVERING)
    phase_is_neutral = phase == TrendPhase.NEUTRAL

    # ── STEP 2: GEX gate ────────────────────────────────────────────────
    gex_threshold = cluster.total_abs_gex * settings.gex_pin_pct
    if gex_threshold > 0 and cluster.net_gex > gex_threshold:
        gex_state = "pin"
    elif gex_threshold > 0 and cluster.net_gex < -gex_threshold:
        gex_state = "trend"
    else:
        gex_state = "neutral"

    # ── STEP 3: NDE alignment ───────────────────────────────────────────
    nde = cluster.net_delta_exposure
    nde_threshold = cluster.total_abs_nde * settings.nde_pct_threshold
    
    if nde_threshold > 0:
        if phase_is_bearish and nde < -nde_threshold:
            nde_state = "confirms"
        elif phase_is_bullish and nde > nde_threshold:
            nde_state = "confirms"
        elif phase_is_bearish and nde > nde_threshold:
            nde_state = "contradicts"
        elif phase_is_bullish and nde < -nde_threshold:
            nde_state = "contradicts"
        else:
            nde_state = "neutral"
    else:
        nde_state = "neutral"

    # ── STEP 4: Vega trap ───────────────────────────────────────────────
    vega_trap = (
        cluster.total_abs_vega > 0
        and cluster.atm_vega_exposure > (cluster.total_abs_vega * settings.vega_high_pct)
        and iv_change_rate < settings.vega_trap_iv_threshold
    )

    # ── STEP 5: PCR confirmation ────────────────────────────────────────
    pcr_confirms = (
        (phase_is_bearish and cluster.pcr < settings.pcr_bearish_threshold)
        or (phase_is_bullish and cluster.pcr > settings.pcr_bullish_threshold)
    )

    # ── STEP 6: Theta dominance ─────────────────────────────────────────
    theta_dominant = cluster.theta_burn_rate > settings.theta_dominant_threshold

    # ── STEP 7: Score assembly (priority order) ─────────────────────────
    # Common kwargs for OIFlowResult construction
    common = dict(
        gex_state=gex_state,
        nde_state=nde_state,
        vega_trap=vega_trap,
        pcr=cluster.pcr,
        iv_skew=cluster.iv_skew,
        ce_wall_strike=cluster.ce_wall_strike,
        ce_wall_oi_cr=cluster.ce_wall_oi_cr,
        pe_wall_strike=cluster.pe_wall_strike,
        pe_wall_oi_cr=cluster.pe_wall_oi_cr,
        pe_unwind_above_spot=cluster.pe_unwind_above_spot,
        ce_unwind_below_spot=cluster.ce_unwind_below_spot,
    )

    def _make_result(score: int, reason: str) -> tuple[ConditionResult, OIFlowResult]:
        status = "GREEN" if score == 1 else "RED"
        oi_result = OIFlowResult(score=score, phase=phase, reason=reason, **common)
        cond_result = _result("oi_flow", status, score, 1, reason)
        return cond_result, oi_result

    # RULE B (highest priority): Vega trap → score=0
    if vega_trap:
        return _make_result(
            0,
            f"Vega trap — IV contracting ({iv_change_rate:+.2f}%) into high premium exposure",
        )

    # RULE C (second priority): NDE contradiction → score=0
    if nde_state == "contradicts":
        nde_dir = "net long delta" if nde > 0 else "net short delta"
        return _make_result(
            0,
            f"NDE contradicts phase — options {nde_dir} vs {phase.value} price action",
        )

    # RULE A (third priority): GEX pin → score=0
    if gex_state == "pin":
        return _make_result(
            0,
            f"GEX pin active ({cluster.net_gex:+,.0f}) — dealers absorbing moves",
        )

    # Neutral phase → score=0
    if phase_is_neutral:
        return _make_result(
            0,
            "Neutral phase — low OI participation, no directional conviction",
        )

    # Theta dominant AND NDE not confirming → score=0
    if theta_dominant and nde_state != "confirms":
        return _make_result(
            0,
            "Theta dominant — writers entrenched, premium decay accelerating",
        )

    # RULE D: ALL conditions must pass for score=1
    if gex_state == "trend" and nde_state == "confirms" and pcr_confirms:
        direction = "bearish" if phase_is_bearish else "bullish"
        return _make_result(
            1,
            f"Unified {direction} conviction — GEX trending, NDE confirms, PCR aligned",
        )

    # Default fallback → score=0
    return _make_result(
        0,
        "Mixed signals — partial conviction, wait for alignment",
    )


def consolidate_oi_flow(
    buffer: deque,
) -> tuple[ConditionResult, OIFlowResult]:
    """
    Consolidates the rolling history of OIFlowResult objects (T-0 to T-N)
    using a consensus voting mechanism (ADR-021).
    
    :param buffer: A rolling deque of OIFlowResult objects.
    :type buffer: collections.deque
    :return: A tuple of (ConditionResult, OIFlowResult) representing the consolidated output.
    """
    if not buffer:
        # Fallback if buffer is empty
        fallback_oi = OIFlowResult(
            score=0,
            phase=TrendPhase.NEUTRAL,
            reason="Warming up",
            gex_state="neutral",
            nde_state="neutral",
            vega_trap=False,
            pcr=1.0,
            iv_skew=0.0,
        )
        fallback_cond = _result("oi_flow", "YELLOW", 0, 1, "Warming up")
        return fallback_cond, fallback_oi

    # The most recent reading (T-0) carries the latest snapshot values (pcr, skew, walls, etc.)
    latest = buffer[-1]

    # 1. Count green cycles in the buffer
    green_count = sum(1 for r in buffer if r.score == 1)

    # 2. Safety overrides: check if traps are active in >= threshold cycles
    vega_trap_count = sum(1 for r in buffer if r.vega_trap)
    gex_pin_count = sum(1 for r in buffer if r.gex_state == "pin")

    # 3. Determine consensus phase (mode / most common phase in buffer)
    phases = [r.phase for r in buffer]
    # Find unique phases in order of appearance (most recent first)
    unique_phases = []
    for p in reversed(phases):
        if p not in unique_phases:
            unique_phases.append(p)
    most_common_phase = max(unique_phases, key=phases.count)

    phase_is_bearish = most_common_phase in (TrendPhase.SHORT_BUILDUP, TrendPhase.LONG_UNWINDING)
    phase_is_bullish = most_common_phase in (TrendPhase.LONG_BUILDUP, TrendPhase.SHORT_COVERING)
    phase_is_neutral = most_common_phase == TrendPhase.NEUTRAL

    # Determine final score and reason
    if vega_trap_count >= settings.oi_consensus_trap_threshold:
        score_val = 0
        reason = f"Vega trap active ({vega_trap_count}/{len(buffer)} cycles) — IV contracting into high premium exposure"
    elif gex_pin_count >= settings.oi_consensus_trap_threshold:
        score_val = 0
        reason = f"GEX pin active ({gex_pin_count}/{len(buffer)} cycles) — dealers absorbing moves"
    elif green_count >= settings.oi_consensus_green_threshold:
        score_val = 1
        direction = "bearish" if phase_is_bearish else "bullish"
        reason = f"Unified {direction} conviction (Consensus {green_count}/{len(buffer)}) — GEX trending, NDE confirms, PCR aligned"
    else:
        score_val = 0
        if phase_is_neutral:
            reason = f"Neutral phase — low OI participation, no directional conviction ({green_count}/{len(buffer)} green)"
        elif most_common_phase in (TrendPhase.SHORT_COVERING, TrendPhase.LONG_UNWINDING):
            reason = f"Pullback phase ({most_common_phase.value}) — avoid trading ({green_count}/{len(buffer)} green)"
        else:
            reason = f"Mixed signals — consensus not met ({green_count}/{len(buffer)} green cycles)"

    status = "GREEN" if score_val == 1 else "RED"
    cond_result = _result("oi_flow", status, score_val, 1, reason)

    consolidated_oi = OIFlowResult(
        score=score_val,
        phase=most_common_phase,
        reason=reason,
        gex_state=latest.gex_state,
        nde_state=latest.nde_state,
        vega_trap=latest.vega_trap,
        pcr=latest.pcr,
        iv_skew=latest.iv_skew,
        ce_wall_strike=latest.ce_wall_strike,
        ce_wall_oi_cr=latest.ce_wall_oi_cr,
        pe_wall_strike=latest.pe_wall_strike,
        pe_wall_oi_cr=latest.pe_wall_oi_cr,
        pe_unwind_above_spot=latest.pe_unwind_above_spot,
        ce_unwind_below_spot=latest.ce_unwind_below_spot,
    )

    return cond_result, consolidated_oi


# ─────────────────────────────────────────────────────────────────────────────
# Condition 4 — Gamma vs Theta Ratio, DTE-Scaled (max 1 point)
# ─────────────────────────────────────────────────────────────────────────────


def score_gamma_theta(atm: ATMStrikes, dte: int) -> ConditionResult:
    """
    Evaluates whether gamma-driven premium acceleration justifies the theta decay cost.

    Dhan API returns theta as an absolute rupee-per-day decay (e.g. -29.3) and gamma
    as a per-point-squared sensitivity (e.g. 0.00047). These are dimensionally
    incompatible, so theta is normalized by the spot price before computing the ratio:

        theta_normalized = abs(theta) / spot_price
        ratio = gamma / theta_normalized

    This converts theta to the same per-point scale as gamma, yielding a ratio that
    is meaningful and stable across different index levels.

    Thresholds are DTE-scaled: expiry-day conditions require a much higher gamma
    advantage before scoring GREEN.

    :param atm: Consolidated ATMStrikes object containing CE and PE rows and spot price.
    :type atm: ATMStrikes
    :param dte: Days To Expiry for the active contract.
    :type dte: int
    :return: GREEN (1 pt) if gamma advantage is sufficient; YELLOW if borderline; RED if theta dominant.
             Returns YELLOW on cold-start zero-value guards instead of a false RED.
    :rtype: ConditionResult
    """
    # Average CE and PE for robustness against one side having a bad reading
    gamma = (atm.ce.gamma + atm.pe.gamma) / 2
    theta = (abs(atm.ce.theta) + abs(atm.pe.theta)) / 2

    if theta == 0:
        return _result("gamma_theta", "YELLOW", 0, 1, "Theta = 0, data not yet populated")

    # Guard against session-open cold start where Dhan returns gamma=0.0
    if gamma == 0:
        return _result("gamma_theta", "YELLOW", 0, 1, "Gamma = 0, data not yet populated")

    # Normalize theta by spot price before computing the ratio.
    # Dhan returns theta as an absolute ₹-per-day decay (e.g. -29.3), while gamma is
    # expressed per point² (e.g. 0.00047). Dividing by spot_price converts theta to a
    # % basis, making both quantities dimensionally compatible for a meaningful ratio.
    theta_normalized = theta / atm.spot_price
    ratio = round(gamma / theta_normalized, 6)  # 6 decimal places — values are in 0.0000xx range

    # Select thresholds based on DTE
    if dte >= 3:
        green_thresh = settings.gamma_theta_dte_high_green
        yellow_thresh = settings.gamma_theta_dte_high_yellow
        scale_label = "DTE≥3"
    elif dte == 2:
        green_thresh = settings.gamma_theta_dte_mid_green
        yellow_thresh = settings.gamma_theta_dte_mid_yellow
        scale_label = "DTE=2"
    else:
        green_thresh = settings.gamma_theta_dte_low_green
        yellow_thresh = settings.gamma_theta_dte_low_yellow
        scale_label = "DTE=1"

    detail = f"Ratio {ratio:.6f} ({scale_label} scale)"

    if ratio > green_thresh:
        return _result("gamma_theta", "GREEN", 1, 1, f"{detail} — Gamma favored")
    elif ratio > yellow_thresh:
        return _result("gamma_theta", "YELLOW", 0, 1, f"{detail} — borderline")
    else:
        return _result("gamma_theta", "RED", 0, 1, f"{detail} — Theta dominant")


# ─────────────────────────────────────────────────────────────────────────────
# Condition 5 — Previous Day High/Low Breakout (max 1 point)
# ─────────────────────────────────────────────────────────────────────────────

def score_pdhl_breakout(
    spot_price: float,
    prev_levels: PreviousDayLevels,
) -> ConditionResult:
    """
    Calculates if the current spot price has escaped the previous day's value area.
    
    Examines if the asset is trending cleanly above the Previous Day High (PDH) or
    below the Previous Day Low (PDL), representing verified momentum.
    
    :param spot_price: The underlying asset's current live trading value.
    :type spot_price: float
    :param prev_levels: The static PreviousDayLevels loaded identically once at session startup.
    :type prev_levels: PreviousDayLevels
    :return: GREEN if cleanly broken out. YELLOW if trapped in the 0.1% near-band. (Max points: 1)
    :rtype: ConditionResult
    """
    pdh = prev_levels.prev_day_high
    pdl = prev_levels.prev_day_low
    near_band = spot_price * (settings.pdhl_near_band_pct / 100)

    detail = f"Spot {spot_price:.0f} | PDH {pdh:.0f} / PDL {pdl:.0f}"

    if spot_price > pdh:
        return _result("pdhl_breakout", "GREEN", 1, 1, f"Above PDH {pdh:.0f} — bullish breakout")
    elif spot_price < pdl:
        return _result("pdhl_breakout", "GREEN", 1, 1, f"Below PDL {pdl:.0f} — bearish breakout")
    elif spot_price >= (pdh - near_band) or spot_price <= (pdl + near_band):
        return _result("pdhl_breakout", "YELLOW", 0, 1, f"Near breakout level | {detail}")
    else:
        return _result("pdhl_breakout", "RED", 0, 1, f"Inside range | {detail}")


# ─────────────────────────────────────────────────────────────────────────────
# Condition 6 — Move Ratio: Realized vs Implied (max 1 point)
# ─────────────────────────────────────────────────────────────────────────────

def score_move_ratio(
    atm: ATMStrikes,
    candle_buffer: deque,
    dte: int = 0,
) -> ConditionResult:
    """
    Compares the asset's realized volatile move against the move priced-in by option sellers.
    
    Utilizes a 15-minute lookback of the `candle_buffer` to capture realized `High - Low` range,
    and maps it proportionally against the percentage cost of the ATM Straddle.
    
    :param atm: Standard ATM options object mapping the absolute LTP premiums.
    :type atm: ATMStrikes
    :param candle_buffer: Full rolling deque of the last 15 minutes of price action.
    :type candle_buffer: collections.deque
    :param dte: Days To Expiry of the active configuration. Defaults to 0 for safety.
    :type dte: int
    :return: GREEN if Ratio > 1.0 (sellers are underpricing the actual volatility). (Max points: 1)
    :rtype: ConditionResult
    """
    lookback = settings.move_ratio_lookback_candles

    if len(candle_buffer) < lookback:
        remaining = lookback - len(candle_buffer)
        return _caution("move_ratio", 1, f"Warming up — {remaining} candles needed")

    # Implied move from straddle price, scaled by sqrt(time)
    # A 15-minute range must be compared against a 15-minute implied window.
    straddle = atm.ce.ltp + atm.pe.ltp
    spot = atm.spot_price
    
    trading_mins_per_day = 375  # 09:15–15:30 IST
    total_remaining_mins = max(dte, 1) * trading_mins_per_day
    
    # Square-root-of-time scaling (ADR-004 principle)
    implied_pct = (straddle / spot) * 100 * math.sqrt(15 / total_remaining_mins)

    # Realized move over 15 candles
    recent: list[OHLCVCandle] = list(candle_buffer)[-lookback:]
    realized_high = max(c.high for c in recent)
    realized_low = min(c.low for c in recent)
    realized_pct = ((realized_high - realized_low) / spot) * 100

    if implied_pct == 0:
        return _caution("move_ratio", 1, "Straddle price = 0, cannot compute ratio")

    ratio = round(realized_pct / implied_pct, 3)
    detail = f"Ratio {ratio:.2f} (realized {realized_pct:.2f}% / implied {implied_pct:.2f}%)"

    if ratio > settings.move_ratio_green:
        return _result("move_ratio", "GREEN", 1, 1, f"{detail} — delivering > priced")
    elif ratio >= settings.move_ratio_yellow:
        return _result("move_ratio", "YELLOW", 0, 1, f"{detail} — roughly fair value")
    else:
        return _result("move_ratio", "RED", 0, 1, f"{detail} — premium overvalued for move")


# ─────────────────────────────────────────────────────────────────────────────
# Condition 7 — VWAP Distance (max 1 point)
# ─────────────────────────────────────────────────────────────────────────────

def score_vwap_distance(
    spot_price: float,
    candle_buffer: deque,
) -> ConditionResult:
    """
    Ensures the underlying index is not statistically over-extended from the session mean.
    
    VWAP is computed incrementally by the DHAN fetcher since 09:15 IST.
    A distance percentage > 0.40% signifies mean-reversion risk, halting continuation entries.
    
    :param spot_price: The live value of the underlying index.
    :type spot_price: float
    :param candle_buffer: Latest candles used purely to lift the most recent incremental VWAP.
    :type candle_buffer: collections.deque
    :return: Evaluated extension risk mapping GREEN/YELLOW/RED. (Max points: 1)
    :rtype: ConditionResult
    """
    if not candle_buffer:
        return _caution("vwap_distance", 1, "No candles yet")

    latest: OHLCVCandle = list(candle_buffer)[-1]
    vwap = latest.vwap

    if vwap == 0:
        return _caution("vwap_distance", 1, "VWAP not yet calculated")

    distance_pct = abs(spot_price - vwap) / vwap * 100
    direction = "above" if spot_price > vwap else "below"
    detail = f"{distance_pct:.2f}% {direction} VWAP {vwap:.0f}"

    if distance_pct < settings.vwap_distance_green:
        return _result("vwap_distance", "GREEN", 1, 1, f"{detail} — room to run")
    elif distance_pct <= settings.vwap_distance_yellow:
        return _result("vwap_distance", "YELLOW", 0, 1, f"{detail} — mildly extended")
    else:
        return _result("vwap_distance", "RED", 0, 1, f"{detail} — extended, exhaustion risk")
