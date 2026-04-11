#!/usr/bin/env python3
"""Audit dashboard: 統一イベントログからセッション状況を集計表示する。

Usage:
  python dashboard.py               # 全セッションの集計
  python dashboard.py --session SID # 特定セッションの集計
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

_hook_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

from event_logger import iter_session_events, list_sessions


def calc_session_stats(events: list[dict]) -> dict:
    """セッション全体の基本統計を計算する。"""
    sessions = {e.get("sid", "") for e in events if e.get("sid")}
    starts = sum(1 for e in events if e.get("type") == "session_start")
    ends = sum(1 for e in events if e.get("type") == "session_end")
    return {
        "total_sessions": len(sessions),
        "session_starts": starts,
        "session_ends": ends,
    }


def calc_route_stats(events: list[dict]) -> dict:
    """route_decision イベントの集計。"""
    decisions = [e for e in events if e.get("type") == "route_decision"]
    non_helper = [d for d in decisions if not (d.get("data") or {}).get("is_helper", False)]
    matched = sum(1 for d in non_helper if (d.get("data") or {}).get("matched", False))
    total = len(non_helper)
    return {
        "total": total,
        "matched": matched,
        "mismatched": total - matched,
        "match_rate": round((matched / total) * 100, 1) if total > 0 else 0.0,
    }


def calc_cli_stats(events: list[dict]) -> dict:
    """cli_call イベントの集計。"""
    calls = [e for e in events if e.get("type") == "cli_call"]
    total = len(calls)
    codex = sum(1 for c in calls if (c.get("data") or {}).get("tool") == "codex")
    gemini = sum(1 for c in calls if (c.get("data") or {}).get("tool") == "gemini")
    success = sum(1 for c in calls if (c.get("data") or {}).get("success", False))

    errors = Counter(
        (c.get("data") or {}).get("error_type")
        for c in calls
        if (c.get("data") or {}).get("error_type")
    )

    return {
        "total": total,
        "codex": codex,
        "gemini": gemini,
        "success": success,
        "success_rate": round((success / total) * 100, 1) if total > 0 else 0.0,
        "errors_by_type": dict(errors),
    }


def calc_subagent_stats(events: list[dict]) -> dict:
    """subagent_start / subagent_end イベントの集計。"""
    starts = [e for e in events if e.get("type") == "subagent_start"]
    ends = [e for e in events if e.get("type") == "subagent_end"]
    agent_types = Counter((s.get("data") or {}).get("agent_type") for s in starts)
    return {
        "total_starts": len(starts),
        "total_ends": len(ends),
        "by_agent_type": dict(agent_types),
    }


def calc_quality_stats(events: list[dict]) -> dict:
    """quality_gate イベントの集計。"""
    gates = [e for e in events if e.get("type") == "quality_gate"]
    passed = sum(1 for g in gates if (g.get("data") or {}).get("passed") is True)
    failed = sum(1 for g in gates if (g.get("data") or {}).get("passed") is False)
    return {"total": len(gates), "passed": passed, "failed": failed}


def calc_event_distribution(events: list[dict]) -> dict:
    """イベントタイプ別の件数分布。"""
    return dict(Counter(e.get("type", "unknown") for e in events))


def render_dashboard(events: list[dict], session_id: str | None = None) -> str:
    """ダッシュボードを文字列として描画する。"""
    lines: list[str] = []
    title = f"Session: {session_id}" if session_id else "All Sessions"
    lines.append(f"=== Audit Dashboard ({title}) ===")
    lines.append(f"Total events: {len(events)}")
    lines.append("")

    if not events:
        lines.append("(no events found)")
        return "\n".join(lines)

    session_stats = calc_session_stats(events)
    lines.append("## Sessions")
    lines.append(f"  total: {session_stats['total_sessions']}")
    lines.append(f"  starts: {session_stats['session_starts']}")
    lines.append(f"  ends: {session_stats['session_ends']}")
    lines.append("")

    route_stats = calc_route_stats(events)
    lines.append("## Routing")
    lines.append(
        f"  decisions: {route_stats['total']} "
        f"(matched: {route_stats['matched']}, mismatched: {route_stats['mismatched']}, "
        f"match_rate: {route_stats['match_rate']}%)"
    )
    lines.append("")

    cli_stats = calc_cli_stats(events)
    lines.append("## CLI Calls")
    lines.append(
        f"  total: {cli_stats['total']} "
        f"(codex: {cli_stats['codex']}, gemini: {cli_stats['gemini']}, "
        f"success_rate: {cli_stats['success_rate']}%)"
    )
    if cli_stats["errors_by_type"]:
        lines.append("  errors:")
        for err_type, count in cli_stats["errors_by_type"].items():
            lines.append(f"    {err_type}: {count}")
    lines.append("")

    sub_stats = calc_subagent_stats(events)
    lines.append("## Subagents")
    lines.append(f"  starts: {sub_stats['total_starts']}, ends: {sub_stats['total_ends']}")
    if sub_stats["by_agent_type"]:
        lines.append("  by type:")
        for agent_type, count in sub_stats["by_agent_type"].items():
            lines.append(f"    {agent_type}: {count}")
    lines.append("")

    quality_stats = calc_quality_stats(events)
    lines.append("## Quality Gates")
    lines.append(
        f"  total: {quality_stats['total']} "
        f"(passed: {quality_stats['passed']}, failed: {quality_stats['failed']})"
    )
    lines.append("")

    distribution = calc_event_distribution(events)
    lines.append("## Event Distribution")
    for event_type, count in sorted(distribution.items(), key=lambda x: -x[1]):
        lines.append(f"  {event_type}: {count}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit dashboard for ai-orchestra")
    parser.add_argument("--session", help="特定セッションのみ集計")
    parser.add_argument("--project", default=None, help="プロジェクトルート（省略時は自動解決）")
    args = parser.parse_args()

    if args.session:
        events = iter_session_events(project_dir=args.project, session_id=args.session)
        print(render_dashboard(events, session_id=args.session))
    else:
        events = iter_session_events(project_dir=args.project)
        sessions = list_sessions(project_dir=args.project)
        print(render_dashboard(events))
        print()
        print(f"Sessions on disk: {len(sessions)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
