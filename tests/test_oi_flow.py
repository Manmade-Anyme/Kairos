"""
test_oi_flow.py — Tests for upgraded Condition 3 (OI Flow) Greeks-aware scoring engine.

Tests cover:
  1. Vega trap fires correctly (3 tests)
  2. NDE contradiction fires correctly (3 tests)
  3. GEX pin caps score (2 tests)
  4. All conditions required for green (4 tests)
  5. Priority ordering B > C > A (2 tests)
  6. Warmup handling (1 test)
"""

from collections import deque

from kairos.models import TrendPhase, OIFlowResult
from kairos.processor import score_oi_flow, consolidate_oi_flow


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build a cluster with specific Greeks state
# ─────────────────────────────────────────────────────────────────────────────

def _cluster(
    make_cluster,
    make_candle,
    *,
    spot=22010,
    ce_oi=5000,
    pe_oi=4000,
    price_change=10.0,
    net_gex=0.0,
    net_delta_exposure=0.0,
    atm_vega_exposure=0.0,
    theta_burn_rate=0.0,
    pcr=1.0,
    iv_skew=0.0,
    pe_unwind_above_spot=False,
    ce_unwind_below_spot=False,
):
    """Convenience wrapper to build a cluster + candle_buffer with Greeks set."""
    buf = deque([make_candle(spot - price_change)] * 6, maxlen=15)
    cluster = make_cluster(
        spot,
        ce_oi_change=ce_oi,
        pe_oi_change=pe_oi,
        price_change=price_change,
        net_gex=net_gex,
        net_delta_exposure=net_delta_exposure,
        atm_vega_exposure=atm_vega_exposure,
        theta_burn_rate=theta_burn_rate,
        pcr=pcr,
        iv_skew=iv_skew,
        pe_unwind_above_spot=pe_unwind_above_spot,
        ce_unwind_below_spot=ce_unwind_below_spot,
    )
    return cluster, buf


# ═════════════════════════════════════════════════════════════════════════════
# 1. VEGA TRAP TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestVegaTrap:
    """Vega trap: high vega exposure + IV contraction → hard RED, score=0."""

    def test_vega_trap_fires_on_contraction(self, make_cluster, make_candle):
        """High vega + negative IV change → vega trap."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            atm_vega_exposure=30_000_000.0,  # > 25M threshold
            net_gex=-2_000_000.0,            # trending
            net_delta_exposure=-500_000.0,    # confirms bearish
            pcr=0.90,                        # confirms bearish
            price_change=-10.0,              # bearish
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=-0.5, candle_buffer=buf)
        assert cond.points == 0
        assert oi.vega_trap is True
        assert "Vega trap" in oi.reason

    def test_vega_trap_no_fire_on_expansion(self, make_cluster, make_candle):
        """High vega but positive IV → no trap."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            atm_vega_exposure=30_000_000.0,
            net_gex=-2_000_000.0,
            net_delta_exposure=-500_000.0,
            pcr=0.90,
            price_change=-10.0,
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=+0.5, candle_buffer=buf)
        assert oi.vega_trap is False

    def test_vega_trap_no_fire_on_low_vega(self, make_cluster, make_candle):
        """Low vega + contraction → no trap (below threshold)."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            atm_vega_exposure=10_000_000.0,  # < 25M threshold
            price_change=-10.0,
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=-0.5, candle_buffer=buf)
        assert oi.vega_trap is False


# ═════════════════════════════════════════════════════════════════════════════
# 2. NDE CONTRADICTION TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestNDEContradiction:
    """NDE contradicting phase → score=0."""

    def test_nde_contradicts_bearish_phase(self, make_cluster, make_candle):
        """Bearish phase but NDE is positive (net long delta) → contradiction."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=-10.0,              # bearish
            net_delta_exposure=500_000.0,     # net LONG delta — contradicts
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.0, candle_buffer=buf)
        assert cond.points == 0
        assert oi.nde_state == "contradicts"
        assert "NDE contradicts" in oi.reason

    def test_nde_contradicts_bullish_phase(self, make_cluster, make_candle):
        """Bullish phase but NDE is negative (net short delta) → contradiction."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=10.0,               # bullish
            net_delta_exposure=-500_000.0,    # net SHORT delta — contradicts
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.0, candle_buffer=buf)
        assert cond.points == 0
        assert oi.nde_state == "contradicts"

    def test_nde_confirms_bearish_phase(self, make_cluster, make_candle):
        """Bearish phase with negative NDE → confirms (no contradiction)."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=-10.0,
            net_delta_exposure=-500_000.0,    # net short delta — confirms below
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.0, candle_buffer=buf)
        assert oi.nde_state == "confirms"


