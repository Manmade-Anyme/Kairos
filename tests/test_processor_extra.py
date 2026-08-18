from collections import deque
import pytest
from kairos.processor import (
    score_iv_change,
    score_momentum,
    score_gamma_theta,
    score_pdhl_breakout,
    score_move_ratio,
    score_vwap_distance,
    score_oi_flow,
    _classify_phase,
)
from kairos.models import PreviousDayLevels, TrendPhase

def test_score_iv_change_dte_high():
    # dte >= 3: green > 0.5, yellow > 0.0
    buf = deque([0.15] * 15, maxlen=20)
    # GREEN
    buf.append(0.7) # 0.7 - 0.15 = 0.55 > 0.5
    assert score_iv_change(buf, dte=3).status == "GREEN"
    # YELLOW
    buf.pop()
    buf.append(0.2) # 0.2 - 0.15 = 0.05
    assert score_iv_change(buf, dte=3).status == "YELLOW"

def test_score_iv_change_dte2():
    # dte=2: green > 0.30, yellow > -0.20
    buf = deque([0.15] * 15, maxlen=20)
    
    # GREEN
    buf.append(0.50) # 0.50 - 0.15 = 0.35 > 0.30
    assert score_iv_change(buf, dte=2).status == "GREEN"
    
    # YELLOW
    buf.pop()
    buf.append(0.10) # 0.1 - 0.15 = -0.05 (between -0.2 and 0.3)
    assert score_iv_change(buf, dte=2).status == "YELLOW"
    
    # RED
    buf.pop()
    buf.append(-0.10) # -0.1 - 0.15 = -0.25 < -0.2
    assert score_iv_change(buf, dte=2).status == "RED"

def test_score_momentum_extra(make_candle):
    # Insufficient candles for window (5)
    assert score_momentum(deque([make_candle(100.0)] * 3)).status == "YELLOW"

    # Insufficient candles for avg_vol lookback (20) but enough for window (5)
    buf = deque(maxlen=20)
    for _ in range(10): # < 20
        buf.append(make_candle(100.0, volume=100))
    
    # Now add 5 candles to reach window=5
    buf.append(make_candle(100.0, low=99.0, high=101.0, volume=100))
    buf.append(make_candle(101.0, low=100.0, high=102.0, volume=100))
    buf.append(make_candle(102.0, low=101.0, high=103.0, volume=100))
    buf.append(make_candle(103.0, low=102.0, high=104.0, volume=100))
    buf.append(make_candle(104.0, low=103.0, high=105.0, volume=100)) # No spike
    
    res = score_momentum(buf)
    assert res.status == "YELLOW"

def test_classify_phase_extra():
    # Short Covering: Price ↑, OI ↓
    assert _classify_phase(-5001, -5001, 10.0) == TrendPhase.SHORT_COVERING # OI change = -10002 < -8000
    
    # Long Unwinding: Price ↓, OI ↓
    assert _classify_phase(-5001, -5001, -10.0) == TrendPhase.LONG_UNWINDING
    
    # Price ↓, OI ↑ -> Short Buildup
    assert _classify_phase(5001, 5001, -10.0) == TrendPhase.SHORT_BUILDUP

    # Neutral: low OI change
    assert _classify_phase(100, 100, 10.0) == TrendPhase.NEUTRAL

def test_score_oi_flow_warmup(make_cluster):
    # Insufficient candles for oi_lookback_cycles (6)
    res_cond, res_oi = score_oi_flow(make_cluster(22000), 0.0, deque())
    assert res_cond.status == "YELLOW"
    assert res_oi.reason == "Warming up"

def test_score_oi_flow_vega_trap(make_cluster, make_candle):
    cluster = make_cluster(22000, ce_oi_change=10000, pe_oi_change=10000)
    cluster.price_change = 10.0 # Long buildup
    cluster.atm_vega_exposure = 30_000_000.0 # > 25M
    # vega trap if IV contraction (iv_change_rate < 0.0)
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    res_cond, res_oi = score_oi_flow(cluster, -0.5, buf)
    assert res_cond.status == "RED"
    assert "Vega trap" in res_oi.reason

def test_score_oi_flow_nde_contradicts(make_cluster, make_candle):
    cluster = make_cluster(22000, ce_oi_change=10000, pe_oi_change=10000)
    cluster.price_change = 10.0 # Long buildup
    cluster.net_delta_exposure = -500_000.0 # Bearsih NDE contradicts Bullish phase
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    res_cond, res_oi = score_oi_flow(cluster, 0.5, buf)
    assert res_cond.status == "RED"
    assert "NDE contradicts phase" in res_oi.reason

