#!/usr/bin/env python3
"""SessionStart hook: Plans.md からタスク状態を読み込み、セッション開始時にサマリーを出力する。

処理フロー:
1. .claude/Plans.md が存在するか確認
2. 状態マーカー（cc:TODO / cc:WIP / cc:done / cc:blocked）を解析
3. WIP / 次の TODO / blocked タスクをサマリーとして stdout に出力
4. stdout はセッションのコンテキストに注入される
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# 状態マーカー定義
DEFAULT_MARKERS = {
    "todo": "cc:TODO",
    "wip": "cc:WIP",
    "done": "cc:done",
    "blocked": "cc:blocked",
}
MARKER_STATE_MAP = {
    "todo": "TODO",
    "wip": "WIP",
    "done": "done",
    "blocked": "blocked",
}
BLOCKED_REASON_PATTERN = re.compile(r"—\s*理由:\s*(.+)$")


def resolve_markers(config: dict) -> dict[str, str]:
    """設定からマーカー定義を解決し、欠落時はデフォルトを使う。"""
    markers = dict(DEFAULT_MARKERS)
    configured_markers = config.get("markers")
    if isinstance(configured_markers, dict):
        for marker_key, default_marker in DEFAULT_MARKERS.items():
            value = configured_markers.get(marker_key)
            if isinstance(value, str) and value.strip():
                markers[marker_key] = value.strip()
            else:
                markers[marker_key] = default_marker
    return markers


def build_marker_parser(
    markers: dict[str, str], *, strict: bool = True
) -> tuple[re.Pattern[str], dict[str, str]]:
    """マーカー定義から parser 用 regex と marker->state の対応表を生成する。"""
    marker_to_state: dict[str, str] = {}
    for marker_key, state in MARKER_STATE_MAP.items():
        marker = markers.get(marker_key) or DEFAULT_MARKERS[marker_key]
        if marker and marker in marker_to_state:
            if strict:
                prev_state = marker_to_state[marker]
                raise ValueError(
                    f"marker '{marker}' is assigned to both '{prev_state}' and '{state}'"
                )
            continue
        if marker:
            marker_to_state[marker] = state

    escaped_markers = "|".join(re.escape(marker) for marker in marker_to_state)
    marker_pattern = re.compile(rf"`({escaped_markers})`")
    return marker_pattern, marker_to_state


DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE = build_marker_parser(DEFAULT_MARKERS)


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
    defaults = {
        "plans_file": ".claude/Plans.md",
        "show_summary_on_start": True,
        "max_display_tasks": 20,
        "markers": dict(DEFAULT_MARKERS),
    }

    candidate_core_hooks: list[Path] = []

    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if orchestra_dir:
        candidate_core_hooks.append(Path(orchestra_dir) / "packages" / "core" / "hooks")

    # AI_ORCHESTRA_DIR 未設定時でも、リポジトリ直下の packages/core/hooks を探索する。
    candidate_core_hooks.append(Path(__file__).resolve().parents[2] / "core" / "hooks")

    for core_hooks in candidate_core_hooks:
        try:
            if not core_hooks.is_dir():
                continue

            core_hooks_str = str(core_hooks)
            if core_hooks_str not in sys.path:
                sys.path.insert(0, core_hooks_str)

            from hook_common import load_package_config

            config = load_package_config("task-memory", "task-memory.yaml", project_dir)
            if config:
                return config
        except Exception:
            continue

    return defaults


def parse_tasks(
    content: str,
    marker_pattern: re.Pattern[str] | None = None,
    marker_to_state: dict[str, str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Plans.md の内容からタスクを状態別に分類する。

    Returns:
        {"WIP": [...], "TODO": [...], "done": [...], "blocked": [...]}
        各要素は {"task": str, "reason": str | None} の dict
    """
    if marker_pattern is None:
        marker_pattern = DEFAULT_MARKER_PATTERN
    if marker_to_state is None:
        marker_to_state = DEFAULT_MARKER_TO_STATE

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

        marker_match = marker_pattern.search(stripped)
        if not marker_match:
            continue

        marker = marker_match.group(1)
        state = marker_to_state.get(marker)
        if not state:
            continue

        # マーカー以降のテキストをタスク名として取得
        after_marker = stripped[marker_match.end() :].strip()

        # blocked の理由を抽出
        reason = None
        if state == "blocked":
            reason_match = BLOCKED_REASON_PATTERN.search(after_marker)
            if reason_match:
                reason = reason_match.group(1).strip()
                after_marker = after_marker[: reason_match.start()].strip()

        if not after_marker:
            continue

        tasks[state].append({"task": after_marker, "reason": reason})

    return tasks


def format_summary(tasks: dict[str, list[dict[str, str]]], max_display: int | None) -> str:
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

    if max_display is None:
        shown_wip = tasks["WIP"]
        shown_todo = tasks["TODO"]
        shown_blocked = tasks["blocked"]
    else:
        remaining = max(max_display, 0)
        shown_wip = tasks["WIP"][:remaining]
        remaining -= len(shown_wip)
        shown_todo = tasks["TODO"][:remaining]
        remaining -= len(shown_todo)
        shown_blocked = tasks["blocked"][:remaining]

    # WIP タスク（最優先で表示）
    if shown_wip:
        parts.append("  WIP:")
        for item in shown_wip:
            parts.append(f"    - {item['task']}")
        omitted_wip = len(tasks["WIP"]) - len(shown_wip)
        if omitted_wip > 0:
            parts.append(f"    ... and {omitted_wip} more")

    # 次の TODO（WIP の次に優先表示）
    if shown_todo:
        parts.append("  Next TODO:")
        for item in shown_todo:
            parts.append(f"    - {item['task']}")
        omitted_todo = len(tasks["TODO"]) - len(shown_todo)
        if omitted_todo > 0:
            parts.append(f"    ... and {omitted_todo} more")

    # blocked タスク
    if shown_blocked:
        parts.append("  Blocked:")
        for item in shown_blocked:
            reason = f" (理由: {item['reason']})" if item["reason"] else ""
            parts.append(f"    - {item['task']}{reason}")
        omitted_blocked = len(tasks["blocked"]) - len(shown_blocked)
        if omitted_blocked > 0:
            parts.append(f"    ... and {omitted_blocked} more")
    elif tasks["blocked"]:
        parts.append(f"  Blocked: (上限のため {len(tasks['blocked'])} 件省略)")

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

    markers = resolve_markers(config)
    try:
        marker_pattern, marker_to_state = build_marker_parser(markers, strict=True)
    except ValueError as e:
        print(f"[task-memory] invalid markers config: {e}; fallback to defaults", file=sys.stderr)
        marker_pattern, marker_to_state = DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE
    tasks = parse_tasks(content, marker_pattern, marker_to_state)

    # 1 つもタスクがなければ何も出力しない
    if not any(tasks.values()):
        return

    configured_max_display = config.get("max_display_tasks", 20)
    if isinstance(configured_max_display, str):
        try:
            configured_max_display = int(configured_max_display.strip())
        except ValueError:
            configured_max_display = None
    if configured_max_display == 0:
        max_display: int | None = None
    elif isinstance(configured_max_display, int) and configured_max_display > 0:
        max_display = configured_max_display
    else:
        max_display = 20
    summary = format_summary(tasks, max_display)
    print(summary)


if __name__ == "__main__":
    main()
