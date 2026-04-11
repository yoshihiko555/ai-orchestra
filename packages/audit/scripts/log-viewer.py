#!/usr/bin/env python3
"""Audit log viewer: 統一イベントログをフィルタ・表示する。

Usage:
  python log-viewer.py                          # 全イベント表示
  python log-viewer.py --type cli_call          # タイプ別フィルタ
  python log-viewer.py --session SID            # セッション別フィルタ
  python log-viewer.py --limit 50               # 件数制限(デフォルト: 最新 100)
  python log-viewer.py --trace TID              # トレース ID でチェーン追跡
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_hook_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

from event_logger import iter_session_events


def filter_events(
    events: list[dict],
    *,
    event_type: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> list[dict]:
    """条件に合致するイベントのみ返す。

    Args:
        events: イベントリスト。
        event_type: 指定すると type でフィルタ。
        session_id: 指定すると sid でフィルタ。
        trace_id: 指定すると tid または ptid でフィルタ。

    Returns:
        フィルタ後のイベントリスト。
    """
    result = events
    if event_type:
        result = [e for e in result if e.get("type") == event_type]
    if session_id:
        result = [e for e in result if e.get("sid") == session_id]
    if trace_id:
        result = [e for e in result if e.get("tid") == trace_id or e.get("ptid") == trace_id]
    return result


def format_event(event: dict) -> str:
    """1件のイベントを 1 行サマリに整形する。

    Args:
        event: v1 スキーマのイベント辞書。

    Returns:
        タイプ別の 1 行サマリ文字列。
    """
    ts = event.get("ts", "")[:19]
    event_type = event.get("type", "unknown")
    sid = (event.get("sid") or "")[:8]
    tid = (event.get("tid") or "")[:8]
    data = event.get("data") or {}

    detail = ""
    if event_type == "prompt":
        detail = (
            f"expected={data.get('expected_route', '?')} "
            f"excerpt={data.get('user_input_excerpt', '')[:60]}"
        )
    elif event_type == "route_decision":
        actual = data.get("actual") or {}
        if isinstance(actual, dict):
            actual_str = f"{actual.get('tool', '')}:{actual.get('detail') or ''}"
        else:
            actual_str = str(actual)
        detail = (
            f"expected={data.get('expected', '?')} "
            f"actual={actual_str} matched={data.get('matched', False)}"
        )
    elif event_type == "cli_call":
        detail = (
            f"tool={data.get('tool', '?')} "
            f"success={data.get('success', False)} "
            f"err={data.get('error_type') or '-'}"
        )
    elif event_type == "subagent_start":
        detail = f"type={data.get('agent_type', '?')} aid={(event.get('aid') or '')[:8]}"
    elif event_type == "subagent_end":
        detail = f"type={data.get('agent_type', '?')} success={data.get('success')}"
    elif event_type == "quality_gate":
        detail = f"passed={data.get('passed')} cmd={(data.get('command') or '')[:50]}"
    elif event_type == "session_start":
        detail = f"packages={len(data.get('packages', []))}"
    elif event_type == "session_end":
        summary = data.get("summary") or {}
        detail = f"events={data.get('event_count', 0)} errors={summary.get('errors', 0)}"

    return f"[{ts}] sid={sid} tid={tid} {event_type:16s} {detail}"


def main() -> int:
    """log-viewer CLI のエントリポイント。"""
    parser = argparse.ArgumentParser(description="Audit log viewer for ai-orchestra")
    parser.add_argument("--type", dest="event_type", help="イベントタイプでフィルタ")
    parser.add_argument("--session", help="セッション ID でフィルタ")
    parser.add_argument("--trace", help="トレース ID でチェーン追跡")
    parser.add_argument(
        "--limit", type=int, default=100, help="表示件数 (デフォルト: 100、0 以下は無制限)"
    )
    parser.add_argument("--raw", action="store_true", help="JSON 生出力")
    parser.add_argument("--project", default=None, help="プロジェクトルート")
    args = parser.parse_args()

    if args.limit < 0:
        parser.error("--limit must be non-negative")

    events = iter_session_events(project_dir=args.project, session_id=args.session)
    events = filter_events(
        events,
        event_type=args.event_type,
        session_id=args.session,
        trace_id=args.trace,
    )

    if args.limit > 0 and len(events) > args.limit:
        events = events[-args.limit :]

    if args.raw:
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
    else:
        for event in events:
            print(format_event(event))

    return 0


if __name__ == "__main__":
    sys.exit(main())