def test_score_oi_flow_fallback(make_cluster, make_candle):
    # Mixed signals fallback (line 345)
    cluster = make_cluster(22000, ce_oi_change=10000, pe_oi_change=10000)
    cluster.price_change = 10.0 # Bullish
    cluster.pcr = 0.8 # < 1.05 (threshold) -> pcr_confirms False
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    res_cond, res_oi = score_oi_flow(cluster, 0.5, buf)
    assert "Mixed signals" in res_oi.reason

def test_score_gamma_theta_exact_red(make_atm):
    # ratio <= yellow_thresh (line 420)
    # DTE=3: yellow_thresh = 0.00004
    atm = make_atm(20000, ce_gamma=0.00000001, ce_theta=-20.0) # ratio = 0.00001 < 40u
    assert score_gamma_theta(atm, 3).status == "RED"

def test_score_move_ratio_exact_red(make_candle, make_atm):
    # score_move_ratio RED (line 518)
    atm = make_atm(20000, ce_ltp=100, pe_ltp=100) # implied ~ 0.2%
    buf = deque([make_candle(20000)] * 15, maxlen=15)
    # realized < 0.7 * implied (0.14%)
    buf.append(make_candle(20000, low=19990, high=20010)) # range 20 pts = 0.1%
    assert score_move_ratio(atm, buf, dte=1).status == "RED"

def test_score_vwap_distance_exact_yellow(make_candle):
    buf = deque([make_candle(20000, vwap=20000)], maxlen=1)
    # distance = 0.3% (between 0.2 and 0.4)
    assert score_vwap_distance(20060, buf).status == "YELLOW" # Line 558

def test_score_oi_flow_gex_pin(make_cluster, make_candle):
    cluster = make_cluster(22000, ce_oi_change=10000, pe_oi_change=10000)
    cluster.price_change = 10.0 # Long buildup
    cluster.net_gex = 2_000_000.0 # > 1.3M threshold
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    res_cond, res_oi = score_oi_flow(cluster, 0.5, buf)
    assert res_cond.status == "RED"
    assert "GEX pin active" in res_oi.reason

def test_score_oi_flow_neutral_phase(make_cluster, make_candle):
    cluster = make_cluster(22000, ce_oi_change=100, pe_oi_change=100) # Neutral
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    res_cond, res_oi = score_oi_flow(cluster, 0.5, buf)
    assert res_cond.status == "RED"
    assert "Neutral phase" in res_oi.reason

def test_score_oi_flow_theta_dominant(make_cluster, make_candle):
    # Theta dominant (burn > 500M) and NDE not confirming
    cluster = make_cluster(22000, ce_oi_change=10000, pe_oi_change=10000)
    cluster.theta_burn_rate = 600_000_000.0 # > 500M
    cluster.net_delta_exposure = 0 # Neutral NDE (not confirms)
    cluster.price_change = 10.0 # Long buildup (Bullish)
    
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    res_cond, res_oi = score_oi_flow(cluster, 0.5, buf)
    assert res_cond.status == "RED"
    assert "Theta dominant" in res_oi.reason

def test_score_oi_flow_unified_conviction(make_cluster, make_candle):
    cluster = make_cluster(22000, ce_oi_change=10000, pe_oi_change=10000)
    cluster.price_change = 10.0 # Bullish
    cluster.net_gex = -2_000_000.0 # GEX trend ( Dealers short gamma )
    cluster.net_delta_exposure = 1_000_000.0 # NDE confirms Bullish
    cluster.pcr = 1.2 # PCR aligned (> 1.05)
    
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    res_cond, res_oi = score_oi_flow(cluster, 0.5, buf)
    assert res_cond.status == "GREEN"
    assert "Unified bullish conviction" in res_oi.reason

def test_score_gamma_theta_dte2(make_atm):
    # dte=2: green > 0.000120, yellow > 0.000060
    # YELLOW (between 60u and 120u)
    atm_yellow = make_atm(20000, ce_gamma=0.00000009, ce_theta=-20.0) # ratio = 0.00009
    assert score_gamma_theta(atm_yellow, 2).status == "YELLOW"
    
    # GREEN
    atm_green = make_atm(20000, ce_gamma=0.00000015, ce_theta=-20.0) # ratio = 0.00015
    assert score_gamma_theta(atm_green, 2).status == "GREEN"

def test_score_gamma_theta_low_dte_yellow(make_atm):
    # dte=0 (low): green > 0.00020, yellow > 0.00008
    atm = make_atm(20000, ce_gamma=0.0000001, ce_theta=-20.0) # ratio = 0.00010 (YELLOW)
    assert score_gamma_theta(atm, 0).status == "YELLOW"

