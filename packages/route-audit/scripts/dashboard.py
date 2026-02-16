#!/usr/bin/env python3
"""統合ダッシュボード: events.jsonl から統計を集計して表示する。

usage: dashboard.py [--days DAYS] [--json] [--log-path PATH]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from collections import Counter

DEFAULT_EVENTS_PATH = os.path.join(".claude", "logs", "orchestration", "events.jsonl")


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


def filter_by_days(rows: list[dict], days: int) -> list[dict]:
    if days <= 0:
        return rows
    now = datetime.datetime.now(datetime.UTC)
    threshold = now - datetime.timedelta(days=days)
    filtered: list[dict] = []
    for row in rows:
        ts = parse_time(str(row.get("timestamp") or ""))
        if ts is None or ts >= threshold:
            filtered.append(row)
    return filtered


def calc_session_stats(events: list[dict]) -> dict:
    """セッション統計を計算する。"""
    ends = [e for e in events if e.get("event_type") == "session_end"]
    durations: list[float] = []
    for e in ends:
        dur = (e.get("data") or {}).get("duration_seconds")
        if dur is not None and isinstance(dur, (int, float)) and dur > 0:
            durations.append(float(dur))

    starts = sum(1 for e in events if e.get("event_type") == "session_start")

    avg_dur = sum(durations) / len(durations) if durations else 0
    max_dur = max(durations) if durations else 0

    return {
        "count": starts,
        "avg_duration_seconds": round(avg_dur, 1),
        "max_duration_seconds": round(max_dur, 1),
    }


def calc_route_stats(events: list[dict]) -> dict:
    """ルート監査統計を計算する（ヘルパーを除外）。"""
    audits = [e for e in events if e.get("event_type") == "route_audit"]
    expected = [e for e in events if e.get("event_type") == "expected_route"]

    effective = [a for a in audits if not (a.get("data") or {}).get("is_helper", False)]
    helpers_excluded = len(audits) - len(effective)

    # 委譲なしプロンプトの照合
    audit_prompt_ids = {str((a.get("data") or {}).get("prompt_id") or "") for a in audits}
    implicit_direct = 0
    for exp in expected:
        pid = str((exp.get("data") or {}).get("prompt_id") or "")
        if pid and pid not in audit_prompt_ids:
            route = str((exp.get("data") or {}).get("expected_route") or "")
            if route == "claude-direct":
                implicit_direct += 1

    total = len(effective) + implicit_direct
    matched = sum(1 for a in effective if (a.get("data") or {}).get("matched", False))
    matched += implicit_direct
    rate = round((matched / total) * 100, 1) if total > 0 else 0.0

    return {
        "total": total,
        "matched": matched,
        "rate": rate,
        "helpers_excluded": helpers_excluded,
        "implicit_direct": implicit_direct,
    }


def calc_cli_stats(events: list[dict]) -> dict:
    """CLI 呼び出し統計を計算する。"""
    calls = [e for e in events if e.get("event_type") == "cli_call"]
    total = len(calls)
    codex = sum(1 for c in calls if (c.get("data") or {}).get("tool") == "codex")
    gemini = sum(1 for c in calls if (c.get("data") or {}).get("tool") == "gemini")
    success = sum(1 for c in calls if (c.get("data") or {}).get("success", False))
    success_rate = round((success / total) * 100, 1) if total > 0 else 0.0

    return {"total": total, "codex": codex, "gemini": gemini, "success_rate": success_rate}


def calc_quality_stats(events: list[dict]) -> dict:
    """品質ゲート統計を計算する。"""
    gates = [e for e in events if e.get("event_type") == "quality_gate"]
    total = len(gates)
    passed = sum(1 for g in gates if (g.get("data") or {}).get("passed", False))
    rate = round((passed / total) * 100, 1) if total > 0 else 0.0

    return {"total": total, "passed": passed, "rate": rate}


def calc_event_distribution(events: list[dict]) -> dict[str, int]:
    """イベントタイプ別の分布を返す。"""
    counter: Counter[str] = Counter()
    for e in events:
        et = e.get("event_type", "unknown")
        counter[et] += 1
    return dict(counter.most_common())


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0m"
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _bar(ratio: float, width: int = 16) -> str:
    filled = int(ratio * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def render_dashboard(
    days: int,
    session: dict,
    route: dict,
    cli: dict,
    quality: dict,
    distribution: dict[str, int],
) -> str:
    width = 52
    border_top = "\u2554" + "\u2550" * width + "\u2557"
    border_mid = "\u2560" + "\u2550" * width + "\u2563"
    border_bot = "\u255a" + "\u2550" * width + "\u255d"

    def row(text: str) -> str:
        return f"\u2551 {text:<{width - 1}}\u2551"

    lines = [
        border_top,
        row(f"Orchestration Dashboard ({days} days)"),
        border_mid,
    ]

    # Sessions
    avg = _format_duration(session["avg_duration_seconds"])
    mx = _format_duration(session["max_duration_seconds"])
    lines.append(row(f"Sessions: {session['count']}  Avg: {avg}  Max: {mx}"))

    # Route Match
    rate = route["rate"]
    bar = _bar(rate / 100)
    lines.append(row(f"Route Match: {rate}%  {bar}"))

    # CLI
    lines.append(
        row(f"CLI: Codex {cli['codex']} / Gemini {cli['gemini']}  Success: {cli['success_rate']}%")
    )

    # Quality
    q_bar = _bar(quality["rate"] / 100)
    lines.append(
        row(f"Tests: {quality['passed']}/{quality['total']} passed  {q_bar}  {quality['rate']}%")
    )

    # Distribution
    dist_parts = [f"{k} {v}" for k, v in distribution.items()]
    dist_str = ", ".join(dist_parts)
    if len(dist_str) > width - 12:
        dist_str = dist_str[: width - 15] + "..."
    lines.append(row(f"Events: {dist_str}"))

    lines.append(border_bot)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="統合ダッシュボード")
    parser.add_argument("--days", type=int, default=7, help="集計期間（日）")
    parser.add_argument("--json", action="store_true", help="JSON 形式で出力")
    parser.add_argument("--log-path", default=DEFAULT_EVENTS_PATH, help="events.jsonl のパス")

    args = parser.parse_args()

    events = read_jsonl(args.log_path)
    events = filter_by_days(events, args.days)

    session = calc_session_stats(events)
    route = calc_route_stats(events)
    cli = calc_cli_stats(events)
    quality = calc_quality_stats(events)
    distribution = calc_event_distribution(events)

    if args.json:
        result = {
            "days": args.days,
            "session": session,
            "route": route,
            "cli": cli,
            "quality": quality,
            "distribution": distribution,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(render_dashboard(args.days, session, route, cli, quality, distribution))


if __name__ == "__main__":
    main()
