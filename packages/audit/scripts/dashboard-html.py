#!/usr/bin/env python3
"""Generate an HTML dashboard from audit event logs."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# sys.path: hooks dir (event_logger) + scripts dir (dashboard_stats)
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOOKS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from dashboard_stats import (  # noqa: E402
    calc_cli_stats,
    calc_event_distribution,
    calc_quality_stats,
    calc_route_stats,
    calc_session_stats,
    calc_subagent_stats,
)
from event_logger import iter_session_events  # noqa: E402

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
COLORS = {
    "bg": "#0d1117",
    "card": "#161b22",
    "border": "#30363d",
    "text": "#c9d1d9",
    "text_secondary": "#8b949e",
    "blue": "#58a6ff",
    "green": "#3fb950",
    "red": "#f85149",
    "yellow": "#d29922",
    "purple": "#bc8cff",
    "orange": "#f0883e",
    "cyan": "#39d2c0",
}

CHART_COLORS = [
    COLORS["blue"],
    COLORS["green"],
    COLORS["yellow"],
    COLORS["purple"],
    COLORS["orange"],
    COLORS["cyan"],
    COLORS["red"],
]


# ---------------------------------------------------------------------------
# Safe stat helpers — never raise
# ---------------------------------------------------------------------------
def _safe_calc(fn: Callable[[list[dict]], dict], events: list[dict]) -> dict:
    """Call a calc function and return empty dict on failure."""
    try:
        return fn(events)
    except (KeyError, TypeError, ValueError):
        logger.warning("calc function %s failed", fn.__name__, exc_info=True)
        return {}


def _pct(value: float) -> str:
    """Format a percentage with one decimal."""
    return f"{value:.1f}"


def _esc(text: str) -> str:
    """HTML-escape a string."""
    return html.escape(str(text))


def _js_dumps(obj: object) -> str:
    """JSON-encode for safe embedding inside a <script> block."""
    return json.dumps(obj).replace("</", r"<\/")


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------
def _summary_card(label: str, value: str | int | float, color: str = COLORS["blue"]) -> str:
    return f"""
    <div class="card summary-card">
      <div class="summary-value" style="color:{color}">{_esc(str(value))}</div>
      <div class="summary-label">{_esc(label)}</div>
    </div>"""


def _section(title: str, body: str) -> str:
    return f"""
    <section class="card">
      <h2>{_esc(title)}</h2>
      {body}
    </section>"""


def _no_data() -> str:
    return '<p class="no-data">No data available</p>'


def _chart(chart_id: str, width: str = "100%", height: str = "260px") -> str:
    return f'<div class="chart-wrapper"><canvas id="{chart_id}" style="width:{width};max-height:{height}"></canvas></div>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return _no_data()
    ths = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    trs = ""
    for row in rows:
        tds = "".join(f"<td>{_esc(str(c))}</td>" for c in row)
        trs += f"<tr>{tds}</tr>\n"
    return f"<table><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def _build_summary_cards(
    session: dict,
    route: dict,
    cli: dict,
    quality: dict,
    total_events: int,
) -> str:
    sessions = session.get("total_sessions", 0)
    accuracy = route.get("match_rate", 0.0)
    cli_total = cli.get("total", 0)
    q_total = quality.get("total", 0)
    pass_rate = (quality.get("passed", 0) / q_total * 100) if q_total else 0.0

    accuracy_color = (
        COLORS["green"] if accuracy >= 80 else COLORS["yellow"] if accuracy >= 50 else COLORS["red"]
    )
    pass_color = (
        COLORS["green"]
        if pass_rate >= 80
        else COLORS["yellow"]
        if pass_rate >= 50
        else COLORS["red"]
    )

    return f"""
    <div class="summary-row">
      {_summary_card("Sessions", sessions)}
      {_summary_card("Total Events", total_events)}
      {_summary_card("Routing Accuracy", f"{_pct(accuracy)}%", accuracy_color)}
      {_summary_card("CLI Calls", cli_total)}
      {_summary_card("Quality Pass Rate", f"{_pct(pass_rate)}%", pass_color)}
    </div>"""


def _build_routing_section(route: dict) -> str:
    total = route.get("total", 0)
    if total == 0:
        return _section("Routing Accuracy", _no_data())

    matched = route.get("matched", 0)
    mismatched = route.get("mismatched", 0)
    body = _chart("routeChart") + _table(
        ["Metric", "Count"],
        [
            ["Total Decisions", str(total)],
            ["Matched", str(matched)],
            ["Mismatched", str(mismatched)],
        ],
    )
    return _section("Routing Accuracy", body)


def _build_cli_section(cli: dict) -> str:
    total = cli.get("total", 0)
    if total == 0:
        return _section("CLI Usage", _no_data())

    rows = [
        ["Codex", str(cli.get("codex", 0))],
        ["Gemini", str(cli.get("gemini", 0))],
        ["Success", str(cli.get("success", 0))],
        ["Success Rate", f"{_pct(cli.get('success_rate', 0.0))}%"],
    ]
    errors = cli.get("errors_by_type", {})
    if errors:
        for etype, count in sorted(errors.items(), key=lambda x: -x[1]):
            rows.append([f"Error: {etype}", str(count)])

    body = _chart("cliChart") + _table(["Metric", "Value"], rows)
    return _section("CLI Usage", body)


def _build_subagent_section(sub: dict) -> str:
    starts = sub.get("total_starts", 0)
    if starts == 0:
        return _section("Subagent Activity", _no_data())

    by_type = sub.get("by_agent_type", {})
    rows = [[t, str(c)] for t, c in sorted(by_type.items(), key=lambda x: -x[1])]
    body = _chart("subagentChart") + _table(["Agent Type", "Count"], rows)
    return _section("Subagent Activity", body)


def _build_quality_section(quality: dict) -> str:
    total = quality.get("total", 0)
    if total == 0:
        return _section("Quality Gate", _no_data())

    body = _chart("qualityChart") + _table(
        ["Result", "Count"],
        [["Passed", str(quality.get("passed", 0))], ["Failed", str(quality.get("failed", 0))]],
    )
    return _section("Quality Gate", body)


def _build_distribution_section(dist: dict) -> str:
    if not dist:
        return _section("Event Distribution", _no_data())

    rows = [[k, str(v)] for k, v in sorted(dist.items(), key=lambda x: -x[1])]
    body = _chart("distChart", height="320px") + _table(["Event Type", "Count"], rows)
    return _section("Event Distribution", body)


# ---------------------------------------------------------------------------
# Chart.js config builder
# ---------------------------------------------------------------------------
def _build_chart_scripts(route: dict, cli: dict, sub: dict, quality: dict, dist: dict) -> str:
    """Return <script> block with Chart.js initialisations."""
    charts: list[str] = []

    # Routing doughnut
    if route.get("total", 0) > 0:
        charts.append(f"""
        new Chart(document.getElementById('routeChart'), {{
          type: 'doughnut',
          data: {{
            labels: ['Matched', 'Mismatched'],
            datasets: [{{ data: [{route.get("matched", 0)}, {route.get("mismatched", 0)}],
                          backgroundColor: ['{COLORS["green"]}', '{COLORS["red"]}'] }}]
          }},
          options: {{ responsive: true, plugins: {{ legend: {{ labels: {{ color: '{COLORS["text"]}' }} }} }} }}
        }});""")

    # CLI bar
    if cli.get("total", 0) > 0:
        charts.append(f"""
        new Chart(document.getElementById('cliChart'), {{
          type: 'bar',
          data: {{
            labels: ['Codex', 'Gemini'],
            datasets: [{{ label: 'Calls', data: [{cli.get("codex", 0)}, {cli.get("gemini", 0)}],
                          backgroundColor: ['{COLORS["blue"]}', '{COLORS["yellow"]}'] }}]
          }},
          options: {{ responsive: true, scales: {{ y: {{ beginAtZero: true, ticks: {{ color: '{COLORS["text_secondary"]}' }} }},
                      x: {{ ticks: {{ color: '{COLORS["text_secondary"]}' }} }} }},
                      plugins: {{ legend: {{ display: false }} }} }}
        }});""")

    # Subagent horizontal bar
    by_type = sub.get("by_agent_type", {})
    if by_type:
        labels = _js_dumps(list(by_type.keys()))
        values = _js_dumps(list(by_type.values()))
        bg = _js_dumps(CHART_COLORS[: len(by_type)])
        charts.append(f"""
        new Chart(document.getElementById('subagentChart'), {{
          type: 'bar',
          data: {{
            labels: {labels},
            datasets: [{{ label: 'Count', data: {values}, backgroundColor: {bg} }}]
          }},
          options: {{ indexAxis: 'y', responsive: true,
                      scales: {{ x: {{ beginAtZero: true, ticks: {{ color: '{COLORS["text_secondary"]}' }} }},
                                 y: {{ ticks: {{ color: '{COLORS["text_secondary"]}' }} }} }},
                      plugins: {{ legend: {{ display: false }} }} }}
        }});""")

    # Quality doughnut
    if quality.get("total", 0) > 0:
        charts.append(f"""
        new Chart(document.getElementById('qualityChart'), {{
          type: 'doughnut',
          data: {{
            labels: ['Passed', 'Failed'],
            datasets: [{{ data: [{quality.get("passed", 0)}, {quality.get("failed", 0)}],
                          backgroundColor: ['{COLORS["green"]}', '{COLORS["red"]}'] }}]
          }},
          options: {{ responsive: true, plugins: {{ legend: {{ labels: {{ color: '{COLORS["text"]}' }} }} }} }}
        }});""")

    # Distribution bar
    if dist:
        sorted_dist = dict(sorted(dist.items(), key=lambda x: -x[1]))
        d_labels = _js_dumps(list(sorted_dist.keys()))
        d_values = _js_dumps(list(sorted_dist.values()))
        bg = _js_dumps(
            (CHART_COLORS * ((len(sorted_dist) // len(CHART_COLORS)) + 1))[: len(sorted_dist)]
        )
        charts.append(f"""
        new Chart(document.getElementById('distChart'), {{
          type: 'bar',
          data: {{
            labels: {d_labels},
            datasets: [{{ label: 'Count', data: {d_values}, backgroundColor: {bg} }}]
          }},
          options: {{ responsive: true,
                      scales: {{ y: {{ beginAtZero: true, ticks: {{ color: '{COLORS["text_secondary"]}' }} }},
                                 x: {{ ticks: {{ color: '{COLORS["text_secondary"]}' }} }} }},
                      plugins: {{ legend: {{ display: false }} }} }}
        }});""")

    return "\n".join(charts)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = f"""
