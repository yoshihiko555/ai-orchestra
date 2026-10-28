#!/usr/bin/env python3
"""
PreToolUse hook: Suggest Codex consultation before Write/Edit on design files.

Analyzes the file being modified and suggests Codex consultation
for design decisions, complex implementations, or architectural changes.
"""

import io
import json
import os
import re
import stat
import sys
import tokenize
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
from suggestion_common import claim_suggestion, is_subagent_context  # noqa: E402

# Input validation constants
MAX_PATH_LENGTH = 4096
MAX_CONTENT_LENGTH = 1_000_000
SOURCE_EXTENSIONS = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".java"}


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


DESIGN_FILENAMES = {"design.md", "architecture.md"}
DESIGN_DIRECTORIES = {"design", "architecture", "schema", "schemas", "interfaces", "contracts"}
JAVA_INTERFACE_DECLARATION = re.compile(
    r"^[ \t]*(?:(?:public|protected|private|static|abstract)[ \t]+)*"
    r"interface[ \t]+\w+[^\n{]*",
    re.MULTILINE,
)
CONTRACT_DECLARATION = re.compile(
    r"^[ \t]*(?:(?:export|public|protected|private)[ \t]+)*"
    r"(?:abstract[ \t]+class[ \t]+\w+[^\n{]*|"
    r"@abstractmethod\b)",
    re.MULTILINE,
)

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


def _contract_declarations(content: str, *, include_java_interfaces: bool = False) -> set[str]:
    declarations = {" ".join(match.split()) for match in CONTRACT_DECLARATION.findall(content)}
    if include_java_interfaces:
        declarations.update(
            " ".join(match.split()) for match in JAVA_INTERFACE_DECLARATION.findall(content)
        )
    header = None
    depth = 0
    in_bases = False
    has_contract_base = False
    try:
        # 基底クラスの前後を正規表現で走査すると、長い不完全な宣言で二次時間になる。
        for token in tokenize.generate_tokens(io.StringIO(content).readline):
            value = token.string
            if token.type == tokenize.NAME and value == "class":
                header = [value]
                depth = 0
                in_bases = False
                has_contract_base = False
            elif header is not None:
                if token.type in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
                    continue
                if token.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                    header = None
                    continue
                header.append(value)
                if token.type == tokenize.OP:
                    if value == "(" and depth == 0:
                        in_bases = True
                    if value in ("(", "["):
                        depth += 1
                    elif value in (")", "]"):
                        depth -= 1
                    elif value == ":" and depth == 0:
                        if has_contract_base:
                            declarations.add(" ".join(header))
                        header = None
                elif in_bases and token.type == tokenize.NAME and value in ("Protocol", "ABC"):
                    has_contract_base = True
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Edit の部分断片は完全な構文とは限らない。検出できた宣言だけを使う。
        pass
    return declarations


def should_suggest_codex(
    file_path: str,
    content: str | None = None,
    *,
    tool_name: str = "",
    old_content: str | None = None,
) -> tuple[bool, str]:
    """設計パスと契約宣言の変更だけを補助提案の対象にする。"""
    path = Path(file_path)
    if path.name.casefold() in {name.casefold() for name in SIMPLE_EDIT_PATTERNS}:
        return False, ""
    if path.name.casefold() in DESIGN_FILENAMES:
        return True, f"設計文書 {path.name} の変更"
    if any(part.casefold() in DESIGN_DIRECTORIES for part in path.parts[:-1]):
        return True, "設計・契約ディレクトリ内の変更"

    include_java_interfaces = path.suffix.casefold() == ".java"
    declarations = _contract_declarations(
        content or "", include_java_interfaces=include_java_interfaces
    )
    previous = _contract_declarations(
        old_content or "", include_java_interfaces=include_java_interfaces
    )
    if declarations != previous:
        return True, "契約・抽象インターフェース宣言の変更"
    return False, ""


def _read_previous_source(target: Path, project_root: str) -> str | None:
    """旧本文を上限内で読む。新規は空、比較できない場合は None を返す。"""
    if target.suffix.casefold() not in SOURCE_EXTENSIONS:
        return None
    root = Path(project_root).resolve() if project_root else target.parent
    directory_fd = None
    try:
        relative = target.relative_to(root)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(root, directory_flags)
        for part in relative.parts[:-1]:
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        # FIFO などは open 自体が待機し得るため、通常ファイル判定前も非ブロックにする。
        source_fd = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        with os.fdopen(source_fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return None
            raw = source.read(MAX_CONTENT_LENGTH * 4 + 1)
        if len(raw) > MAX_CONTENT_LENGTH * 4:
            return None
        content = raw.decode("utf-8")
        return content if len(content) <= MAX_CONTENT_LENGTH else None
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        return None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


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
        if is_subagent_context(data):
            sys.exit(0)

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
        project_dir = _resolve_project_root(data)
        if not has_project_config("agent-routing", "cli-tools.yaml", project_dir):
            sys.exit(0)
        config = load_package_config("agent-routing", "cli-tools.yaml", project_dir)
        if not is_cli_enabled("codex", config, default=False):
            sys.exit(0)

        tool_name = data.get("tool_name", "")
        if tool_name not in ("Edit", "Write"):
            sys.exit(0)
        tool_input = data.get("tool_input", {})
        file_path = tool_input.get("file_path", "")
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        old_content = tool_input.get("old_string", "") if tool_name == "Edit" else None

        if not validate_input(file_path, content) or not validate_input(
            file_path, old_content or ""
        ):
            sys.exit(0)

        project_root = _resolve_project_root(data)
        if not _is_within_project(file_path, project_root):
            sys.exit(0)
        target = Path(file_path)
        if project_root and not target.is_absolute():
            target = Path(project_root) / target
        target = target.resolve()
        relative_path = (
            str(target.relative_to(Path(project_root).resolve())) if project_root else file_path
        )
        if tool_name == "Write" and not should_suggest_codex(relative_path)[0]:
            old_content = _read_previous_source(target, project_root)
            if old_content is None:
                sys.exit(0)
        should_suggest, reason = should_suggest_codex(
            relative_path, content, tool_name=tool_name, old_content=old_content
        )

        if should_suggest and claim_suggestion(data, project_root, "codex-write", str(target)):
            codex_cmd = _build_codex_command(config)
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"[Codex Suggestion] {reason}. "
                        "任意の助言です。明示 routing・スキルの委譲指定を優先し、"
                        "相談済み・適用外なら追加相談せず続行できます。相談が有用な場合のみ:\n"
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
