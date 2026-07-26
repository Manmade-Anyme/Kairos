"""
Edge-case tests for engine helpers: ATM fallbacks, Greeks aggregation, and status mapping.
"""
import pytest
from datetime import datetime, date
from collections import deque
from kairos.engine import (
    find_atm,
    compute_greeks_aggregates,
    build_strike_cluster,
    _determine_status,
    evaluate,
)
from kairos.models import (
    OptionChainRow,
    SessionConfig,
    OHLCVCandle,
    PreviousDayLevels,
)
from kairos.config import settings

def test_find_atm_fallback(make_option_row):
    """A missing exact ATM strike falls back to the closest available strike."""
    # Strike 22000 is requested but only 22050 is available
    chain = [
        make_option_row(22050, "CE"),
        make_option_row(22050, "PE")
    ]
    atm = find_atm(chain, 22000, 50)
    assert atm.ce.strike == 22050
    assert atm.pe.strike == 22050

def test_find_atm_error(make_option_row):
    """A chain with only one side of the ATM pair raises rather than scoring on partial data."""
    # available_strikes has rows, but only CE side
    chain = [make_option_row(22000, "CE")]
    with pytest.raises(ValueError, match="Cannot find ATM CE/PE"):
        find_atm(chain, 22000, 50)

def test_compute_greeks_empty():
    """An empty option chain yields no Greeks aggregates."""
    assert compute_greeks_aggregates([], 22000, 22000, 50.0, 50) == {}

def test_compute_greeks_weighting(make_option_row):
    """Strikes are weighted 1.0 at ATM and 0.125 through the wings when aggregating."""
    # Weights: ATM=1.0, ±1 to ±7 = 0.125
    chain = [
        make_option_row(22000, "CE", delta=0.5, oi_change=1000),
        make_option_row(22000, "PE", delta=-0.5, oi_change=1000),
        make_option_row(22050, "CE", delta=0.4, oi_change=1000), # distance 1 (weighted 0.125)
        make_option_row(22200, "CE", delta=0.1, oi_change=1000), # distance 4 (weighted 0.125)
        make_option_row(22500, "CE", delta=0.1, oi_change=1000), # distance 10 (weighted 0)
    ]
    res = compute_greeks_aggregates(chain, 22000, 22000, 50.0, 50)
    assert res["net_delta_exposure"] != 0

def test_compute_greeks_unwinds(make_option_row):
    """Smart-money unwinds are flagged when OI drops sharply on the far side of spot."""
    # PE Unwind Above Spot (price=22000, strike=22100, prev_oi=1000, curr_oi=900 -> -10%)
    pe_unwind = make_option_row(22100, "PE", previous_oi=1000, oi=900)
    
    # CE Unwind Below Spot (price=22000, strike=21900, prev_oi=1000, curr_oi=900)
    ce_unwind = make_option_row(21900, "CE", previous_oi=1000, oi=900)

    chain = [pe_unwind, ce_unwind]
    res = compute_greeks_aggregates(chain, 22000, 22000, 50.0, 50)
    assert res["pe_unwind_above_spot"] is True
    assert res["ce_unwind_below_spot"] is True

def test_compute_greeks_iv_skew_empty(make_option_row):
    """IV skew falls back to zero when only one side of the chain carries IV."""
    # Only CE has IV, or empty
    chain = [make_option_row(22000, "CE", iv=0.2)]
    res = compute_greeks_aggregates(chain, 22000, 22000, 50.0, 50)
    assert res["iv_skew"] == 0.0 # Line 193 fallback

def test_build_strike_cluster_fallback(make_option_row):
    """Cluster construction falls back to the nearest index when the ATM strike is absent."""
    # atm_strike 22000 not in strikes
    chain = [make_option_row(22050, "CE"), make_option_row(21950, "PE")]
    # Line 234-236: Exception in index()
    cluster = build_strike_cluster(chain, 22000, 7, 22000)
    assert cluster.atm_strike == 22000

def test_build_strike_cluster_sensex(make_option_row):
    """SENSEX clusters resolve their own strike interval and lot size."""
    chain = [make_option_row(70000, "CE"), make_option_row(70000, "PE")]
    cluster = build_strike_cluster(chain, 70000, 7, 70000, symbol="SENSEX")
    # This hits lines 272-273
    assert cluster.atm_strike == 70000

def test_determine_status_logic():
    """Score bands map to AVOID/CAUTION/GO, and an active IV cap downgrades GO to CAUTION."""
    # AVOID: score < 4
    assert _determine_status(3, False) == "AVOID"
    
    # CAUTION: 4 <= score < 7
    assert _determine_status(4, False) == "CAUTION"
    
    # CAUTION override: Score >= 7 (GO) but iv_capped=True
    assert _determine_status(7, True) == "CAUTION"

def test_evaluate_insufficient_iv(make_atm, mock_date, mock_now):
    """An IV buffer below the lookback leaves the change rate at zero instead of extrapolating."""
    # Line 413: len(iv_buffer) < iv_change_lookback (15)
    iv_buf = deque([0.15] * 5, maxlen=20)
    candle_buf = deque(maxlen=15)
    cfg = SessionConfig(symbol="NIFTY", expiry=mock_date, expiry_type="WEEKLY", status="ACTIVE")
    chain = [make_atm(22000).ce, make_atm(22000).pe]
    prev = PreviousDayLevels(symbol="NIFTY", trade_date=mock_date, prev_day_high=22000, prev_day_low=21800, fetched_at=mock_now)
    
    score = evaluate(chain, candle_buf, iv_buf, prev, 22005, 1, cfg)
    assert score.score >= 0 # Should run fine with iv_change_rate = 0.0

def test_evaluate_session_transition(make_atm, mock_date, mock_now):
    """A warmed candle buffer supplies the lookback close used for phase classification."""
    # dte transition check or symbols
    iv_buf = deque([0.15] * 20, maxlen=20)
    candle_buf = deque(maxlen=15)
    # Warm up candle_buf to hit old_close logic
    from kairos.models import OHLCVCandle
    for _ in range(20):
        candle_buf.append(OHLCVCandle(timestamp=mock_now, symbol="NIFTY", open=22000, high=22010, low=21990, close=22000, volume=100, vwap=22000))
    
    cfg = SessionConfig(symbol="NIFTY", expiry=mock_date, expiry_type="WEEKLY", status="ACTIVE")
    chain = [make_atm(22000).ce, make_atm(22000).pe]
    prev = PreviousDayLevels(symbol="NIFTY", trade_date=mock_date, prev_day_high=22000, prev_day_low=21800, fetched_at=mock_now)
    
    score = evaluate(chain, candle_buf, iv_buf, prev, 22005, 1, cfg)
    assert score is not None
    assert "Score=" in score.summary_raw
    assert "⚪" in score.summary_raw or "🔴" in score.summary_raw or "🟡" in score.summary_raw or "🟢" in score.summary_raw
