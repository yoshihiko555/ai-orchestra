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
from datetime import date
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

            config = load_package_config("core", "task-memory.yaml", project_dir)
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


def detect_completed_projects(
    content: str, marker_pattern: re.Pattern[str], marker_to_state: dict[str, str]
) -> list[dict]:
    """Plans.md から完了済みプロジェクトを検出する。"""
    lines = content.splitlines()
    completed: list[dict] = []

    project_starts = [i for i, line in enumerate(lines) if line.startswith("## Project:")]
    for project_start in project_starts:
        project_end = len(lines) - 1
        for i in range(project_start + 1, len(lines)):
            if lines[i].startswith("## "):
                project_end = i - 1
                break

        if project_end < project_start:
            project_end = project_start

        phase_starts: list[int] = []
        for i in range(project_start, project_end + 1):
            if lines[i].startswith("### Phase"):
                phase_starts.append(i)

        # フェーズが 1 つもないプロジェクトは完了扱いにしない
        if not phase_starts:
            continue

        project_done = True
        for idx, phase_start in enumerate(phase_starts):
            phase_end = phase_starts[idx + 1] - 1 if idx + 1 < len(phase_starts) else project_end
            phase_header = lines[phase_start]

            header_marker_match = marker_pattern.search(phase_header)
            if header_marker_match:
                state = marker_to_state.get(header_marker_match.group(1))
                if state == "done":
                    continue
                project_done = False
                break

            # フェーズ見出しにマーカーがない場合は、配下タスクが全て done かどうかで判定
            phase_done = True
            has_task = False
            for line in lines[phase_start + 1 : phase_end + 1]:
                stripped = line.strip()
                if not stripped.startswith("- "):
                    continue

                has_task = True
                task_marker_match = marker_pattern.search(stripped)
                if not task_marker_match:
                    phase_done = False
                    break

                state = marker_to_state.get(task_marker_match.group(1))
                if state != "done":
                    phase_done = False
                    break

            # タスクが 1 件もないフェーズは未完了扱い
            if not has_task:
                phase_done = False

            if not phase_done:
                project_done = False
                break

        if project_done:
            project_name = lines[project_start].split("## Project:", 1)[1].strip()
            project_content = "\n".join(lines[project_start : project_end + 1])
            completed.append(
                {
                    "name": project_name,
                    "start_line": project_start,
                    "end_line": project_end,
                    "content": project_content,
                }
            )

    return completed


