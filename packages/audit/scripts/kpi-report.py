#!/usr/bin/env python3
"""Audit KPI report: 統一イベントログから KPI スコアカードを markdown で生成する。

Usage:
  python kpi-report.py                    # 全期間
  python kpi-report.py --days 7           # 直近 N 日間
  python kpi-report.py --output FILE      # ファイルに出力
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from collections import Counter

_hook_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

from event_logger import iter_session_events


def filter_by_days(events: list[dict], days: int | None) -> list[dict]:
    """直近 N 日間のイベントのみ返す。

    Args:
        events: 全イベントリスト。
        days: フィルタ対象日数。None/0 の場合はフィルタなし。

    Returns:
        フィルタ後のイベントリスト。
    """
    if not days or days <= 0:
        return events
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    cutoff_str = cutoff.isoformat()
    return [e for e in events if e.get("ts", "") >= cutoff_str]


def build_scorecard(events: list[dict]) -> dict:
    """KPI スコアカードを構築する。

    Args:
        events: 集計対象のイベントリスト。

    Returns:
        ルーティング・CLI・サブエージェント・品質ゲートの集計結果を含む辞書。
    """
    sessions = {e.get("sid", "") for e in events if e.get("sid")}

    decisions = [e for e in events if e.get("type") == "route_decision"]
    non_helper = [d for d in decisions if not (d.get("data") or {}).get("is_helper", False)]
    matched = sum(1 for d in non_helper if (d.get("data") or {}).get("matched", False))
    total_routes = len(non_helper)

    cli_calls = [e for e in events if e.get("type") == "cli_call"]
    cli_success = sum(1 for c in cli_calls if (c.get("data") or {}).get("success", False))

    cli_by_tool = Counter((c.get("data") or {}).get("tool") for c in cli_calls)
    cli_errors = Counter(
        (c.get("data") or {}).get("error_type")
        for c in cli_calls
        if (c.get("data") or {}).get("error_type")
    )

    subagent_starts = [e for e in events if e.get("type") == "subagent_start"]
    subagent_types = Counter((s.get("data") or {}).get("agent_type") for s in subagent_starts)

    quality_gates = [e for e in events if e.get("type") == "quality_gate"]
    quality_passed = sum(1 for g in quality_gates if (g.get("data") or {}).get("passed") is True)

    return {
        "total_events": len(events),
        "total_sessions": len(sessions),
        "routing": {
            "total": total_routes,
            "matched": matched,
            "match_rate": round((matched / total_routes) * 100, 1) if total_routes > 0 else 0.0,
        },
        "cli": {
            "total": len(cli_calls),
            "success": cli_success,
            "success_rate": round((cli_success / len(cli_calls)) * 100, 1) if cli_calls else 0.0,
            "by_tool": dict(cli_by_tool),
            "errors": dict(cli_errors),
        },
        "subagents": {
            "total_starts": len(subagent_starts),
            "by_type": dict(subagent_types),
        },
        "quality_gates": {
            "total": len(quality_gates),
            "passed": quality_passed,
        },
    }


def render_markdown(scorecard: dict, period: str) -> str:
    """スコアカードを markdown で描画する。

    Args:
        scorecard: `build_scorecard()` が返した辞書。
        period: 集計期間の説明文字列。

    Returns:
        markdown 形式の文字列。
    """
    lines: list[str] = []
    lines.append(f"# Audit KPI Scorecard ({period})")
    lines.append("")
    lines.append(f"- **Total events**: {scorecard['total_events']}")
    lines.append(f"- **Total sessions**: {scorecard['total_sessions']}")
    lines.append("")

    routing = scorecard["routing"]
    lines.append("## Routing")
    lines.append(f"- Total decisions: {routing['total']}")
    lines.append(f"- Matched: {routing['matched']}")
    lines.append(f"- Match rate: **{routing['match_rate']}%**")
    lines.append("")

    cli = scorecard["cli"]
    lines.append("## CLI Calls")
    lines.append(f"- Total: {cli['total']}")
    lines.append(f"- Success rate: **{cli['success_rate']}%**")
    if cli["by_tool"]:
        lines.append("- By tool:")
        for tool, count in cli["by_tool"].items():
            lines.append(f"  - {tool}: {count}")
    if cli["errors"]:
        lines.append("- Errors:")
        for err, count in cli["errors"].items():
            lines.append(f"  - {err}: {count}")
    lines.append("")

    sub = scorecard["subagents"]
    lines.append("## Subagents")
    lines.append(f"- Total starts: {sub['total_starts']}")
    if sub["by_type"]:
        lines.append("- By type:")
        for agent_type, count in sorted(sub["by_type"].items(), key=lambda x: -x[1]):
            lines.append(f"  - {agent_type}: {count}")
    lines.append("")

    qg = scorecard["quality_gates"]
    lines.append("## Quality Gates")
    lines.append(f"- Total: {qg['total']}")
    lines.append(f"- Passed: {qg['passed']}")

    return "\n".join(lines) + "\n"


def main() -> int:
    """kpi-report CLI のエントリポイント。"""
    parser = argparse.ArgumentParser(description="KPI scorecard for ai-orchestra audit logs")
    parser.add_argument("--days", type=int, default=None, help="直近 N 日間で集計")
    parser.add_argument("--output", help="出力ファイル (省略時は stdout)")
    parser.add_argument("--project", default=None, help="プロジェクトルート")
    args = parser.parse_args()

    if args.days is not None and args.days < 0:
        parser.error("--days must be non-negative")

    events = iter_session_events(project_dir=args.project)
    events = filter_by_days(events, args.days)

    period = f"last {args.days} days" if args.days else "all time"
    scorecard = build_scorecard(events)
    markdown = render_markdown(scorecard, period)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(markdown)
        print(f"Report written to {args.output}")
    else:
        print(markdown)

    return 0


if __name__ == "__main__":
    sys.exit(main())
