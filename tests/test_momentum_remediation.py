from collections import deque
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from kairos.fetcher import DhanFetcher
from kairos.models import ConditionResult, EnvironmentScore, OHLCVCandle, PreviousDayLevels, SessionConfig
from kairos.processor import score_momentum
from kairos.scheduler import (
    candle_is_completed,
    condition_fingerprint,
    upsert_completed_candle,
)


IST = ZoneInfo("Asia/Kolkata")


def candle(timestamp, close, volume=100.0, high=None, low=None):
    return OHLCVCandle(
        timestamp=timestamp,
        symbol="NIFTY",
        open=close,
        high=high if high is not None else close + 0.1,
        low=low if low is not None else close - 0.1,
        close=close,
        volume=volume,
        vwap=close,
    )


def momentum_window(closes, *, current_volume=200.0, baseline_volume=100.0):
    """Return 16 consecutive, completed candles with the supplied latest six closes."""
    start = datetime(2026, 3, 23, 10, 0, tzinfo=IST)
    prefix = [100.0] * 10
    values = prefix + closes
    return deque(
        candle(start + timedelta(minutes=index), close,
               current_volume if index == len(values) - 1 else baseline_volume)
        for index, close in enumerate(values)
    )


@pytest.mark.parametrize(
    "closes,direction",
    [
        ([100, 101, 100.8, 102, 103, 104], "up"),
        ([104, 103, 103.2, 102, 101, 100], "down"),
    ],
)
def test_momentum_green_accepts_four_of_five_with_one_counter_move(closes, direction):
    result = score_momentum(momentum_window(closes))

    assert (result.status, result.points) == ("GREEN", 1)
    assert result.diagnostics.direction == direction
    assert result.diagnostics.dominant_count == 4
    assert result.diagnostics.denominator == 5


def test_momentum_three_of_five_is_yellow_not_red_or_green():
    result = score_momentum(momentum_window([100, 101, 102, 103, 102, 101]))

    assert (result.status, result.points) == ("YELLOW", 0)
    assert result.diagnostics.dominant_count == 3
    assert "trend" in result.diagnostics.failed_gates


def test_momentum_ties_and_flats_are_never_labelled_down():
    result = score_momentum(momentum_window([100, 101, 100, 101, 100, 100]))

    assert result.status == "RED"
    assert result.diagnostics.direction == "mixed"
    assert (result.diagnostics.up_count, result.diagnostics.down_count, result.diagnostics.flat_count) == (2, 2, 1)


@pytest.mark.parametrize("range_pct", [0.15, 0.30])
def test_exact_range_thresholds_are_yellow(range_pct):
    window = momentum_window([100, 100.01, 100.02, 100.03, 100.04, 100.05])
    for index in range(-5, 0):
        current = window[index]
        window[index] = candle(current.timestamp, current.close, current.volume, current.close, current.close)
    current_close = window[-1].close
    first_range_candle = window[-5]
    window[-5] = candle(
        first_range_candle.timestamp, first_range_candle.close, first_range_candle.volume,
        first_range_candle.close, current_close * (1 - range_pct / 100),
    )

    result = score_momentum(window)

    assert (result.status, result.points) == ("YELLOW", 0)
    assert result.diagnostics.range_pct == pytest.approx(range_pct)


@pytest.mark.parametrize("current_volume,status", [(150.0, "YELLOW"), (151.0, "GREEN")])
def test_volume_uses_preceding_fifteen_and_has_a_strict_boundary(current_volume, status):
    result = score_momentum(
        momentum_window([100, 101, 100.8, 102, 103, 104], current_volume=current_volume)
    )

    assert result.status == status
    assert result.diagnostics.baseline_count == 15
    assert result.diagnostics.baseline_average == 100.0
    assert result.diagnostics.volume_ratio == pytest.approx(current_volume / 100.0)


@pytest.mark.parametrize("volume", [None, float("nan"), -1, 0])
def test_unusable_or_all_zero_volume_is_data_unavailable(volume):
    window = momentum_window([100, 101, 100.8, 102, 103, 104])
    if volume == 0:
        window = deque(candle(c.timestamp, c.close, 0, c.high, c.low) for c in window)
    else:
        window[-1] = candle(window[-1].timestamp, window[-1].close, volume)

    result = score_momentum(window)

    assert (result.status, result.points) == ("YELLOW", 0)
    assert result.diagnostics.readiness == "data_unavailable"
    assert "volume" in result.diagnostics.failed_gates


def test_sparse_or_stale_candles_are_data_unavailable_before_scoring():
    window = momentum_window([100, 101, 100.8, 102, 103, 104])
    window[8] = candle(window[8].timestamp + timedelta(minutes=1), window[8].close)

    result = score_momentum(window)

    assert (result.status, result.points) == ("YELLOW", 0)
    assert result.diagnostics.readiness == "data_unavailable"
    assert "gap" in result.diagnostics.failed_gates


def test_stale_candle_window_is_data_unavailable_and_identified():
    window = momentum_window([100, 101, 100.8, 102, 103, 104])
    now = window[-1].timestamp + timedelta(minutes=3)

    result = score_momentum(window, now=now)

    assert (result.status, result.points) == ("YELLOW", 0)
    assert result.diagnostics.readiness == "data_unavailable"
    assert result.diagnostics.failed_gates == ["stale"]
    assert result.detail.startswith(f"[{window[-1].timestamp:%Y-%m-%d %H:%M}]")


