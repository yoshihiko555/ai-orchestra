#!/usr/bin/env python3
"""Collect task state and git info for Codex CLI handoff.

Outputs JSON to stdout with:
- Plans.md tasks (WIP/TODO/blocked) and the per-Project order sections
- Decisions from Plans.md
- Git branch, branch status, recent commits, uncommitted diff stat, untracked files
- Working context (modified files)
- The handoff file path and the Codex launch command (or why it is unavailable)

Usage:
    python3 handoff.py [--project-dir PATH]
"""

from __future__ import annotations

import json
import os
import re
import shlex
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
_AC_CHECKBOX_PATTERN = re.compile(r"^- \[([ xX])\]")
AC_SECTION_HEADING = "#### Acceptance Criteria"
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


# Sensitive file patterns to exclude from diff
SENSITIVE_PATTERNS = {".env", "credentials", "secret", ".pem", ".key"}
# Same allowed set as hook_common._CODEX_MODEL_SAFE_PATTERN (a test pins the two together).
CODEX_MODEL_SAFE_PATTERN = re.compile(r"[A-Za-z0-9_.,:/@+=\-]+")
LAUNCH_SANDBOX = "workspace-write"
# Same candidates as git-workflow resolve_base_branch.CANDIDATES, plus origin/HEAD at runtime.
INTEGRATION_BRANCHES = ("staging", "stage", "develop", "main", "master")
UNTRACKED_LIMIT = 50


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
    """Group flat `tasks` (from `parse_tasks`, which carries a "project_index" field
    per item — the 0-based ordinal of the enclosing `## Project:` heading in document
    order) by that ordinal and attach them to the order entry at the same list
    position in `orders` (mutates `orders` in place).

    Matching by position (rather than by the "project" name field) keeps duplicate
    `## Project: <same name>` sections independent: `parse_order_sections` appends
    exactly one entry per `## Project:` heading in document order, so `orders[i]`
    always corresponds to `project_index == i`, even when two headings share a name.

    Each entry that has at least one task gets `entry["tasks"] = {"WIP": [...],
    "TODO": [...], "blocked": [...]}`. Task dicts inside `entry["tasks"]` omit the
    now-redundant "project"/"project_index" keys (implied by the surrounding order
    entry) and keep only `{"task": str, "reason": str | None}`.

    Tasks whose `project_index` is `None` (not under any `## Project:` heading) are
    not attached anywhere here — they remain visible via the flat top-level `tasks`
    dict.
    """
    by_index: dict[int, dict[str, list[dict[str, str | None]]]] = {}
    for state, items in tasks.items():
        for item in items:
            project_index = item.get("project_index")
            if project_index is None:
                continue
            bucket = by_index.setdefault(project_index, {"WIP": [], "TODO": [], "blocked": []})
            bucket[state].append({"task": item["task"], "reason": item["reason"]})

    for index, entry in enumerate(orders):
        project_tasks = by_index.get(index)
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

    Each task dict also carries `project` and its 0-based `project_index`.
    """
    tasks: dict[str, list[dict[str, str | None]]] = {
        "WIP": [],
        "TODO": [],
        "blocked": [],
    }

    lines = content.splitlines()
    excluded = structural_exclusions(lines)
    order_lines = _order_section_line_indices(lines)
    in_ac_section = False
    current_project: str | None = None
    current_project_index: int | None = None
    project_ordinal = -1

    for line_index, line in enumerate(lines):
        if line_index in excluded:
            continue

        stripped = line.strip()

        if stripped.startswith(("## ", "### ", "#### ")):
            if stripped.startswith("## "):
                if stripped.startswith("## Project:"):
                    project_ordinal += 1
                    current_project = stripped.split("## Project:", 1)[1].strip()
                    current_project_index = project_ordinal
                else:
                    current_project = None
                    current_project_index = None
            in_ac_section = stripped == AC_SECTION_HEADING
            continue

        if not stripped.startswith("- "):
            continue
        if line_index in order_lines:
            continue
        if in_ac_section and _AC_CHECKBOX_PATTERN.match(stripped):
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
            tasks[state].append(
                {
                    "task": task_text,
                    "reason": reason,
                    "project": current_project,
                    "project_index": current_project_index,
                }
            )

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


def is_sensitive_path(path: str) -> bool:
    """Return True if the path matches one of the sensitive file patterns."""
    lowered = path.lower()
    return any(pat in lowered for pat in SENSITIVE_PATTERNS)


def get_untracked_files(cwd: Path, limit: int = UNTRACKED_LIMIT) -> tuple[list[str], bool]:
    """List untracked (not ignored) files, excluding sensitive paths.

    Returns the first ``limit`` paths and whether the list was truncated.
    """
    # Read bytes: text mode would translate a CR inside a file name into LF.
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=cwd,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return [], False
    if result.returncode != 0:
        return [], False
    names = [os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]
    files = [path for path in names if not is_sensitive_path(path)]
    return files[:limit], len(files) > limit


def get_default_branch(cwd: Path) -> str | None:
    """Return the remote default branch from ``origin/HEAD`` (e.g. ``main``), if known."""
    ref = run_git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd)
    if not ref:
        return None
    _remote, _, branch = ref.partition("/")
    return branch or None


def get_branch_status(cwd: Path) -> dict[str, str | bool | None]:
    """Describe the current branch and whether committing on it would hit an integration branch."""
    if run_git(["rev-parse", "--is-inside-work-tree"], cwd) != "true":
        return {
            "git_repository": False,
            "current": None,
            "detached": False,
            "default_branch": None,
            "on_integration_branch": False,
        }
    current = run_git(["branch", "--show-current"], cwd) or None
    default_branch = get_default_branch(cwd)
    integration = set(INTEGRATION_BRANCHES)
    if default_branch:
        integration.add(default_branch)
    return {
        "git_repository": True,
        "current": current,
        "detached": current is None,
        "default_branch": default_branch,
        "on_integration_branch": current is None or current in integration,
    }


def handoff_file_path(project_dir: Path, now: datetime) -> Path:
    """Absolute path of the handoff file the skill writes for this run (never an existing file)."""
    handoffs_dir = project_dir.resolve() / ".claude" / "handoffs"
    stem = now.strftime("%Y%m%d-%H%M%S")
    candidate = handoffs_dir / f"{stem}.md"
    suffix = 2
    while candidate.exists():
        candidate = handoffs_dir / f"{stem}-{suffix}.md"
        suffix += 1
    return candidate


def load_cli_tools(project_dir: Path) -> dict | None:
    """Load cli-tools.yaml (+ .local.yaml) through hook_common, or None if unavailable."""
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if not orchestra_dir:
        return None
    hooks_dir = str(Path(orchestra_dir) / "packages" / "core" / "hooks")
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    try:
        from hook_common import load_cli_tools_config

        if not _local_override_is_readable(project_dir):
            return None
        config = load_cli_tools_config(str(project_dir))
    except Exception:
        return None
    return config if isinstance(config, dict) and config else None


def _local_override_is_readable(project_dir: Path) -> bool:
    """False if the project's cli-tools.local.yaml exists but cannot be parsed as a mapping.

    hook_common treats an unreadable override as empty, which would fall back to the base
    values; the launch command must not be built from base values the user meant to override.
    """
    local_path = project_dir / ".claude" / "config" / "agent-routing" / "cli-tools.local.yaml"
    if not local_path.is_file():
        return True
    try:
        import yaml

        data = yaml.safe_load(local_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data is None or isinstance(data, dict)


def _launch_unavailable(reason: str) -> dict[str, str | bool | None]:
    return {"available": False, "command": None, "reason": reason}


def build_launch(
    project_dir: Path, handoff_path: Path, config: dict | None
) -> dict[str, str | bool | None]:
    """Build the Codex launch command for the handoff file, failing closed on unsafe config.

    ``codex.flags`` is for ``codex exec`` and is intentionally not part of the interactive
    launch command.
    """
    if config is None:
        return _launch_unavailable("cli-tools.yaml could not be loaded")
    codex = config.get("codex")
    if not isinstance(codex, dict) or codex.get("enabled") is not True:
        return _launch_unavailable("codex.enabled is not true")
    sandbox_section = codex.get("sandbox")
    sandbox = sandbox_section.get("implementation") if isinstance(sandbox_section, dict) else None
    if sandbox != LAUNCH_SANDBOX:
        return _launch_unavailable(f"codex.sandbox.implementation is not {LAUNCH_SANDBOX}")
    model = codex.get("model")
    if not isinstance(model, str) or not CODEX_MODEL_SAFE_PATTERN.fullmatch(model):
        return _launch_unavailable(
            "codex.model is missing or contains characters outside the allowed set"
        )
    command = (
        f"codex -C {shlex.quote(str(project_dir))} --model {shlex.quote(model)} "
        f'--sandbox {shlex.quote(sandbox)} "$(cat {shlex.quote(str(handoff_path))})"'
    )
    return {"available": True, "command": command, "reason": None}


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

    now = datetime.now(UTC)
    branch = get_branch(project_dir)
    commits = get_recent_commits(project_dir)
    diff_stat = filter_sensitive_lines(get_diff_stat(project_dir))
    untracked, untracked_truncated = get_untracked_files(project_dir)
    working_ctx = load_working_context(project_dir)
    handoff_path = handoff_file_path(project_dir, now)

    return {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "project_dir": str(project_dir),
        "branch": branch,
        "branch_status": get_branch_status(project_dir),
        "handoff_path": str(handoff_path),
        "launch": build_launch(project_dir.resolve(), handoff_path, load_cli_tools(project_dir)),
        "tasks": tasks,
        "decisions": decisions,
        "order": orders,
        "order_markdown": render_order_markdown(orders),
        "recent_commits": commits,
        "diff_stat": diff_stat,
        "untracked_files": untracked,
        "untracked_truncated": untracked_truncated,
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
