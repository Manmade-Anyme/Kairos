"""
engine.py — Orchestrates all 7 conditions into a final EnvironmentScore.
Applies IV weighting (2pts), IV contraction cap, and generates summary_raw.
Computes Greeks aggregates for the upgraded Condition 3 scoring engine.
"""

from collections import deque
from datetime import datetime
from statistics import mean
from typing import Optional
from zoneinfo import ZoneInfo

from kairos.config import settings
from kairos.models import (
    ATMStrikes,
    ConditionResult,
    EnvironmentScore,
    OHLCVCandle,
    OIFlowResult,
    OptionChainRow,
    PreviousDayLevels,
    SessionConfig,
    StrikeCluster,
)
from kairos.processor import (
    consolidate_oi_flow,
    score_gamma_theta,
    score_iv_change,
    score_momentum,
    score_move_ratio,
    score_oi_flow,
    score_pdhl_breakout,
    score_vwap_distance,
)

IST = ZoneInfo("Asia/Kolkata")


def find_atm(
    option_chain: list[OptionChainRow],
    spot_price: float,
    strike_interval: int,
) -> ATMStrikes:
    """
    Determine the ATM strike and find the corresponding CE and PE rows.
    ATM = round(spot / interval) * interval
    """
    atm_strike = round(spot_price / strike_interval) * strike_interval

    ce_row = next(
        (r for r in option_chain if r.strike == atm_strike and r.option_type == "CE"),
        None,
    )
    pe_row = next(
        (r for r in option_chain if r.strike == atm_strike and r.option_type == "PE"),
        None,
    )

    if ce_row is None or pe_row is None:
        # Fallback: find closest available strike
        available_strikes = sorted({r.strike for r in option_chain})
        closest = min(available_strikes, key=lambda s: abs(s - spot_price))
        ce_row = next(
            (r for r in option_chain if r.strike == closest and r.option_type == "CE"),
            None,
        )
        pe_row = next(
            (r for r in option_chain if r.strike == closest and r.option_type == "PE"),
            None,
        )

    if ce_row is None or pe_row is None:
        raise ValueError(f"Cannot find ATM CE/PE for spot {spot_price} in option chain")

    return ATMStrikes(
        atm_strike=atm_strike,
        ce=ce_row,
        pe=pe_row,
        spot_price=spot_price,
    )