# ═════════════════════════════════════════════════════════════════════════════
# 3. GEX PIN TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestGEXPin:
    """GEX pin active → dealers absorbing, score=0."""

    def test_gex_pin_caps_score(self, make_cluster, make_candle):
        """Net GEX above pin threshold → score=0 regardless of other signals."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            net_gex=2_000_000.0,             # > 1.3M pin threshold
            net_delta_exposure=-500_000.0,    # would confirm if not pinned
            pcr=0.90,                        # would confirm
            price_change=-10.0,
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.5, candle_buffer=buf)
        assert cond.points == 0
        assert oi.gex_state == "pin"
        assert "GEX pin" in oi.reason

    def test_gex_trend_detected(self, make_cluster, make_candle):
        """Net GEX below -threshold → trend state."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            net_gex=-2_000_000.0,            # < -1.3M trend threshold
            price_change=-10.0,
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.5, candle_buffer=buf)
        assert oi.gex_state == "trend"


# ═════════════════════════════════════════════════════════════════════════════
# 4. FULL GREEN (RULE D) TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestFullGreen:
    """All conditions aligned → score=1 (Rule D)."""

    def test_bearish_full_conviction(self, make_cluster, make_candle):
        """GEX trend + NDE confirms + PCR bearish → score=1."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=-10.0,              # bearish (Short Buildup with OI up)
            net_gex=-2_000_000.0,            # trending
            net_delta_exposure=-500_000.0,   # confirms bearish
            pcr=0.90,                        # < 0.95 = put writers absent = bearish
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.5, candle_buffer=buf)
        assert cond.points == 1
        assert cond.status == "GREEN"
        assert oi.score == 1
        assert "bearish" in oi.reason.lower()

    def test_bullish_full_conviction(self, make_cluster, make_candle):
        """GEX trend + NDE confirms + PCR bullish → score=1."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=10.0,               # bullish (Long Buildup with OI up)
            net_gex=-2_000_000.0,            # trending
            net_delta_exposure=500_000.0,    # confirms bullish
            pcr=1.10,                        # > 1.05 = put writers defending = bullish
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.5, candle_buffer=buf)
        assert cond.points == 1
        assert cond.status == "GREEN"
        assert oi.score == 1

    def test_missing_gex_trend_stays_red(self, make_cluster, make_candle):
        """NDE confirms + PCR aligned but GEX neutral → score=0 (mixed signals)."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=-10.0,
            net_gex=0.0,                     # neutral (not trending)
            net_delta_exposure=-500_000.0,
            pcr=0.90,
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.5, candle_buffer=buf)
        assert cond.points == 0
        assert "Mixed signals" in oi.reason

    def test_missing_pcr_stays_red(self, make_cluster, make_candle):
        """GEX trend + NDE confirms but PCR balanced → score=0."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=-10.0,
            net_gex=-2_000_000.0,
            net_delta_exposure=-500_000.0,
            pcr=1.00,                        # balanced, doesn't confirm bearish
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.5, candle_buffer=buf)
        assert cond.points == 0


# ═════════════════════════════════════════════════════════════════════════════
# 5. PRIORITY ORDERING TESTS (B > C > A)
# ═════════════════════════════════════════════════════════════════════════════