def test_momentum_uses_configured_session_boundaries(monkeypatch):
    from kairos.processor import settings as processor_settings

    monkeypatch.setattr(processor_settings, "session_1_start", (8, 30))
    monkeypatch.setattr(processor_settings, "session_1_end", (9, 0))
    window = deque(
        candle(datetime(2026, 3, 23, 8, 40, tzinfo=IST) + timedelta(minutes=index), 100 + index)
        for index in range(16)
    )

    result = score_momentum(window)

    assert "session" not in result.diagnostics.failed_gates


@pytest.mark.parametrize("mode", ["history", "session", "gap", "invalid_ohlcv", "zero_baseline"])
def test_readiness_failures_include_the_evaluated_timestamp(mode):
    window = momentum_window([100, 101, 100.8, 102, 103, 104])
    if mode == "history":
        window = deque(list(window)[-5:])
    elif mode == "session":
        window[-1] = candle(datetime(2026, 3, 23, 12, 0, tzinfo=IST), window[-1].close)
    elif mode == "gap":
        window[-2] = candle(window[-2].timestamp - timedelta(minutes=1), window[-2].close)
    elif mode == "invalid_ohlcv":
        window[-1] = candle(window[-1].timestamp, window[-1].close, float("nan"))
    else:
        window = deque(candle(item.timestamp, item.close, 0, item.high, item.low) for item in window)

    result = score_momentum(window)

    assert result.diagnostics.readiness == "data_unavailable"
    assert result.detail.startswith(f"[{window[-1].timestamp:%Y-%m-%d %H:%M}]")


def test_completed_candle_upsert_revisions_and_orders_without_duplicates():
    now = datetime(2026, 3, 23, 10, 5, tzinfo=IST)
    buffer = deque(maxlen=20)
    first = candle(now - timedelta(minutes=3), 100)
    revised = candle(now - timedelta(minutes=3), 101)
    older = candle(now - timedelta(minutes=4), 99)

    assert upsert_completed_candle(buffer, first, now=now)
    assert upsert_completed_candle(buffer, older, now=now)
    assert upsert_completed_candle(buffer, revised, now=now)
    assert [item.close for item in buffer] == [99, 101]
    assert not upsert_completed_candle(buffer, candle(now - timedelta(seconds=30), 102), now=now)
    assert not candle_is_completed(now, now=now)


def test_fingerprint_tracks_meaningful_momentum_diagnostics_but_not_detail_jitter():
    base = ConditionResult(
        name="momentum", status="GREEN", points=1, max_points=1, detail="0.31% range"
    )
    jitter = base.model_copy(update={"detail": "0.32% range"})
    changed = base.model_copy(update={"status": "RED", "points": 0})

    assert condition_fingerprint([base]) == condition_fingerprint([jitter])
    assert condition_fingerprint([base]) != condition_fingerprint([changed])


@pytest.mark.asyncio
async def test_fetcher_selects_the_latest_completed_candle_and_retains_history():
    now = datetime(2026, 3, 23, 10, 5, tzinfo=IST)
    fetcher = DhanFetcher()
    timestamps = [int((now - timedelta(minutes=2)).timestamp()), int((now - timedelta(minutes=1)).timestamp()), int(now.timestamp())]
    fetcher._post = AsyncMock(return_value={
        "timestamp": timestamps,
        "open": [100, 101, 102], "high": [101, 102, 103],
        "low": [99, 100, 101], "close": [100, 101, 102], "volume": [10, 20, 30],
    })

    latest = await fetcher.get_latest_candle("NIFTY", now=now)

    assert latest.timestamp == now - timedelta(minutes=1)
    assert [item.timestamp for item in fetcher.last_completed_candles] == [
        now - timedelta(minutes=2), now - timedelta(minutes=1)
    ]


@pytest.mark.asyncio
async def test_scheduler_retries_failed_low_score_recovery_and_alerts_deterioration(monkeypatch):
    import kairos.scheduler as scheduler

    now = datetime.now(IST).replace(second=0, microsecond=0)
    latest = candle(now - timedelta(minutes=1), 100)
    config = SessionConfig(symbol="NIFTY", expiry=date.today(), expiry_type="WEEKLY", status="ACTIVE")
    scheduler.state.reset_buffers()
    scheduler.state.startup_done = scheduler.state.in_session = scheduler.state.warmup_complete = True
    scheduler.state.active_config = config
    scheduler.state.prev_levels = PreviousDayLevels(symbol="NIFTY", trade_date=date.today(), prev_day_high=101, prev_day_low=99, fetched_at=now)
    monkeypatch.setattr(scheduler, "is_active_session", lambda: True)
    monkeypatch.setattr(scheduler.fetcher, "get_option_chain", AsyncMock(return_value=[]))
    monkeypatch.setattr(scheduler.fetcher, "get_latest_candle", AsyncMock(return_value=latest))
    monkeypatch.setattr(scheduler.db, "get_active_session", AsyncMock(return_value=config))
    monkeypatch.setattr(scheduler.db, "write_environment_log", AsyncMock())
    monkeypatch.setattr(scheduler.notifier, "post_environment_alert", AsyncMock(side_effect=[True, False, True, True]))

    def score(status, points):
        return EnvironmentScore(
            timestamp=now, symbol="NIFTY", expiry=date.today(), dte=0, score=points,
            status="CAUTION", summary_raw="raw", conditions=[
                ConditionResult(name="momentum", status=status, points=1 if status == "GREEN" else 0,
                                max_points=1, detail=status)
            ],
        )

    monkeypatch.setattr(scheduler, "evaluate", MagicMock(side_effect=[
        score("RED", 4), score("GREEN", 5), score("GREEN", 5), score("RED", 4),
    ]))
    for _ in range(4):
        await scheduler.run_cycle()

    assert scheduler.notifier.post_environment_alert.await_count == 4
    assert scheduler.state.last_successfully_notified_fingerprint == condition_fingerprint(score("RED", 4).conditions)
