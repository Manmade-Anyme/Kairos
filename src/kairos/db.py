"""
db.py — Supabase client. Reads and writes to exactly 4 tables.
This is the shared state bridge between Python and Discord Orchestrator.
No raw market data is stored here — only scores and session config.
"""

from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger
from supabase import AsyncClient, acreate_client

from kairos.config import settings
from kairos.models import (
    AvailableExpiry,
    EnvironmentScore,
    PreviousDayLevels,
    SessionConfig,
)

IST = ZoneInfo("Asia/Kolkata")


class SupabaseDB:
    """
    Async Supabase client wrapper.
    Create once at startup via start(), close on shutdown via stop().
    """

    def __init__(self) -> None:
        self._client: Optional[AsyncClient] = None

    async def start(self) -> None:
        """Initialise the Supabase async client. Call once at startup."""
        self._client = await acreate_client(
            settings.supabase_url,
            settings.supabase_key,
        )
        logger.info("SupabaseDB: client initialised")

    async def stop(self) -> None:
        """Clean up the client on shutdown."""
        self._client = None
        logger.info("SupabaseDB: client closed")

    def _check_client(self) -> None:
        if self._client is None:
            raise RuntimeError("SupabaseDB not started — call await db.start() first")

    async def test_connectivity(self) -> bool:
        """Lightweight connectivity test used at startup health check."""
        try:
            self._check_client()
            await self._client.table("session_config").select("id").limit(1).execute()
            return True
        except Exception as e:
            logger.error(f"Supabase connectivity test failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────
    # session_config table
    # ─────────────────────────────────────────────────────────────────────

    async def get_active_session(self) -> Optional[SessionConfig]:
        """
        Read the active session config written by Discord Orchestrator after /start-monitor.
        Returns None if no active session exists.
        """
        self._check_client()
        try:
            result = (
                await self._client.table("session_config")
                .select("*")
                .eq("status", "ACTIVE")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if not result.data:
                return None
            row = result.data[0]
            return SessionConfig(
                symbol=row["symbol"],
                expiry=date.fromisoformat(row["expiry"]),
                expiry_type=row["expiry_type"],
                status=row["status"],
            )
        except Exception as e:
            logger.error(f"Failed to read session_config: {e}")
            return None

    async def set_active_session(self, symbol: str, expiry: date, expiry_type: str = "WEEKLY") -> None:
        """
        Set a new active session via CLI, bypassing Discord.
        Marks all existing ACTIVE sessions as STOPPED first.
        """
        self._check_client()
        try:
            # Mark existing active sessions as STOPPED
            await self._client.table("session_config").update({"status": "STOPPED"}).eq("status", "ACTIVE").execute()
            
            # Insert the new session
            await self._client.table("session_config").insert({
                "symbol": symbol,
                "expiry": expiry.isoformat(),
                "expiry_type": expiry_type,
                "status": "ACTIVE"
            }).execute()
            logger.info(f"CLI override: Started {symbol} {expiry_type} session for {expiry}")
        except Exception as e:
            logger.error(f"CLI override failed: {e}")
            raise

    async def stop_active_session(self) -> None:
        """Stops any currently ACTIVE session."""
        self._check_client()
        try:
            await self._client.table("session_config").update({"status": "STOPPED"}).eq("status", "ACTIVE").execute()
            logger.info("CLI override: Stopped active session")
        except Exception as e:
            logger.error(f"CLI override stop failed: {e}")
            raise

    # ─────────────────────────────────────────────────────────────────────
    # available_expiries table
    # ─────────────────────────────────────────────────────────────────────

    async def write_available_expiries(
        self,
        expiries: list[AvailableExpiry],
    ) -> None:
        """
        Write fresh expiry list to Supabase so Discord Orchestrator can build the dropdown.
        Clears old rows for the symbol first.
        """
        self._check_client()
        if not expiries:
            return

        symbol = expiries[0].symbol

        try:
            # Clear old expiries for this symbol
            await self._client.table("available_expiries").delete().eq("symbol", symbol).execute()

            # Insert fresh list
            rows = [
                {
                    "symbol": e.symbol,
                    "expiry": e.expiry.isoformat(),
                    "expiry_type": e.expiry_type,
                    "fetched_at": e.fetched_at.isoformat(),
                }
                for e in expiries
            ]
            await self._client.table("available_expiries").insert(rows).execute()
            logger.info(f"Wrote {len(expiries)} expiries for {symbol}")

        except Exception as e:
            logger.error(f"Failed to write available_expiries: {e}")
            raise

    # ─────────────────────────────────────────────────────────────────────
    # previous_day_levels table
    # ─────────────────────────────────────────────────────────────────────

    async def write_previous_day_levels(self, levels: PreviousDayLevels) -> None:
        """
        Store PDH/PDL for today's session.
        Upserts so re-running /start-monitor doesn't create duplicate rows.
        """
        self._check_client()
        try:
            await self._client.table("previous_day_levels").upsert(
                {
                    "symbol": levels.symbol,
                    "trade_date": levels.trade_date.isoformat(),
                    "prev_day_high": levels.prev_day_high,
                    "prev_day_low": levels.prev_day_low,
                    "fetched_at": levels.fetched_at.isoformat(),
                },
                on_conflict="symbol,trade_date",
            ).execute()
            logger.info(
                f"PDH/PDL stored: {levels.symbol} "
                f"H={levels.prev_day_high} L={levels.prev_day_low}"
            )
        except Exception as e:
            logger.error(f"Failed to write previous_day_levels: {e}")
            raise

    async def get_previous_day_levels(
        self,
        symbol: str,
        trade_date: date,
    ) -> Optional[PreviousDayLevels]:
        """
        Read PDH/PDL for today. Used to reload after a process restart.
        """
        self._check_client()
        try:
            result = (
                await self._client.table("previous_day_levels")
                .select("*")
                .eq("symbol", symbol)
                .eq("trade_date", trade_date.isoformat())
                .limit(1)
                .execute()
            )
            if not result.data:
                return None
            row = result.data[0]
            return PreviousDayLevels(
                symbol=row["symbol"],
                trade_date=date.fromisoformat(row["trade_date"]),
                prev_day_high=row["prev_day_high"],
                prev_day_low=row["prev_day_low"],
                fetched_at=datetime.fromisoformat(row["fetched_at"]),
            )
        except Exception as e:
            logger.error(f"Failed to read previous_day_levels: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────
    # environment_log table
    # ─────────────────────────────────────────────────────────────────────

    async def write_environment_log(self, score: EnvironmentScore) -> None:
        """
        Write one scored cycle result to environment_log.
        Called every cycle regardless of state change.
        """
        self._check_client()

        # Build flat condition fields from conditions list
        def get_status(name: str) -> str:
            c = score.get_condition(name)
            return c.status if c else "YELLOW"

        try:
            await self._client.table("environment_log").insert(
                {
                    "timestamp": score.timestamp.isoformat(),
                    "symbol": score.symbol,
                    "expiry": score.expiry.isoformat(),
                    "dte": score.dte,
                    "score": score.score,
                    "status": score.status,
                    "iv_trend": get_status("iv_trend"),
                    "momentum": get_status("momentum"),
                    "oi_flow": get_status("oi_flow"),
                    "gamma_theta": get_status("gamma_theta"),
                    "pdhl_breakout": get_status("pdhl_breakout"),
                    "move_ratio": get_status("move_ratio"),
                    "vwap_distance": get_status("vwap_distance"),
                    "iv_capped": score.iv_capped,
                    "ce_oi_change": score.total_ce_oi_change,
                    "pe_oi_change": score.total_pe_oi_change,
                    "summary_raw": score.summary_raw,
                    "summary": score.summary,
                }
            ).execute()

        except Exception as e:
            # Log but don't raise — scoring continues even if DB write fails
            logger.error(f"Failed to write environment_log (scoring continues): {e}")

    async def get_previous_status(self, symbol: str) -> Optional[str]:
        """
        Read the most recent environment status for state-change detection.
        Returns the status string ("GO", "CAUTION", "AVOID") or None.
        """
        self._check_client()
        try:
            result = (
                await self._client.table("environment_log")
                .select("status")
                .eq("symbol", symbol)
                .order("timestamp", desc=True)
                .limit(1)
                .execute()
            )
            if not result.data:
                return None
            return result.data[0]["status"]
        except Exception as e:
            logger.error(f"Failed to read previous status: {e}")
            return None


# Module-level singleton
db = SupabaseDB()


async def load_dhan_credentials_from_supabase() -> None:
    """
    Connect to Supabase using existing SUPABASE_URL and SUPABASE_KEY credentials,
    query the api_keys table where provider = 'DHAN' to select client_id and access_token.
    Retrieve the values and dynamically overwrite system settings instance attributes.
    """
    logger.info("Connecting to Supabase to fetch Dhan credentials...")
    
    # We can use the existing db client if started, otherwise create a temporary one.
    client = db._client
    temp_client = False
    if client is None:
        client = await acreate_client(settings.supabase_url, settings.supabase_key)
        temp_client = True

    try:
        result = (
            await client.table("api_keys")
            .select("client_id, access_token")
            .eq("provider", "DHAN")
            .limit(1)
            .execute()
        )
        if not result.data:
            raise ValueError("No API credentials found in Supabase for provider 'DHAN'")
        
        data = result.data[0]
        client_id = data.get("client_id")
        access_token = data.get("access_token")
        
        if not client_id or not access_token:
            raise ValueError(
                f"Dhan credentials fetched from Supabase are invalid/missing: "
                f"client_id={'Found' if client_id else 'Missing'}, "
                f"access_token={'Found' if access_token else 'Missing'}"
            )

        settings.dhan_client_id = client_id
        settings.dhan_access_token = access_token
        logger.info("Successfully retrieved and updated Dhan credentials from Supabase")
    except Exception as e:
        logger.error(f"Failed to load Dhan credentials from Supabase: {e}")
        raise

