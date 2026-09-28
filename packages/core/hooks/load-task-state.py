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

_HOOK_DIR = Path(__file__).resolve().parent
if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

# AC (Acceptance Criteria) 解析ロジックは共有モジュールへ切り出し済み（Issue #299）。
# task-state スキル支援ツール等、他コンポーネントからも ac_parser を直接 import して再利用できる。
from ac_parser import AC_SECTION_HEADING, classify_checkbox_line  # noqa: E402

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
ORDER_SECTION_HEADINGS = {
    "#### Goal",
    "#### Context",
    "#### Out of Scope",
    "#### Constraints",
    "#### Open Questions",
}

_FENCE_OPEN_PATTERN = re.compile(r"^(`{3,}|~{3,})")
_FENCE_CLOSE_PATTERN = re.compile(r"^(`{3,}|~{3,})\s*$")
_HTML_COMMENT_START = "<!--"
_HTML_COMMENT_END = "-->"
_HTML_COMMENT_ONLY_PATTERN = re.compile(r"^(?:<!--.*?-->\s*)+$", re.DOTALL)


def structural_exclusions(lines: list[str]) -> set[int]:
    """Plans.md の構造解析（見出し / cc: マーカー行判定）から除外すべき行番号を返す。

    以下の 3 つの関心事を一箇所にまとめ、ファイル内の全パーサが同じ判定に従うようにする:
    - 先頭の YAML frontmatter（先頭行が '---' で始まり、次に現れる、インデントのない
      単独 '---' 行まで）。閉じ側は raw line が '---' と完全一致する行でのみ終端する
      （strip() 一致にすると、YAML ブロックスカラー内のインデントされた '---' で
      誤って閉じてしまうため）。
    - フェンス付きコードブロック。フェンスは ` または ~ を3文字以上並べた行で開始し、
      「同じ文字」かつ「開始時の文字数以上の文字数」を持つ単独行でのみ閉じる
      （CommonMark に準拠したセマンティクス）。これにより、四連バッククォートの中に
      三連バッククォートが入れ子で登場しても、内側のフェンスで外側が誤って閉じない。
    - HTML コメント（`html_comment_line_indices` 参照）。複数行スパンのコメント内に
      見出しに見える文字列（例: '#### Context'）が現れても、見出し/箇条書き判定より
      前にコメント行として除外することで、パーサの状態機械が誤って遷移しないようにする。
    """
    excluded: set[int] = set()

    start = 0
    if lines and lines[0].strip() == "---":
        closing = None
        for i in range(1, len(lines)):
            if lines[i] == "---":
                closing = i
                break
        if closing is not None:
            excluded.update(range(0, closing + 1))
            start = closing + 1

    fence_char: str | None = None
    fence_len = 0
    for i in range(start, len(lines)):
        stripped = lines[i].strip()

        if fence_char is not None:
            excluded.add(i)
            close_match = _FENCE_CLOSE_PATTERN.match(stripped)
            if (
                close_match
                and stripped[:1] == fence_char
                and len(close_match.group(1)) >= fence_len
            ):
                fence_char = None
                fence_len = 0
            continue

        open_match = _FENCE_OPEN_PATTERN.match(stripped)
        if open_match:
            excluded.add(i)
            fence_char = stripped[0]
            fence_len = len(open_match.group(1))

    excluded |= html_comment_line_indices(lines)
    return excluded


def html_comment_line_indices(lines: list[str]) -> set[int]:
    """HTML コメント（<!-- ... -->）だけで構成される行の番号を返す（複数行スパン対応）。

    行が（strip 後）'<!--' で始まる場合にのみコメントスパンを開始する。
    箇条書き本文の中に現れるインラインコメント（例: '- <!-- memo -->'）は
    `is_html_comment_only` で呼び出し側が個別に判定する。
    """
    excluded: set[int] = set()
    in_comment = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if in_comment:
            excluded.add(i)
            if _HTML_COMMENT_END in stripped:
                in_comment = False
            continue
        if not stripped.startswith(_HTML_COMMENT_START):
            continue
        excluded.add(i)
        if _HTML_COMMENT_END not in stripped[len(_HTML_COMMENT_START) :]:
            in_comment = True
    return excluded


def is_html_comment_only(text: str) -> bool:
    """text（先頭の '- ' 等を除去済み）が HTML コメントのみで構成されるか判定する。"""
    stripped = text.strip()
    return bool(stripped) and bool(_HTML_COMMENT_ONLY_PATTERN.match(stripped))


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


def order_section_line_indices(lines: list[str]) -> set[int]:
    """Project 直下の order セクションに属する本文行番号を返す。"""
    excluded = structural_exclusions(lines)
    indices: set[int] = set()
    in_project = False
    phase_started = False
    in_order_section = False

    for line_index, line in enumerate(lines):
        if line_index in excluded:
            if in_order_section:
                indices.add(line_index)
            continue

        stripped = line.strip()

        if stripped.startswith("## "):
            in_project = stripped.startswith("## Project:")
            phase_started = False
            in_order_section = False
            continue

        if not in_project:
            continue

        if stripped.startswith("### "):
            phase_started = True
            in_order_section = False
            continue

        if stripped.startswith("#### "):
            in_order_section = not phase_started and stripped in ORDER_SECTION_HEADINGS
            continue

        if in_order_section:
            indices.add(line_index)

    return indices