def compute_greeks_aggregates(
    option_chain: list[OptionChainRow],
    atm_strike: int,
    spot_price: float,
    strike_step: float,
    lot_size: int,
    strike_window: int = 7,
    vega_window: int = 3,
) -> dict:
    """
    Compute full-chain Greeks aggregates from parsed OptionChainRow list.
    Returns a dict of field_name → value to merge into StrikeCluster.

    Weight function (same as existing OI weighting):
      ATM = 1.0, wings ±1 to ±strike_window = 0.125 each.
    """
    # Build a lookup: (strike, option_type) → OptionChainRow
    rows_by_strike: dict[int, dict[str, OptionChainRow]] = {}
    for r in option_chain:
        rows_by_strike.setdefault(r.strike, {})[r.option_type] = r

    all_strikes = sorted(rows_by_strike.keys())
    if not all_strikes:
        return {}

    def _weight(strike: int) -> float:
        distance = abs(strike - atm_strike) / strike_step
        if distance == 0:
            return 1.0
        elif distance <= strike_window:
            return 0.125
        else:
            return 0.0

    # Accumulators
    total_nde = 0.0
    total_gex = 0.0
    total_theta = 0.0
    total_vega = 0.0
    total_abs_nde = 0.0
    total_abs_gex = 0.0
    total_abs_vega = 0.0
    total_ce_oi = 0  # unweighted for PCR
    total_pe_oi = 0  # unweighted for PCR
    ce_iv_list: list[float] = []
    pe_iv_list: list[float] = []
    ce_walls: dict[int, int] = {}
    pe_walls: dict[int, int] = {}
    pe_unwind_above_spot = False
    ce_unwind_below_spot = False

    for strike in all_strikes:
        sides = rows_by_strike[strike]
        w = _weight(strike)

        ce = sides.get("CE")
        pe = sides.get("PE")

        ce_oi = ce.oi if ce else 0
        ce_delta = ce.delta if ce else 0.0
        ce_gamma = ce.gamma if ce else 0.0
        ce_vega = ce.vega if ce else 0.0
        ce_theta = ce.theta if ce else 0.0
        ce_iv = ce.iv if ce else 0.0
        ce_prev_oi = ce.previous_oi if ce else 0

        pe_oi = pe.oi if pe else 0
        pe_delta = pe.delta if pe else 0.0  # Already negative from Dhan
        pe_gamma = pe.gamma if pe else 0.0
        pe_vega = pe.vega if pe else 0.0
        pe_theta = pe.theta if pe else 0.0
        pe_iv = pe.iv if pe else 0.0
        pe_prev_oi = pe.previous_oi if pe else 0

        if w > 0:
            # NDE: delta × OI (pe_delta already negative)
            total_nde += w * (ce_delta * ce_oi + pe_delta * pe_oi)
            total_abs_nde += abs(w * ce_delta * ce_oi) + abs(w * pe_delta * pe_oi)
            
            # GEX: (ce_gamma × ce_oi - pe_gamma × pe_oi) × lot_size
            total_gex += w * (ce_gamma * ce_oi - pe_gamma * pe_oi) * lot_size
            total_abs_gex += (w * ce_gamma * ce_oi * lot_size) + (w * pe_gamma * pe_oi * lot_size)
            
            # Theta burn: abs(theta) × OI
            total_theta += w * (abs(ce_theta) * ce_oi + abs(pe_theta) * pe_oi)
            # PCR accumulators (unweighted)
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            # Walls
            ce_walls[strike] = ce_oi
            pe_walls[strike] = pe_oi

            # Vega and IV skew — tighter window
            vega_distance = abs(strike - atm_strike) / strike_step
            if vega_distance <= vega_window:
                total_vega += w * (ce_vega * ce_oi + pe_vega * pe_oi)
                total_abs_vega += abs(w * ce_vega * ce_oi) + abs(w * pe_vega * pe_oi)
                if ce_iv > 0:
                    ce_iv_list.append(ce_iv)
                if pe_iv > 0:
                    pe_iv_list.append(pe_iv)

        # Smart money unwind scan (all strikes, not just windowed)
        if strike > spot_price and pe_prev_oi > 0 and pe:
            pe_delta_pct = (pe_oi - pe_prev_oi) / pe_prev_oi * 100
            if pe_delta_pct < -8.0:
                pe_unwind_above_spot = True
        if strike < spot_price and ce_prev_oi > 0 and ce:
            ce_delta_pct = (ce_oi - ce_prev_oi) / ce_prev_oi * 100
            if ce_delta_pct < -8.0:
                ce_unwind_below_spot = True

    # PCR
    pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0

    # IV skew
    if pe_iv_list and ce_iv_list:
        iv_skew = mean(pe_iv_list) - mean(ce_iv_list)
    else:
        iv_skew = 0.0

    # Wall detection
    ce_wall_strike = max(ce_walls, key=ce_walls.get) if ce_walls else None
    pe_wall_strike = max(pe_walls, key=pe_walls.get) if pe_walls else None
    ce_wall_oi_cr = round(ce_walls[ce_wall_strike] / 10_000_000, 2) if ce_wall_strike else None
    pe_wall_oi_cr = round(pe_walls[pe_wall_strike] / 10_000_000, 2) if pe_wall_strike else None

    return {
        "net_delta_exposure": total_nde,
        "net_gex": total_gex,
        "atm_vega_exposure": total_vega,
        "theta_burn_rate": total_theta,
        "pcr": pcr,
        "iv_skew": iv_skew,
        "pe_unwind_above_spot": pe_unwind_above_spot,
        "ce_unwind_below_spot": ce_unwind_below_spot,
        "ce_wall_strike": float(ce_wall_strike) if ce_wall_strike is not None else None,
        "ce_wall_oi_cr": ce_wall_oi_cr,
        "pe_wall_strike": float(pe_wall_strike) if pe_wall_strike is not None else None,
        "pe_wall_oi_cr": pe_wall_oi_cr,
        "total_abs_gex": total_abs_gex,
        "total_abs_nde": total_abs_nde,
        "total_abs_vega": total_abs_vega,
    }