:root {{
  --bg: {COLORS["bg"]};
  --card: {COLORS["card"]};
  --border: {COLORS["border"]};
  --text: {COLORS["text"]};
  --text-secondary: {COLORS["text_secondary"]};
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, sans-serif;
        padding: 24px; min-height: 100vh; }}
h1 {{ font-size: 1.5rem; font-weight: 600; margin-bottom: 4px; }}
h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 16px; color: var(--text); }}
.subtitle {{ color: var(--text-secondary); font-size: 0.85rem; margin-bottom: 24px; }}
.summary-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                gap: 16px; margin-bottom: 24px; }}
.card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }}
.summary-card {{ text-align: center; }}
.summary-value {{ font-size: 1.8rem; font-weight: 700; }}
.summary-label {{ font-size: 0.8rem; color: var(--text-secondary); margin-top: 4px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px;
         margin-bottom: 16px; }}
.chart-wrapper {{ margin-bottom: 16px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }}
th {{ color: var(--text-secondary); font-weight: 500; }}
.no-data {{ color: var(--text-secondary); font-style: italic; text-align: center; padding: 32px 0; }}
footer {{ text-align: center; color: var(--text-secondary); font-size: 0.75rem; margin-top: 32px;
          padding-top: 16px; border-top: 1px solid var(--border); }}
