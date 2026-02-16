#!/usr/bin/env python3
"""events.jsonl のログビューア。

usage: log-viewer.py [--type TYPE] [--since SINCE] [--session ID] [--last N] [--no-color] [--json]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

DEFAULT_EVENTS_PATH = os.path.join(".claude", "logs", "orchestration", "events.jsonl")

# ANSI カラー
COLORS = {
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
    "reset": "\033[0m",
    "bold": "\033[1m",
}


def read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def parse_time(ts: str) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_since(since_str: str) -> datetime.datetime:
    """'24h', '7d', '30m' のような文字列を datetime に変換する。"""
    match = re.match(r"^(\d+)\s*([hdm])$", since_str.strip().lower())
    if not match:
        raise ValueError(f"Invalid since format: {since_str!r} (use e.g. '24h', '7d', '30m')")
    value = int(match.group(1))
    unit = match.group(2)
    now = datetime.datetime.now(datetime.UTC)
    if unit == "h":
        return now - datetime.timedelta(hours=value)
    elif unit == "d":
        return now - datetime.timedelta(days=value)
    elif unit == "m":
        return now - datetime.timedelta(minutes=value)
    return now


def filter_events(
    events: list[dict],
    *,
    event_type: str | None = None,
    since: datetime.datetime | None = None,
    session_id: str | None = None,
) -> list[dict]:
    filtered: list[dict] = []
    for ev in events:
        if event_type and ev.get("event_type") != event_type:
            continue
        if session_id and not ev.get("session_id", "").startswith(session_id):
            continue
        if since:
            ts = parse_time(str(ev.get("timestamp", "")))
            if ts is not None:
                # naive/aware の混在を解消
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.UTC)
                if since.tzinfo is None:
                    since = since.replace(tzinfo=datetime.UTC)
            if ts is not None and ts < since:
                continue
        filtered.append(ev)
    return filtered


def _color(name: str, text: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{COLORS.get(name, '')}{text}{COLORS['reset']}"


def _event_color(event_type: str) -> str:
    mapping = {
        "session_start": "green",
        "session_end": "red",
        "expected_route": "cyan",
        "route_audit": "yellow",
        "cli_call": "magenta",
        "quality_gate": "red",
        "subagent_start": "cyan",
    }
    return mapping.get(event_type, "dim")


def _format_data(event_type: str, data: dict) -> str:
    if event_type == "session_start":
        return f"project={data.get('project_name', '')}"
    if event_type == "session_end":
        dur = data.get("duration_seconds")
        if dur is not None:
            minutes = int(dur) // 60
            return f"duration={minutes}m"
        return "duration=?"
    if event_type == "expected_route":
        route = data.get("expected_route", "")
        excerpt = data.get("prompt_excerpt", "")[:40]
        return f'route={route} prompt="{excerpt}"'
    if event_type == "route_audit":
        expected = data.get("expected_route", "")
        actual = data.get("actual_route", "")
        matched = data.get("matched", False)
        tag = "OK" if matched else "MISS"
        return f"expected={expected} actual={actual} [{tag}]"
    if event_type == "cli_call":
        tool = data.get("tool", "")
        model = data.get("model", "")
        success = data.get("success", False)
        tag = "OK" if success else "FAIL"
        return f"tool={tool} model={model} [{tag}]"
    if event_type == "quality_gate":
        cmd = data.get("command", "")[:40]
        passed = data.get("passed")
        tag = "PASS" if passed else "FAIL"
        return f"cmd={cmd} [{tag}]"
    if event_type == "subagent_start":
        atype = data.get("agent_type", "")
        aid = data.get("agent_id", "")[:7]
        return f"type={atype} id={aid}"
    return json.dumps(data, ensure_ascii=False)[:80]


def format_event_line(event: dict, *, use_color: bool = True) -> str:
    ts_raw = event.get("timestamp", "")
    ts = parse_time(ts_raw)
    time_str = ts.strftime("%H:%M:%S") if ts else ts_raw[:8]

    event_type = event.get("event_type", "")
    session = event.get("session_id", "")[:8]
    data = event.get("data", {})

    color_name = _event_color(event_type)
    data_str = _format_data(event_type, data)

    # route_audit の MISS を強調
    if event_type == "route_audit" and not data.get("matched", False):
        data_str = _color("yellow", data_str, use_color)
    elif event_type == "quality_gate" and not data.get("passed", True):
        data_str = _color("red", data_str, use_color)
    else:
        data_str = data_str

    type_str = _color(color_name, f"{event_type:<16}", use_color)
    session_str = _color("dim", session, use_color)

    return f"{time_str}  {type_str}  {session_str}  {data_str}"


def main() -> None:
    parser = argparse.ArgumentParser(description="events.jsonl ログビューア")
    parser.add_argument("--type", dest="event_type", help="イベントタイプでフィルタ")
    parser.add_argument("--since", help="期間フィルタ (例: 24h, 7d, 30m)")
    parser.add_argument("--session", help="セッションIDでフィルタ (前方一致)")
    parser.add_argument("--last", type=int, default=50, help="表示件数 (デフォルト: 50)")
    parser.add_argument("--no-color", action="store_true", help="カラー出力を無効化")
    parser.add_argument("--json", action="store_true", help="JSON 形式で出力")
    parser.add_argument("--log-path", default=DEFAULT_EVENTS_PATH, help="events.jsonl のパス")

    args = parser.parse_args()
    use_color = not args.no_color and sys.stdout.isatty()

    events = read_jsonl(args.log_path)

    since = parse_since(args.since) if args.since else None
    events = filter_events(events, event_type=args.event_type, since=since, session_id=args.session)

    if args.last > 0:
        events = events[-args.last :]

    if args.json:
        for ev in events:
            print(json.dumps(ev, ensure_ascii=False))
        return

    header = f"{'TIME':<10}{'TYPE':<18}{'SESSION':<10}DATA"
    sep = "\u2500" * 70
    print(_color("bold", header, use_color))
    print(_color("dim", sep, use_color))

    for ev in events:
        print(format_event_line(ev, use_color=use_color))

    print(_color("dim", f"\n{len(events)} events", use_color))


if __name__ == "__main__":
    main()
