"""
test_qa_coverage_round2.py — Coverage for 19 lines flagged by QA Agent (Round 2).

Targets:
  config.py:166,168,170,172,174   — validate_momentum_settings ValueErrors
  fetcher.py:295,308-309,311,331  — intraday candle array validation paths
  notifier.py:117                 — httpx.RequestError in _post webhook
  processor.py:141-143,146-148   — afternoon session candles; session-boundary cross
  scheduler.py:503-504            — upsert_completed_candle rejection in run_cycle
"""

import sys
from collections import deque
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

# ── Bypass APScheduler v4 Alpha imports (same guard as test_scheduler.py) ──
sys.modules.setdefault("apscheduler", MagicMock())
sys.modules.setdefault("apscheduler.schedulers", MagicMock())
sys.modules.setdefault("apscheduler.schedulers.asyncio", MagicMock())
sys.modules.setdefault("apscheduler.triggers.interval", MagicMock())

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────────────────
# 1. config.py:166,168,170,172,174  — validate_momentum_settings ValueErrors
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateMomentumSettings:
    """
    Pydantic model_validator re-runs only on fresh construction, NOT model_copy.
    We construct Settings directly with required env fields + the override.
    """

    _REQUIRED = dict(
        supabase_url="http://localhost",
        supabase_key="test",
        discord_webhook_url="http://localhost",
        discord_health_webhook_url="http://localhost",
    )

    def _make(self, **overrides):
        from kairos.config import Settings
        return Settings(**{**self._REQUIRED, **overrides})

    def test_wrong_candle_window_raises(self):
        """config.py:166 — momentum_candle_window != 5 → ValueError."""
        with pytest.raises(ValueError, match="momentum_candle_window must be 5"):
            self._make(momentum_candle_window=4)

    def test_non_positive_volume_lookback_raises(self):
        """config.py:168 — momentum_volume_lookback <= 0 → ValueError."""
        with pytest.raises(ValueError, match="momentum_volume_lookback must be positive"):
            self._make(momentum_volume_lookback=0)

    def test_inverted_range_thresholds_raises(self):
        """config.py:170 — range_yellow >= range_green → ValueError."""
        with pytest.raises(ValueError, match="momentum range thresholds must be ordered"):
            self._make(momentum_range_yellow=0.40, momentum_range_green=0.30)

    def test_trend_count_thresholds_out_of_order_raises(self):
        """config.py:172 — yellow >= green → ValueError."""
        with pytest.raises(ValueError, match="momentum trend thresholds must be within five deltas"):
            self._make(momentum_trend_count_yellow=3, momentum_trend_count_green=3)

    def test_non_finite_volume_multiplier_raises(self):
        """config.py:174 — volume_multiplier == inf → ValueError."""
        with pytest.raises(ValueError, match="momentum_volume_multiplier must be positive and finite"):
            self._make(momentum_volume_multiplier=float("inf"))

    def test_negative_volume_multiplier_raises(self):
        """config.py:174 — volume_multiplier <= 0 → ValueError."""
        with pytest.raises(ValueError, match="momentum_volume_multiplier must be positive and finite"):
            self._make(momentum_volume_multiplier=-1.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. fetcher.py:295,308-309,311,331  — intraday candle paths
# ─────────────────────────────────────────────────────────────────────────────


def _build_fetcher(raw_response: dict):
    """Return a DhanFetcher with _post mocked to return raw_response."""
    from kairos.fetcher import DhanFetcher
    fetcher = DhanFetcher()
    fetcher._post = AsyncMock(return_value=raw_response)
    fetcher._client = MagicMock()  # bypass the _client=None guard
    return fetcher


@pytest.mark.asyncio
async def test_mismatched_array_lengths_raises_api_error():
    """fetcher.py:295 — arrays of different lengths → DhanAPIError."""
    from kairos.fetcher import DhanAPIError
    now = datetime(2026, 3, 23, 10, 5, tzinfo=IST)
    fetcher = _build_fetcher({
        "timestamp": [int((now - timedelta(minutes=2)).timestamp()),
                      int((now - timedelta(minutes=1)).timestamp())],
        "open": [100, 101],
        "high": [101, 102],
        "low": [99, 100],
        "close": [100],          # only 1 element — mismatch
        "volume": [10, 20],
    })
    with pytest.raises(DhanAPIError, match="Malformed intraday candle arrays"):
        await fetcher.get_latest_candle("NIFTY", now=now)


@pytest.mark.asyncio
async def test_bad_row_type_is_skipped_and_valid_row_returned():
    """fetcher.py:308-309 — TypeError/ValueError in row parsing → continue."""
    now = datetime(2026, 3, 23, 10, 5, tzinfo=IST)
    fetcher = _build_fetcher({
        "timestamp": [
            int((now - timedelta(minutes=2)).timestamp()),
            int((now - timedelta(minutes=1)).timestamp()),
        ],
        "open": ["bad", 101],      # first row: non-numeric → skipped
        "high": [101, 102],
        "low": [99, 100],
        "close": [100, 101],
        "volume": [10, 20],
    })
    candle = await fetcher.get_latest_candle("NIFTY", now=now)
    assert candle.close == 101.0


@pytest.mark.asyncio
async def test_no_completed_valid_candle_raises_api_error():
    """fetcher.py:311 — all rows are forming (ts + 1min > now) → DhanAPIError."""
    from kairos.fetcher import DhanAPIError
    now = datetime(2026, 3, 23, 10, 5, tzinfo=IST)
    # Timestamp == now means ts + 1min > now → not completed
    fetcher = _build_fetcher({
        "timestamp": [int(now.timestamp())],
        "open": [100],
        "high": [101],
        "low": [99],
        "close": [100],
        "volume": [10],
    })
    with pytest.raises(DhanAPIError, match="No completed valid one-minute candle"):
        await fetcher.get_latest_candle("NIFTY", now=now)


@pytest.mark.asyncio
async def test_zero_volume_falls_back_to_twap():
    """fetcher.py:331 — all volumes == 0 → equal-weighted TWAP fallback."""
    now = datetime(2026, 3, 23, 10, 5, tzinfo=IST)
    ts1 = int((now - timedelta(minutes=2)).timestamp())
    ts2 = int((now - timedelta(minutes=1)).timestamp())
    fetcher = _build_fetcher({
        "timestamp": [ts1, ts2],
        "open":   [100, 102],
        "high":   [101, 103],
        "low":    [99,  101],
        "close":  [100, 102],
        "volume": [0,   0],        # cum_vol == 0 → TWAP branch
    })
    candle = await fetcher.get_latest_candle("NIFTY", now=now)
    # TWAP = mean((h+l+c)/3) across both completed candles
    expected_twap = ((101 + 99 + 100) / 3 + (103 + 101 + 102) / 3) / 2
    assert candle.vwap == round(expected_twap, 2)


# ─────────────────────────────────────────────────────────────────────────────
# 3. notifier.py:117 — httpx.RequestError in _post webhook
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notifier_post_request_error_returns_false():
    """notifier.py:117 — httpx.RequestError → _post returns False, does not raise."""
    from kairos.notifier import Notifier
    notifier = Notifier()
    await notifier.start()
    # Patch the httpx client to raise RequestError on POST
    notifier._client.post = AsyncMock(
        side_effect=httpx.RequestError("connection refused", request=MagicMock())
    )
    result = await notifier._post(
        "http://valid-looking-url.example.com/webhook",
        {"content": "test"},
    )
    assert result is False
    await notifier.stop()


# ─────────────────────────────────────────────────────────────────────────────
# 4. processor.py:141-143,146-148
#    — afternoon session candles; session-boundary crossing
# ─────────────────────────────────────────────────────────────────────────────


def _candle(timestamp: datetime, close: float = 100.0, volume: float = 200.0):
    from kairos.models import OHLCVCandle
    return OHLCVCandle(
        timestamp=timestamp,
        symbol="NIFTY",
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=volume,
        vwap=close,
    )


def test_afternoon_session_candles_pass_session_gate():
    """
    processor.py:141-143 — candles fully inside afternoon (13:00–15:25) get
    session label 'afternoon'; the session gate passes (set size == 1).
    """
    from kairos.processor import score_momentum

    start = datetime(2026, 3, 23, 13, 0, tzinfo=IST)
    buf = deque(
        _candle(start + timedelta(minutes=i), 100.0 + i * 0.1)
        for i in range(16)
    )
    result = score_momentum(buf)
    assert "session" not in (result.diagnostics.failed_gates or [])


def test_afternoon_session_score_momentum_does_not_return_session_error():
    """
    processor.py:141-143 — afternoon candles should not trigger the
    data_unavailable / session-boundary path (146-148).
    """
    from kairos.processor import score_momentum

    start = datetime(2026, 3, 23, 14, 0, tzinfo=IST)
    # 16 candles anchored in the middle of afternoon session
    buf = deque(
        _candle(start + timedelta(minutes=i), 100.0)
        for i in range(16)
    )
    result = score_momentum(buf)
    # session gate must not trigger
    assert "session" not in (result.diagnostics.failed_gates or [])


def test_session_boundary_crossing_is_data_unavailable():
    """
    processor.py:146-148 — the six-candle window spans morning AND afternoon →
    data_unavailable with 'session' in failed_gates.
    """
    from kairos.processor import score_momentum

    # Build 8 morning candles then 8 afternoon candles (non-consecutive times
    # so the window's last 5 will straddle both sessions)
    morning_start = datetime(2026, 3, 23, 11, 41, tzinfo=IST)   # inside morning session
    afternoon_start = datetime(2026, 3, 23, 13, 0, tzinfo=IST)  # inside afternoon session

    morning = [_candle(morning_start + timedelta(minutes=i), 100.0) for i in range(8)]
    afternoon = [_candle(afternoon_start + timedelta(minutes=i), 100.0) for i in range(8)]
    buf = deque(morning + afternoon)

    result = score_momentum(buf)

    assert result.diagnostics.readiness == "data_unavailable"
    assert "session" in result.diagnostics.failed_gates


# ─────────────────────────────────────────────────────────────────────────────
# 5. scheduler.py:503-504 — run_cycle discards invalid candle via upsert gate
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_deps_sched(mocker):
    mocker.patch("kairos.scheduler.db", new_callable=AsyncMock)
    mocker.patch("kairos.scheduler.fetcher", new_callable=AsyncMock)
    mocker.patch("kairos.scheduler.notifier", new_callable=AsyncMock)
    mocker.patch("kairos.scheduler.evaluate")
    mocker.patch(
        "kairos.scheduler.load_dhan_credentials_from_supabase", new_callable=AsyncMock
    )


@pytest.mark.asyncio
async def test_run_cycle_discards_invalid_candle_before_scoring(mock_deps_sched, mocker):
    """
    scheduler.py:503-504 — when upsert_completed_candle returns False for the
    incoming OHLCVCandle, run_cycle logs a warning and returns early without
    calling evaluate().
    """
    import kairos.scheduler as sched
    from kairos.models import OHLCVCandle, PreviousDayLevels, SessionConfig

    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.active_config = SessionConfig(
        symbol="NIFTY",
        expiry=date.today(),
        expiry_type="WEEKLY",
        status="ACTIVE",
    )
    sched.state.prev_levels = PreviousDayLevels(
        symbol="NIFTY",
        trade_date=date.today(),
        prev_day_high=22100.0,
        prev_day_low=21900.0,
        fetched_at=datetime.now(IST),
    )

    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    sched.db.get_active_session.return_value = sched.state.active_config
    sched.fetcher.get_option_chain.return_value = []

    # Candle timestamped 1 minute ago → passes the staleness guard (< 2 min)
    fresh_ts = datetime.now(IST).replace(second=0, microsecond=0) - timedelta(minutes=1)

    # Geometry-invalid candle: high < close → candle_is_completed() returns False
    # → upsert_completed_candle rejects it → scheduler.py:503-504 is reached
    bad_candle = OHLCVCandle(
        timestamp=fresh_ts,
        symbol="NIFTY",
        open=22000.0,
        high=21990.0,      # high < open/close → invalid geometry
        low=21990.0,
        close=22000.0,
        volume=1000.0,
        vwap=22000.0,
    )
    sched.fetcher.get_latest_candle.return_value = bad_candle
    # Ensure scheduler takes the OHLCVCandle branch (not the test-double path)
    sched.fetcher.last_completed_candles = [bad_candle]

    await sched.run_cycle()

    # evaluate must NOT have been called — the cycle returned at the upsert gate
    sched.evaluate.assert_not_called()

# ─────────────────────────────────────────────────────────────────────────────
# 6. processor.py:195-196 (PR Reviewer P2)
#    — evaluated candle timestamp is included in momentum detail string
# ─────────────────────────────────────────────────────────────────────────────


def test_momentum_detail_includes_evaluated_candle_timestamp():
    """
    processor.py:195-196 — the formatted candle timestamp (YYYY-MM-DD HH:MM)
    must appear at the start of the detail string for every non-data-unavailable
    result so it surfaces in Discord alerts and the environment log.
    """
    from kairos.processor import score_momentum

    # Anchor candles to a specific time so we can assert the exact string prefix
    anchor = datetime(2026, 3, 23, 10, 30, tzinfo=IST)
    prefix_closes = [100.0] * 10
    signal_closes = [101.0, 102.0, 101.8, 103.0, 104.0, 105.0]
    all_closes = prefix_closes + signal_closes

    buf = deque(
        _candle(anchor + timedelta(minutes=i), c, volume=200.0)
        for i, c in enumerate(all_closes)
    )
    # Give the last candle a volume spike so we hit GREEN
    last = buf[-1]
    from kairos.models import OHLCVCandle
    buf[-1] = OHLCVCandle(
        timestamp=last.timestamp,
        symbol="NIFTY",
        open=last.close,
        high=last.close + 0.5,
        low=last.close - 0.5,
        close=last.close,
        volume=500.0,   # well above 1.5× baseline of 200
        vwap=last.close,
    )

    expected_ts_prefix = last.timestamp.strftime("%Y-%m-%d %H:%M")
    result = score_momentum(buf)

    # Detail must start with the formatted timestamp regardless of gate outcome
    assert result.detail.startswith(f"[{expected_ts_prefix}]"), (
        f"Expected detail to start with '[{expected_ts_prefix}]', got: {result.detail!r}"
    )