@media (max-width: 900px) {{
  .grid {{ grid-template-columns: 1fr; }}
  .summary-row {{ grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); }}
}}
"""


# ---------------------------------------------------------------------------
# Main HTML generator
# ---------------------------------------------------------------------------
def generate_html(
    events: list[dict],
    title: str = "AI Orchestra Dashboard",
    session_id: str | None = None,
) -> str:
    """Generate a self-contained HTML dashboard string."""
    session = _safe_calc(calc_session_stats, events)
    route = _safe_calc(calc_route_stats, events)
    cli = _safe_calc(calc_cli_stats, events)
    sub = _safe_calc(calc_subagent_stats, events)
    quality = _safe_calc(calc_quality_stats, events)
    dist = _safe_calc(calc_event_distribution, events)
    total_events = len(events)

    scope = f"Session: {_esc(session_id)}" if session_id else "All Sessions"
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    summary = _build_summary_cards(session, route, cli, quality, total_events)
    chart_js = _build_chart_scripts(route, cli, sub, quality, dist)

    body_sections = f"""
    {summary}
    <div class="grid">
      {_build_routing_section(route)}
      {_build_cli_section(cli)}
    </div>
    <div class="grid">
      {_build_subagent_section(sub)}
      {_build_quality_section(quality)}
    </div>
    {_build_distribution_section(dist)}
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<h1>{_esc(title)}</h1>
<p class="subtitle">{scope} &mdash; {total_events} events &mdash; Generated {generated}</p>

{body_sections}

<footer>Generated by AI Orchestra audit dashboard</footer>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
Chart.defaults.color = '{COLORS["text_secondary"]}';
Chart.defaults.borderColor = '{COLORS["border"]}';
{chart_js}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    """dashboard-html CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate HTML audit dashboard. "
        "Charts require internet access (Chart.js loaded via CDN). "
        "Data tables are always visible offline.",
    )
    parser.add_argument("--session", help="Filter by session ID")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: .claude/YYYYMMDD-dashboard.html, use '-' for stdout)",
    )
    parser.add_argument("--project", help="Project directory override")
    parser.add_argument("--title", default="AI Orchestra Dashboard", help="Dashboard title")
    args = parser.parse_args()

    project_dir = args.project
    events: list[dict] = []

    if args.session:
        events = list(iter_session_events(session_id=args.session, project_dir=project_dir))
    else:
        events = list(iter_session_events(project_dir=project_dir))

    html_content = generate_html(events, title=args.title, session_id=args.session)

    if args.output == "-":
        print(html_content)
    else:
        if args.output:
            out_path = os.path.abspath(args.output)
        else:
            base_dir = project_dir or "."
            claude_dir = os.path.join(base_dir, ".claude")
            date_str = datetime.now(UTC).strftime("%Y%m%d")
            out_path = os.path.abspath(os.path.join(claude_dir, f"{date_str}-dashboard.html"))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"Dashboard written to {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