def test_score_gamma_theta_theta_zero(make_atm):
    atm = make_atm(22000, ce_theta=0.0)
    res = score_gamma_theta(atm, 3)
    assert res.status == "YELLOW"
    assert "Theta = 0" in res.detail

def test_score_pdhl_breakout_extra(mock_now, mock_date):
    prev = PreviousDayLevels(symbol="NIFTY", trade_date=mock_date, prev_day_high=22000, prev_day_low=21800, fetched_at=mock_now)
    
    # Below PDL
    assert score_pdhl_breakout(21700, prev).status == "GREEN"
    
    # Near PDH (within 0.1% of 22000 is +/- 22)
    assert score_pdhl_breakout(21990, prev).status == "YELLOW"
    
    # Near PDL
    assert score_pdhl_breakout(21810, prev).status == "YELLOW"

def test_score_move_ratio_extra(make_candle, make_atm):
    atm = make_atm(22000, ce_ltp=100, pe_ltp=100)
    
    # Warmup
    buf_short = deque([make_candle(22000)] * 5, maxlen=15)
    assert score_move_ratio(atm, buf_short).status == "YELLOW"
    
    # straddle = 0 (implied_pct = 0)
    atm_zero = make_atm(22000, ce_ltp=0, pe_ltp=0)
    buf_full = deque([make_candle(22000)] * 15, maxlen=15)
    assert score_move_ratio(atm_zero, buf_full).status == "YELLOW"
    
    # YELLOW status (Fair value 0.7 - 1.0)
    # dte=1: scaling = sqrt(15 / 375) = 0.2
    # straddle = 200, spot = 20000 -> core implied = 1% -> scaled = 0.2%
    # realized range = 0.16% (ratio = 0.16 / 0.2 = 0.8)
    atm_y = make_atm(20000, ce_ltp=100, pe_ltp=100)
    buf_y = deque([make_candle(20000)] * 14, maxlen=15)
    buf_y.append(make_candle(20000, low=19984, high=20016)) # range 32 pts = 0.16%
    assert score_move_ratio(atm_y, buf_y, dte=1).status == "YELLOW"

def test_score_momentum_no_vol_avg(make_candle):
    # len(all_candles) >= 20 is False, but len(buf) >= 5 is True
    buf = deque(maxlen=20)
    for i in range(10):
        # range > 0.3, trend mixed
        buf.append(make_candle(100.0 + i, high=101.0 + i, low=99.0 + i))
    
    # This hits line 132 (else branch)
    assert score_momentum(buf).status == "YELLOW"

def test_score_oi_flow_nde_states(make_cluster, make_candle):
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    
    # NDE Confirms (Bearish)
    cluster_bear = make_cluster(22000, ce_oi_change=10000, pe_oi_change=10000)
    cluster_bear.price_change = -10.0 # Bearish
    cluster_bear.net_delta_exposure = -500_000.0 # < -400k
    res_cond, res_oi = score_oi_flow(cluster_bear, 0.5, buf)
    assert res_oi.nde_state == "confirms" # Line 253 hit

    # NDE Contradicts (Bearish phase but bullish delta)
    cluster_bear.net_delta_exposure = 500_000.0 # Contradicts
    res_cond, res_oi = score_oi_flow(cluster_bear, 0.5, buf)
    assert res_oi.nde_state == "contradicts" # Line 257 hit

def test_score_oi_flow_nde_states_green(make_cluster, make_candle):
    # NDE Confirms (Bullish) and reach GREEN
    cluster_bull = make_cluster(22000, ce_oi_change=10000, pe_oi_change=10000)
    cluster_bull.price_change = 10.0 # Bullish
    cluster_bull.net_gex = -2_000_000.0 # GEX trend
    cluster_bull.net_delta_exposure = 500_000.0 # Confirms
    cluster_bull.pcr = 1.2 # PCR Aligned
    buf = deque([make_candle(22000)] * 10, maxlen=15)
    res_cond, res_oi = score_oi_flow(cluster_bull, 0.5, buf)
    assert res_oi.nde_state == "confirms" # Line 255 hit
    assert res_cond.points == 1

def test_score_vwap_distance_cautions():
    # not candle_buffer
    assert score_vwap_distance(22000, deque()).status == "YELLOW" # Line 543
    
    # vwap = 0
    from kairos.models import OHLCVCandle
    from datetime import datetime
    c = OHLCVCandle(timestamp=datetime.now(), symbol="N", open=1, high=1, low=1, close=1, volume=1, vwap=0)
    assert score_vwap_distance(22000, deque([c])).status == "YELLOW" # Line 549
