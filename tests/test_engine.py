from collections import deque
from kairos.engine import find_atm, evaluate
from kairos.models import PreviousDayLevels, SessionConfig

def test_find_atm_exact(make_option_row):
    chain = [
        make_option_row(21950, "CE"), make_option_row(21950, "PE"),
        make_option_row(22000, "CE"), make_option_row(22000, "PE"),
        make_option_row(22050, "CE"), make_option_row(22050, "PE"),
    ]
    atm = find_atm(chain, 22010.5, 50)
    assert atm.atm_strike == 22000
    assert atm.ce.strike == 22000
    assert atm.pe.strike == 22000

def test_evaluate_iv_cap(make_candle, make_option_row, mock_now, mock_date):
    chain = [
        make_option_row(22000, "CE", iv=0.10, gamma=0.3, theta=-0.5, oi_change=1000, ltp=150.0),
        make_option_row(22000, "PE", iv=0.10, gamma=0.3, theta=-0.5, oi_change=-500, ltp=150.0)
    ]
    c_buf = deque(maxlen=15)
    for _ in range(10):
        c_buf.append(make_candle(22000.0, volume=100, vwap=22000.0, low=21700.0, high=22300.0))

    c_buf.append(make_candle(22005.0, volume=1000, vwap=22000.0, low=21700.0, high=22300.0))
    c_buf.append(make_candle(22010.0, volume=1000, vwap=22000.0, low=21700.0, high=22300.0))
    c_buf.append(make_candle(22015.0, volume=1000, vwap=22000.0, low=21700.0, high=22300.0))
    c_buf.append(make_candle(22020.0, volume=1000, vwap=22000.0, low=21700.0, high=22300.0))
    c_buf.append(make_candle(22025.0, low=21700.0, high=22500.0, volume=1000, vwap=22000.0))
    
    i_buf = deque([0.30] * 15, maxlen=20)
    i_buf.append(0.10) # IV contracting (RED) -> triggers cap!
    
    prev = PreviousDayLevels(symbol="NIFTY", trade_date=mock_date, prev_day_high=21000, prev_day_low=20000, fetched_at=mock_now)
    config = SessionConfig(symbol="NIFTY", expiry=mock_date, expiry_type="WEEKLY", status="ACTIVE")

    score = evaluate(chain, c_buf, i_buf, prev, 22025.0, 3, config)
    assert score.iv_capped == True
    assert score.status == "CAUTION"

def test_evaluate_go_no_cap(make_candle, make_option_row, mock_now, mock_date):
    chain = [
        make_option_row(22000, "CE", iv=0.10, gamma=0.3, theta=-0.5, oi_change=1000, ltp=150.0),
        make_option_row(22000, "PE", iv=0.10, gamma=0.3, theta=-0.5, oi_change=-500, ltp=150.0)
    ]
    c_buf = deque(maxlen=15)
    for _ in range(10):
        c_buf.append(make_candle(22000.0, volume=100, vwap=22000.0, low=21700.0, high=22300.0))

    c_buf.append(make_candle(22005.0, volume=1000, vwap=22000.0, low=21700.0, high=22300.0))
    c_buf.append(make_candle(22010.0, volume=1000, vwap=22000.0, low=21700.0, high=22300.0))
    c_buf.append(make_candle(22015.0, volume=1000, vwap=22000.0, low=21700.0, high=22300.0))
    c_buf.append(make_candle(22020.0, volume=1000, vwap=22000.0, low=21700.0, high=22300.0))
    c_buf.append(make_candle(22025.0, low=21700.0, high=22500.0, volume=1000, vwap=22000.0))
    
    i_buf = deque([0.10] * 15, maxlen=20)
    i_buf.append(2.50) # +2.4 expansion => Green (2 pts)
    
    prev = PreviousDayLevels(symbol="NIFTY", trade_date=mock_date, prev_day_high=21000, prev_day_low=20000, fetched_at=mock_now)
    config = SessionConfig(symbol="NIFTY", expiry=mock_date, expiry_type="WEEKLY", status="ACTIVE")

    score = evaluate(chain, c_buf, i_buf, prev, 22025.0, 3, config)
    assert score.iv_capped == False
    assert score.score >= 7
    assert score.status == "GO"


def test_evaluate_honest_8_of_8(make_candle, make_option_row, mock_now, mock_date):
    """
    A textbook mid-session bullish breakout reaches a genuine 8/8 through the real
    scoring paths — the OI Flow consensus is built by running evaluate() repeatedly
    on genuinely aligned chain data, not by hand-fabricating OIFlowResult objects.

    Pre-ADR-024 this exact scenario capped at 7/8: 0.45% VWAP extension scored RED
    under the mean-reversion bands even though the trend itself was clean.
    """
    # Aligned chain: LONG_BUILDUP OI flow (total +11k > 8k threshold), dealer GEX
    # net-negative (trend regime), NDE net-long confirming, PCR 3.0 bullish,
    # realistic Dhan-scale greeks (gamma/theta ratio ≈ 1.93).
    chain = [
        make_option_row(
            22100, "CE", iv=14.0, delta=0.5, gamma=0.00132, theta=-15.15,
            oi=10_000, oi_change=9_000, ltp=90.0,
        ),
        make_option_row(
            22100, "PE", iv=14.0, delta=-0.10, gamma=0.00132, theta=-15.15,
            oi=30_000, oi_change=2_000, ltp=90.0,
        ),
    ]

    # 10 flat candles then a clean 5-candle thrust: range 0.32%, volume spike,
    # 4/4 up-transitions, VWAP anchored at 22000.
    c_buf = deque(maxlen=15)
    for _ in range(10):
        c_buf.append(make_candle(22000.0, volume=100, vwap=22000.0))
    for close in (22030.0, 22050.0, 22065.0, 22080.0):
        c_buf.append(make_candle(close, volume=100, vwap=22000.0))
    c_buf.append(make_candle(22100.0, volume=600, vwap=22000.0))

    # IV expanding +0.60 — clears the DTE>=3 GREEN bar (0.50) without a
    # crash-level jump, and disarms the vega trap (iv_change_rate > 0).
    i_buf = deque([14.0] * 15, maxlen=20)
    i_buf.append(14.6)

    # Spot 22100 breaks the prior-day high 22000 → C5 GREEN, C7 trend-mode.
    prev = PreviousDayLevels(symbol="NIFTY", trade_date=mock_date, prev_day_high=22000, prev_day_low=21800, fetched_at=mock_now)
    config = SessionConfig(symbol="NIFTY", expiry=mock_date, expiry_type="WEEKLY", status="ACTIVE")

    # Consensus is earned: 5 consecutive cycles of genuinely green raw OI flow.
    oi_buf = deque(maxlen=8)
    score = None
    for _ in range(5):
        score = evaluate(chain, c_buf, i_buf, prev, 22100.0, 3, config, oi_flow_buffer=oi_buf)

    oi_cond = score.get_condition("oi_flow")
    assert oi_cond is not None
    assert oi_cond.status == "GREEN"
    assert oi_cond.points == 1

    vwap_cond = score.get_condition("vwap_distance")
    assert vwap_cond.status == "GREEN"
    assert "trend-mode" in vwap_cond.detail

    assert not score.iv_capped
    assert score.max_score == 8
    assert score.score == 8
    assert score.status == "GO"

