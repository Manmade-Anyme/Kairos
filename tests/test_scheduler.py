import pytest
import sys
from unittest.mock import MagicMock

# --- Bypass APScheduler v4 Alpha imports ---
sys.modules['apscheduler'] = MagicMock()
sys.modules['apscheduler.schedulers'] = MagicMock()
sys.modules['apscheduler.schedulers.asyncio'] = MagicMock()
sys.modules['apscheduler.triggers.interval'] = MagicMock()

from datetime import date, datetime, timedelta
from collections import deque
from zoneinfo import ZoneInfo

from kairos.scheduler import run_cycle, run_heartbeat, state, is_active_session, run_startup_checks
from kairos.models import SessionConfig, OHLCVCandle, PreviousDayLevels, EnvironmentScore, OptionChainRow

@pytest.fixture
def mock_dependencies(mocker):
    # Mock all global service singletons accessed inside scheduler
    mocker.patch("kairos.scheduler.db")
    mocker.patch("kairos.scheduler.fetcher")
    mocker.patch("kairos.scheduler.notifier")
    # Patch session time check so tests do not fail organically based on clock time
    mocker.patch("kairos.scheduler.is_active_session", return_value=True)

@pytest.fixture
def dummy_session():
    return SessionConfig(
        symbol="NIFTY",
        expiry=date.today(),
        expiry_type="WEEKLY",
        status="ACTIVE"
    )

@pytest.fixture
def dummy_candle():
    return OHLCVCandle(
        timestamp=datetime.now(ZoneInfo("Asia/Kolkata")).replace(second=0, microsecond=0) - timedelta(minutes=1),
        symbol="NIFTY",
        interval=1,
        open=22000.0,
        high=22010.0,
        low=21990.0,
        close=22005.0,
        volume=1000,
        vwap=22000.0,
    )

@pytest.fixture
def dummy_levels():
    return PreviousDayLevels(
        symbol="NIFTY",
        trade_date=date(2026, 3, 25),
        prev_day_high=22100.0,
        prev_day_low=21900.0,
        fetched_at=datetime.now()
    )

@pytest.fixture
def dummy_score():
    return EnvironmentScore(
        timestamp=datetime.now(),
        symbol="NIFTY",
        expiry=date(2026, 3, 26),
        dte=1,
        score=8,
        max_score=8,
        status="GO",
        iv_capped=False,
        conditions=[],
        summary_raw="Raw",
        summary="Summary"
    )

from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_run_startup_checks(mock_dependencies, dummy_session, dummy_levels, mocker):
    import kairos.scheduler as sched
    
    # Setup happy path mock bounds
    sched.fetcher.test_connectivity = AsyncMock(return_value=True)
    sched.db.test_connectivity = AsyncMock(return_value=True)
    sched.fetcher.get_previous_day_levels = AsyncMock(return_value=dummy_levels)
    sched.fetcher.get_available_expiries = AsyncMock(return_value=[])
    sched.db.write_previous_day_levels = AsyncMock()
    sched.db.write_available_expiries = AsyncMock()
    sched.notifier.post_startup = AsyncMock()
    sched.notifier.post_critical_alert = AsyncMock()
    
    result = await sched.run_startup_checks(dummy_session)
    assert result is True
    assert sched.state.dhan_ok is True
    assert sched.state.supabase_ok is True

