#!/usr/bin/env python3
"""Collect task state and git info for Codex CLI handoff.

Outputs JSON to stdout with:
- Plans.md tasks (WIP/TODO/blocked)
- Decisions from Plans.md
- Git branch, recent commits, uncommitted diff stat
- Working context (modified files)

Usage:
    python3 handoff.py [--project-dir PATH]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Plans.md marker patterns (same as load-task-state.py)
MARKER_PATTERN = re.compile(r"`(cc:TODO|cc:WIP|cc:done|cc:blocked)`")
MARKER_TO_STATE = {
    "cc:TODO": "TODO",
    "cc:WIP": "WIP",
    "cc:done": "done",
    "cc:blocked": "blocked",
}
BLOCKED_REASON_PATTERN = re.compile(r"—\s*理由:\s*(.+)$")
ORDER_SECTION_HEADINGS = {
    "#### Goal",
    "#### Context",
    "#### Out of Scope",
    "#### Constraints",
    "#### Open Questions",
}
HANDOFF_ORDER_SECTIONS = (
    ("goal", "#### Goal"),
    ("context", "#### Context"),
    ("constraints", "#### Constraints"),
)

_FENCE_OPEN_PATTERN = re.compile(r"^(`{3,}|~{3,})")
_FENCE_CLOSE_PATTERN = re.compile(r"^(`{3,}|~{3,})\s*$")
_HTML_COMMENT_START = "<!--"
_HTML_COMMENT_END = "-->"
_HTML_COMMENT_ONLY_PATTERN = re.compile(r"^(?:<!--.*?-->\s*)+$", re.DOTALL)


def structural_exclusions(lines: list[str]) -> set[int]:
    """Plans.md の構造解析（見出し / cc: マーカー行判定）から除外すべき行番号を返す。

    以下の 2 つの関心事を一箇所にまとめ、ファイル内の全パーサが同じ判定に従うようにする:
    - 先頭の YAML frontmatter（先頭行が '---' で始まり、次に現れる単独 '---' 行まで）。
    - フェンス付きコードブロック。フェンスは ` または ~ を3文字以上並べた行で開始し、
      「同じ文字」かつ「開始時の文字数以上の文字数」を持つ単独行でのみ閉じる
      （CommonMark に準拠したセマンティクス）。これにより、四連バッククォートの中に
      三連バッククォートが入れ子で登場しても、内側のフェンスで外側が誤って閉じない。
    """
    excluded: set[int] = set()

    start = 0
    if lines and lines[0].strip() == "---":
        closing = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
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


# Sensitive file patterns to exclude from diff
SENSITIVE_PATTERNS = {".env", "credentials", "secret", ".pem", ".key"}


def find_project_root(start: Path | None = None) -> Path:
    """Find project root by locating .claude directory."""
    cwd = start or Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".claude").is_dir():
            return parent
    return cwd


def run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _order_section_line_indices(lines: list[str]) -> set[int]:
    """Return body line indices for pre-Phase order sections under Projects."""
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


def parse_order_sections(content: str) -> list[dict[str, list[str] | str]]:
    """Extract Goal, Context, and Constraints bullets for each Project."""
    lines = content.splitlines()
    excluded = structural_exclusions(lines)
    order_lines = _order_section_line_indices(lines)
    heading_to_key = {heading: key for key, heading in HANDOFF_ORDER_SECTIONS}
    orders: list[dict[str, list[str] | str]] = []
    current_entry: dict[str, list[str] | str] | None = None
    phase_started = False
    current_section: str | None = None
    last_bullet_list: list[str] | None = None

    for line_index, line in enumerate(lines):
        if line_index in excluded:
            last_bullet_list = None
            continue

        stripped = line.strip()

        if stripped.startswith("## "):
            if stripped.startswith("## Project:"):
                project_name = stripped.split("## Project:", 1)[1].strip()
                current_entry = {"name": project_name}
                orders.append(current_entry)
                phase_started = False
            else:
                current_entry = None
            current_section = None
            last_bullet_list = None
            continue

        if current_entry is None:
            continue

        if stripped.startswith("### "):
            phase_started = True
            current_section = None
            last_bullet_list = None
            continue

        if stripped.startswith("#### "):
            current_section = heading_to_key.get(stripped) if not phase_started else None
            last_bullet_list = None
            continue

        if line_index not in order_lines or current_section is None:
            last_bullet_list = None
            continue

        bullet_line = line.lstrip()
        is_indented = line != bullet_line
        is_bullet = bullet_line.startswith("- ")

        if is_bullet:
            bullet_text = bullet_line[2:]
            if (
                bullet_text.strip()
                and not bullet_text.strip().startswith("{")
                and not is_html_comment_only(bullet_text)
            ):
                bullets = current_entry.setdefault(current_section, [])
                if isinstance(bullets, list):
                    bullets.append(bullet_text)
                    last_bullet_list = bullets
            else:
                last_bullet_list = None
            continue

        if last_bullet_list is not None and is_indented and bullet_line.strip():
            last_bullet_list[-1] = f"{last_bullet_list[-1]} {bullet_line.strip()}"
            continue

        last_bullet_list = None

    return orders


STATE_TO_MARKER = {"WIP": "cc:WIP", "TODO": "cc:TODO", "blocked": "cc:blocked"}


def attach_tasks_to_orders(
    orders: list[dict], tasks: dict[str, list[dict[str, str | None]]]
) -> None:
    """Group flat `tasks` (from `parse_tasks`, which carries a "project" field per
    item) by project name and attach them to the matching order entry in `orders`
    (mutates `orders` in place).

    Each entry that has at least one task gets `entry["tasks"] = {"WIP": [...],
    "TODO": [...], "blocked": [...]}`. Task dicts inside `entry["tasks"]` omit the
    now-redundant "project" key (it is implied by the surrounding order entry) and
    keep only `{"task": str, "reason": str | None}`.

    Entries in `orders` are matched by `entry["name"] == item["project"]`. Tasks
    whose `project` is `None` (not under any `## Project:` heading) are not
    attached anywhere here — `parse_order_sections` never creates a bare
    `{"name": None}` entry for them, so there is nothing to attach to; they remain
    visible via the flat top-level `tasks` dict.
    """
    by_project: dict[str, dict[str, list[dict[str, str | None]]]] = {}
    for state, items in tasks.items():
        for item in items:
            project = item.get("project")
            if project is None:
                continue
            bucket = by_project.setdefault(project, {"WIP": [], "TODO": [], "blocked": []})
            bucket[state].append({"task": item["task"], "reason": item["reason"]})

    for entry in orders:
        project_tasks = by_project.get(entry["name"])
        if project_tasks and any(project_tasks.values()):
            entry["tasks"] = project_tasks


def _render_task_bullets(project_tasks: dict[str, list[dict[str, str | None]]]) -> list[str]:
    r"""Render a project's WIP/TODO/blocked tasks as `- \`cc:STATE\` text` bullets."""
    bullets: list[str] = []
    for state in ("WIP", "TODO", "blocked"):
        for item in project_tasks.get(state, []):
            marker = STATE_TO_MARKER[state]
            reason = item.get("reason")
            suffix = f" — 理由: {reason}" if state == "blocked" and reason else ""
            bullets.append(f"- `{marker}` {item['task']}{suffix}")
    return bullets


