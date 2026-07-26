"""
score_audit.py — Kairos Score Distribution Audit.

Pulls historical scored cycles from the Supabase `environment_log` table and produces
a markdown report answering: what scores actually occur, which conditions block them,
and what raw metric values the thresholds should be calibrated against.

Sections of the report:
    1. Score histogram (overall and per DTE) — reveals the empirical score ceiling.
    2. Per-condition status rates — how often each condition is GREEN/YELLOW/RED.
    3. Pairwise green co-occurrence — P(condition GREEN | other condition GREEN),
       which quantifies structural conflicts (e.g. VWAP distance vs the trend cluster).
    4. Numeric metric distributions parsed from `summary_raw` — the measured inputs
       (IV change, momentum range, gamma/theta ratio, move ratio, VWAP distance)
       that threshold calibration must be based on (ADR-024).

Usage:
    python execution/score_audit.py            # last 30 days
    python execution/score_audit.py --days 7   # last 7 days
"""

import argparse
import asyncio
import os
import re
import sys
from datetime import date
from statistics import mean, median

# Ensure 'src' is in path so we can import 'kairos' without PYTHONPATH setup
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

CONDITION_COLUMNS = [
    "iv_trend",
    "momentum",
    "oi_flow",
    "gamma_theta",
    "pdhl_breakout",
    "move_ratio",
    "vwap_distance",
]

# Patterns to lift raw metric values out of each condition's line in summary_raw.
# Line format (engine._build_summary_raw): "🟢 iv_trend: +0.87 — IV expanding (DTE≥3) [2/2]"
METRIC_PATTERNS = {
    "iv_change": re.compile(r"iv_trend: ([+-]?\d+(?:\.\d+)?)"),
    "momentum_range_pct": re.compile(r"momentum: (\d+(?:\.\d+)?)% range"),
    "gamma_theta_ratio": re.compile(r"gamma_theta: Ratio (\d+(?:\.\d+)?)"),
    "move_ratio": re.compile(r"move_ratio: Ratio (\d+(?:\.\d+)?)"),
    "vwap_distance_pct": re.compile(r"vwap_distance: (\d+(?:\.\d+)?)% (?:above|below)"),
    # C3 raw telemetry line (ADR-025): "📐 c3_raw: gex=+0.42 nde=-0.07 pcr=0.93"
    "c3_gex_ratio": re.compile(r"c3_raw: gex=([+-]?\d+(?:\.\d+)?)"),
    "c3_nde_ratio": re.compile(r"c3_raw:.* nde=([+-]?\d+(?:\.\d+)?)"),
    "c3_pcr": re.compile(r"c3_raw:.* pcr=(\d+(?:\.\d+)?)"),
}


def parse_summary_metrics(summary_raw: str) -> dict[str, float]:
    """
    Extract raw numeric metric values from one cycle's summary_raw string.

    :param summary_raw: The machine-readable per-cycle summary stored by the engine.
    :return: Mapping of metric name → parsed float, for every metric found.
    """
    metrics: dict[str, float] = {}
    if not summary_raw:
        return metrics
    for name, pattern in METRIC_PATTERNS.items():
        match = pattern.search(summary_raw)
        if match:
            metrics[name] = float(match.group(1))
    return metrics


def score_histogram(rows: list[dict]) -> dict[int, int]:
    """Count cycles per total score (0-8)."""
    hist: dict[int, int] = {}
    for row in rows:
        score = row.get("score")
        if score is not None:
            hist[score] = hist.get(score, 0) + 1
    return dict(sorted(hist.items()))


