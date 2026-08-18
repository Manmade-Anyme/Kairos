"""
models.py — All Pydantic data models for Kairos.
These define the shape of every data object flowing through the system.
No business logic here — only structure and validation.
"""

from datetime import date, datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_validator


class TrendPhase(str, Enum):
    """Market trend phases derived from price action vs OI flow."""
    LONG_BUILDUP = "Long Buildup"
    SHORT_BUILDUP = "Short Buildup"
    SHORT_COVERING = "Short Covering"
    LONG_UNWINDING = "Long Unwinding"
    NEUTRAL = "Neutral"


class OptionChainRow(BaseModel):
    """One strike/type row from the Dhan option chain API response."""

    model_config = ConfigDict(frozen=False)

    timestamp: datetime
    symbol: str                  # "NIFTY" or "SENSEX"
    expiry: date
    strike: int
    option_type: str             # "CE" or "PE"
    iv: float                    # implied volatility
    delta: float
    gamma: float
    theta: float
    vega: float
    oi: int                      # open interest
    previous_oi: int = 0         # previous day's close OI (from Dhan API)
    oi_change: int               # OI change vs previous snapshot
    volume: int
    ltp: float                   # last traded price
    bid: float
    ask: float

    @field_validator("option_type")
    @classmethod
    def validate_option_type(cls, v: str) -> str:
        if v not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {v}")
        return v

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        if v not in ("NIFTY", "SENSEX"):
            raise ValueError(f"symbol must be NIFTY or SENSEX, got {v}")
        return v


class OHLCVCandle(BaseModel):
    """One 1-minute OHLCV candle for the underlying index."""

    model_config = ConfigDict(frozen=False)

    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float    # calculated incrementally by fetcher each cycle


class PreviousDayLevels(BaseModel):
    """Previous trading day's high and low — fetched once at session start."""

    model_config = ConfigDict(frozen=False)

    symbol: str
    trade_date: date             # today's date (these levels apply to today)
    prev_day_high: float
    prev_day_low: float
    fetched_at: datetime


