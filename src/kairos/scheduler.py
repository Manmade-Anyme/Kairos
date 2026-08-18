"""
scheduler.py — Main entry point. Orchestrates the full system.
Manages in-memory buffers, scoring cycles, heartbeat, and health checks.
Uses APScheduler AsyncScheduler with IntervalTrigger to prevent cycle drift.
"""

import asyncio
import signal
import sys
from collections import deque
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from kairos.config import settings
from kairos.db import db, load_dhan_credentials_from_supabase
from kairos.engine import evaluate
from kairos.fetcher import DhanAPIError, DhanAuthError, fetcher
from kairos.models import PreviousDayLevels, SessionConfig
from kairos.notifier import notifier

IST = ZoneInfo("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────────────────
# Session state — shared across cycle and heartbeat jobs
# ─────────────────────────────────────────────────────────────────────────────

class SessionState:
    """Mutable session state shared between the two scheduler jobs."""

    def __init__(self) -> None:
        # In-memory rolling buffers
        self.candle_buffer: deque = deque(maxlen=settings.candle_buffer_size)
        self.iv_buffer: deque = deque(maxlen=settings.iv_buffer_size)
        self.oi_flow_buffer: deque = deque(maxlen=settings.oi_consensus_window)

        # OI rolling snapshots (15-minute rolling window)
        self.oi_snapshot_buffer: deque = deque(maxlen=settings.oi_lookback_cycles)
        self.last_alerted_oi_phase: Optional[str] = None

        # Session tracking
        self.active_config: Optional[SessionConfig] = None
        self.prev_levels: Optional[PreviousDayLevels] = None
        self.previous_status: Optional[str] = None
        self.previous_conditions: list[ConditionResult] = []

        # Health tracking
        self.cycle_count: int = 0
        self.last_fetch_time: Optional[datetime] = None
        self.last_successful_cycle_time: Optional[datetime] = None
        self.consecutive_failures: int = 0
        self.api_warning_sent: bool = False
        self.stale_alert_sent: bool = False
        self.is_silenced: bool = False

        # Session boundary tracking
        self.in_session: bool = False
        self.warmup_complete: bool = False
        self.lunch_break_alert_sent: bool = False
        self.startup_done: bool = False

        # API health
        self.dhan_ok: bool = True
        self.supabase_ok: bool = True
        self.iv_cap_active: bool = False


    def reset_buffers(self) -> None:
        """Reset all in-memory state when a new session starts."""
        self.candle_buffer.clear()
        self.iv_buffer.clear()
        self.oi_flow_buffer.clear()
        self.oi_snapshot_buffer.clear()
        self.last_alerted_oi_phase = None
        self.cycle_count = 0
        self.warmup_complete = False
        self.previous_status = None
        self.previous_conditions = []
        self.consecutive_failures = 0
        self.api_warning_sent = False
        self.stale_alert_sent = False
        self.iv_cap_active = False
        self.is_silenced = False
        logger.info("SessionState: buffers reset for new session")


    @property
    def warmup_cycles_remaining(self) -> int:
        """Cycles until all buffers have enough data for full scoring."""
        return max(0, settings.candle_buffer_size - len(self.candle_buffer))

    def check_warmup_complete(self) -> bool:
        """Returns True if buffers are full enough for all conditions."""
        return (
            len(self.candle_buffer) >= settings.candle_buffer_size
            and len(self.iv_buffer) >= settings.iv_change_lookback
        )


state = SessionState()


# ─────────────────────────────────────────────────────────────────────────────
# Session gate — check if within active trading hours
# ─────────────────────────────────────────────────────────────────────────────

def is_active_session() -> bool:
    """
    Returns True if current IST time is within an active trading session.
    Includes a 15-minute warmup window ONLY before the post-lunch session (Session 2).
    """
    now = datetime.now(IST)
    current = (now.hour, now.minute)

    # Official session windows
    s1_start = settings.session_1_start
    s1_end = settings.session_1_end
    s2_start = settings.session_2_start
    s2_end = settings.session_2_end

    # Calculate Session 2 Warmup start (15 minutes before s2_start)
    s2_dt = datetime.combine(now.date(), datetime.min.time().replace(hour=s2_start[0], minute=s2_start[1]))
    warmup_mins = max(settings.candle_buffer_size, settings.iv_change_lookback)
    s2_warmup_start_dt = s2_dt - timedelta(minutes=warmup_mins)
    s2_warmup_start = (s2_warmup_start_dt.hour, s2_warmup_start_dt.minute)

    # Session 1 starts exactly at 09:15 (no data available before open)
    in_s1 = s1_start <= current <= s1_end
    # Session 2 starts with warmup at ~12:45 IST
    in_s2 = s2_warmup_start <= current <= s2_end

    return in_s1 or in_s2


def is_lunch_break() -> bool:
    """Returns True if current IST time falls in the gap before the Session 2 warmup begins."""
    now = datetime.now(IST)
    current = (now.hour, now.minute)

    # Warmup for Session 2 starts 15 minutes before the official time
    s2_h, s2_m = settings.session_2_start
    s2_dt = datetime.combine(now.date(), datetime.min.time().replace(hour=s2_h, minute=s2_m))
    warmup_mins = max(settings.candle_buffer_size, settings.iv_change_lookback)
    s2_warmup_start_dt = s2_dt - timedelta(minutes=warmup_mins)
    s2_warmup_start = (s2_warmup_start_dt.hour, s2_warmup_start_dt.minute)

    return settings.session_1_end < current < s2_warmup_start


def get_session_name() -> str:
    now = datetime.now(IST)
    h, m = now.hour, now.minute
    if settings.session_1_start <= (h, m) <= settings.session_1_end:
        return f"Open ({settings.session_1_start[0]:02d}:{settings.session_1_start[1]:02d}–{settings.session_1_end[0]:02d}:{settings.session_1_end[1]:02d})"
    return f"Post-Lunch ({settings.session_2_start[0]:02d}:{settings.session_2_start[1]:02d}–{settings.session_2_end[0]:02d}:{settings.session_2_end[1]:02d})"


# ─────────────────────────────────────────────────────────────────────────────
# Startup health check
# ─────────────────────────────────────────────────────────────────────────────

async def run_startup_checks(config: SessionConfig) -> bool:
    """
    Run connectivity checks and load initial data.
    Posts results to #system-check.
    Returns True if all critical checks pass.
    """
    logger.info("Running startup health checks...")

    # Test Dhan API
    try:
        state.dhan_ok = await fetcher.test_connectivity()
    except DhanAuthError:
        state.dhan_ok = False
        await notifier.post_critical_alert(
            error_type="Dhan API Auth Failed (401)",
            error_detail="Access token expired or invalid",
            last_signal_time=None,
            action_hint="Update DHAN_ACCESS_TOKEN in .env and restart service",
        )
        return False

    # Test Supabase
    state.supabase_ok = await db.test_connectivity()

    # Fetch PDH/PDL
    pdh, pdl = 0.0, 0.0
    try:
        levels = await fetcher.get_previous_day_levels(config.symbol)
        await db.write_previous_day_levels(levels)
        state.prev_levels = levels
        pdh = levels.prev_day_high
        pdl = levels.prev_day_low
        logger.info(f"PDH/PDL loaded: H={pdh} L={pdl}")
    except Exception as e:
        logger.error(f"Failed to load PDH/PDL: {e}")

    # Fetch and write available expiries for dropdown
    try:
        expiries = await fetcher.get_available_expiries(config.symbol)
        await db.write_available_expiries(expiries)
    except Exception as e:
        logger.warning(f"Failed to refresh available expiries: {e}")

    # Post startup notice
    expiry_str = f"{config.expiry.strftime('%d %b %Y')} ({config.expiry_type.title()})"
    await notifier.post_startup(
        dhan_ok=state.dhan_ok,
        supabase_ok=state.supabase_ok,
        pdh=pdh,
        pdl=pdl,
        symbol=config.symbol,
        expiry_str=expiry_str,
    )

    return state.dhan_ok


# ─────────────────────────────────────────────────────────────────────────────
# Main scoring cycle — runs every 60 seconds
# ─────────────────────────────────────────────────────────────────────────────

async def run_cycle() -> None:
    """
    One complete scoring cycle.
    Fetch → Buffer → Score → Log → Alert (if state changed)
    Uses asyncio.gather for concurrent API calls to prevent cycle drift.
    """
    # 1. Read session config
    config = await db.get_active_session()
    if config is None or config.status == "STOPPED":
        if state.in_session:
            state.in_session = False
            logger.info("Session stopped")
        return

    # Ignore active sessions that have an expired date
    if config.expiry < date.today():
        if state.active_config is None or state.active_config.expiry != config.expiry or state.active_config.symbol != config.symbol:
            logger.warning(f"Active session expiry {config.expiry} is in the past. Ignoring until a new session starts.")
            state.active_config = config
            state.startup_done = False
            
            # Fetch fresh expiries and attempt auto-rollover
            try:
                expiries = await fetcher.get_available_expiries(config.symbol)
                await db.write_available_expiries(expiries)
                logger.info(f"Refreshed {len(expiries)} expiries for {config.symbol} despite stale session.")
                
                # Auto-rollover to the next upcoming expiry of the same type
                future_expiries = [e for e in expiries if e.expiry >= date.today() and e.expiry_type == config.expiry_type]
                if future_expiries:
                    future_expiries.sort(key=lambda x: x.expiry)
                    next_expiry = future_expiries[0]
                    logger.info(f"Auto-rollover triggered: updating session to {next_expiry.expiry}")
                    await db.set_active_session(
                        symbol=config.symbol, 
                        expiry=next_expiry.expiry, 
                        expiry_type=config.expiry_type
                    )
            except Exception as e:
                logger.warning(f"Failed to auto-rollover for stale session {config.symbol}: {e}")

        if state.in_session:
            state.in_session = False
        return

    # First time we see an active config — run startup
    if not state.startup_done:
        state.active_config = config
        ok = await run_startup_checks(config)
        if not ok:
            return
        state.startup_done = True
        state.reset_buffers()
        
        if not is_active_session():
            logger.info("Startup complete. Currently outside active session hours. Waiting for market schedule to begin...")

    # Config changed (new expiry selected) — reset buffers
    if (
        state.active_config
        and (
            state.active_config.expiry != config.expiry
            or state.active_config.symbol != config.symbol
        )
    ):
        logger.info(f"Session config changed — resetting buffers")
        state.active_config = config
        state.startup_done = False
        state.reset_buffers()
        return

    state.active_config = config

    # 2. Session gate check
    currently_in_session = is_active_session()
    if not currently_in_session:
        if state.in_session:
            state.in_session = False
            await notifier.post_session_boundary(entering=False, session_name="session")

        # Exit process if it's past the final session end time (Market Close)
        now = datetime.now(IST)
        if (now.hour, now.minute) >= settings.session_2_end:
            logger.info("Market close reached. Shutting down for the day.")
            # Give time for the boundary message to be sent
            await asyncio.sleep(2)
            sys.exit(0)

        # Fire lunch break alert once when we enter the gap between sessions
        if is_lunch_break() and not state.lunch_break_alert_sent:
            state.lunch_break_alert_sent = True
            await notifier.post_lunch_break()
        elif not is_lunch_break():
            # Reset the flag outside lunch break so it can fire again next day
            state.lunch_break_alert_sent = False

        return

    if not state.in_session:
        state.in_session = True
        state.reset_buffers()
        await notifier.post_session_boundary(
            entering=True,
            session_name=get_session_name(),
        )

    # 3. Fetch all data concurrently
    try:
        max_auth_retries = 1
        auth_retry_count = 0
        while True:
            try:
                option_chain, latest_candle = await asyncio.gather(
                    fetcher.get_option_chain(config.symbol, config.expiry),
                    fetcher.get_latest_candle(config.symbol),
                )
                break
            except (DhanAuthError, Exception) as e:
                is_auth_error = isinstance(e, DhanAuthError) or "401" in str(e) or "auth" in str(e).lower()
                if is_auth_error and auth_retry_count < max_auth_retries:
                    auth_retry_count += 1
                    logger.warning(f"ARES detected authentication error: {e}. Reloading credentials from Supabase and retrying...")
                    try:
                        await load_dhan_credentials_from_supabase()
                        # Safe mock-aware await of fetcher.stop() and fetcher.start()
                        import inspect
                        stop_fn = fetcher.stop
                        if inspect.iscoroutinefunction(stop_fn) or isinstance(stop_fn, AsyncMock):
                            await stop_fn()
                        else:
                            stop_fn()
                        start_fn = fetcher.start
                        if inspect.iscoroutinefunction(start_fn) or isinstance(start_fn, AsyncMock):
                            await start_fn()
                        else:
                            start_fn()
                        logger.info("Credentials reloaded and DhanFetcher reinitialized. Retrying API calls...")
                        continue
                    except Exception as reload_err:
                        logger.error(f"Failed to reload credentials during recovery: {reload_err}")
                        raise e
                else:
                    raise e

        # Calculate 15-minute rolling OI delta
        current_snapshot = {(r.strike, r.option_type): r.oi for r in option_chain}
        state.oi_snapshot_buffer.append(current_snapshot)

        # Baseline snapshot is the oldest one in the buffer
        baseline_snapshot = state.oi_snapshot_buffer[0]

        for row in option_chain:
            key = (row.strike, row.option_type)
            if key in baseline_snapshot:
                row.oi_change = row.oi - baseline_snapshot[key]
            else:
                row.oi_change = 0

        state.last_fetch_time = datetime.now(IST)
        if state.api_warning_sent:
            await notifier.post_api_recovered()
            state.api_warning_sent = False

        state.consecutive_failures = 0
        state.dhan_ok = True
        state.stale_alert_sent = False

    except DhanAuthError as e:
        state.dhan_ok = False
        state.consecutive_failures += 1
        logger.error(f"Dhan auth error: {e}")
        await notifier.post_critical_alert(
            error_type="Dhan API Auth Failed (401)",
            error_detail=str(e),
            last_signal_time=state.last_successful_cycle_time,
            action_hint="Verify Supabase api_keys table has active tokens",
        )
        return

    except DhanAPIError as e:
        state.consecutive_failures += 1
        state.dhan_ok = False
        logger.warning(f"Dhan fetch failed (attempt {state.consecutive_failures}): {e}")

        if state.consecutive_failures >= 3 and not state.api_warning_sent:
            state.api_warning_sent = True
            await notifier.post_api_warning(state.consecutive_failures, str(e))

        if state.consecutive_failures >= 5 and not state.stale_alert_sent:
            state.stale_alert_sent = True
            await notifier.post_stale_signal_warning(
                last_cycle_time=state.last_successful_cycle_time or datetime.now(IST),
                symbol=config.symbol,
            )
        return

    # 4. Guard: we need PDH/PDL
    if state.prev_levels is None:
        try:
            levels = await fetcher.get_previous_day_levels(config.symbol)
            await db.write_previous_day_levels(levels)
            state.prev_levels = levels
        except Exception as e:
            logger.error(f"Cannot score without PDH/PDL: {e}")
            return

    # 5. Update in-memory buffers
    state.candle_buffer.append(latest_candle)

    # Find ATM IV and append to iv_buffer
    interval = settings.nifty_strike_interval
    if config.symbol == "SENSEX" or config.symbol.isdigit():
        interval = settings.sensex_strike_interval

    atm_strike = round(latest_candle.close / interval) * interval
    atm_ce = next(
        (r for r in option_chain if r.strike == atm_strike and r.option_type == "CE"),
        None,
    )
    if atm_ce:
        state.iv_buffer.append(atm_ce.iv)
    else:
        logger.warning(f"ATM CE not found in option chain at strike {atm_strike}")

    state.cycle_count += 1

    # 6. Check warmup
    just_warmed_up = False
    if not state.warmup_complete and state.check_warmup_complete():
        state.warmup_complete = True
        just_warmed_up = True
        await notifier.post_warmup_complete(config.symbol)
        logger.info("Warmup complete — all conditions scoring normally")

    # 7. Calculate DTE
    dte = max(0, (config.expiry - date.today()).days)

    # 8. Score all 7 conditions
    try:
        score = evaluate(
            option_chain=option_chain,
            candle_buffer=state.candle_buffer,
            iv_buffer=state.iv_buffer,
            prev_levels=state.prev_levels,
            spot_price=latest_candle.close,
            dte=dte,
            session_config=config,
            previous_status=state.previous_status,
            oi_flow_buffer=state.oi_flow_buffer,
        )
    except Exception as e:
        logger.error(f"Scoring failed: {e}")
        return

    # 9. Hysteresis: IV cap turns ON when RED, turns OFF only when GREEN (ADR-011)
    #    This prevents the cap from flickering on/off during borderline YELLOW readings.
    #    Cap ON  = IV Trend scored RED this cycle (engine.py sets iv_capped=True)
    #    Cap OFF = IV Trend scored GREEN (genuine expansion confirmed)
    #    Cap HOLD = IV Trend is YELLOW but cap was previously active → keep capping
    iv_condition = score.get_condition("iv_trend")

    if score.iv_capped:
        # Engine flagged RED this cycle — activate cap
        state.iv_cap_active = True
    elif state.iv_cap_active and (iv_condition and iv_condition.status != "GREEN"):
        # Cap was previously active and IV hasn't recovered to GREEN yet — hold cap
        score.iv_capped = True
        if score.status == "GO":
            score.status = "CAUTION"
    else:
        # Either cap was never active, or IV recovered to GREEN — release
        state.iv_cap_active = False

    # 10. Write to Supabase
    await db.write_environment_log(score)
    state.last_successful_cycle_time = datetime.now(IST)

    # 11. State change detection and Discord alert

    # Detect significant changes: Status, Color, or OI Phase shifts (ADR-017)
    significant_change = False
    current_oi_phase = None

    for c in score.conditions:
        if c.name == "oi_flow":
            current_oi_phase = c.detail.split('|')[0].strip() if '|' in c.detail else c.detail

    if state.previous_conditions:
        if len(state.previous_conditions) != len(score.conditions):
            significant_change = True
        else:
            for old_c, new_c in zip(state.previous_conditions, score.conditions):
                # Rule 1: Always alert if the Color (Status) of any condition changes
                if old_c.status != new_c.status:
                    significant_change = True
            
            # Rule 2: For OI Flow, alert if the Phase changes (consensus-smoothed)
            if current_oi_phase and state.last_alerted_oi_phase:
                if current_oi_phase != state.last_alerted_oi_phase:
                    significant_change = True
    elif score.conditions:
        significant_change = True  # First cycle with scoring data

    if (score.state_changed or just_warmed_up or significant_change) and state.warmup_complete:
        should_alert = False

        if score.score >= 6:
            state.is_silenced = False
            should_alert = True
        else:
            if not state.is_silenced:
                state.is_silenced = True
                should_alert = True

        if should_alert:
            if score.state_changed:
                logger.info(
                    f"State change: {state.previous_status} → {score.status} "
                    f"(score {score.score}/8)"
                )
            
            if current_oi_phase:
                state.last_alerted_oi_phase = current_oi_phase

            # Post environment alert for significant shifts
            await notifier.post_environment_alert(score)
        else:
            logger.debug(f"Alert silenced (score {score.score}/8 < 6)")

    state.previous_status = score.status
    state.previous_conditions = score.conditions
    logger.debug(
        f"Cycle {state.cycle_count}: {score.status} {score.score}/8 "
        f"(DTE={dte}, iv_capped={score.iv_capped})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat job — runs every 5 minutes
# ─────────────────────────────────────────────────────────────────────────────

async def run_heartbeat() -> None:
    """
    Posts a heartbeat to #system-check every 5 minutes.
    Also checks if scoring cycle has gone silent (stale signal detection).
    Heartbeats are suppressed when no active session exists.
    """
    config = await db.get_active_session()
    if config is None or config.status == "STOPPED":
        return  # no active session — suppress heartbeat

    now = datetime.now(IST)

    # Check for stale signal (no successful cycle for N minutes during session)
    if (
        state.in_session
        and state.last_successful_cycle_time
        and not state.stale_alert_sent
    ):
        minutes_silent = (now - state.last_successful_cycle_time).total_seconds() / 60
        if minutes_silent >= settings.stale_signal_threshold_minutes:
            state.stale_alert_sent = True
            await notifier.post_stale_signal_warning(
                last_cycle_time=state.last_successful_cycle_time,
                symbol=config.symbol,
            )
            return

    last_fetch = state.last_fetch_time
    fetch_time_str = last_fetch.strftime('%H:%M:%S') if last_fetch is not None else 'Never'

    # Log a compact heartbeat summary
    logger.info(
        f"💓 HEARTBEAT | {config.symbol} | Cycle {state.cycle_count} | "
        f"Fetch: {fetch_time_str} | Warmup: {'Done' if state.warmup_complete else 'Pending'}"
    )

    # Discord heartbeat suppressed per ADR-006 to reduce noise.
    # It now only triggers when an issue is detected (handled via stale signal warning).
    # await notifier.post_heartbeat(...)



# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """
    Main async entry point.
    Starts all services, configures APScheduler, runs until shutdown.
    """
    # Configure logging
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
    )
    logger.add(
        "logs/kairos.log",
        rotation=settings.log_rotation,
        retention=settings.log_retention,
        level=settings.log_file_level,
        compression="zip",
    )

    logger.info("Kairos starting up...")

    # Start services
    await db.start()
    await load_dhan_credentials_from_supabase()
    await fetcher.start()
    await notifier.start()

    # Graceful shutdown handler
    def handle_shutdown(sig, frame):
        logger.info(f"Received signal {sig} — shutting down")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Start APScheduler with two independent interval jobs
    # IntervalTrigger fires on fixed intervals regardless of job duration — no drift
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_cycle,
        'interval',
        seconds=settings.poll_interval_seconds,
        id="scoring_job",
        max_instances=1,
    )
    scheduler.add_job(
        run_heartbeat,
        'interval',
        seconds=settings.heartbeat_interval_seconds,
        id="heartbeat_job",
        max_instances=1,
    )

    logger.info(
        f"Scheduler running — "
        f"scoring every {settings.poll_interval_seconds}s, "
        f"heartbeat every {settings.heartbeat_interval_seconds}s"
    )
    
    scheduler.start()

    # Run first cycle immediately on startup
    await run_cycle()

    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down gracefully...")
    finally:
        scheduler.shutdown()

    # Cleanup
    await fetcher.stop()
    await db.stop()
    await notifier.stop()
    logger.info("Kairos shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