def render_order_markdown(orders: list[dict]) -> str:
    """Render extracted order data (and any attached per-project tasks) as Markdown."""
    projects = [
        entry
        for entry in orders
        if any(entry.get(key) for key, _heading in HANDOFF_ORDER_SECTIONS) or entry.get("tasks")
    ]
    if not projects:
        return ""

    lines = ["## Order"]
    for entry in projects:
        lines.extend(["", f"### {entry['name']}"])
        for key, heading in HANDOFF_ORDER_SECTIONS:
            bullets = entry.get(key)
            if not bullets:
                continue
            lines.extend(["", heading, ""])
            lines.extend(f"- {bullet}" for bullet in bullets)

        task_bullets = _render_task_bullets(entry["tasks"]) if entry.get("tasks") else []
        if task_bullets:
            lines.extend(["", "#### Tasks", ""])
            lines.extend(task_bullets)

    return "\n".join(lines)


def parse_tasks(content: str) -> dict[str, list[dict[str, str | None]]]:
    """Parse Plans.md content and extract tasks by state.

    Each task dict is `{"task": str, "reason": str | None, "project": str | None}`.
    `project` is the name from the nearest enclosing `## Project:` heading, or `None`
    if the task is not under any `## Project:` section (legacy flat Plans.md).
    """
    tasks: dict[str, list[dict[str, str | None]]] = {
        "WIP": [],
        "TODO": [],
        "blocked": [],
    }

    lines = content.splitlines()
    excluded = structural_exclusions(lines)
    order_lines = _order_section_line_indices(lines)
    current_project: str | None = None

    for line_index, line in enumerate(lines):
        if line_index in excluded:
            continue

        stripped = line.strip()

        if stripped.startswith("## "):
            current_project = (
                stripped.split("## Project:", 1)[1].strip()
                if stripped.startswith("## Project:")
                else None
            )

        if not stripped.startswith("- "):
            continue
        if line_index in order_lines:
            continue

        match = MARKER_PATTERN.search(stripped)
        if not match:
            continue

        marker = match.group(1)
        state = MARKER_TO_STATE.get(marker)
        if not state or state == "done":
            continue

        task_text = stripped[match.end() :].strip()

        reason = None
        if state == "blocked":
            reason_match = BLOCKED_REASON_PATTERN.search(task_text)
            if reason_match:
                reason = reason_match.group(1).strip()
                task_text = task_text[: reason_match.start()].strip()

        if task_text:
            tasks[state].append({"task": task_text, "reason": reason, "project": current_project})

    return tasks