def build_strike_cluster(
    option_chain: list[OptionChainRow],
    atm_strike: int,
    count: int,
    spot_price: float,
    symbol: str = "NIFTY",
) -> StrikeCluster:
    """
    Extracts high-conviction cluster (ATM ± count strikes).
    Dhan returns a full chain; we find the ATM index and slice.
    Also computes Greeks aggregates for the upgraded C3 scorer.
    """
    # 1. Get unique sorted strikes
    strikes = sorted(list(set(r.strike for r in option_chain)))

    try:
        atm_idx = strikes.index(atm_strike)
    except ValueError:
        # Fallback if exact ATM strike missing
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm_strike))

    start_idx = max(0, atm_idx - count)
    end_idx = min(len(strikes), atm_idx + count + 1)

    target_strikes = strikes[start_idx:end_idx]

    ce_cluster = [r for r in option_chain if r.strike in target_strikes and r.option_type == "CE"]
    pe_cluster = [r for r in option_chain if r.strike in target_strikes and r.option_type == "PE"]

    # Calculate Distance-Weighted Mean for the cluster (ADR-012 refinement)
    # Weights decay linearly from 1.0 (ATM) downwards to ~0.125 at ±7 strikes
    def get_weighted_sum(rows: list[OptionChainRow]) -> tuple[float, float]:
        total_weighted_oi = 0.0
        total_weight = 0.0
        for r in rows:
            try:
                offset = abs(target_strikes.index(r.strike) - target_strikes.index(atm_strike))
            except (ValueError, IndexError):
                offset = 0

            weight = max(0.01, 1.0 - (offset / (count + 1)))
            total_weighted_oi += r.oi_change * weight
            total_weight += weight

        return total_weighted_oi, total_weight

    ce_w_sum, ce_w_total = get_weighted_sum(ce_cluster)
    pe_w_sum, pe_w_total = get_weighted_sum(pe_cluster)

    # Final Mean = Weighted Sum / Total Weight
    ce_mean = ce_w_sum / ce_w_total if ce_w_total > 0 else 0
    pe_mean = pe_w_sum / pe_w_total if pe_w_total > 0 else 0

    # Resolve instrument-specific parameters
    if symbol == "SENSEX":
        strike_step = float(settings.sensex_strike_interval)
        lot_size = settings.sensex_lot_size
    else:
        strike_step = float(settings.nifty_strike_interval)
        lot_size = settings.nifty_lot_size

    # Compute Greeks aggregates for upgraded Condition 3
    greeks = compute_greeks_aggregates(
        option_chain=option_chain,
        atm_strike=atm_strike,
        spot_price=spot_price,
        strike_step=strike_step,
        lot_size=lot_size,
        strike_window=settings.oi_flow_strike_window,
        vega_window=settings.oi_flow_vega_window,
    )

    return StrikeCluster(
        atm_strike=atm_strike,
        spot_price=spot_price,
        ce_cluster=ce_cluster,
        pe_cluster=pe_cluster,
        total_ce_oi_change=int(round(ce_mean)),
        total_pe_oi_change=int(round(pe_mean)),
        **greeks,
    )



def _determine_status(score: int, iv_capped: bool) -> str:
    """
    Map numeric score to GO / CAUTION / AVOID.
    If IV cap is active, maximum status is CAUTION.
    """
    if score >= settings.score_go_min:
        raw_status = "GO"
    elif score >= settings.score_caution_min:
        raw_status = "CAUTION"
    else:
        raw_status = "AVOID"

    # IV contraction cap — downgrade GO to CAUTION
    if iv_capped and raw_status == "GO":
        return "CAUTION"

    return raw_status


def _build_summary_raw(
    conditions: list[ConditionResult],
    score: int,
    max_score: int,
    iv_capped: bool,
    dte: int,
) -> str:
    """
    Build a compact machine-readable summary string.
    This gets stored in environment_log.summary_raw.
    Gemini/Discord Orchestrator polishes this into the final Discord message.
    """
    parts = []
    for c in conditions:
        emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(c.status, "⚪")
        parts.append(f"{emoji} {c.name}: {c.detail} [{c.points}/{c.max_points}]")

    cap_note = " | ⚠️ IV cap active" if iv_capped else ""
    summary = (
        f"DTE={dte} | Score={score}/{max_score}{cap_note}\n"
        + "\n".join(parts)
    )
    return summary


