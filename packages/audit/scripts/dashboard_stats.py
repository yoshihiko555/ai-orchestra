"""Dashboard statistics calculation module — shared between text and HTML dashboards."""

from __future__ import annotations

from collections import Counter


def calc_session_stats(events: list[dict]) -> dict:
    """セッション全体の基本統計を計算する。

    Args:
        events: 集計対象のイベントリスト。

    Returns:
        `{"total_sessions": int, "session_starts": int, "session_ends": int}`
    """
    sessions = {e.get("sid", "") for e in events if e.get("sid")}
    starts = sum(1 for e in events if e.get("type") == "session_start")
    ends = sum(1 for e in events if e.get("type") == "session_end")
    return {
        "total_sessions": len(sessions),
        "session_starts": starts,
        "session_ends": ends,
    }


def calc_route_stats(events: list[dict]) -> dict:
    """route_decision イベントの集計を計算する。

    Args:
        events: 集計対象のイベントリスト。

    Returns:
        total / matched / mismatched / match_rate を含む辞書。
    """
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
    """cli_call イベントの集計を計算する。

    Args:
        events: 集計対象のイベントリスト。

    Returns:
        total / codex / gemini / success_rate / errors_by_type を含む辞書。
    """
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
    """subagent_start / subagent_end イベントの集計を計算する。

    Args:
        events: 集計対象のイベントリスト。

    Returns:
        total_starts / total_ends / by_agent_type を含む辞書。
    """
    starts = [e for e in events if e.get("type") == "subagent_start"]
    ends = [e for e in events if e.get("type") == "subagent_end"]
    agent_types = Counter((s.get("data") or {}).get("agent_type") for s in starts)
    return {
        "total_starts": len(starts),
        "total_ends": len(ends),
        "by_agent_type": dict(agent_types),
    }


def calc_quality_stats(events: list[dict]) -> dict:
    """quality_gate イベントの集計を計算する。

    Args:
        events: 集計対象のイベントリスト。

    Returns:
        total / passed / failed を含む辞書。
    """
    gates = [e for e in events if e.get("type") == "quality_gate"]
    passed = sum(1 for g in gates if (g.get("data") or {}).get("passed") is True)
    failed = sum(1 for g in gates if (g.get("data") or {}).get("passed") is False)
    return {"total": len(gates), "passed": passed, "failed": failed}


def calc_event_distribution(events: list[dict]) -> dict:
    """イベントタイプ別の件数分布を計算する。

    Args:
        events: 集計対象のイベントリスト。

    Returns:
        `{event_type: count}` 形式の辞書。
    """
    return dict(Counter(e.get("type") or "unknown" for e in events))
