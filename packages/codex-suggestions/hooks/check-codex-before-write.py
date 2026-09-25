#!/usr/bin/env python3
"""
PreToolUse hook: Suggest Codex consultation before Write/Edit on design files.

Analyzes the file being modified and suggests Codex consultation
for design decisions, complex implementations, or architectural changes.
"""

import json
import os
import sys
from pathlib import Path

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import (  # noqa: E402
    DEFAULT_CODEX_FLAGS,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_SANDBOX_ANALYSIS,
    has_project_config,
    is_cli_enabled,
    load_package_config,
)

# Input validation constants
MAX_PATH_LENGTH = 4096
MAX_CONTENT_LENGTH = 1_000_000


def validate_input(file_path: str, content: str) -> bool:
    """Validate input for security."""
    if not file_path or len(file_path) > MAX_PATH_LENGTH:
        return False
    if len(content) > MAX_CONTENT_LENGTH:
        return False
    if ".." in file_path:
        return False
    return True


def _resolve_project_root(data: dict) -> str:
    """Resolve the project root used for file containment checks."""
    project_root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_root:
        return project_root
    return data.get("cwd", "") or ""


def _is_within_project(file_path: str, project_root: str) -> bool:
    """Return whether file_path resolves inside project_root."""
    if not project_root:
        return True

    try:
        resolved_project_root = Path(project_root).resolve()
        resolved_file_path = Path(file_path)
        if not resolved_file_path.is_absolute():
            resolved_file_path = resolved_project_root / resolved_file_path
        resolved_file_path = resolved_file_path.resolve()
    except (OSError, RuntimeError, ValueError):
        # Unlike to_relative_path() in update-working-context.py, this intentionally
        # fails open so an unexpected resolution error does not suppress a suggestion.
        return True

    return resolved_file_path.is_relative_to(resolved_project_root)


# Patterns that suggest design/architecture decisions
DESIGN_INDICATORS = [
    # File patterns
    "DESIGN.md",
    "ARCHITECTURE.md",
    "architecture",
    "design",
    "schema",
    "model",
    "interface",
    "abstract",
    "base_",
    "core/",
    "/core/",
    "config",
    "settings",
    # Code patterns
    "class ",
    "interface ",
    "abstract class",
    "def __init__",
    "from abc import",
    "Protocol",
    "@dataclass",
    "TypedDict",
]

# Files that are typically simple edits (skip suggestion)
SIMPLE_EDIT_PATTERNS = [
    ".gitignore",
    "README.md",
    "CHANGELOG.md",
    "requirements.txt",
    "package.json",
    "pyproject.toml",
    ".env.example",
]


def _size_trigger_reason(file_path: str, tool_name: str, *, source_file: bool) -> str:
    """Return a size-trigger reason that reflects the write operation."""
    if tool_name == "Edit":
        return "Large edit to source file" if source_file else "Large edit with significant content"
    if tool_name == "Write" and os.path.exists(file_path):
        return (
            "Overwriting existing source file"
            if source_file
            else "Overwriting existing file with significant content"
        )
    return "New source file" if source_file else "Creating new file with significant content"


def should_suggest_codex(
    file_path: str, content: str | None = None, *, tool_name: str = ""
) -> tuple[bool, str]:
    """Determine if Codex consultation should be suggested."""
    filepath_lower = file_path.lower()

    # Skip simple edits
    for pattern in SIMPLE_EDIT_PATTERNS:
        if pattern.lower() in filepath_lower:
            return False, ""

    # Check file path for design indicators
    for indicator in DESIGN_INDICATORS:
        if indicator.lower() in filepath_lower:
            return True, f"File path contains '{indicator}'"

    # Check content if available
    if content:
        if len(content) > 500:
            return True, _size_trigger_reason(file_path, tool_name, source_file=False)

        for indicator in DESIGN_INDICATORS:
            if indicator in content:
                return True, f"Content contains '{indicator}'"

    # New files in src/ directory
    if "/src/" in file_path or file_path.startswith("src/"):
        if content and len(content) > 200:
            return True, _size_trigger_reason(file_path, tool_name, source_file=True)

    return False, ""


def _build_codex_command(config: dict) -> str:
    """config から Codex コマンド文字列を構築する。"""
    codex = config.get("codex", {})
    model = codex.get("model", DEFAULT_CODEX_MODEL)
    sandbox = codex.get("sandbox", {}).get("analysis", DEFAULT_CODEX_SANDBOX_ANALYSIS)
    flags = codex.get("flags", DEFAULT_CODEX_FLAGS)
    flags_part = f"{flags} " if flags else ""
    return f"`codex exec --model {model} --sandbox {sandbox} {flags_part}'...' < /dev/null 2>/dev/null`"


def main():
    try:
        data = json.load(sys.stdin)

        # Codex CLI が無効化されている場合は提案をスキップ。
        # codex セクション自体が未定義の場合はデフォルト無効（2026-07-03 人間
        # レビュー裁定、EV-15）。他パッケージが共有する is_cli_enabled の
        # デフォルト（True）には影響しない。
        #
        # project が agent-routing を導入しておらず project-local な
        # cli-tools.yaml が存在しない場合、load_package_config はパッケージ
        # 同梱フォールバック（$AI_ORCHESTRA_DIR 配下）に暗黙フォールバックする。
        # そのフォールバック内の codex.enabled は project の明示 opt-in ではない
        # ため、先に has_project_config でガードする（Issue #129 PR #247
        # レビュー指摘: fallback config を opt-in 扱いしない）。
        project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
        if not has_project_config("agent-routing", "cli-tools.yaml", project_dir):
            sys.exit(0)
        config = load_package_config("agent-routing", "cli-tools.yaml", project_dir)
        if not is_cli_enabled("codex", config, default=False):
            sys.exit(0)

        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})
        file_path = tool_input.get("file_path", "")
        content = tool_input.get("content", "") or tool_input.get("new_string", "")

        if not validate_input(file_path, content):
            sys.exit(0)

        project_root = _resolve_project_root(data)
        if not _is_within_project(file_path, project_root):
            sys.exit(0)

        should_suggest, reason = should_suggest_codex(file_path, content, tool_name=tool_name)

        if should_suggest:
            codex_cmd = _build_codex_command(config)
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"[Codex Suggestion] {reason}. "
                        "Consider consulting Codex before this change:\n"
                        f"{codex_cmd}"
                    ),
                }
            }
            print(json.dumps(output))

        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
