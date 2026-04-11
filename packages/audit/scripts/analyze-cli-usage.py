#!/usr/bin/env python3
"""Audit CLI usage analyzer: 統一イベントログから CLI 呼び出しパターンを分析する。

Usage:
  python analyze-cli-usage.py                    # 全期間
  python analyze-cli-usage.py --days 7           # 直近 N 日間
  python analyze-cli-usage.py --format json      # JSON 出力
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from collections import Counter

_hook_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

from event_logger import iter_session_events


def filter_cli_calls(events: list[dict], days: int | None = None) -> list[dict]:
    """cli_call イベントのみ抽出し、日数フィルタを適用する。

    Args:
        events: 全イベントリスト。
        days: フィルタ対象日数。None/0 の場合はフィルタなし。負値は拒否される。

    Returns:
        フィルタ後の cli_call イベントリスト。

    Raises:
        ValueError: days が負値の場合。
    """
    if days is not None and days < 0:
        msg = "days must be non-negative"
        raise ValueError(msg)

    calls = [e for e in events if e.get("type") == "cli_call"]
    if days is None or days == 0:
        return calls
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    cutoff_str = cutoff.isoformat()
    return [c for c in calls if c.get("ts", "") >= cutoff_str]


def extract_keywords(prompt: str) -> list[str]:
    """プロンプトから主要キーワードを抽出する（簡易版）。

    Args:
        prompt: プロンプトテキスト。

    Returns:
        小文字化されたキーワードのリスト。ストップワードは除外。
    """
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{3,}\b", prompt.lower())
    stop_words = {
        "with",
        "this",
        "that",
        "have",
        "from",
        "your",
        "what",
        "when",
        "will",
        "there",
        "which",
        "their",
        "would",
        "could",
        "should",
        "about",
        "into",
        "only",
        "some",
        "them",
        "other",
        "than",
    }
    return [w for w in words if w not in stop_words]


def analyze(calls: list[dict]) -> dict:
    """CLI 呼び出しを分析する。

    Args:
        calls: cli_call イベントのリスト。

    Returns:
        集計結果辞書（total, success_rate, by_tool, errors_by_type 等）。
    """
    total = len(calls)
    if total == 0:
        return {"total": 0}

    by_tool: Counter[str] = Counter()
    by_model: Counter[str] = Counter()
    by_error: Counter[str] = Counter()
    success_count = 0
    total_duration_ms = 0
    duration_samples = 0
    keyword_counter: Counter[str] = Counter()
    retry_total = 0

    for call in calls:
        data = call.get("data") or {}
        tool = data.get("tool", "unknown")
        by_tool[tool] += 1
        by_model[data.get("model", "unknown")] += 1

        if data.get("success"):
            success_count += 1
        if data.get("error_type"):
            by_error[data["error_type"]] += 1

        duration = data.get("duration_ms")
        if isinstance(duration, int | float) and duration > 0:
            total_duration_ms += duration
            duration_samples += 1

        retry = data.get("retry_count", 0)
        if isinstance(retry, int):
            retry_total += retry

        prompt = data.get("prompt", "") or ""
        for kw in extract_keywords(prompt)[:10]:
            keyword_counter[kw] += 1

    avg_duration_ms = total_duration_ms / duration_samples if duration_samples > 0 else 0

    return {
        "total": total,
        "success": success_count,
        "success_rate": round((success_count / total) * 100, 1),
        "by_tool": dict(by_tool),
        "by_model": dict(by_model.most_common(10)),
        "errors_by_type": dict(by_error),
        "avg_duration_ms": round(avg_duration_ms, 1),
        "total_retries": retry_total,
        "top_keywords": dict(keyword_counter.most_common(20)),
    }


def render_text(analysis: dict, period: str) -> str:
    """分析結果をテキストで整形する。

    Args:
        analysis: `analyze()` が返した辞書。
        period: 集計期間の説明文字列。

    Returns:
        改行区切りのテキスト。
    """
    lines: list[str] = []
    lines.append(f"=== CLI Usage Analysis ({period}) ===")

    total = analysis.get("total", 0)
    if total == 0:
        lines.append("(no CLI calls found)")
        return "\n".join(lines)

    lines.append(f"Total calls: {total}")
    lines.append(f"Success: {analysis['success']} ({analysis['success_rate']}%)")
    lines.append(f"Avg duration: {analysis['avg_duration_ms']} ms")
    lines.append(f"Total retries: {analysis['total_retries']}")
    lines.append("")

    lines.append("## By tool")
    for tool, count in analysis["by_tool"].items():
        lines.append(f"  {tool}: {count}")
    lines.append("")

    lines.append("## By model")
    for model, count in analysis["by_model"].items():
        lines.append(f"  {model}: {count}")
    lines.append("")

    if analysis.get("errors_by_type"):
        lines.append("## Errors")
        for err, count in analysis["errors_by_type"].items():
            lines.append(f"  {err}: {count}")
        lines.append("")

    if analysis.get("top_keywords"):
        lines.append("## Top keywords")
        for kw, count in list(analysis["top_keywords"].items())[:15]:
            lines.append(f"  {kw}: {count}")

    return "\n".join(lines)


def main() -> int:
    """analyze-cli-usage CLI のエントリポイント。"""
    parser = argparse.ArgumentParser(description="Analyze CLI usage patterns")
    parser.add_argument("--days", type=int, default=None, help="直近 N 日間")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="出力形式")
    parser.add_argument("--project", default=None, help="プロジェクトルート")
    args = parser.parse_args()

    if args.days is not None and args.days < 0:
        parser.error("--days must be non-negative")

    events = iter_session_events(project_dir=args.project)
    calls = filter_cli_calls(events, days=args.days)
    analysis = analyze(calls)

    period = f"last {args.days} days" if args.days else "all time"
    if args.format == "json":
        print(json.dumps({"period": period, "analysis": analysis}, indent=2, ensure_ascii=False))
    else:
        print(render_text(analysis, period))

    return 0


if __name__ == "__main__":
    sys.exit(main())