def condition_rates(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Count GREEN/YELLOW/RED occurrences per condition column."""
    rates: dict[str, dict[str, int]] = {c: {} for c in CONDITION_COLUMNS}
    for row in rows:
        for cond in CONDITION_COLUMNS:
            status = row.get(cond)
            if status:
                rates[cond][status] = rates[cond].get(status, 0) + 1
    return rates


def green_cooccurrence(rows: list[dict]) -> dict[str, dict[str, float]]:
    """
    P(column GREEN | row-condition GREEN) for every ordered pair of conditions.
    Low off-diagonal values expose structurally conflicting conditions.
    """
    matrix: dict[str, dict[str, float]] = {}
    for base in CONDITION_COLUMNS:
        base_green = [r for r in rows if r.get(base) == "GREEN"]
        matrix[base] = {}
        for other in CONDITION_COLUMNS:
            if other == base or not base_green:
                continue
            both = sum(1 for r in base_green if r.get(other) == "GREEN")
            matrix[base][other] = round(both / len(base_green), 3)
    return matrix


def metric_distributions(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Summary statistics (min/median/mean/max) per parsed raw metric."""
    values: dict[str, list[float]] = {}
    for row in rows:
        for name, value in parse_summary_metrics(row.get("summary_raw") or "").items():
            values.setdefault(name, []).append(value)

    stats: dict[str, dict[str, float]] = {}
    for name, series in values.items():
        stats[name] = {
            "count": len(series),
            "min": round(min(series), 4),
            "median": round(median(series), 4),
            "mean": round(mean(series), 4),
            "max": round(max(series), 4),
        }
    return stats


def build_report(rows: list[dict], days: int) -> str:
    """Render the full markdown audit report from environment_log rows."""
    lines = [
        f"# Kairos Score Audit — last {days} days",
        f"**Cycles analyzed:** {len(rows)}",
        "",
    ]

    if not rows:
        lines.append("_No environment_log rows found for the selected window._")
        return "\n".join(lines)

    # 1. Score histogram
    hist = score_histogram(rows)
    observed_max = max(hist) if hist else 0
    lines += [
        "## 1. Score distribution",
        f"**Observed maximum score:** {observed_max}/8",
        "",
        "| Score | Cycles | Share |",
        "|---|---|---|",
    ]
    total = sum(hist.values())
    for score, count in hist.items():
        lines.append(f"| {score}/8 | {count} | {count / total:.1%} |")

    # Per-DTE observed max
    per_dte: dict[int, int] = {}
    for row in rows:
        dte = row.get("dte")
        if dte is not None and row.get("score") is not None:
            per_dte[dte] = max(per_dte.get(dte, 0), row["score"])
    if per_dte:
        lines += ["", "**Observed max per DTE:** "
                  + ", ".join(f"DTE {d}: {s}/8" for d, s in sorted(per_dte.items()))]

    # 2. Condition rates
    lines += ["", "## 2. Per-condition status rates", "", "| Condition | GREEN | YELLOW | RED |", "|---|---|---|---|"]
    for cond, counts in condition_rates(rows).items():
        n = sum(counts.values()) or 1
        lines.append(
            f"| {cond} | {counts.get('GREEN', 0) / n:.1%} "
            f"| {counts.get('YELLOW', 0) / n:.1%} "
            f"| {counts.get('RED', 0) / n:.1%} |"
        )

    # 3. Co-occurrence matrix
    lines += [
        "",
        "## 3. Green co-occurrence — P(column GREEN | row GREEN)",
        "",
        "| given ↓ / then → | " + " | ".join(CONDITION_COLUMNS) + " |",
        "|---" * (len(CONDITION_COLUMNS) + 1) + "|",
    ]
    matrix = green_cooccurrence(rows)
    for base in CONDITION_COLUMNS:
        cells = [
            "—" if other == base else f"{matrix[base].get(other, 0):.0%}" if matrix[base] else "n/a"
            for other in CONDITION_COLUMNS
        ]
        lines.append(f"| **{base}** | " + " | ".join(cells) + " |")

    # 4. Raw metric distributions
    lines += ["", "## 4. Raw metric distributions (threshold calibration inputs)", "",
              "| Metric | Count | Min | Median | Mean | Max |", "|---|---|---|---|---|---|"]
    for name, s in metric_distributions(rows).items():
        lines.append(
            f"| {name} | {s['count']} | {s['min']} | {s['median']} | {s['mean']} | {s['max']} |"
        )

    lines.append("")
    return "\n".join(lines)


async def main() -> None:
    """Fetch environment_log history and write the markdown audit report."""
    parser = argparse.ArgumentParser(description="Kairos score distribution audit")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    args = parser.parse_args()

    # Settings validation happens at import time — a missing .env raises here.
    try:
        from kairos.config import settings  # noqa: F401
        from kairos.db import db
    except Exception as e:
        print("Could not load Kairos settings — SUPABASE_URL / SUPABASE_KEY missing?")
        print("Set them in .env (see .env.example) and re-run.")
        print(f"({e.__class__.__name__}: {e})")
        sys.exit(1)

    await db.start()
    try:
        rows = await db.fetch_environment_log(days=args.days)
    except Exception as e:
        print(f"Failed to fetch environment_log history — aborting audit: {e}")
        sys.exit(1)
    finally:
        await db.stop()

    report = build_report(rows, args.days)

    os.makedirs("reports", exist_ok=True)
    out_path = os.path.join("reports", f"score_audit_{date.today().isoformat()}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"Analyzed {len(rows)} cycles — report written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
