"""Regression coverage for the MANM-103 OI Flow remediation."""

from collections import deque
from datetime import timedelta

from kairos.engine import compute_greeks_aggregates, evaluate
from kairos.models import OIFlowResult, PreviousDayLevels, SessionConfig, TrendPhase
from kairos.processor import consolidate_oi_flow, score_oi_flow


def _reading(
    phase: TrendPhase,
    *,
    score: int = 1,
    reason: str = "fixture",
    gex_state: str = "trend",
    nde_state: str = "confirms",
    vega_trap: bool = False,
    data_valid: bool = True,
    stale: bool = False,
    veto_reason: str | None = None,
) -> OIFlowResult:
    return OIFlowResult(
        score=score,
        phase=phase,
        reason=reason,
        gex_state=gex_state,
        nde_state=nde_state,
        vega_trap=vega_trap,
        data_valid=data_valid,
        stale=stale,
        veto_reason=veto_reason,
        pcr=1.0,
        iv_skew=0.0,
    )


def test_pcr_is_passive_for_a_valid_bullish_reading(make_cluster, make_candle):
    cluster = make_cluster(
        22000,
        ce_oi_change=10000,
        pe_oi_change=10000,
        price_change=10.0,
        net_gex=0.0,
        net_delta_exposure=500000.0,
        pcr=0.10,
    )
    candles = deque([make_candle(21990)] * 6, maxlen=15)

    condition, result = score_oi_flow(cluster, 0.1, candles)

    assert condition.status == "GREEN"
    assert result.score == 1
    assert "PCR" not in result.reason


def test_current_vega_trap_overrides_seven_historical_green_votes():
    history = deque(
        [_reading(TrendPhase.LONG_BUILDUP) for _ in range(7)]
        + [
            _reading(
                TrendPhase.LONG_BUILDUP,
                score=0,
                vega_trap=True,
            )
        ],
        maxlen=8,
    )

    condition, result = consolidate_oi_flow(history)

    assert condition.status == "RED"
    assert result.score == 0
    assert result.reason.startswith("Current Vega trap")


def test_mixed_direction_votes_do_not_form_consensus():
    history = deque(
        [_reading(TrendPhase.SHORT_BUILDUP) for _ in range(4)]
        + [_reading(TrendPhase.LONG_BUILDUP) for _ in range(4)],
        maxlen=8,
    )

    condition, result = consolidate_oi_flow(history)

    assert condition.status == "RED"
    assert result.score == 0
    assert result.effective_veto is True
    assert result.veto_reason == result.reason
    assert "4/8" in result.reason


def test_three_prior_traps_create_a_recovery_hold_after_a_clean_reading():
    history = deque(
        [
            _reading(TrendPhase.LONG_BUILDUP, score=0, vega_trap=True),
            _reading(TrendPhase.LONG_BUILDUP, score=0, vega_trap=True),
            _reading(TrendPhase.LONG_BUILDUP, score=0, vega_trap=True),
        ]
        + [_reading(TrendPhase.LONG_BUILDUP) for _ in range(5)],
        maxlen=8,
    )

    condition, result = consolidate_oi_flow(history)

    assert condition.status == "RED"
    assert result.effective_veto is True
    assert result.reason.startswith("Historical recovery hold")


def test_stale_trap_duplicates_do_not_create_a_recovery_hold():
    history = deque(
        [
            _reading(TrendPhase.LONG_BUILDUP, score=0, vega_trap=True),
            _reading(TrendPhase.LONG_BUILDUP, score=0, vega_trap=True, stale=True),
            _reading(TrendPhase.LONG_BUILDUP, score=0, vega_trap=True, stale=True),
        ]
        + [_reading(TrendPhase.LONG_BUILDUP) for _ in range(5)],
        maxlen=8,
    )

    condition, _ = consolidate_oi_flow(history)

    assert condition.status == "GREEN"


def test_current_stale_reading_reports_staleness_before_invalid_data():
    history = deque(
        [_reading(TrendPhase.LONG_BUILDUP, score=0, data_valid=False, stale=True)],
        maxlen=8,
    )

    _, result = consolidate_oi_flow(history)

    assert result.reason.startswith("Current Stale OI observation")


def test_invalid_latest_reading_precedes_derived_vetoes():
    invalid_reason = "Invalid OI data — missing gamma"
    history = deque(
        [
            _reading(
                TrendPhase.NEUTRAL,
                score=0,
                gex_state="pin",
                vega_trap=True,
                data_valid=False,
                reason=invalid_reason,
                veto_reason=invalid_reason,
            )
        ],
        maxlen=8,
    )

    _, result = consolidate_oi_flow(history)

    assert result.reason.startswith(f"Current {invalid_reason}")


def test_stale_latest_reading_precedes_derived_vetoes():
    history = deque(
        [
            _reading(
                TrendPhase.NEUTRAL,
                score=0,
                gex_state="pin",
                vega_trap=True,
                stale=True,
            )
        ],
        maxlen=8,
    )

    _, result = consolidate_oi_flow(history)

    assert result.reason.startswith("Current Stale OI observation")


def test_invalid_latest_reading_preserves_raw_reason_without_a_veto_reason():
    invalid_reason = "Invalid OI data — unusable Vega denominator"
    history = deque(
        [
            _reading(
                TrendPhase.LONG_BUILDUP,
                score=0,
                data_valid=False,
                reason=invalid_reason,
            )
        ],
        maxlen=8,
    )

    _, result = consolidate_oi_flow(history)

    assert result.reason.startswith(f"Current {invalid_reason}")