def parse_decisions(content: str) -> list[str]:
    """Extract decisions from Plans.md ## Decisions section."""
    lines = content.splitlines()
    excluded = structural_exclusions(lines)
    decisions: list[str] = []
    in_decisions = False

    for line_index, line in enumerate(lines):
        if line_index in excluded:
            continue
        if line.startswith("## Decisions"):
            in_decisions = True
            continue
        if in_decisions and line.startswith("## "):
            break
        if in_decisions and line.strip().startswith("- "):
            decision = line.strip()[2:].strip()
            if decision and not decision.startswith("{"):
                decisions.append(decision)

    return decisions


def get_branch(cwd: Path) -> str:
    """Get current git branch name."""
    return run_git(["branch", "--show-current"], cwd) or "unknown"


def get_recent_commits(cwd: Path, count: int = 5) -> list[dict[str, str]]:
    """Get recent git commits."""
    output = run_git(
        ["log", f"-{count}", "--pretty=format:%h|%s"],
        cwd,
    )
    if not output:
        return []

    commits = []
    for line in output.splitlines():
        parts = line.split("|", 1)
        if len(parts) == 2:
            commits.append({"hash": parts[0], "message": parts[1]})
    return commits


def get_diff_stat(cwd: Path) -> str:
    """Get uncommitted changes as diff --stat output."""
    # Include both staged and unstaged
    stat = run_git(["diff", "--stat", "HEAD"], cwd)
    if not stat:
        # Maybe no commits yet, try just diff
        stat = run_git(["diff", "--stat"], cwd)
    return stat or ""


def filter_sensitive_lines(diff_stat: str) -> str:
    """Remove lines referencing sensitive files from diff stat."""
    if not diff_stat:
        return ""
    filtered = []
    for line in diff_stat.splitlines():
        if any(pat in line.lower() for pat in SENSITIVE_PATTERNS):
            continue
        filtered.append(line)
    return "\n".join(filtered)


def load_working_context(project_root: Path) -> dict:
    """Load working-context.json if available."""
    ctx_file = project_root / ".claude" / "context" / "shared" / "working-context.json"
    if not ctx_file.is_file():
        return {}
    try:
        return json.loads(ctx_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def collect_handoff_data(project_dir: Path) -> dict:
    """Collect all data needed for the handoff file."""
    plans_path = project_dir / ".claude" / "Plans.md"

    if not plans_path.is_file():
        return {"error": "Plans.md not found at .claude/Plans.md"}

    content = plans_path.read_text(encoding="utf-8")
    tasks = parse_tasks(content)
    decisions = parse_decisions(content)
    orders = parse_order_sections(content)
    attach_tasks_to_orders(orders, tasks)

    branch = get_branch(project_dir)
    commits = get_recent_commits(project_dir)
    diff_stat = filter_sensitive_lines(get_diff_stat(project_dir))
    working_ctx = load_working_context(project_dir)

    return {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "project_dir": str(project_dir),
        "branch": branch,
        "tasks": tasks,
        "decisions": decisions,
        "order": orders,
        "order_markdown": render_order_markdown(orders),
        "recent_commits": commits,
        "diff_stat": diff_stat,
        "working_context": working_ctx,
        "plans_content": content,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Collect handoff data for Codex CLI")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="Project root directory (default: auto-detect)",
    )
    args = parser.parse_args()

    project_dir = args.project_dir or find_project_root()
    data = collect_handoff_data(project_dir)
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