def parse_orders(content: str) -> list[dict]:
    """Plans.md の Project ごとに Goal と Open Questions を抽出する。"""
    lines = content.splitlines()
    excluded = structural_exclusions(lines)
    order_lines = order_section_line_indices(lines)
    orders: list[dict] = []
    current_entry: dict | None = None
    phase_started = False
    current_section: str | None = None

    for line_index, line in enumerate(lines):
        if line_index in excluded:
            continue

        stripped = line.strip()

        if stripped.startswith("## "):
            if stripped.startswith("## Project:"):
                project_name = stripped.split("## Project:", 1)[1].strip()
                current_entry = {"name": project_name, "goal": None, "open_questions": 0}
                orders.append(current_entry)
                phase_started = False
            else:
                current_entry = None
            current_section = None
            continue

        if current_entry is None:
            continue

        if stripped.startswith("### "):
            phase_started = True
            current_section = None
            continue

        if stripped.startswith("#### "):
            current_section = (
                stripped if not phase_started and stripped in ORDER_SECTION_HEADINGS else None
            )
            continue

        if line_index not in order_lines:
            continue

        if current_section == "#### Goal" and current_entry["goal"] is None and stripped:
            goal = stripped[2:].strip() if stripped.startswith("- ") else stripped
            if re.match(r"^\[[ xX]\]\s+", goal):
                goal = goal[3:].strip()
            if is_html_comment_only(goal):
                continue
            if not goal.startswith("{"):
                current_entry["goal"] = goal
        elif current_section == "#### Open Questions" and line.startswith("- "):
            question = line[2:].strip()
            normalized = question.rstrip(".。").casefold()
            if not question.startswith("{") and normalized not in {"なし", "none", "n/a"}:
                current_entry["open_questions"] += 1

    return orders


def read_hook_input() -> dict:
    """stdin から JSON を読み取って dict を返す。"""
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}


def get_project_dir(data: dict) -> str:
    """hook 入力からプロジェクトディレクトリを取得する。"""
    try:
        from context_store import get_project_dir as get_context_project_dir

        return get_context_project_dir(data)
    except Exception:
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
) -> dict[str, list[dict[str, str | None]]]:
    """Plans.md の内容からタスクを状態別に分類する。

    Returns:
        {"WIP": [...], "TODO": [...], "done": [...], "blocked": [...]}
        各要素は {"task": str, "reason": str | None} の dict
    """
    if marker_pattern is None:
        marker_pattern = DEFAULT_MARKER_PATTERN
    if marker_to_state is None:
        marker_to_state = DEFAULT_MARKER_TO_STATE

    tasks: dict[str, list[dict[str, str | None]]] = {
        "WIP": [],
        "TODO": [],
        "done": [],
        "blocked": [],
    }

    lines = content.splitlines()
    excluded = structural_exclusions(lines)
    order_lines = order_section_line_indices(lines)
    in_ac_section = False
    for line_index, line in enumerate(lines):
        if line_index in excluded:
            continue

        stripped = line.strip()

        if stripped == AC_SECTION_HEADING:
            in_ac_section = True
            continue
        if stripped.startswith(("#### ", "### ", "## ")):
            in_ac_section = False
            continue

        if not stripped.startswith("- "):
            continue

        if line_index in order_lines:
            continue

        if in_ac_section and classify_checkbox_line(stripped) is not None:
            # AC セクション内のチェックボックス行は cc: マーカーの有無にかかわらずタスクではない
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


def format_summary(
    tasks: dict[str, list[dict[str, str | None]]],
    max_display: int | None,
    *,
    orders: list[dict] | None = None,
) -> str:
    """タスク状態のサマリーをフォーマットする。"""
    parts: list[str] = []

    # 統計
    total = sum(len(v) for v in tasks.values())
    stats = []
    for state in ("done", "WIP", "TODO", "blocked"):
        count = len(tasks[state])
        if count > 0:
            stats.append(f"{state}: {count}")
    if stats:
        parts.append(f"[task-memory] {total} tasks ({', '.join(stats)})")
    else:
        parts.append(f"[task-memory] {total} tasks")

    if orders:
        qualify = len(orders) > 1
        goals = [
            (project_order["name"], project_order.get("goal"))
            for project_order in orders
            if project_order.get("goal") is not None
        ]
        for project_name, goal in goals:
            label = f"Goal ({project_name})" if qualify else "Goal"
            parts.append(f"  {label}: {goal}")

        open_questions = [
            (project_order["name"], project_order.get("open_questions", 0))
            for project_order in orders
            if project_order.get("open_questions", 0) > 0
        ]
        for project_name, count in open_questions:
            label = f"Open Questions ({project_name})" if qualify else "Open Questions"
            parts.append(f"  {label}: {count}")

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

    if not config.get("show_summary_on_start", True):
        return

    if not content.strip():
        return

    orders = parse_orders(content)
    tasks = parse_tasks(content, marker_pattern, marker_to_state)

    has_orders = any(
        project_order.get("goal") or project_order.get("open_questions", 0) > 0
        for project_order in orders
    )
    if not any(tasks.values()) and not has_orders:
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
    summary = format_summary(tasks, max_display, orders=orders)
    print(summary)


if __name__ == "__main__":
    main()