class TestPriorityOrdering:
    """Rule priority: B (vega trap) > C (NDE contra) > A (GEX pin)."""

    def test_vega_trap_overrides_nde_and_gex(self, make_cluster, make_candle):
        """Even with GEX pin and NDE contradiction, vega trap takes priority."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=-10.0,
            atm_vega_exposure=30_000_000.0,  # triggers vega trap
            net_gex=2_000_000.0,             # would trigger GEX pin
            net_delta_exposure=500_000.0,    # would trigger NDE contradiction
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=-0.5, candle_buffer=buf)
        assert oi.vega_trap is True
        assert "Vega trap" in oi.reason

    def test_nde_contradiction_overrides_gex_pin(self, make_cluster, make_candle):
        """NDE contradiction (Rule C) takes priority over GEX pin (Rule A)."""
        cluster, buf = _cluster(
            make_cluster, make_candle,
            price_change=-10.0,              # bearish
            net_gex=2_000_000.0,             # GEX pin active
            net_delta_exposure=500_000.0,    # net long delta — contradicts bearish
        )
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.5, candle_buffer=buf)
        assert "NDE contradicts" in oi.reason
        # GEX pin should NOT be the reason since NDE contra has higher priority
        assert "GEX pin" not in oi.reason


# ═════════════════════════════════════════════════════════════════════════════
# 6. WARMUP TEST
# ═════════════════════════════════════════════════════════════════════════════

class TestWarmup:
    """Warmup handling before enough candles exist."""

    def test_warmup_returns_caution(self, make_cluster, make_candle):
        """Insufficient candle_buffer → YELLOW with warmup message."""
        cluster = make_cluster(22000, ce_oi_change=1000, pe_oi_change=1000)
        buf = deque([make_candle(22000)] * 2, maxlen=15)  # Only 2, need 6
        cond, oi = score_oi_flow(cluster, iv_change_rate=0.0, candle_buffer=buf)
        assert cond.status == "YELLOW"
        assert "Warming up" in cond.detail
        assert oi.phase == TrendPhase.NEUTRAL


# ═════════════════════════════════════════════════════════════════════════════
# 7. CONSENSUS FILTER TESTS (ADR-021)
# ═════════════════════════════════════════════════════════════════════════════

class TestConsensusFilter:
    """Consensus voting filter tests."""

    def test_empty_buffer_returns_warmup(self):
        buf = deque(maxlen=8)
        cond, oi = consolidate_oi_flow(buf)
        assert cond.status == "YELLOW"
        assert "Warming up" in cond.detail
        assert oi.score == 0

    def test_green_consensus_met(self):
        buf = deque(maxlen=8)
        # 5 green, 3 red
        for _ in range(3):
            buf.append(OIFlowResult(score=0, phase=TrendPhase.NEUTRAL, reason="No trend", gex_state="neutral", nde_state="neutral", vega_trap=False, pcr=1.0, iv_skew=0.0))
        for _ in range(5):
            buf.append(OIFlowResult(score=1, phase=TrendPhase.LONG_BUILDUP, reason="Green buildup", gex_state="trend", nde_state="confirms", vega_trap=False, pcr=1.1, iv_skew=0.0))

        cond, oi = consolidate_oi_flow(buf)
        assert cond.status == "GREEN"
        assert cond.points == 1
        assert oi.score == 1
        assert oi.phase == TrendPhase.LONG_BUILDUP
        assert "Consensus 5/8" in cond.detail

    def test_green_consensus_not_met(self):
        buf = deque(maxlen=8)
        # 4 green, 4 red
        for _ in range(4):
            buf.append(OIFlowResult(score=0, phase=TrendPhase.NEUTRAL, reason="No trend", gex_state="neutral", nde_state="neutral", vega_trap=False, pcr=1.0, iv_skew=0.0))
        for _ in range(4):
            buf.append(OIFlowResult(score=1, phase=TrendPhase.LONG_BUILDUP, reason="Green buildup", gex_state="trend", nde_state="confirms", vega_trap=False, pcr=1.1, iv_skew=0.0))

        cond, oi = consolidate_oi_flow(buf)
        assert cond.status == "RED"
        assert cond.points == 0
        assert oi.score == 0
        assert "consensus not met" in cond.detail

    def test_vega_trap_override(self):
        buf = deque(maxlen=8)
        # 5 green, but 3 of the red/green cycles have vega trap
        for _ in range(5):
            buf.append(OIFlowResult(score=1, phase=TrendPhase.LONG_BUILDUP, reason="Green", gex_state="trend", nde_state="confirms", vega_trap=False, pcr=1.1, iv_skew=0.0))
        for _ in range(2):
            buf.append(OIFlowResult(score=0, phase=TrendPhase.LONG_BUILDUP, reason="Vega trap", gex_state="trend", nde_state="confirms", vega_trap=True, pcr=1.1, iv_skew=0.0))
        for _ in range(1):
            buf.append(OIFlowResult(score=1, phase=TrendPhase.LONG_BUILDUP, reason="Vega trap", gex_state="trend", nde_state="confirms", vega_trap=True, pcr=1.1, iv_skew=0.0))
        # Total vega_trap = 3. Even though green_count = 6 (could be close to green), vega trap count = 3 forces RED.
        
        cond, oi = consolidate_oi_flow(buf)
        assert cond.status == "RED"
        assert cond.points == 0
        assert oi.score == 0
        assert "Vega trap active (3/8 cycles)" in cond.detail

    def test_gex_pin_override(self):
        buf = deque(maxlen=8)
        # 3 gex pins
        for _ in range(5):
            buf.append(OIFlowResult(score=1, phase=TrendPhase.LONG_BUILDUP, reason="Green", gex_state="trend", nde_state="confirms", vega_trap=False, pcr=1.1, iv_skew=0.0))
        for _ in range(3):
            buf.append(OIFlowResult(score=0, phase=TrendPhase.LONG_BUILDUP, reason="Gex Pin", gex_state="pin", nde_state="confirms", vega_trap=False, pcr=1.1, iv_skew=0.0))

        cond, oi = consolidate_oi_flow(buf)
        assert cond.status == "RED"
        assert cond.points == 0
        assert oi.score == 0
        assert "GEX pin active (3/8 cycles)" in cond.detail

    def test_mode_phase_wins(self):
        buf = deque(maxlen=8)
        # 3 Long Buildup, 2 Short Buildup, 3 Neutral
        # To resolve tie, check unique_phases reversed (most recent first wins)
        buf.append(OIFlowResult(score=0, phase=TrendPhase.NEUTRAL, reason="N", gex_state="n", nde_state="n", vega_trap=False, pcr=1.0, iv_skew=0.0))
        buf.append(OIFlowResult(score=0, phase=TrendPhase.NEUTRAL, reason="N", gex_state="n", nde_state="n", vega_trap=False, pcr=1.0, iv_skew=0.0))
        buf.append(OIFlowResult(score=0, phase=TrendPhase.NEUTRAL, reason="N", gex_state="n", nde_state="n", vega_trap=False, pcr=1.0, iv_skew=0.0))
        
        buf.append(OIFlowResult(score=1, phase=TrendPhase.SHORT_BUILDUP, reason="S", gex_state="t", nde_state="c", vega_trap=False, pcr=0.9, iv_skew=0.0))
        buf.append(OIFlowResult(score=1, phase=TrendPhase.SHORT_BUILDUP, reason="S", gex_state="t", nde_state="c", vega_trap=False, pcr=0.9, iv_skew=0.0))
        
        buf.append(OIFlowResult(score=1, phase=TrendPhase.LONG_BUILDUP, reason="L", gex_state="t", nde_state="c", vega_trap=False, pcr=1.1, iv_skew=0.0))
        buf.append(OIFlowResult(score=1, phase=TrendPhase.LONG_BUILDUP, reason="L", gex_state="t", nde_state="c", vega_trap=False, pcr=1.1, iv_skew=0.0))
        buf.append(OIFlowResult(score=1, phase=TrendPhase.LONG_BUILDUP, reason="L", gex_state="t", nde_state="c", vega_trap=False, pcr=1.1, iv_skew=0.0))
        # Counts: Long Buildup=3, Neutral=3, Short Buildup=2
        # Ties between Neutral and Long Buildup. Since Long Buildup is the most recent (at the end), it should win.
        cond, oi = consolidate_oi_flow(buf)
        assert oi.phase == TrendPhase.LONG_BUILDUP

