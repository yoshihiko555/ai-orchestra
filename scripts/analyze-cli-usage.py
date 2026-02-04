#!/usr/bin/env python3
"""
Codex/Gemini CLI usage analyzer.

Analyzes logs/cli-tools.jsonl and generates usage reports.

Usage:
    python scripts/analyze-cli-usage.py              # Full report
    python scripts/analyze-cli-usage.py --days 7     # Last 7 days
    python scripts/analyze-cli-usage.py --json       # JSON output
    python scripts/analyze-cli-usage.py --export     # Export to CSV
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = PROJECT_ROOT / "logs" / "cli-tools.jsonl"
EXPORT_DIR = PROJECT_ROOT / "logs"


def load_logs(since: datetime | None = None) -> list[dict[str, Any]]:
    """Load log entries from JSONL file."""
    if not LOG_FILE.exists():
        return []

    entries: list[dict[str, Any]] = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if since:
                    ts = datetime.fromisoformat(
                        entry["timestamp"].replace("Z", "+00:00")
                    )
                    if ts < since:
                        continue
                entries.append(entry)
            except (json.JSONDecodeError, KeyError):
                continue

    return entries


def extract_keywords(prompt: str) -> list[str]:
    """Extract key topics from prompt."""
    keywords: list[str] = []

    # Common patterns to detect
    patterns = {
        "design": r"(design|architect|structure|pattern)",
        "debug": r"(debug|error|bug|fix|issue|problem)",
        "review": r"(review|check|analyze|evaluate)",
        "research": r"(research|investigate|find|search|look up)",
        "implement": r"(implement|create|build|develop|write)",
        "refactor": r"(refactor|simplify|clean|improve)",
        "test": r"(test|spec|verify|validate)",
        "security": r"(security|vulnerab|auth|permission)",
        "performance": r"(performance|optimi|speed|slow|fast)",
    }

    prompt_lower = prompt.lower()
    for keyword, pattern in patterns.items():
        if re.search(pattern, prompt_lower):
            keywords.append(keyword)

    return keywords if keywords else ["other"]


def create_bar(value: int, max_value: int, width: int = 20) -> str:
    """Create ASCII bar chart."""
    if max_value == 0:
        return ""
    filled = int((value / max_value) * width)
    return "█" * filled + "░" * (width - filled)


def format_report(entries: list[dict[str, Any]], period_days: int | None) -> str:
    """Generate formatted usage report."""
    if not entries:
        return "No log entries found.\n\nRun Codex/Gemini commands to generate logs."

    lines: list[str] = []

    # Header
    lines.append("=" * 50)
    lines.append("  Codex/Gemini Usage Report")
    lines.append("=" * 50)
    lines.append("")

    # Period
    timestamps = [
        datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        for e in entries
    ]
    start_date = min(timestamps).strftime("%Y-%m-%d")
    end_date = max(timestamps).strftime("%Y-%m-%d")
    lines.append(f"Period: {start_date} ~ {end_date}")
    if period_days:
        lines.append(f"Filter: Last {period_days} days")
    lines.append("")

    # Tool usage summary
    codex_entries = [e for e in entries if e.get("tool") == "codex"]
    gemini_entries = [e for e in entries if e.get("tool") == "gemini"]
    total = len(entries)
    codex_count = len(codex_entries)
    gemini_count = len(gemini_entries)

    lines.append("## Tool Usage")
    lines.append("")
    max_count = max(codex_count, gemini_count, 1)

    codex_pct = (codex_count / total * 100) if total > 0 else 0
    gemini_pct = (gemini_count / total * 100) if total > 0 else 0

    lines.append(
        f"  Codex:  {create_bar(codex_count, max_count)} "
        f"{codex_count:3d} calls ({codex_pct:.0f}%)"
    )
    lines.append(
        f"  Gemini: {create_bar(gemini_count, max_count)} "
        f"{gemini_count:3d} calls ({gemini_pct:.0f}%)"
    )
    lines.append("")

    # Daily trend
    daily: dict[str, dict[str, int]] = defaultdict(lambda: {"codex": 0, "gemini": 0})
    for entry in entries:
        date = entry["timestamp"][:10]
        tool = entry.get("tool", "unknown")
        if tool in ("codex", "gemini"):
            daily[date][tool] += 1

    if daily:
        lines.append("## Daily Trend")
        lines.append("")
        max_daily = max(
            sum(d.values()) for d in daily.values()
        ) if daily else 1

        for date in sorted(daily.keys())[-14:]:  # Last 14 days
            data = daily[date]
            day_total = data["codex"] + data["gemini"]
            bar = create_bar(day_total, max_daily, width=15)
            lines.append(
                f"  {date[5:]}: {bar} Codex: {data['codex']:2d}, Gemini: {data['gemini']:2d}"
            )
        lines.append("")

    # Success rate
    codex_success = sum(1 for e in codex_entries if e.get("success", False))
    gemini_success = sum(1 for e in gemini_entries if e.get("success", False))

    lines.append("## Success Rate")
    lines.append("")
    if codex_count > 0:
        codex_rate = codex_success / codex_count * 100
        lines.append(f"  Codex:  {codex_rate:5.1f}% ({codex_success}/{codex_count})")
    if gemini_count > 0:
        gemini_rate = gemini_success / gemini_count * 100
        lines.append(f"  Gemini: {gemini_rate:5.1f}% ({gemini_success}/{gemini_count})")
    lines.append("")

    # Top use cases
    all_keywords: list[str] = []
    for entry in entries:
        prompt = entry.get("prompt", "")
        all_keywords.extend(extract_keywords(prompt))

    keyword_counts = Counter(all_keywords)

    lines.append("## Top Use Cases (by prompt keywords)")
    lines.append("")
    for i, (keyword, count) in enumerate(keyword_counts.most_common(10), 1):
        lines.append(f"  {i:2d}. {keyword.capitalize():15s} ({count} calls)")
    lines.append("")

    # Model usage
    models: Counter[str] = Counter()
    for entry in entries:
        model = entry.get("model", "unknown")
        models[model] += 1

    if models:
        lines.append("## Models Used")
        lines.append("")
        for model, count in models.most_common():
            lines.append(f"  - {model}: {count} calls")
        lines.append("")

    # Footer
    lines.append("-" * 50)
    lines.append(f"Total: {total} calls | Log: logs/cli-tools.jsonl")

    return "\n".join(lines)


def export_to_csv(entries: list[dict[str, Any]]) -> Path:
    """Export logs to CSV file."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_file = EXPORT_DIR / f"cli-usage-{timestamp}.csv"

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp", "tool", "model", "success", "prompt", "response"],
        )
        writer.writeheader()
        for entry in entries:
            writer.writerow({
                "timestamp": entry.get("timestamp", ""),
                "tool": entry.get("tool", ""),
                "model": entry.get("model", ""),
                "success": entry.get("success", ""),
                "prompt": entry.get("prompt", "")[:500],
                "response": entry.get("response", "")[:500],
            })

    return csv_file