def evaluate(
    option_chain: list[OptionChainRow],
    candle_buffer: deque,
    iv_buffer: deque,
    prev_levels: PreviousDayLevels,
    spot_price: float,
    dte: int,
    session_config: SessionConfig,
    previous_status: Optional[str] = None,
    oi_flow_buffer: Optional[deque] = None,
) -> EnvironmentScore:
    """
    Orchestrates the 7 technical scoring conditions into a unified EnvironmentScore.
    
    This function acts as the core mathematical engine for the Kairos system. It extracts the
    ATM options, delegates processing to the individual scoring modules, sums the points (Max: 8),
    and strictly applies the `iv_capped` logic before yielding a final status.
    
    All inputs are injected from purely in-memory buffers or single-cycle API fetches. 
    Zero disk I/O or network calls occur within this function.
    
    :param option_chain: Full list of current OptionChainRow objects fetched from the broker.
    :type option_chain: list[OptionChainRow]
    :param candle_buffer: Rolling 15-minute buffer of OHLCVCandle data.
    :type candle_buffer: collections.deque
    :param iv_buffer: Rolling 15-minute buffer of Implied Volatility floats.
    :type iv_buffer: collections.deque
    :param prev_levels: PreviousDayLevels loaded cleanly at session startup.
    :type prev_levels: PreviousDayLevels
    :param spot_price: The live value of the underlying index.
    :type spot_price: float
    :param dte: Days To Expiry of the active configuration.
    :type dte: int
    :param session_config: The system configuration dictating target symbols and active flags.
    :type session_config: SessionConfig
    :param previous_status: The text status from the T-1 scoring cycle to measure state changes.
    :type previous_status: Optional[str]
    :param oi_flow_buffer: Rolling consensus buffer of OIFlowResult objects.
    :type oi_flow_buffer: Optional[collections.deque]
    :return: The aggregated and capped EnvironmentScore ready for database serialization.
    :rtype: EnvironmentScore
    """
    if oi_flow_buffer is None:
        oi_flow_buffer = deque(maxlen=settings.oi_consensus_window)

    strike_interval = (
        settings.nifty_strike_interval
        if session_config.symbol == "NIFTY"
        else settings.sensex_strike_interval
    )

    # 1. Find single ATM row for Greek-heavy conditions (IV, Gamma/Theta, Move Ratio)
    atm = find_atm(option_chain, spot_price, strike_interval)

    # 2. Build multi-strike cluster for OI Flow and Trend Phases (ADR-012)
    cluster = build_strike_cluster(
        option_chain,
        atm.atm_strike,
        settings.oi_multi_strike_count,
        spot_price,
        symbol=session_config.symbol,
    )

    # ── Pre-compute price_change for Condition 3 phase classification ────
    lookback = settings.oi_lookback_cycles
    if len(candle_buffer) >= lookback:
        old_close = list(candle_buffer)[-lookback].close
        cluster.price_change = spot_price - old_close

    # ── Compute iv_change_rate for Condition 3 vega trap detection ────────
    iv_lookback = settings.iv_change_lookback
    if len(iv_buffer) >= iv_lookback:
        iv_change_rate = round(iv_buffer[-1] - iv_buffer[-iv_lookback], 3)
    else:
        iv_change_rate = 0.0

    # ── Run all 7 conditions ─────────────────────────────────────────────
    c1_iv = score_iv_change(iv_buffer, dte)
    c2_mom = score_momentum(candle_buffer)
    
    # Condition 3 raw + consensus filtering (ADR-021)
    c3_oi_raw, oi_flow_result_raw = score_oi_flow(cluster, iv_change_rate, candle_buffer)
    oi_flow_buffer.append(oi_flow_result_raw)
    c3_oi, oi_flow_result = consolidate_oi_flow(oi_flow_buffer)

    c4_gt = score_gamma_theta(atm, dte)
    c5_pdhl = score_pdhl_breakout(spot_price, prev_levels)
    c6_move = score_move_ratio(atm, candle_buffer, dte)
    c7_vwap = score_vwap_distance(spot_price, candle_buffer)

    conditions = [c1_iv, c2_mom, c3_oi, c4_gt, c5_pdhl, c6_move, c7_vwap]

    # ── Compute total score ───────────────────────────────────────────────
    total_score = sum(c.points for c in conditions)
    max_score = sum(c.max_points for c in conditions)   # should be 8

    # ── IV contraction cap ────────────────────────────────────────────────
    iv_capped = c1_iv.status == "RED"

    # ── Determine final status ────────────────────────────────────────────
    status = _determine_status(total_score, iv_capped)

    # ── Build summary_raw ─────────────────────────────────────────────────
    summary_raw = _build_summary_raw(conditions, total_score, max_score, iv_capped, dte)

    return EnvironmentScore(
        timestamp=datetime.now(IST),
        symbol=session_config.symbol,
        expiry=session_config.expiry,
        dte=dte,
        score=total_score,
        max_score=max_score,
        status=status,
        iv_capped=iv_capped,
        total_ce_oi_change=cluster.total_ce_oi_change,
        total_pe_oi_change=cluster.total_pe_oi_change,
        conditions=conditions,
        oi_flow_result=oi_flow_result,
        summary_raw=summary_raw,
        previous_status=previous_status,
    )