class SessionConfig(BaseModel):
    """Active monitoring session as selected by trader via Discord /start-monitor."""

    model_config = ConfigDict(frozen=False)

    symbol: str
    expiry: date
    expiry_type: str             # "WEEKLY" or "MONTHLY"
    status: str                  # "ACTIVE" or "STOPPED"

    @field_validator("expiry_type")
    @classmethod
    def validate_expiry_type(cls, v: str) -> str:
        if v not in ("WEEKLY", "MONTHLY"):
            raise ValueError(f"expiry_type must be WEEKLY or MONTHLY, got {v}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("ACTIVE", "STOPPED"):
            raise ValueError(f"status must be ACTIVE or STOPPED, got {v}")
        return v


class AvailableExpiry(BaseModel):
    """One available expiry date for a symbol — used to build Discord dropdown."""

    model_config = ConfigDict(frozen=False)

    symbol: str
    expiry: date
    expiry_type: str             # "WEEKLY" or "MONTHLY"
    fetched_at: datetime


class ATMStrikes(BaseModel):
    """The ATM CE and PE rows for the current snapshot — computed each cycle."""

    model_config = ConfigDict(frozen=False)

    atm_strike: int
    ce: OptionChainRow
    pe: OptionChainRow
    spot_price: float


class StrikeCluster(BaseModel):
    """
    ATM + wings (±7 strikes) for the current snapshot.
    Used for multi-strike OI Flow, Trend Phase detection, and Greeks aggregation.
    """
    model_config = ConfigDict(frozen=False)

    atm_strike: int
    spot_price: float
    ce_cluster: list[OptionChainRow]
    pe_cluster: list[OptionChainRow]

    total_ce_oi_change: int = 0
    total_pe_oi_change: int = 0

    # ── Greeks aggregates (computed by compute_greeks_aggregates) ─────────
    price_change: float = 0.0
    # Spot delta for phase classification. Computed upstream in evaluate().

    net_delta_exposure: float = 0.0
    # Σ W_i × (ce_delta_i × ce_oi_i + pe_delta_i × pe_oi_i)
    # pe_delta is already negative from Dhan — no sign flip.

    net_gex: float = 0.0
    # Σ W_i × (ce_gamma_i × ce_oi_i - pe_gamma_i × pe_oi_i) × LOT_SIZE
    # Positive = dealers net long gamma = PIN. Negative = TREND.

    atm_vega_exposure: float = 0.0
    # Σ W_i × (ce_vega_i × ce_oi_i + pe_vega_i × pe_oi_i)
    # ATM ± VEGA_WINDOW only.

    theta_burn_rate: float = 0.0
    # Σ W_i × (|ce_theta_i| × ce_oi_i + |pe_theta_i| × pe_oi_i)
    # Uses abs() — Dhan returns theta as negative.

    pcr: float = 1.0
    # Σ pe_oi / Σ ce_oi across ATM ± STRIKE_WINDOW (unweighted).

    iv_skew: float = 0.0
    # avg(pe_iv) - avg(ce_iv) across ATM ± VEGA_WINDOW.

    pe_unwind_above_spot: bool = False
    # True if ANY PE strike ABOVE spot has intraday OI delta < -8%.

    ce_unwind_below_spot: bool = False
    # True if ANY CE strike BELOW spot has intraday OI delta < -8%.

    ce_wall_strike: Optional[float] = None
    # Strike with highest total CE OI (resistance wall).

    ce_wall_oi_cr: Optional[float] = None
    # CE wall OI in Crore (raw OI / 10_000_000).

    pe_wall_strike: Optional[float] = None
    # Strike with highest total PE OI (support wall).

    pe_wall_oi_cr: Optional[float] = None
    # PE wall OI in Crore.

    total_abs_gex: float = 0.0
    total_abs_nde: float = 0.0
    total_abs_vega: float = 0.0


class ConditionResult(BaseModel):
    """
    Result of one scored condition.
    Every condition returns this same shape — scoring engine loops over them.
    """

    model_config = ConfigDict(frozen=False)

    name: str          # e.g. "iv_trend", "momentum", "oi_flow"
    status: str        # "GREEN", "YELLOW", "RED"
    points: int        # actual points scored this cycle
    max_points: int    # maximum possible for this condition
    detail: str        # human-readable value, e.g. "+2.1 IV expanding"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("GREEN", "YELLOW", "RED"):
            raise ValueError(f"status must be GREEN, YELLOW, or RED, got {v}")
        return v


class OIFlowResult(BaseModel):
    """
    Rich output from the upgraded Condition 3 (OI Flow) scorer.
    Carries all Greeks-derived signals for Discord formatting and diagnostics.
    """
    model_config = ConfigDict(frozen=False)

    score: int                                 # 0 or 1
    phase: TrendPhase                          # classified trend phase
    reason: str                                # 1-line human-readable reason
    gex_state: str                             # "pin" | "trend" | "neutral"
    nde_state: str                             # "confirms" | "contradicts" | "neutral"
    vega_trap: bool
    pcr: float
    iv_skew: float
    ce_wall_strike: Optional[float] = None
    ce_wall_oi_cr: Optional[float] = None
    pe_wall_strike: Optional[float] = None
    pe_wall_oi_cr: Optional[float] = None
    pe_unwind_above_spot: bool = False
    ce_unwind_below_spot: bool = False


class EnvironmentScore(BaseModel):
    """
    Full scored output of one evaluation cycle.
    This is the central object — written to Supabase and posted to Discord.
    """

    model_config = ConfigDict(frozen=False)

    timestamp: datetime
    symbol: str
    expiry: date
    dte: int                              # days to expiry
    score: int                            # total points scored (0–8)
    max_score: int = 8
    status: str                           # "GO", "CAUTION", "AVOID"
    iv_capped: bool = False               # True if IV contraction cap was applied
    total_ce_oi_change: int = 0           # Aggregate CE OI change across cluster
    total_pe_oi_change: int = 0           # Aggregate PE OI change across cluster
    conditions: list[ConditionResult]     # all 7 condition results
    oi_flow_result: Optional[OIFlowResult] = None  # Rich C3 output for Discord formatting
    summary_raw: str                      # Python-generated template string
    summary: str = ""                     # Gemini-polished (filled by Discord Orchestrator)
    previous_status: Optional[str] = None # previous cycle's status for change detection

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("GO", "CAUTION", "AVOID"):
            raise ValueError(f"status must be GO, CAUTION, or AVOID, got {v}")
        return v

    @property
    def state_changed(self) -> bool:
        """True if environment status changed from previous cycle."""
        return self.previous_status != self.status

    @property
    def status_emoji(self) -> str:
        """Emoji for the current status."""
        return {"GO": "🟢", "CAUTION": "🟡", "AVOID": "🔴"}.get(self.status, "⚪")

    def get_condition(self, name: str) -> Optional[ConditionResult]:
        """Retrieve a specific condition result by name."""
        return next((c for c in self.conditions if c.name == name), None)


class HealthStatus(BaseModel):
    """System health snapshot posted to #system-check every heartbeat."""

    model_config = ConfigDict(frozen=False)

    timestamp: datetime
    is_fetching: bool             # True if last fetch succeeded
    last_fetch_time: Optional[datetime]
    dhan_api_ok: bool
    supabase_ok: bool
    cycle_count: int              # cycles completed this session
    symbol: str
    expiry: Optional[date]
    warmup_complete: bool         # True once all buffers have enough data
    warmup_cycles_remaining: int  # 0 when fully warmed up
