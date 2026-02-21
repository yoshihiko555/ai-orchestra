#!/usr/bin/env python3
"""SessionStart hook: Plans.md からタスク状態を読み込み、セッション開始時にサマリーを出力する。

処理フロー:
1. .claude/Plans.md が存在するか確認
2. 状態マーカー（cc:TODO / cc:WIP / cc:done / cc:blocked）を解析
3. WIP / blocked / 次の TODO タスクをサマリーとして stdout に出力
4. stdout はセッションのコンテキストに注入される
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


# 状態マーカーのパターン
MARKER_PATTERN = re.compile(r"`(cc:(?:TODO|WIP|done|blocked))`")
BLOCKED_REASON_PATTERN = re.compile(r"—\s*理由:\s*(.+)$")


def read_hook_input() -> dict:
    """stdin から JSON を読み取って dict を返す。"""
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}


def get_project_dir(data: dict) -> str:
    """hook 入力からプロジェクトディレクトリを取得"""
    cwd = data.get("cwd") or ""
    if cwd:
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def load_config(project_dir: str) -> dict:
    """task-memory 設定を読み込む。"""
    try:
        orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        if orchestra_dir:
            core_hooks = os.path.join(orchestra_dir, "packages", "core", "hooks")
            if core_hooks not in sys.path:
                sys.path.insert(0, core_hooks)
            from hook_common import load_package_config

            return load_package_config("task-memory", "task-memory.yaml", project_dir)
    except Exception:
        pass
    return {
        "plans_file": ".claude/Plans.md",
        "show_summary_on_start": True,
        "max_display_tasks": 20,
    }


def parse_tasks(content: str) -> dict[str, list[dict[str, str]]]:
    """Plans.md の内容からタスクを状態別に分類する。

    Returns:
        {"WIP": [...], "TODO": [...], "done": [...], "blocked": [...]}
        各要素は {"task": str, "reason": str | None} の dict
    """
    tasks: dict[str, list[dict[str, str]]] = {
        "WIP": [],
        "TODO": [],
        "done": [],
        "blocked": [],
    }

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue

        marker_match = MARKER_PATTERN.search(stripped)
        if not marker_match:
            continue

        marker = marker_match.group(1)
        # マーカー以降のテキストをタスク名として取得
        after_marker = stripped[marker_match.end() :].strip()

        # blocked の理由を抽出
        reason = None
        if marker == "cc:blocked":
            reason_match = BLOCKED_REASON_PATTERN.search(after_marker)
            if reason_match:
                reason = reason_match.group(1).strip()
                after_marker = after_marker[: reason_match.start()].strip()

        state = marker.replace("cc:", "")
        tasks[state].append({"task": after_marker, "reason": reason})

    return tasks


def format_summary(tasks: dict[str, list[dict[str, str]]], max_display: int) -> str:
    """タスク状態のサマリーをフォーマットする。"""
    parts: list[str] = []

    # 統計
    total = sum(len(v) for v in tasks.values())
    stats = []
    for state in ("done", "WIP", "TODO", "blocked"):
        count = len(tasks[state])
        if count > 0:
            stats.append(f"{state}: {count}")
    parts.append(f"[task-memory] {total} tasks ({', '.join(stats)})")

    # WIP タスク（最優先で表示）
    if tasks["WIP"]:
        parts.append("  WIP:")
        for item in tasks["WIP"][:max_display]:
            parts.append(f"    - {item['task']}")

    # blocked タスク
    if tasks["blocked"]:
        parts.append("  Blocked:")
        for item in tasks["blocked"][:max_display]:
            reason = f" (理由: {item['reason']})" if item["reason"] else ""
            parts.append(f"    - {item['task']}{reason}")

    # 次の TODO（上位のみ）
    if tasks["TODO"]:
        remaining = max_display - len(tasks.get("WIP", [])) - len(tasks.get("blocked", []))
        show_count = max(3, remaining) if remaining > 0 else 3
        parts.append("  Next TODO:")
        for item in tasks["TODO"][:show_count]:
            parts.append(f"    - {item['task']}")
        if len(tasks["TODO"]) > show_count:
            parts.append(f"    ... and {len(tasks['TODO']) - show_count} more")

    return "\n".join(parts)


def main() -> None:
    data = read_hook_input()
    project_dir = get_project_dir(data)

    config = load_config(project_dir)

    if not config.get("show_summary_on_start", True):
        return

    plans_file = config.get("plans_file", ".claude/Plans.md")
    plans_path = Path(project_dir) / plans_file

    if not plans_path.is_file():
        return

    try:
        content = plans_path.read_text(encoding="utf-8")
    except OSError:
        return

    if not content.strip():
        return

    tasks = parse_tasks(content)

    # 1 つもタスクがなければ何も出力しない
    if not any(tasks.values()):
        return

    max_display = config.get("max_display_tasks", 20) or 20
    summary = format_summary(tasks, max_display)
    print(summary)


if __name__ == "__main__":
    main()
