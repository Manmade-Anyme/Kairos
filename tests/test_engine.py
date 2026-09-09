from collections import deque
from datetime import timedelta
from kairos.engine import find_atm, evaluate
from kairos.models import PreviousDayLevels, SessionConfig


def _complete_scoring_chain(make_option_row, **row_kwargs):
    return [
        make_option_row(strike, option_type, **row_kwargs)
        for strike in range(21650, 22351, 50)
        for option_type in ("CE", "PE")
    ]


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
    chain = _complete_scoring_chain(
        make_option_row, iv=0.10, gamma=0.3, theta=-0.5, vega=1.0, oi_change=10000, ltp=150.0
    )
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

def test_evaluate_initial_consensus_blocks_go(make_candle, make_option_row, mock_now, mock_date):
    chain = _complete_scoring_chain(
        make_option_row, iv=0.10, gamma=0.3, theta=-0.5, vega=1.0, oi_change=10000, ltp=150.0
    )
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
    assert score.status == "CAUTION"
    assert score.oi_flow_result.effective_veto is True


def test_evaluate_with_consensus_buffer(make_candle, make_option_row, mock_now, mock_date):
    from kairos.models import OIFlowResult, TrendPhase
    chain = _complete_scoring_chain(
        make_option_row, iv=0.10, gamma=0.3, theta=-0.5, vega=1.0, oi_change=10000, ltp=150.0
    )
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

    # Construct an oi_flow_buffer with 5 green cycles
    oi_buf = deque(maxlen=8)
    for _ in range(5):
        oi_buf.append(OIFlowResult(
            score=1,
            phase=TrendPhase.LONG_BUILDUP,
            reason="Green",
            gex_state="trend",
            nde_state="confirms",
            vega_trap=False,
            pcr=1.1,
            iv_skew=0.0
        ))
    
    score = evaluate(chain, c_buf, i_buf, prev, 22025.0, 3, config, oi_flow_buffer=oi_buf)
    
    oi_cond = score.get_condition("oi_flow")
    assert oi_cond is not None
    assert oi_cond.status == "GREEN"
    assert oi_cond.points == 1
    assert score.score == 8
    assert score.status == "GO"


def test_evaluate_caps_go_when_directional_consensus_is_not_met(
    make_candle, make_option_row, mock_now, mock_date, monkeypatch
):
    from kairos.models import ConditionResult, OIFlowResult, TrendPhase

    chain = _complete_scoring_chain(
        make_option_row, iv=0.10, gamma=0.3, theta=-0.5, vega=1.0, oi_change=10000, ltp=150.0
    )
    candles = deque(
        [make_candle(22000.0, volume=100, low=21700.0, high=22300.0, vwap=22000.0)] * 10,
        maxlen=15,
    )
    candles.extend(
        make_candle(close, low=21700.0, high=22500.0, volume=1000, vwap=22000.0)
        for close in (22005.0, 22010.0, 22015.0, 22020.0, 22025.0)
    )
    iv_buffer = deque([0.10] * 15 + [2.50], maxlen=20)
    previous_day = PreviousDayLevels(
        symbol="NIFTY", trade_date=mock_date, prev_day_high=21000, prev_day_low=20000, fetched_at=mock_now
    )
    config = SessionConfig(symbol="NIFTY", expiry=mock_date, expiry_type="WEEKLY", status="ACTIVE")
    raw_oi = OIFlowResult(
        score=1, phase=TrendPhase.LONG_BUILDUP, reason="Unified bullish conviction",
        gex_state="trend", nde_state="confirms", vega_trap=False, pcr=1.1, iv_skew=0.0,
    )
    monkeypatch.setattr(
        "kairos.engine.score_oi_flow",
        lambda *_: (
            ConditionResult(
                name="oi_flow", status="GREEN", points=1, max_points=1, detail=raw_oi.reason
            ),
            raw_oi,
        ),
    )
    oi_buffer = deque([raw_oi.model_copy() for _ in range(3)], maxlen=8)

    score = evaluate(chain, candles, iv_buffer, previous_day, 22025.0, 3, config, oi_flow_buffer=oi_buffer)

    assert (score.status, score.score, score.oi_flow_result.effective_veto) == ("CAUTION", 7, True)
def test_evaluate_rejects_a_regressed_observation_timestamp(
    make_candle, make_option_row, mock_now, mock_date
):
    from kairos.models import OIFlowResult, TrendPhase

    chain = _complete_scoring_chain(
        make_option_row, iv=0.10, gamma=0.3, theta=-0.5, vega=1.0, oi_change=10000
    )
    regressed_candle = make_candle(22025.0, low=21700.0, high=22500.0).model_copy(
        update={"timestamp": mock_now - timedelta(minutes=1)}
    )
    candles = deque([regressed_candle] * 15, maxlen=15)
    iv_buffer = deque([0.10] * 16, maxlen=20)
    previous_day = PreviousDayLevels(
        symbol="NIFTY", trade_date=mock_date, prev_day_high=21000, prev_day_low=20000, fetched_at=mock_now
    )
    config = SessionConfig(symbol="NIFTY", expiry=mock_date, expiry_type="WEEKLY", status="ACTIVE")
    oi_buffer = deque(
        [
            OIFlowResult(
                score=1,
                phase=TrendPhase.LONG_BUILDUP,
                reason="Green",
                gex_state="trend",
                nde_state="confirms",
                vega_trap=False,
                pcr=1.1,
                iv_skew=0.0,
                observation_timestamp=mock_now,
            ),
            OIFlowResult(
                score=0,
                phase=TrendPhase.LONG_BUILDUP,
                reason="Stale OI observation",
                gex_state="trend",
                nde_state="confirms",
                vega_trap=False,
                pcr=1.1,
                iv_skew=0.0,
                stale=True,
                observation_timestamp=mock_now - timedelta(minutes=2),
            ),
        ],
        maxlen=8,
    )

    score = evaluate(chain, candles, iv_buffer, previous_day, 22025.0, 3, config, oi_flow_buffer=oi_buffer)

    assert score.oi_flow_result.stale is True
