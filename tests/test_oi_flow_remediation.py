"""Regression coverage for the MANM-103 OI Flow remediation."""

from collections import deque

from kairos.engine import compute_greeks_aggregates
from kairos.models import OIFlowResult, TrendPhase
from kairos.processor import consolidate_oi_flow, score_oi_flow


def _reading(
    phase: TrendPhase,
    *,
    score: int = 1,
    gex_state: str = "trend",
    nde_state: str = "confirms",
    vega_trap: bool = False,
) -> OIFlowResult:
    return OIFlowResult(
        score=score,
        phase=phase,
        reason="fixture",
        gex_state=gex_state,
        nde_state=nde_state,
        vega_trap=vega_trap,
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


def test_zero_vega_denominator_is_invalid_data(make_option_row):
    chain = [
        make_option_row(22000, "CE", gamma=0.2, delta=0.5, vega=0.0),
        make_option_row(22000, "PE", gamma=0.2, delta=-0.5, vega=0.0),
    ]

    aggregates = compute_greeks_aggregates(chain, 22000, 22000, 50.0, 65)

    assert aggregates["data_valid"] is False
