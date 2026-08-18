"""
config.py — All environment variables, constants, and scoring thresholds.
Single source of truth. Every threshold in the system lives here.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Dhan API ────────────────────────────────────────────────────────
    dhan_client_id: str | None = None
    dhan_access_token: str | None = None
    dhan_base_url: str = "https://api.dhan.co/v2"

    # ── Supabase ─────────────────────────────────────────────────────────
    supabase_url: str
    supabase_key: str

    # ── Discord ──────────────────────────────────────────────────────────
    discord_webhook_url: str          # #environment channel
    discord_health_webhook_url: str   # #system-check channel

    # ── Scheduler ────────────────────────────────────────────────────────
    poll_interval_seconds: int = 60
    heartbeat_interval_seconds: int = 300   # 5 minutes

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file_level: str = "INFO"
    log_rotation: str = "20 MB"
    log_retention: str = "3 days"

    # ── Instrument constants ──────────────────────────────────────────────
    nifty_strike_interval: int = 50
    sensex_strike_interval: int = 100   # Phase 2

    # ── Active session windows (IST, 24h) ────────────────────────────────
    session_1_start: tuple[int, int] = (9, 15)
    session_1_end: tuple[int, int] = (11, 45)
    session_2_start: tuple[int, int] = (13, 0)
    session_2_end: tuple[int, int] = (15, 25)

    # ── In-memory buffer sizes ────────────────────────────────────────────
    candle_buffer_size: int = 20   # max candles kept in RAM
    iv_buffer_size: int = 20       # max IV readings kept in RAM

    # ── Retry config ─────────────────────────────────────────────────────
    api_max_retries: int = 3
    api_retry_delay_seconds: float = 5.0
    stale_signal_threshold_minutes: int = 3   # alert if no cycle for this long

    # ── Condition 1: IV Change Rate — DTE-Scaled (ADR-011) ─────────────────
    # iv_change = ATM_IV_now - ATM_IV_15_minutes_ago
    iv_change_lookback: int = 15          # minutes ago to compare
    # DTE >= 3 (early week — demand real expansion for multi-day holds)
    iv_change_dte_high_green: float = 0.5
    iv_change_dte_high_yellow: float = 0.0
    # DTE = 2 (mid-week — moderate)
    iv_change_dte_mid_green: float = 0.3
    iv_change_dte_mid_yellow: float = -0.2
    # DTE = 0-1 (expiry — accept normal theta-drift, cap only on genuine crush)
    iv_change_dte_low_green: float = 0.2
    iv_change_dte_low_yellow: float = -0.5

    # ── Condition 2: Momentum + Directional Consistency ───────────────────
    momentum_candle_window: int = 5              # candles for range + trend check
    momentum_range_green: float = 0.30           # % range for green

    momentum_range_yellow: float = 0.15          # % range for yellow
    momentum_volume_multiplier: float = 1.5      # volume spike threshold
    momentum_volume_lookback: int = 15           # candles for avg volume
    momentum_trend_count_green: int = 4          # out of 5 candles trending
    momentum_trend_count_yellow: int = 3

    # ── Condition 3: OI Flow & Trend Phase ───────────────────────────────
    oi_lookback_cycles: int = 16        # compare OI against N cycles ago (16 = 15 min delta)
    oi_multi_strike_count: int = 7     # number of strikes on each side of ATM (7 up + 7 down + 1 ATM = 15 total)
    trend_phase_oi_threshold: int = 8000 # min WEIGHTED MEAN OI change across cluster to register buildup/unwinding

    # ── Condition 3: OI Flow Consensus Filter (ADR-021) ──────────────────
    oi_consensus_window: int = 8
    oi_consensus_green_threshold: int = 5
    oi_consensus_trap_threshold: int = 3

    # ── OI Flow (Condition 3) — Full-Chain Greeks Parameters ─────────────
    # These constants power the upgraded Greeks-aware scoring engine.
    # All tuning happens here — no logic changes needed.

    nifty_lot_size: int = 65
    # NIFTY lot size as of 2026. Used in GEX calculation.

    sensex_lot_size: int = 20
    # SENSEX lot size. Used when symbol == "SENSEX".

    oi_flow_strike_window: int = 7
    # ATM ± N strikes for main aggregates (NDE, GEX, theta, PCR).
    # 50pt spacing → covers ±350pts from ATM.

    oi_flow_vega_window: int = 3
    # ATM ± N strikes for vega exposure and IV skew.
    # Tighter window because vega concentrates near ATM.

    gex_pin_pct: float = 0.20
    # |net_gex| > total_abs_gex * 0.20 -> pin/trend

    nde_pct_threshold: float = 0.20
    # |nde| > total_abs_nde * 0.20 -> confirms/contradicts

    vega_high_pct: float = 0.40
    # atm_vega_exposure > total_abs_vega * 0.40 -> vega trap

    vega_trap_iv_threshold: float = 0.0
    # iv_change_rate must be BELOW this for vega trap to activate.
    # 0.0 = any IV contraction. Raise to e.g. -0.30 for more lenience.

    theta_dominant_threshold: float = 500_000_000.0
    # theta_burn_rate above this = writers deeply entrenched.
    # Floors score at 0 if NDE is not confirming (soft cap).

    pcr_bearish_threshold: float = 0.95
    # PCR below this = put writers absent = structurally bearish.

    pcr_bullish_threshold: float = 1.05
    # PCR above this = put writers defending = structurally bullish.

    # ── Condition 4: Gamma/Theta — DTE-scaled ────────────────────────────
    # DTE >= 3 (lenient — early week, plenty of time)
    gamma_theta_dte_high_green: float = 0.00008    # DTE≥3 lenient
    gamma_theta_dte_high_yellow: float = 0.00004
    # DTE = 2 (standard)
    gamma_theta_dte_mid_green: float = 0.00012     # DTE=2 standard
    gamma_theta_dte_mid_yellow: float = 0.00006
    # DTE = 1 (strict — expiry day, theta ruthless)
    gamma_theta_dte_low_green: float = 0.00020     # DTE=1 strict — expiry day
    gamma_theta_dte_low_yellow: float = 0.00008

    # ── Condition 5: PDH/PDL Breakout ─────────────────────────────────────
    pdhl_near_band_pct: float = 0.10   # % within PDH/PDL = "near breakout"

    # ── Condition 6: Move Ratio (15-min realized vs implied) ─────────────
    move_ratio_green: float = 1.0
    move_ratio_yellow: float = 0.7
    move_ratio_lookback_candles: int = 15   # must match candle_buffer_size

    # ── Condition 7: VWAP Distance ───────────────────────────────────────
    vwap_distance_green: float = 0.20    # % from VWAP
    vwap_distance_yellow: float = 0.40

    # ── Scoring thresholds ───────────────────────────────────────────────
    # Total max score = 8 (IV=2, all others=1 each)
    score_go_min: int = 7        # 7–8 → GO
    score_caution_min: int = 4   # 4–6 → CAUTION; 0–3 → AVOID



# Module-level singleton — import this everywhere
settings = Settings()