def archive_projects(
    plans_path: Path, archive_path: Path, completed: list[dict], content: str
) -> str:
    """完了済みプロジェクトを archive に移し、Plans.md から除去する。"""
    _ = plans_path  # シグネチャを維持しつつ将来拡張の余地を残す

    if not completed:
        return content

    lines = content.splitlines()
    removed_ranges: list[tuple[int, int]] = []

    for project in sorted(completed, key=lambda p: p["start_line"]):
        start = int(project["start_line"])
        end = int(project["end_line"])

        if start < 0 or start >= len(lines):
            continue
        end = min(end, len(lines) - 1)

        # プロジェクト直後の空行 + `---` 区切り線を除去範囲に含める
        cursor = end + 1
        while cursor < len(lines) and lines[cursor].strip() == "":
            cursor += 1
        if cursor < len(lines) and lines[cursor].strip() == "---":
            end = cursor

        removed_ranges.append((start, end))

    if removed_ranges:
        merged_ranges: list[list[int]] = []
        for start, end in removed_ranges:
            if not merged_ranges or start > merged_ranges[-1][1] + 1:
                merged_ranges.append([start, end])
            else:
                merged_ranges[-1][1] = max(merged_ranges[-1][1], end)

        kept_lines: list[str] = []
        for i, line in enumerate(lines):
            should_remove = any(start <= i <= end for start, end in merged_ranges)
            if not should_remove:
                kept_lines.append(line)
    else:
        kept_lines = list(lines)

    has_projects = any(line.startswith("## Project:") for line in kept_lines)
    moved_sections: list[str] = []

    if not has_projects:
        section_ranges: list[tuple[int, int]] = []
        i = 0
        while i < len(kept_lines):
            line = kept_lines[i]
            if line.startswith("## Decisions") or line.startswith("## Notes"):
                start = i
                j = i + 1
                while j < len(kept_lines):
                    if kept_lines[j].startswith("## "):
                        break
                    j += 1
                section_ranges.append((start, j - 1))
                moved_sections.append("\n".join(kept_lines[start:j]).strip())
                i = j
                continue
            i += 1

        if section_ranges:
            filtered_lines: list[str] = []
            for idx, line in enumerate(kept_lines):
                should_remove = any(start <= idx <= end for start, end in section_ranges)
                if not should_remove:
                    filtered_lines.append(line)
            kept_lines = filtered_lines

            # Decisions/Notes を削除した結果、末尾に残る区切り線を除去
            while kept_lines and kept_lines[-1].strip() == "":
                kept_lines.pop()
            if kept_lines and kept_lines[-1].strip() == "---":
                kept_lines.pop()
            while kept_lines and kept_lines[-1].strip() == "":
                kept_lines.pop()

    today = date.today().isoformat()
    archive_exists = archive_path.is_file()

    archive_blocks: list[str] = []
    if not archive_exists:
        archive_blocks.append("# Archived Plans")

    for project in completed:
        project_content_lines = project["content"].splitlines()
        while project_content_lines and project_content_lines[-1].strip() == "":
            project_content_lines.pop()
        if project_content_lines and project_content_lines[-1].strip() == "---":
            project_content_lines.pop()
        while project_content_lines and project_content_lines[-1].strip() == "":
            project_content_lines.pop()
        project_content = "\n".join(project_content_lines).strip()
        if not project_content:
            continue

        archive_blocks.append(f"## Archived: {today}")
        archive_blocks.append(project_content)
        archive_blocks.append("---")

    if moved_sections:
        archive_blocks.append(f"## Archived: {today}")
        archive_blocks.append("\n\n".join(section for section in moved_sections if section))
        archive_blocks.append("---")

    archive_text = "\n\n".join(block for block in archive_blocks if block).strip()
    if archive_text:
        if archive_exists:
            existing = archive_path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
            if existing.strip():
                archive_path.write_text(existing + "\n" + archive_text + "\n", encoding="utf-8")
            else:
                archive_path.write_text(archive_text + "\n", encoding="utf-8")
        else:
            archive_path.write_text(archive_text + "\n", encoding="utf-8")

    updated_content = "\n".join(kept_lines).rstrip()
    return updated_content + "\n" if updated_content else ""


def main() -> None:
    data = read_hook_input()
    project_dir = get_project_dir(data)

    # コンテキストディレクトリを初期化（冪等）
    try:
        from context_store import init_context_dir

        init_context_dir(project_dir)
    except Exception:
        pass  # context_store が利用できなくてもタスク状態表示は続行

    config = load_config(project_dir)

    plans_file = config.get("plans_file", ".claude/Plans.md")
    plans_path = Path(project_dir) / plans_file

    if not plans_path.is_file():
        return

    try:
        content = plans_path.read_text(encoding="utf-8")
    except OSError:
        return

    markers = resolve_markers(config)
    try:
        marker_pattern, marker_to_state = build_marker_parser(markers, strict=True)
    except ValueError as e:
        print(f"[task-memory] invalid markers config: {e}; fallback to defaults", file=sys.stderr)
        marker_pattern, marker_to_state = DEFAULT_MARKER_PATTERN, DEFAULT_MARKER_TO_STATE

    archive_path = plans_path.parent / "Plans.archive.md"
    completed = detect_completed_projects(content, marker_pattern, marker_to_state)
    if completed:
        try:
            content = archive_projects(plans_path, archive_path, completed, content)
            plans_path.write_text(content, encoding="utf-8")
            archived_names = [p["name"] for p in completed]
            print(
                f"[task-memory] archived {len(completed)} completed project(s): "
                + ", ".join(archived_names)
            )
        except OSError as e:
            print(f"[task-memory] archive failed, skipping: {e}", file=sys.stderr)

    if not config.get("show_summary_on_start", True):
        return

    if not content.strip():
        return

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
