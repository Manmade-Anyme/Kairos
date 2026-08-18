"""
notifier.py — All Discord communication.
Posts environment alerts to #environment and health messages to #system-check.
Uses Discord webhooks directly — no bot token needed.
"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from kairos.config import settings
from kairos.models import ConditionResult, EnvironmentScore, HealthStatus, OIFlowResult

IST = ZoneInfo("Asia/Kolkata")

# Emoji maps
STATUS_EMOJI = {"GO": "🟢", "CAUTION": "🟡", "AVOID": "🔴"}
CONDITION_EMOJI = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}
CONDITION_LABELS = {
    "iv_trend": "IV Trend",
    "momentum": "Momentum",
    "oi_flow": "OI Flow",
    "gamma_theta": "Gamma/θ",
    "pdhl_breakout": "PDH/PDL",
    "move_ratio": "Move",
    "vwap_distance": "VWAP",
}

_PHASE_EMOJI = {
    "Long Buildup":   "🟢",
    "Short Buildup":  "🔴",
    "Long Unwinding": "🟠",
    "Short Covering": "🔵",
    "Neutral":        "🟡",
}


def _format_pcr(pcr: float) -> str:
    """Format PCR value with qualitative label for Discord."""
    if pcr < 0.90:
        return f"{pcr:.2f} — put writers absent (strongly bearish)"
    if pcr < 0.95:
        return f"{pcr:.2f} — put writers absent (bearish lean)"
    if pcr <= 1.05:
        return f"{pcr:.2f} — balanced"
    if pcr <= 1.15:
        return f"{pcr:.2f} — put writers defending (bullish lean)"
    return f"{pcr:.2f} — put writers dominant (strongly bullish)"


def _format_oi_flow_line(r: OIFlowResult) -> str:
    """Build the compact OI Flow condition line for Discord."""
    phase_str = f"{r.phase.value} {_PHASE_EMOJI.get(r.phase.value, '🟡')}"

    gex_token = {
        "pin":     "GEX pin 🔴",
        "trend":   "GEX trend 🟢",
        "neutral": "GEX neutral 🟡",
    }[r.gex_state]

    nde_token = {
        "confirms":    "NDE confirms 🟢",
        "contradicts": "NDE contra 🔴",
        "neutral":     "NDE ambiguous 🟡",
    }[r.nde_state]

    vega_token = " | Vega trap 🔥" if r.vega_trap else ""

    return f"{phase_str} | {gex_token} | {nde_token}{vega_token}"


class Notifier:
    """
    Discord webhook poster.
    Uses a shared httpx.AsyncClient for all requests.
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        logger.info("Notifier: HTTP client initialised")

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
        logger.info("Notifier: HTTP client closed")

    async def _post(self, webhook_url: str, payload: dict) -> None:
        """Fire-and-forget webhook POST. Logs on failure, never raises."""
        # Always print payload to terminal for local debugging
        content = payload.get("content", "")
        content_snippet = content[:100].replace("\n", " ")
        logger.info(f"📢 Discord Message -> Content: {content_snippet}...")
        
        if not webhook_url or not webhook_url.startswith("http"):
            logger.error(f"Failed to post to Discord: Webhook URL is invalid or not configured correctly in .env. Skipping.")
            return
            
        if not self._client:
            logger.error("Notifier not started")
            return
        try:
            response = await self._client.post(webhook_url, json=payload)
            if response.status_code not in (200, 204):
                logger.error(
                    f"Discord webhook returned {response.status_code}: "
                    f"{response.text[:200]}"
                )
        except Exception as e:
            logger.error(f"Failed to post to Discord: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # Environment alerts → #environment channel
    # ─────────────────────────────────────────────────────────────────────

    async def post_environment_alert(self, score: EnvironmentScore) -> None:
        """
        Post GO / CAUTION / AVOID alert to #environment channel.
        Only called when state changes.
        """
        status_emoji = STATUS_EMOJI.get(score.status, "⚪")
        cap_note = "  ⚠️ IV Cap Active" if (score.iv_capped and score.score >= settings.score_go_min) else ""

        # Build condition lines — use compact OI flow line if available
        condition_lines = []
        for c in score.conditions:
            emoji = CONDITION_EMOJI.get(c.status, "⚪")
            label = CONDITION_LABELS.get(c.name, c.name)
            pts = f"[{c.points}/{c.max_points}]" if c.max_points > 1 else ""

            if c.name == "oi_flow" and score.oi_flow_result:
                # Use compact OI flow line with GEX/NDE/vega tokens
                oi_detail = _format_oi_flow_line(score.oi_flow_result)
                condition_lines.append(f"{emoji} {label:<9}: {oi_detail}")
            else:
                condition_lines.append(f"{emoji} {label:<9}: {c.detail} {pts}".rstrip())

        session_label = _get_session_label()
        time_str = datetime.now(IST).strftime("%H:%M IST")

        # Use summary if Gemini has polished it, otherwise fall back to summary_raw
        summary_text = score.summary if score.summary else _generate_fallback_summary(score)

        # PCR line (only if oi_flow_result is available)
        pcr_line = ""
        if score.oi_flow_result:
            pcr_line = f"PCR      : {_format_pcr(score.oi_flow_result.pcr)}\n"

        lines = [
            f"{status_emoji} **ENVIRONMENT: {score.status}**{cap_note}",
            "─" * 34,
            f"Index    : {score.symbol}  |  DTE: {score.dte}",
            f"Session  : {session_label}",
            f"Expiry   : {score.expiry.strftime('%d %b %Y')} ({_expiry_type_label(score)})",
            f"{pcr_line}{'─' * 34}",
        ] + condition_lines + [
            "─" * 34,
            f"Score    : {score.score}/{score.max_score}",
        ]

        if score.iv_capped and score.score >= settings.score_go_min:
            lines.append(f"⚠️ Capped at CAUTION — IV contracting")
        elif score.iv_capped:
            lines.append(f"⚠️ IV contracting — premium at risk")

        # OI Flow reason footer (only on state-change alerts)
        if score.oi_flow_result and score.state_changed:
            prefix = "✓" if score.oi_flow_result.score == 1 else "⚠️"
            lines.append(f"{prefix} {score.oi_flow_result.reason}")

        lines.append(summary_text)
        lines.append(f"🕐 {time_str}")

        content = "\n".join(lines)
        await self._post(settings.discord_webhook_url, {"content": content})
        logger.info(f"Posted {score.status} alert to #environment — score {score.score}/8")

    # ─────────────────────────────────────────────────────────────────────
    # Health messages → #system-check channel
    # ─────────────────────────────────────────────────────────────────────

    async def post_startup(
        self,
        dhan_ok: bool,
        supabase_ok: bool,
        pdh: float,
        pdl: float,
        symbol: str,
        expiry_str: str,
    ) -> None:
        """Post startup sequence result to #system-check."""
        dhan_status = "✅ Connected" if dhan_ok else "❌ FAILED"
        supa_status = "✅ Connected" if supabase_ok else "❌ FAILED"
        time_str = datetime.now(IST).strftime("%H:%M:%S IST")

        content = "\n".join([
            "ℹ️ **ENGINE STARTING**",
            f"Kairos v{_get_version()} starting on VPS",
            f"Time: {time_str}",
            "",
            f"Dhan API  : {dhan_status}",
            f"Supabase  : {supa_status}",
            f"PDH/PDL   : H={pdh:.0f} / L={pdl:.0f}",
            "",
            "⏳ **WARMUP IN PROGRESS**",
            "Buffers empty — scoring in CAUTION mode",
            "Full scoring available in ~15 minutes (cycle 15)",
            f"Monitoring : {symbol} | {expiry_str}",
        ])
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_warmup_complete(self, symbol: str) -> None:
        """Post warmup complete notice to #system-check."""
        time_str = datetime.now(IST).strftime("%H:%M IST")
        content = (
            f"✅ **WARMUP COMPLETE**\n"
            f"All 7 conditions now scoring normally.\n"
            f"{symbol} | {time_str}"
        )
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_heartbeat(
        self,
        last_fetch_time: Optional[datetime],
        cycle_count: int,
        symbol: str,
        expiry_str: str,
        warmup_complete: bool,
    ) -> None:
        """Post 5-minute heartbeat to #system-check."""
        fetch_str = (
            last_fetch_time.strftime("%H:%M:%S IST")
            if last_fetch_time
            else "Never"
        )
        time_str = datetime.now(IST).strftime("%H:%M IST")
        warmup_note = "" if warmup_complete else "\n⏳ Still warming up..."

        content = "\n".join([
            "💓 **HEARTBEAT** — System alive",
            "─" * 30,
            f"Status    : ✅ Fetching normally",
            f"Last fetch: {fetch_str}",
            f"API       : ✅ Dhan responding",
            f"Cycle     : {cycle_count} of session",
            "─" * 30,
            f"{symbol} | {expiry_str}",
            f"🕐 {time_str}{warmup_note}",
        ])
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_api_warning(self, attempt: int, error_str: str) -> None:
        """Post API failure warning — only called on 'Start' of warning."""
        time_str = datetime.now(IST).strftime("%H:%M:%S IST")
        content = (
            f"⚠️ **DHAN API WARNING (START)**\n"
            f"Failed to fetch data (attempt {attempt})\n"
            f"Error: {error_str}\n"
            f"Monitoring for recovery... | {time_str}"
        )
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_api_recovered(self) -> None:
        """Post API recovery — only called on 'End' of warning."""
        time_str = datetime.now(IST).strftime("%H:%M:%S IST")
        content = (
            f"✅ **DHAN API RECOVERED (END)**\n"
            f"Connectivity restored. Normal operations resumed.\n"
            f"🕐 {time_str}"
        )
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_critical_alert(
        self,
        error_type: str,
        error_detail: str,
        last_signal_time: Optional[datetime],
        action_hint: str,
    ) -> None:
        """
        Post critical system failure to #system-check.
        Tells trader not to trade based on last signal.
        """
        time_str = datetime.now(IST).strftime("%H:%M:%S IST")
        last_signal_str = (
            last_signal_time.strftime("%H:%M IST")
            if last_signal_time
            else "unknown"
        )

        content = "\n".join([
            "🚨 **SYSTEM ALERT — ACTION REQUIRED**",
            "─" * 34,
            f"Error  : {error_type}",
            f"Detail : {error_detail}",
            f"Time   : {time_str}",
            f"Impact : ⚠️ Last signal posted at {last_signal_str} may be STALE",
            "",
            f"Action : {action_hint}",
            "─" * 34,
            "**DO NOT trade based on last GO signal.**",
        ])
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_stale_signal_warning(
        self,
        last_cycle_time: datetime,
        symbol: str,
    ) -> None:
        """
        Post stale signal warning to BOTH channels when engine is unresponsive.
        """
        time_str = datetime.now(IST).strftime("%H:%M IST")
        last_str = last_cycle_time.strftime("%H:%M IST")

        content = (
            f"⚠️ **SIGNAL MAY BE STALE**\n"
            f"Last successful cycle: {last_str}\n"
            f"Current time: {time_str}\n"
            f"Engine appears unresponsive.\n"
            f"**Disregard last environment status until system recovers.**"
        )
        # Post to both channels
        await self._post(settings.discord_webhook_url, {"content": content})
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_stopped(self, symbol: str) -> None:
        """Post engine stopped confirmation to #system-check."""
        time_str = datetime.now(IST).strftime("%H:%M IST")
        content = (
            f"ℹ️ **ENGINE STOPPED**\n"
            f"{symbol} monitoring paused via /stop-monitor\n"
            f"🕐 {time_str}"
        )
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_session_boundary(self, entering: bool, session_name: str) -> None:
        """Post session start/end to #system-check."""
        time_str = datetime.now(IST).strftime("%H:%M IST")
        if entering:
            content = f"✅ **SESSION ACTIVE** — {session_name}\nMonitoring begins | {time_str}"
        else:
            content = f"ℹ️ **SESSION ENDED** — {session_name}\nEngine idle until next session | {time_str}"
        await self._post(settings.discord_health_webhook_url, {"content": content})

    async def post_lunch_break(self) -> None:
        """Post lunch break alert to BOTH channels — no trading during this window."""
        time_str = datetime.now(IST).strftime("%H:%M IST")
        s1_end = settings.session_1_end
        s2_start = settings.session_2_start
        resume_str = f"{s2_start[0]:02d}:{s2_start[1]:02d}"

        content = "\n".join([
            "⏸️ **LUNCH BREAK — NO TRADING**",
            "─" * 34,
            f"Session 1 ended at {s1_end[0]:02d}:{s1_end[1]:02d} IST",
            f"Session 2 resumes at {resume_str} IST",
            "",
            "Engine is idle. **Do NOT trade during this window.**",
            "Signals will resume automatically at next session.",
            f"🕐 {time_str}",
        ])
        # Post to both channels so trader sees it everywhere
        await self._post(settings.discord_webhook_url, {"content": content})
        await self._post(settings.discord_health_webhook_url, {"content": content})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_session_label() -> str:
    now = datetime.now(IST)
    h, m = now.hour, now.minute
    current = (h, m)
    
    s1_start, s1_end = settings.session_1_start, settings.session_1_end
    s2_start, s2_end = settings.session_2_start, settings.session_2_end

    if s1_start <= current <= s1_end:
        return f"Open ({s1_start[0]:02d}:{s1_start[1]:02d}–{s1_end[0]:02d}:{s1_end[1]:02d})"
    elif s2_start <= current <= s2_end:
        return f"Post-Lunch ({s2_start[0]:02d}:{s2_start[1]:02d}–{s2_end[0]:02d}:{s2_end[1]:02d})"
    return "Outside session"


def _expiry_type_label(score: EnvironmentScore) -> str:
    """Returns 'Weekly' or 'Monthly' — reads from environment_log not directly available here."""
    return "Weekly"   # Discord Orchestrator enriches this from session_config


def _generate_fallback_summary(score: EnvironmentScore) -> str:
    """Minimal fallback summary if Gemini hasn't polished yet."""
    if score.status == "GO":
        return "Conditions favorable for options buying."
    elif score.status == "CAUTION":
        if score.iv_capped:
            return "Strong conditions but IV contracting — wait for IV to stabilize."
        return "Mixed conditions — wait for a cleaner setup."
    else:
        return "⚠️ Premium will decay faster than market moves."


def _get_version() -> str:
    try:
        from kairos import __version__
        return __version__
    except Exception:
        return "0.1.0"


# Module-level singleton
notifier = Notifier()