@pytest.mark.asyncio
async def test_run_cycle(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    import kairos.scheduler as sched
    sched.state.reset_buffers()
    sched.state.startup_done = True  # manually skip startup in basic cycle
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()

    sched.notifier.post_warmup_complete = AsyncMock()
    
    # Need to pretend we are in session regardless of actual time when test runs
    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    mocker.patch("kairos.scheduler.is_lunch_break", return_value=False)

    # Mock evaluate to avoid deep logic requirements here
    mocker.patch("kairos.scheduler.evaluate", return_value=dummy_score)
    
    await sched.run_cycle()
    
    # Verify DB writing was invoked mapping a healthy execution frame
    sched.db.write_environment_log.assert_called_once()
    # Verify first cycle alert was sent (ADR-008)
    sched.notifier.post_environment_alert.assert_called_once()
    assert getattr(sched.state, "cycle_count") == 1


@pytest.mark.asyncio
async def test_run_cycle_scores_and_logs_a_stale_candle(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    import kairos.scheduler as sched

    sched.state.reset_buffers()
    sched.state.startup_done = sched.state.in_session = sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    stale_candle = dummy_candle.model_copy(
        update={"timestamp": datetime.now(ZoneInfo("Asia/Kolkata")).replace(second=0, microsecond=0) - timedelta(minutes=3)}
    )
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=stale_candle)
    sched.fetcher.last_completed_candles = [stale_candle]
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    mocker.patch("kairos.scheduler.evaluate", return_value=dummy_score)

    await sched.run_cycle()

    sched.evaluate.assert_called_once()
    assert sched.evaluate.call_args.kwargs["now"].tzinfo == ZoneInfo("Asia/Kolkata")
    sched.db.write_environment_log.assert_called_once_with(dummy_score)

@pytest.mark.asyncio
async def test_run_cycle_evaluates_invalid_completed_candle(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    import kairos.scheduler as sched
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    sched.state.reset_buffers()
    sched.state.startup_done = sched.state.in_session = sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    # Completed but invalid (close=0)
    invalid_candle = dummy_candle.model_copy(
        update={"timestamp": (now - timedelta(minutes=1)).replace(second=0, microsecond=0), "close": 0}
    )
    
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=invalid_candle)
    sched.fetcher.last_completed_candles = [invalid_candle]
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    
    dummy_score.score = 5
    dummy_score.status = "CAUTION"
    evaluate_mock = mocker.patch("kairos.scheduler.evaluate", return_value=dummy_score)

    await sched.run_cycle()

    # It MUST NOT be in the persistent state buffer
    assert len(sched.state.candle_buffer) == 0
    
    # But it MUST be passed to evaluate via the temporary eval_candle_buffer
    evaluate_mock.assert_called_once()
    passed_buffer = evaluate_mock.call_args.kwargs["candle_buffer"]
    assert len(passed_buffer) == 1
    assert passed_buffer[0].close == 0

@pytest.mark.asyncio
async def test_run_heartbeat(mock_dependencies, dummy_session):
    import kairos.scheduler as sched
    sched.state.active_config = dummy_session
    sched.state.in_session = True
    
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.notifier.post_heartbeat = AsyncMock()
    sched.notifier.post_stale_signal_warning = AsyncMock()
    
    await sched.run_heartbeat()
    # Heartbeat is now suppressed per ADR-006 to reduce noise
    sched.notifier.post_heartbeat.assert_not_called()



@pytest.mark.asyncio
async def test_run_heartbeat_suppressed_when_stopped(mock_dependencies, dummy_session):
    import kairos.scheduler as sched
    from kairos.models import SessionConfig
    import datetime

    stopped_session = SessionConfig(
        symbol="NIFTY",
        expiry=datetime.date(2026, 3, 26),
        expiry_type="WEEKLY",
        status="STOPPED"
    )

    sched.db.get_active_session = AsyncMock(return_value=stopped_session)
    sched.notifier.post_heartbeat = AsyncMock()

    await sched.run_heartbeat()
    sched.notifier.post_heartbeat.assert_not_called()


@pytest.mark.asyncio
async def test_run_heartbeat_suppressed_when_no_session(mock_dependencies):
    import kairos.scheduler as sched

    sched.db.get_active_session = AsyncMock(return_value=None)
    sched.notifier.post_heartbeat = AsyncMock()

    await sched.run_heartbeat()
    sched.notifier.post_heartbeat.assert_not_called()

def test_session_helpers():
    from kairos.scheduler import is_active_session, get_session_name
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ts = datetime.now(ZoneInfo("Asia/Kolkata"))
    is_active_session()
    get_session_name()

@pytest.mark.asyncio
async def test_run_startup_checks_failure(mock_dependencies, dummy_session, mocker):
    import kairos.scheduler as sched
    sched.fetcher.test_connectivity = AsyncMock(return_value=False)
    sched.db.test_connectivity = AsyncMock(return_value=False)
    sched.fetcher.get_previous_day_levels = AsyncMock(return_value=None)
    sched.notifier.post_critical_alert = AsyncMock()
    sched.notifier.post_startup = AsyncMock()
    
    result = await sched.run_startup_checks(dummy_session)
    assert result is False

@pytest.mark.asyncio
async def test_run_cycle_auth_error(mock_dependencies, dummy_session, mocker):
    import kairos.scheduler as sched
    from kairos.fetcher import DhanAuthError
    
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.active_config = dummy_session
    
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_latest_candle = AsyncMock()
    sched.db.write_environment_log = AsyncMock()
    sched.fetcher.get_option_chain = AsyncMock(side_effect=DhanAuthError("auth error"))
    sched.notifier.post_api_warning = AsyncMock()
    sched.notifier.post_critical_alert = AsyncMock()
    
    await sched.run_cycle()
    sched.notifier.post_critical_alert.assert_called()

@pytest.mark.asyncio
async def test_run_cycle_api_error(mock_dependencies, dummy_session, mocker):
    import kairos.scheduler as sched
    from kairos.fetcher import DhanAPIError
    
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.active_config = dummy_session
    
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_latest_candle = AsyncMock()
    sched.db.write_environment_log = AsyncMock()
    sched.fetcher.get_option_chain = AsyncMock(side_effect=DhanAPIError("api err"))
    sched.notifier.post_api_warning = AsyncMock()
    sched.notifier.post_critical_alert = AsyncMock()
    
    await sched.run_cycle()
    assert sched.state.consecutive_failures == 1

@pytest.mark.asyncio
async def test_run_cycle_alert_on_caution(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Transition to CAUTION should trigger Discord alert per ADR-015."""
    import kairos.scheduler as sched
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    sched.state.previous_status = "AVOID"  # Start at AVOID
    
    # Change to CAUTION (previously suppressed, now alerts)
    caution_score = dummy_score.model_copy()
    caution_score.status = "CAUTION"
    caution_score.previous_status = "AVOID"

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    
    mocker.patch("kairos.scheduler.evaluate", return_value=caution_score)
    
    await sched.run_cycle()
    
    # Verify alert WAS called for CAUTION per ADR-015
    sched.notifier.post_environment_alert.assert_called_once()
    assert sched.state.previous_status == "CAUTION"

@pytest.mark.asyncio
async def test_run_cycle_signal_start(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Entering GO (Signal Start) should trigger Discord alert."""
    import kairos.scheduler as sched
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    sched.state.previous_status = "AVOID"  # Start at AVOID
    
    go_score = dummy_score.model_copy()
    go_score.status = "GO"
    go_score.previous_status = "AVOID"
    
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    
    mocker.patch("kairos.scheduler.evaluate", return_value=go_score)
    
    await sched.run_cycle()
    
    # SHOULD be called for GO
    sched.notifier.post_environment_alert.assert_called_once()
    assert sched.state.previous_status == "GO"

@pytest.mark.asyncio
async def test_run_cycle_signal_end(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Leaving GO (Signal End) should trigger Discord alert."""
    import kairos.scheduler as sched
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    sched.state.previous_status = "GO"  # Start at GO
    
    avoid_score = dummy_score.model_copy()
    avoid_score.status = "AVOID"
    avoid_score.previous_status = "GO"
    
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    
    mocker.patch("kairos.scheduler.evaluate", return_value=avoid_score)
    
    await sched.run_cycle()
    
    # SHOULD be called when leaving GO
    sched.notifier.post_environment_alert.assert_called_once()
    assert sched.state.previous_status == "AVOID"

@pytest.mark.asyncio
async def test_run_cycle_first_alert_and_subsequent_suppression(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Verify first cycle alerts and subsequent suppression ONLY if absolutely nothing changes."""
    import kairos.scheduler as sched
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    
    # 1. First cycle (AVOID) -> Should alert
    first_score = dummy_score.model_copy()
    first_score.status = "AVOID"
    first_score.previous_status = None
    first_score.conditions = []

    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    mocker.patch("kairos.scheduler.is_lunch_break", return_value=False)

    # Use a side effect to update previous_status correctly for subsequent calls
    def evaluate_mock(**kwargs):
        res = first_score.model_copy()
        res.previous_status = kwargs.get("previous_status")
        return res

    mocker.patch("kairos.scheduler.evaluate", side_effect=evaluate_mock)
    
    await sched.run_cycle()
    sched.notifier.post_environment_alert.assert_called_once()
    sched.notifier.post_environment_alert.reset_mock()
    
    # 2. Second cycle (Same status, same conditions) -> Should NOT alert
    await sched.run_cycle()
    sched.notifier.post_environment_alert.assert_not_called()

@pytest.mark.asyncio
async def test_run_cycle_suppress_digit_jitter(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Verify that a small digit change in detail (jitter) does NOT trigger an alert per ADR-017."""
    import kairos.scheduler as sched
    from kairos.models import ConditionResult
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    
    # Initial cycle
    c1 = ConditionResult(name="mom", status="GREEN", points=1, max_points=1, detail="0.35% range")
    score1 = dummy_score.model_copy()
    score1.status = "GO"
    score1.previous_status = "GO"
    score1.conditions = [c1]

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    
    mocker.patch("kairos.scheduler.evaluate", return_value=score1)
    
    # Cycle 1: Proof of life alert
    await sched.run_cycle()
    sched.notifier.post_environment_alert.assert_called_once()
    sched.notifier.post_environment_alert.reset_mock()
    
    # Cycle 2: Detail changes (0.35% -> 0.36%) but status and name same.
    # ADR-017 should suppress this jitter.
    c2 = ConditionResult(name="mom", status="GREEN", points=1, max_points=1, detail="0.36% range")
    score2 = dummy_score.model_copy()
    score2.status = "GO"
    score2.previous_status = "GO"
    score2.conditions = [c2]
    
    mocker.patch("kairos.scheduler.evaluate", return_value=score2)
    
    await sched.run_cycle()
    
    # Verify alert was NOT called for digit jitter
    sched.notifier.post_environment_alert.assert_not_called()

@pytest.mark.asyncio
async def test_run_cycle_alert_on_oi_phase_change(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Verify that a change in OI Trend Phase triggers an alert per ADR-017."""
    import kairos.scheduler as sched
    from kairos.models import ConditionResult
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    
    # Initial cycle: Neutral phase
    c1 = ConditionResult(name="oi_flow", status="RED", points=0, max_points=1, detail="Neutral | CE Δ+1000 | PE Δ+1000")
    score1 = dummy_score.model_copy()
    score1.status = "CAUTION"
    score1.previous_status = "CAUTION"
    score1.conditions = [c1]

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    
    mocker.patch("kairos.scheduler.evaluate", return_value=score1)
    
    # Cycle 1
    await sched.run_cycle()
    sched.notifier.post_environment_alert.assert_called_once()
    sched.notifier.post_environment_alert.reset_mock()
    
    # Cycle 2: Phase changes (Neutral -> Short Buildup)
    c2 = ConditionResult(name="oi_flow", status="GREEN", points=1, max_points=1, detail="Bearish — Short Buildup | CE Δ+20000 | PE Δ+1000")
    score2 = dummy_score.model_copy()
    score2.status = "CAUTION"
    score2.previous_status = "CAUTION"
    score2.conditions = [c2]
    
    mocker.patch("kairos.scheduler.evaluate", return_value=score2)
    
    await sched.run_cycle()
    
    # Verify alert WAS called for Phase change
    sched.notifier.post_environment_alert.assert_called_once()


@pytest.mark.asyncio
async def test_run_cycle_session_transition_resets_state(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Verify that state is reset when entering a new session window."""
    import kairos.scheduler as sched
    from unittest.mock import AsyncMock
    
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = False  # Start OUTSIDE session
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    sched.state.previous_status = "AVOID"  # Remnant from morning session
    
    # Mock dependencies for run_cycle
    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_api_warning = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    sched.notifier.post_session_boundary = AsyncMock()
    sched.notifier.post_warmup_complete = AsyncMock()

    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    mocker.patch("kairos.scheduler.is_lunch_break", return_value=False)
    mocker.patch("kairos.scheduler.state.check_warmup_complete", return_value=True)

    def evaluate_side_effect(**kwargs):
        res = dummy_score.model_copy()
        res.status = "AVOID"
        res.previous_status = kwargs.get("previous_status")
        return res

    mocker.patch("kairos.scheduler.evaluate", side_effect=evaluate_side_effect)
    
    await sched.run_cycle()
    
    # After fix, this should be True because reset_buffers sets previous_status=None,
    # causing is_first_cycle to be True.
    # Currently (before fix), it will be False because previous_status is still "AVOID".
    sched.notifier.post_environment_alert.assert_called_once()


@pytest.mark.asyncio
async def test_iv_cap_hysteresis_holds(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Verify that the IV cap is held if recovery is below threshold (0.3)."""
    import kairos.scheduler as sched
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    sched.state.iv_cap_active = True  # cap was already triggered last cycle

    from kairos.models import ConditionResult
    mild_iv_score = dummy_score.model_copy()
    mild_iv_score.status = "CAUTION"
    mild_iv_score.iv_capped = False  # engine says not capped (IV slightly positive)
    mild_iv_score.conditions = [
        ConditionResult(name="iv_trend", status="YELLOW", points=1, max_points=2, detail="+0.10 — mild expansion"),
    ]

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()

    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    mocker.patch("kairos.scheduler.is_lunch_break", return_value=False)

    mocker.patch("kairos.scheduler.evaluate", return_value=mild_iv_score)

    await sched.run_cycle()
    assert sched.state.iv_cap_active is True  # cap should still be held
    assert mild_iv_score.iv_capped is True     # logic should have overridden it


@pytest.mark.asyncio
async def test_iv_cap_hysteresis_releases(mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker):
    """Verify that the IV cap is released if recovery is above threshold (0.3)."""
    import kairos.scheduler as sched
    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session
    sched.state.iv_cap_active = True  # cap was already triggered last cycle

    from kairos.models import ConditionResult
    strong_iv_score = dummy_score.model_copy()
    strong_iv_score.status = "GO"
    strong_iv_score.iv_capped = False
    strong_iv_score.conditions = [
        ConditionResult(name="iv_trend", status="GREEN", points=2, max_points=2, detail="+0.35 — strong expansion"),
    ]

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock()
    mocker.patch("kairos.scheduler.evaluate", return_value=strong_iv_score)

    await sched.run_cycle()
    assert sched.state.iv_cap_active is False  # cap should be released
    assert strong_iv_score.iv_capped is False


# ─────────────────────────────────────────────────────────────────────────────
# MANM-137: Alert debouncing and deduplication during low-score periods
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_cycle_suppress_sub_indicator_jitter_in_caution(
    mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker
):
    """
    MANM-137 Change 1:
    When in CAUTION (score < 6), alert once on entry with initial blocking reasons,
    then remain completely silent during internal diagnostic shifts (vega_trap toggles,
    nde_state toggles, failure reason changes, or candle evaluated_at timestamp advances).
    """
    import kairos.scheduler as sched
    from kairos.models import ConditionResult, OIFlowResult, TrendPhase, MomentumDiagnostics

    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock(return_value=True)

    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    mocker.patch("kairos.scheduler.is_lunch_break", return_value=False)

    t0 = datetime(2026, 3, 23, 10, 5, tzinfo=ZoneInfo("Asia/Kolkata"))

    # Cycle 1: Enter CAUTION (score 4/8). Initial alert fires.
    oi_1 = OIFlowResult(
        score=0, phase=TrendPhase.NEUTRAL,
        reason="Current Vega trap active — NO TRADE",
        gex_state="neutral", nde_state="ambiguous", vega_trap=True,
        pcr=1.0, iv_skew=0.0, effective_veto=True,
    )
    score_1 = dummy_score.model_copy(update={
        "score": 4, "status": "CAUTION", "oi_flow_result": oi_1,
        "conditions": [
            ConditionResult(
                name="momentum", status="RED", points=0, max_points=1, detail="down",
                diagnostics=MomentumDiagnostics(evaluated_at=t0, readiness="ready", direction="neutral", dominant_count=0),
            ),
            ConditionResult(name="oi_flow", status="RED", points=0, max_points=1, detail=oi_1.reason),
        ],
    })
    mocker.patch("kairos.scheduler.evaluate", return_value=score_1)

    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1
    assert sched.state.is_silenced is True

    # Cycle 2: vega_trap toggles off, but reason is still RED. MUST NOT alert.
    oi_2 = oi_1.model_copy(update={"vega_trap": False})
    score_2 = score_1.model_copy(update={"oi_flow_result": oi_2})
    sched.evaluate.return_value = score_2
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1

    # Cycle 3: nde_state toggles from ambiguous to confirms, but overall RED. MUST NOT alert.
    oi_3 = oi_2.model_copy(update={"nde_state": "confirms"})
    score_3 = score_1.model_copy(update={"oi_flow_result": oi_3})
    sched.evaluate.return_value = score_3
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1

    # Cycle 4: Reason transitions from Current Vega Trap to Historical recovery hold. MUST NOT alert.
    oi_4 = oi_3.model_copy(update={"reason": "Historical recovery hold — Vega trap active in 3/8 cycles — NO TRADE"})
    score_4 = score_1.model_copy(update={
        "oi_flow_result": oi_4,
        "conditions": [
            score_1.conditions[0],
            ConditionResult(name="oi_flow", status="RED", points=0, max_points=1, detail=oi_4.reason),
        ],
    })
    sched.evaluate.return_value = score_4
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1

    # Cycle 5: Reason transitions to Mixed signals consensus not met. MUST NOT alert.
    oi_5 = oi_4.model_copy(update={"reason": "Mixed signals — directional consensus not met (3/8 matching green cycles) (NO TRADE)"})
    score_5 = score_1.model_copy(update={
        "oi_flow_result": oi_5,
        "conditions": [
            score_1.conditions[0],
            ConditionResult(name="oi_flow", status="RED", points=0, max_points=1, detail=oi_5.reason),
        ],
    })
    sched.evaluate.return_value = score_5
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1

    # Cycle 6: New 1-minute candle arrives; evaluated_at advances by 1 minute.
    # Sub-indicator timestamp advancement must NOT trigger an alert while in CAUTION.
    t1 = t0 + timedelta(minutes=1)
    score_6 = score_5.model_copy(update={
        "conditions": [
            ConditionResult(
                name="momentum", status="RED", points=0, max_points=1, detail="down",
                diagnostics=MomentumDiagnostics(evaluated_at=t1, readiness="ready", direction="neutral", dominant_count=0),
            ),
            score_5.conditions[1],
        ],
    })
    sched.evaluate.return_value = score_6
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1


@pytest.mark.asyncio
async def test_run_cycle_oi_flow_alerts_only_on_red_green_flips(
    mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker
):
    """
    MANM-137 Change 2:
    For Condition 3 (OI Flow), only trigger an alert if the overall condition status
    flips from RED 🔴 to GREEN 🟢 or from GREEN 🟢 to RED 🔴.
    Do not alert when transitioning between different RED failure reasons.
    """
    import kairos.scheduler as sched
    from kairos.models import ConditionResult, OIFlowResult, TrendPhase

    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock(return_value=True)

    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    mocker.patch("kairos.scheduler.is_lunch_break", return_value=False)

    # Cycle 1: CAUTION, OI Flow is RED (Current Vega trap). Alerts on entry.
    oi_red1 = OIFlowResult(
        score=0, phase=TrendPhase.LONG_BUILDUP,
        reason="Current Vega trap active — NO TRADE",
        gex_state="trend", nde_state="confirms", vega_trap=True,
        pcr=1.0, iv_skew=0.0, effective_veto=True,
    )
    score_1 = dummy_score.model_copy(update={
        "score": 4, "status": "CAUTION", "oi_flow_result": oi_red1,
        "conditions": [
            ConditionResult(name="oi_flow", status="RED", points=0, max_points=1, detail=oi_red1.reason),
        ],
    })
    mocker.patch("kairos.scheduler.evaluate", return_value=score_1)

    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1

    # Cycle 2: OI Flow transitions from Current Vega Trap to Historical recovery hold (still RED).
    # MUST NOT alert!
    oi_red2 = oi_red1.model_copy(update={"reason": "Historical recovery hold — Vega trap active — NO TRADE", "vega_trap": False})
    score_2 = score_1.model_copy(update={
        "oi_flow_result": oi_red2,
        "conditions": [
            ConditionResult(name="oi_flow", status="RED", points=0, max_points=1, detail=oi_red2.reason),
        ],
    })
    sched.evaluate.return_value = score_2
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1

    # Cycle 3: OI Flow flips from RED to GREEN! MUST alert!
    oi_green = OIFlowResult(
        score=1, phase=TrendPhase.LONG_BUILDUP,
        reason="Unified bullish conviction (Consensus 6/8) — GEX trend, NDE confirms",
        gex_state="trend", nde_state="confirms", vega_trap=False,
        pcr=1.0, iv_skew=0.0, effective_veto=False,
    )
    score_3 = score_1.model_copy(update={
        "score": 5, "oi_flow_result": oi_green,
        "conditions": [
            ConditionResult(name="oi_flow", status="GREEN", points=1, max_points=1, detail=oi_green.reason),
        ],
    })
    sched.evaluate.return_value = score_3
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 2

    # Cycle 4: OI Flow remains GREEN. MUST NOT alert (deduplicated).
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 2

    # Cycle 5: OI Flow flips back from GREEN to RED! MUST alert!
    score_5 = score_2.model_copy()
    sched.evaluate.return_value = score_5
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 3


@pytest.mark.asyncio
async def test_run_cycle_immediate_alert_on_go_and_deduplication(
    mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker
):
    """
    MANM-137 Change 3 & 4:
    Immediate alert when conditions align to ENVIRONMENT: GO (score >= 6).
    Silence gate unlocks immediately.
    Repeated identical GO states remain deduplicated.
    Transitioning back to CAUTION alerts immediately.
    """
    import kairos.scheduler as sched
    from kairos.models import ConditionResult, OIFlowResult, TrendPhase

    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock(return_value=True)

    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    mocker.patch("kairos.scheduler.is_lunch_break", return_value=False)

    # Cycle 1: Environment in CAUTION (score 5/8). Alerts on entry.
    c_caution = [
        ConditionResult(name="momentum", status="GREEN", points=1, max_points=1, detail="up"),
        ConditionResult(name="oi_flow", status="RED", points=0, max_points=1, detail="Mixed"),
    ]
    caution_score = dummy_score.model_copy(update={"score": 5, "status": "CAUTION", "conditions": c_caution})
    mocker.patch("kairos.scheduler.evaluate", return_value=caution_score)

    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1
    assert sched.state.is_silenced is True

    # Cycle 2: Same CAUTION state -> silenced.
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1

    # Cycle 3: Transitions to ENVIRONMENT: GO (score 8/8).
    # MUST unlock silence gate and alert immediately!
    c_go = [
        ConditionResult(name="momentum", status="GREEN", points=1, max_points=1, detail="up"),
        ConditionResult(name="oi_flow", status="GREEN", points=1, max_points=1, detail="Unified"),
    ]
    go_score = dummy_score.model_copy(update={"score": 8, "status": "GO", "conditions": c_go})
    sched.evaluate.return_value = go_score

    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 2
    assert sched.state.is_silenced is False

    # Cycle 4: Identical GO state -> deduplicated, MUST NOT alert.
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 2

    # Cycle 5: Still in GO, but score drops to 7/8 (genuine change in GO).
    go_score_7 = dummy_score.model_copy(update={"score": 7, "status": "GO", "conditions": c_go})
    sched.evaluate.return_value = go_score_7
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 3

    # Cycle 6: Drops out of GO into CAUTION (score 5/8).
    # MUST immediately alert and engage silence gate!
    sched.evaluate.return_value = caution_score
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 4
    assert sched.state.is_silenced is True

    # Cycle 7: Remains in CAUTION (score 5/8).
    # MUST NOT alert (silenced).
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 4


@pytest.mark.asyncio
async def test_run_cycle_recovery_and_deterioration_between_caution_and_avoid(
    mock_dependencies, dummy_session, dummy_candle, dummy_levels, dummy_score, mocker
):
    """
    Acceptance Criteria:
    A genuine recovery or deterioration is not suppressed solely because the score remains below 6.
    Transition between CAUTION (4-5) and AVOID (<4) alerts once on each transition.
    """
    import kairos.scheduler as sched
    from kairos.models import ConditionResult

    sched.state.reset_buffers()
    sched.state.startup_done = True
    sched.state.in_session = True
    sched.state.warmup_complete = True
    sched.state.prev_levels = dummy_levels
    sched.state.active_config = dummy_session

    sched.db.get_active_session = AsyncMock(return_value=dummy_session)
    sched.fetcher.get_option_chain = AsyncMock(return_value=[])
    sched.fetcher.get_latest_candle = AsyncMock(return_value=dummy_candle)
    sched.db.write_environment_log = AsyncMock()
    sched.notifier.post_environment_alert = AsyncMock(return_value=True)

    mocker.patch("kairos.scheduler.is_active_session", return_value=True)
    mocker.patch("kairos.scheduler.is_lunch_break", return_value=False)

    # Cycle 1: CAUTION (score 4/8). Initial alert.
    score_caution_4 = dummy_score.model_copy(update={"score": 4, "status": "CAUTION", "conditions": []})
    mocker.patch("kairos.scheduler.evaluate", return_value=score_caution_4)
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 1

    # Cycle 2: Deteriorates to AVOID (score 3/8). Must alert!
    score_avoid_3 = dummy_score.model_copy(update={"score": 3, "status": "AVOID", "conditions": []})
    sched.evaluate.return_value = score_avoid_3
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 2

    # Cycle 3: Remains in AVOID (score 3/8). Must be silent!
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 2

    # Cycle 4: Recovers to CAUTION (score 5/8). Must alert!
    score_caution_5 = dummy_score.model_copy(update={"score": 5, "status": "CAUTION", "conditions": []})
    sched.evaluate.return_value = score_caution_5
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 3

    # Cycle 5: Remains in CAUTION (score 5/8). Must be silent!
    await sched.run_cycle()
    assert sched.notifier.post_environment_alert.await_count == 3


