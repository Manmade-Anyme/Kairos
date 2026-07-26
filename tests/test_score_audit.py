"""
Tests for execution/score_audit.py — pure parsing/aggregation functions only.
No network or Supabase client involvement.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "execution")))

from score_audit import (
    build_report,
    condition_rates,
    green_cooccurrence,
    parse_summary_metrics,
    score_histogram,
)

# A realistic summary_raw exactly as engine._build_summary_raw emits it.
SAMPLE_SUMMARY = (
    "DTE=6 | Score=6/8\n"
    "🟢 iv_trend: +0.87 — IV expanding (DTE≥3) [2/2]\n"
    "🟡 momentum: 0.18% range, no vol spike, trend 3/5 down [0/1]\n"
    "🔴 oi_flow: Mixed signals — consensus not met (2/8 green cycles) [0/1]\n"
    "🟢 gamma_theta: Ratio 1.989443 (DTE≥3 scale) — Gamma favored [1/1]\n"
    "🟢 pdhl_breakout: Below PDL 24136 — bearish breakout [1/1]\n"
    "🟢 move_ratio: Ratio 4.34 (realized 0.47% / implied 0.11%) — delivering > priced [1/1]\n"
    "🟢 vwap_distance: 0.15% below VWAP 24092 — room to run [1/1]\n"
    "📐 c3_raw: gex=+0.42 nde=-0.07 pcr=0.93"
)


def _row(score, dte=6, summary_raw="", **statuses):
    """Build one environment_log row, defaulting every condition status so tests set only what they assert on."""
    defaults = {
        "iv_trend": "YELLOW",
        "momentum": "YELLOW",
        "oi_flow": "RED",
        "gamma_theta": "GREEN",
        "pdhl_breakout": "RED",
        "move_ratio": "YELLOW",
        "vwap_distance": "GREEN",
    }
    defaults.update(statuses)
    return {"score": score, "dte": dte, "summary_raw": summary_raw, **defaults}


def test_parse_summary_metrics_full():
    """Every metric pattern lifts its raw value out of a full summary_raw block."""
    metrics = parse_summary_metrics(SAMPLE_SUMMARY)
    assert metrics["iv_change"] == 0.87
    assert metrics["momentum_range_pct"] == 0.18
    assert metrics["gamma_theta_ratio"] == 1.989443
    assert metrics["move_ratio"] == 4.34
    assert metrics["vwap_distance_pct"] == 0.15
    assert metrics["c3_gex_ratio"] == 0.42
    assert metrics["c3_nde_ratio"] == -0.07
    assert metrics["c3_pcr"] == 0.93


def test_parse_summary_metrics_empty():
    """An absent or empty summary_raw yields no metrics rather than raising."""
    assert parse_summary_metrics("") == {}
    assert parse_summary_metrics(None or "") == {}


def test_score_histogram():
    """Cycles are tallied per total score and returned in ascending score order."""
    rows = [_row(5), _row(6), _row(6), _row(3)]
    assert score_histogram(rows) == {3: 1, 5: 1, 6: 2}


def test_condition_rates():
    """Per-condition GREEN/YELLOW/RED counts are tallied independently for each column."""
    rows = [_row(6, momentum="GREEN"), _row(5, momentum="RED")]
    rates = condition_rates(rows)
    assert rates["momentum"] == {"GREEN": 1, "RED": 1}
    assert rates["gamma_theta"] == {"GREEN": 2}


def test_green_cooccurrence_detects_conflict():
    """Co-occurrence reports P(other GREEN | base GREEN), exposing structurally conflicting conditions."""
    # vwap_distance GREEN in 3 rows; momentum GREEN in only 1 of those.
    rows = [
        _row(6, vwap_distance="GREEN", momentum="GREEN"),
        _row(5, vwap_distance="GREEN", momentum="RED"),
        _row(5, vwap_distance="GREEN", momentum="RED"),
        _row(4, vwap_distance="RED", momentum="GREEN"),
    ]
    matrix = green_cooccurrence(rows)
    assert matrix["vwap_distance"]["momentum"] == round(1 / 3, 3)


def test_build_report_contains_sections():
    """The rendered report carries the observed maximum, per-condition rates, co-occurrence, and parsed metrics."""
    rows = [_row(6, summary_raw=SAMPLE_SUMMARY), _row(5)]
    report = build_report(rows, days=30)
    assert "Observed maximum score:** 6/8" in report
    assert "Per-condition status rates" in report
    assert "Green co-occurrence" in report
    assert "gamma_theta_ratio" in report


def test_build_report_empty():
    """An empty result set produces an explicit 'no rows' report instead of misleading zeros."""
    report = build_report([], days=7)
    assert "No environment_log rows" in report
