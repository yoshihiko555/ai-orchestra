#!/usr/bin/env python3
"""SessionEnd hook: セッション終了時にサマリーを記録する。"""

from __future__ import annotations

import json
import os
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)
else:
    if _hook_dir not in sys.path:
        sys.path.insert(0, _hook_dir)

from datetime import UTC

from event_logger import emit_event, get_session_log_path
from hook_common import read_hook_input, safe_hook_execution


def _count_events(session_log_path: str) -> dict:
    """セッションログからイベント数をカウントする。"""
    counts: dict[str, int] = {}
    error_count = 0
    first_ts = ""
    if not os.path.exists(session_log_path):
        return {"event_count": 0, "summary": {}, "duration_ms": None}

    with open(session_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type", "unknown")
            counts[event_type] = counts.get(event_type, 0) + 1
            if not first_ts:
                first_ts = event.get("ts", "")
            # エラー検出
            data = event.get("data", {})
            if data.get("error_type") or data.get("success") is False:
                error_count += 1

    total = sum(counts.values())
    return {
        "event_count": total,
        "summary": {
            "cli_calls": counts.get("cli_call", 0),
            "subagents": counts.get("subagent_start", 0),
            "route_decisions": counts.get("route_decision", 0),
            "errors": error_count,
        },
        "first_ts": first_ts,
    }


def _calc_duration_ms(first_ts: str, last_ts: str) -> int | None:
    """2つの ISO8601 タイムスタンプから経過ミリ秒を計算する。"""
    if not first_ts or not last_ts:
        return None
    try:
        from datetime import datetime

        fmt_options = ["%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f"]
        t1 = t2 = None
        for fmt in fmt_options:
            try:
                t1 = t1 or datetime.strptime(first_ts, fmt)
            except ValueError:
                pass
            try:
                t2 = t2 or datetime.strptime(last_ts, fmt)
            except ValueError:
                pass
        if t1 and t2:
            if t1.tzinfo is None:
                t1 = t1.replace(tzinfo=UTC)
            if t2.tzinfo is None:
                t2 = t2.replace(tzinfo=UTC)
            return int((t2 - t1).total_seconds() * 1000)
    except Exception:
        pass
    return None


@safe_hook_execution
def main() -> None:
    data = read_hook_input()
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return

    cwd = str(data.get("cwd") or "") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    session_log = get_session_log_path(session_id, project_dir=cwd)
    stats = _count_events(session_log)

    from datetime import datetime

    now = datetime.now(UTC).isoformat()
    duration_ms = _calc_duration_ms(stats.get("first_ts", ""), now)

    emit_event(
        "session_end",
        {
            "duration_ms": duration_ms,
            "event_count": stats["event_count"],
            "summary": stats["summary"],
        },
        session_id=session_id,
        project_dir=cwd,
    )


if __name__ == "__main__":
    main()