def generate_json_report(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate JSON format report."""
    codex_entries = [e for e in entries if e.get("tool") == "codex"]
    gemini_entries = [e for e in entries if e.get("tool") == "gemini"]

    daily: dict[str, dict[str, int]] = defaultdict(lambda: {"codex": 0, "gemini": 0})
    for entry in entries:
        date = entry["timestamp"][:10]
        tool = entry.get("tool", "unknown")
        if tool in ("codex", "gemini"):
            daily[date][tool] += 1

    all_keywords: list[str] = []
    for entry in entries:
        all_keywords.extend(extract_keywords(entry.get("prompt", "")))

    return {
        "total_calls": len(entries),
        "codex": {
            "calls": len(codex_entries),
            "success": sum(1 for e in codex_entries if e.get("success", False)),
        },
        "gemini": {
            "calls": len(gemini_entries),
            "success": sum(1 for e in gemini_entries if e.get("success", False)),
        },
        "daily": dict(daily),
        "keywords": dict(Counter(all_keywords).most_common(10)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze Codex/Gemini CLI usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        help="Only include data from last N days",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Export to CSV file",
    )
    args = parser.parse_args()

    # Calculate since date
    since = None
    if args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    # Load logs
    entries = load_logs(since)

    if args.json:
        report = generate_json_report(entries)
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.export:
        if not entries:
            print("No entries to export.")
            return
        csv_file = export_to_csv(entries)
        print(f"Exported to: {csv_file}")
    else:
        report = format_report(entries, args.days)
        print(report)


if __name__ == "__main__":
    main()