def test_invalid_trap_duplicates_do_not_create_a_recovery_hold():
    history = deque(
        [
            _reading(TrendPhase.LONG_BUILDUP, score=0, vega_trap=True),
            _reading(
                TrendPhase.LONG_BUILDUP,
                score=0,
                vega_trap=True,
                data_valid=False,
            ),
            _reading(
                TrendPhase.LONG_BUILDUP,
                score=0,
                vega_trap=True,
                data_valid=False,
            ),
        ]
        + [_reading(TrendPhase.LONG_BUILDUP) for _ in range(5)],
        maxlen=8,
    )

    condition, _ = consolidate_oi_flow(history)

    assert condition.status == "GREEN"


def test_valid_neutral_gex_can_qualify_when_nde_confirms(make_cluster, make_candle):
    cluster = make_cluster(
        22000,
        ce_oi_change=10000,
        pe_oi_change=10000,
        price_change=10.0,
        net_gex=0.0,
        net_delta_exposure=500000.0,
        pcr=0.10,
    )
    candles = deque([make_candle(21990)] * 6, maxlen=15)

    condition, _ = score_oi_flow(cluster, 0.1, candles)

    assert condition.status == "GREEN"


def test_vega_denominator_uses_the_wider_main_window(make_option_row):
    chain = []
    for strike in range(21850, 22201, 50):
        vega = 1.0 if abs(strike - 22000) <= 150 else 4.0
        chain.extend(
            [
                make_option_row(strike, "CE", vega=vega, oi=1000),
                make_option_row(strike, "PE", vega=vega, oi=1000),
            ]
        )

    aggregates = compute_greeks_aggregates(chain, 22000, 22000, 50.0, 65)

    assert aggregates["atm_vega_exposure"] < aggregates["total_abs_vega"]


def test_missing_expected_strike_pair_invalidates_full_scoring_grid(make_option_row):
    chain = []
    for strike in range(21650, 22351, 50):
        if strike == 22050:
            continue
        chain.extend(
            [
                make_option_row(strike, "CE", gamma=0.2, vega=1.0, oi=1000),
                make_option_row(strike, "PE", gamma=0.2, vega=1.0, oi=1000),
            ]
        )

    aggregates = compute_greeks_aggregates(chain, 22000, 22000, 50.0, 65)

    assert (aggregates["data_valid"], aggregates["data_invalid_reason"]) == (
        False,
        "missing CE/PE Greek side",
    )


def test_zero_weight_nonfinite_greeks_do_not_invalidate_active_window(make_option_row):
    chain = []
    for strike in range(21650, 22351, 50):
        chain.extend(
            [
                make_option_row(strike, "CE", delta=0.5, gamma=0.2, vega=1.0, oi=1000),
                make_option_row(strike, "PE", delta=-0.5, gamma=0.2, vega=1.0, oi=1000),
            ]
        )
    chain.append(make_option_row(23000, "CE", delta=float("nan"), gamma=0.2, vega=1.0, oi=1000))

    aggregates = compute_greeks_aggregates(chain, 22000, 22000, 50.0, 65)

    assert aggregates["data_valid"] is True


def test_zero_vega_denominator_is_invalid_data(make_option_row):
    chain = []
    for strike in range(21650, 22351, 50):
        chain.extend(
            [
                make_option_row(strike, "CE", gamma=0.2, delta=0.5, vega=0.0),
                make_option_row(strike, "PE", gamma=0.2, delta=-0.5, vega=0.0),
            ]
        )

    aggregates = compute_greeks_aggregates(chain, 22000, 22000, 50.0, 65)

    assert (aggregates["data_valid"], aggregates["data_invalid_reason"]) == (
        False,
        "unusable Greek exposure denominator",
    )


def test_bounded_oi_buffer_rejects_nine_identical_or_regressed_timestamps(
    make_candle, make_option_row, mock_now, mock_date
):
    option_chain = [
        make_option_row(
            strike,
            option_type,
            iv=0.2,
            gamma=0.3,
            theta=-0.5,
            vega=1.0,
            oi_change=10_000,
            ltp=150.0,
        )
        for strike in range(21650, 22351, 50)
        for option_type in ("CE", "PE")
    ]
    candle = make_candle(
        22000.0,
        low=21700.0,
        high=22300.0,
        vwap=22000.0,
    ).model_copy(update={"timestamp": mock_now})
    candle_buffer = deque([candle] * 15, maxlen=15)
    iv_buffer = deque([0.2] * 16, maxlen=20)
    previous_day = PreviousDayLevels(
        symbol="NIFTY",
        trade_date=mock_date,
        prev_day_high=23000.0,
        prev_day_low=21000.0,
        fetched_at=mock_now,
    )
    config = SessionConfig(
        symbol="NIFTY",
        expiry=mock_date,
        expiry_type="WEEKLY",
        status="ACTIVE",
    )
    oi_flow_buffer = deque(maxlen=8)
    obsolete_timestamps = [
        mock_now if index % 2 == 0 else mock_now - timedelta(minutes=1)
        for index in range(9)
    ]

    scores = []
    for observation_timestamp in [mock_now, *obsolete_timestamps]:
        candle_buffer[-1] = candle.model_copy(
            update={"timestamp": observation_timestamp}
        )
        scores.append(
            evaluate(
                option_chain,
                candle_buffer,
                iv_buffer,
                previous_day,
                22000.0,
                1,
                config,
                oi_flow_buffer=oi_flow_buffer,
            )
        )

    assert scores[0].oi_flow_result.stale is False
    assert all(score.oi_flow_result.stale for score in scores[1:])
