#!/usr/bin/env python3
"""オーケストレーションKPIの週次スコアカードを生成する。"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from collections import defaultdict


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


def percent(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def build_scorecard(route_rows: list[dict], quality_rows: list[dict]) -> dict:
    by_prompt: dict[str, list[dict]] = defaultdict(list)
    for row in route_rows:
        prompt_id = str(row.get("prompt_id") or "")
        if not prompt_id:
            continue
        by_prompt[prompt_id].append(row)

    observed_prompts = len(by_prompt)
    helper_only_prompts = 0
    first_attempt_matches = 0
    re_instruction_prompts = 0
    unnecessary_calls = 0
    external_calls = 0

    for prompt_rows in by_prompt.values():
        prompt_rows.sort(key=lambda r: str(r.get("timestamp") or ""))

        # ヘルパーを除外した実効レコード
        effective_rows = [r for r in prompt_rows if not r.get("is_helper", False)]
        if not effective_rows:
            helper_only_prompts += 1
            continue

        first = effective_rows[0]
        if bool(first.get("matched")):
            first_attempt_matches += 1

        expected = str(first.get("expected_route") or "")
        actual_routes = [str(r.get("actual_route") or "") for r in effective_rows]

        if len(set(actual_routes)) > 1:
            re_instruction_prompts += 1

        for route in actual_routes:
            if route in {"bash:codex", "bash:gemini"}:
                external_calls += 1
                if expected not in {"codex", "gemini"}:
                    unnecessary_calls += 1

    effective_prompts = observed_prompts - helper_only_prompts
    matched_rate = percent(first_attempt_matches, effective_prompts)
    re_instruction_rate = percent(re_instruction_prompts, effective_prompts)
    unnecessary_call_rate = percent(unnecessary_calls, external_calls)

    quality_total = len(quality_rows)
    quality_pass = 0
    for row in quality_rows:
        if row.get("passed") is True:
            quality_pass += 1

    quality_pass_rate = percent(quality_pass, quality_total)

    # 重み: 品質40 / 決定性30 / 効率20 / 可視性10
    quality_score = quality_pass_rate
    determinism_score = matched_rate
    efficiency_score = max(0.0, round(100.0 - unnecessary_call_rate, 2))
    visibility_score = 100.0 if effective_prompts > 0 else 0.0

    composite_score = round(
        quality_score * 0.40
        + determinism_score * 0.30
        + efficiency_score * 0.20
        + visibility_score * 0.10,
        2,
    )

    return {
        "summary": {
            "observed_prompts": observed_prompts,
            "effective_prompts": effective_prompts,
            "helper_only_prompts": helper_only_prompts,
            "external_calls": external_calls,
            "quality_checks": quality_total,
        },
        "metrics": {
            "expected_route_match_rate": matched_rate,
            "re_instruction_rate": re_instruction_rate,
            "unnecessary_call_rate": unnecessary_call_rate,
            "quality_pass_rate": quality_pass_rate,
        },
        "scores": {
            "quality": quality_score,
            "determinism": determinism_score,
            "efficiency": efficiency_score,
            "visibility": visibility_score,
            "composite": composite_score,
        },
    }


def render_markdown(card: dict, days: int) -> str:
    metrics = card["metrics"]
    scores = card["scores"]
    summary = card["summary"]

    lines = [
        f"# Orchestration KPI Scorecard ({days} days)",
        "",
        "## Summary",
        f"- observed_prompts: {summary['observed_prompts']}",
        f"- effective_prompts: {summary['effective_prompts']}",
        f"- helper_only_prompts: {summary['helper_only_prompts']}",
        f"- external_calls: {summary['external_calls']}",
        f"- quality_checks: {summary['quality_checks']}",
        "",
        "## Metrics",
        f"- expected_route_match_rate: {metrics['expected_route_match_rate']}%",
        f"- unnecessary_call_rate: {metrics['unnecessary_call_rate']}%",
        f"- re_instruction_rate: {metrics['re_instruction_rate']}%",
        f"- quality_pass_rate: {metrics['quality_pass_rate']}%",
        "",
        "## Weighted Scores",
        f"- quality (40): {scores['quality']}",
        f"- determinism (30): {scores['determinism']}",
        f"- efficiency (20): {scores['efficiency']}",
        f"- visibility (10): {scores['visibility']}",
        f"- composite: {scores['composite']}",
        "",
        "## Gate",
    ]

    match_rate = metrics["expected_route_match_rate"]
    if match_rate >= 90:
        gate = "Month 3 gate (>=90%) 達成"
    elif match_rate >= 85:
        gate = "Month 2 gate (>=85%) 達成"
    elif match_rate >= 80:
        gate = "Month 1 gate (>=80%) 達成"
    else:
        gate = "未達（まず 80% を目標）"

    lines.append(f"- {gate}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate orchestration KPI scorecard")
    parser.add_argument("--days", type=int, default=7, help="集計期間（日）")
    parser.add_argument(
        "--log-dir",
        default=".claude/logs/orchestration",
        help="ログディレクトリ",
    )
    parser.add_argument(
        "--out",
        default=".claude/logs/orchestration/scorecard.md",
        help="Markdown 出力先",
    )
    parser.add_argument(
        "--json-out",
        default=".claude/logs/orchestration/scorecard.json",
        help="JSON 出力先",
    )

    args = parser.parse_args()

    route_path = os.path.join(args.log_dir, "route-audit.jsonl")
    quality_path = os.path.join(args.log_dir, "quality-gate.jsonl")

    route_rows = filter_by_days(read_jsonl(route_path), args.days)
    quality_rows = filter_by_days(read_jsonl(quality_path), args.days)

    card = build_scorecard(route_rows, quality_rows)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    os.makedirs(os.path.dirname(args.json_out), exist_ok=True)

    markdown = render_markdown(card, args.days)

    with open(args.out, "w") as f:
        f.write(markdown + "\n")

    with open(args.json_out, "w") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    print(markdown)
    print(f"\nSaved: {args.out}")
    print(f"Saved: {args.json_out}")


if __name__ == "__main__":
    main()
