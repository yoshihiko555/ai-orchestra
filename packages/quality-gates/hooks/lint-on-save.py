#!/usr/bin/env python3
"""
PostToolUse hook: 対応ファイルの編集後に formatter / linter を実行する。

編集されたファイルパスから言語種別を判定し、
適切なツール群へ振り分ける。
"""

import json
import subprocess
import sys
from pathlib import Path

PYTHON_EXTENSIONS = {".py"}
JS_TS_EXTENSIONS = {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
PRETTIER_EXTENSIONS = {".css", ".html", ".json", ".jsonc", ".md", ".yaml", ".yml"}
SHELL_EXTENSIONS = {".bash", ".sh", ".zsh"}
GO_EXTENSIONS = {".go"}
RUST_EXTENSIONS = {".rs"}

MISSING_TOOL_PATTERNS = (
    "command not found",
    "could not determine executable to run",
    "executable file not found",
    "couldn't find a package.json file",
    "package.json not found",
)


def is_shell_script(file_path: str) -> bool:
    """拡張子または shebang から shell script かどうかを判定する。"""
    path = Path(file_path)
    if path.suffix.lower() in SHELL_EXTENSIONS:
        return True
    if path.suffix:
        return False

    try:
        first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (FileNotFoundError, IndexError, OSError):
        return False

    return first_line.startswith("#!") and any(
        shell in first_line for shell in ("sh", "bash", "zsh")
    )


def get_file_kind(file_path: str) -> str | None:
    """ファイルパスに対応するツール種別を返す。"""
    suffix = Path(file_path).suffix.lower()
    if suffix in PYTHON_EXTENSIONS:
        return "python"
    if suffix in JS_TS_EXTENSIONS:
        return "javascript"
    if suffix in PRETTIER_EXTENSIONS:
        return "prettier"
    if suffix in GO_EXTENSIONS:
        return "go"
    if suffix in RUST_EXTENSIONS:
        return "rust"
    if is_shell_script(file_path):
        return "shell"
    return None


def node_tool_commands(tool: str, *args: str) -> list[list[str]]:
    """Node 系ツールの実行コマンド候補を組み立てる。"""
    return [
        ["pnpm", "exec", tool, *args],
        ["npm", "exec", "--", tool, *args],
        ["yarn", tool, *args],
        ["npx", "--no-install", tool, *args],
        [tool, *args],
    ]


def build_lint_steps(file_path: str) -> list[dict]:
    """ファイル種別ごとの formatter / linter 実行手順を返す。"""
    kind = get_file_kind(file_path)
    if kind == "python":
        return [
            {
                "name": "ruff format",
                "commands": [
                    ["uv", "run", "ruff", "format", file_path],
                    ["ruff", "format", file_path],
                ],
            },
            {
                "name": "ruff check",
                "commands": [
                    ["uv", "run", "ruff", "check", "--fix", file_path],
                    ["ruff", "check", "--fix", file_path],
                ],
            },
        ]
    if kind == "javascript":
        return [
            {
                "name": "biome check",
                "commands": node_tool_commands("biome", "check", "--write", file_path),
            },
            {
                "name": "prettier",
                "commands": node_tool_commands("prettier", "--write", file_path),
            },
            {
                "name": "eslint",
                "commands": node_tool_commands("eslint", "--fix", file_path),
            },
        ]
    if kind == "prettier":
        return [
            {
                "name": "prettier",
                "commands": node_tool_commands("prettier", "--write", file_path),
            }
        ]
    if kind == "shell":
        return [
            {
                "name": "shfmt",
                "commands": [["shfmt", "-w", file_path]],
            },
            {
                "name": "shellcheck",
                "commands": [["shellcheck", file_path]],
            },
        ]
    if kind == "go":
        return [
            {
                "name": "gofmt",
                "commands": [["gofmt", "-w", file_path]],
            }
        ]
    if kind == "rust":
        return [
            {
                "name": "rustfmt",
                "commands": [["rustfmt", file_path]],
            }
        ]
    return []


def is_missing_tool_output(output: str) -> bool:
    """出力内容から「ツール未導入による失敗」かを判定する。"""
    lowered = output.lower()
    if 'command "' in lowered and '" not found' in lowered:
        return True
    return any(pattern in lowered for pattern in MISSING_TOOL_PATTERNS)


def run_step(step: dict, file_dir: str) -> dict | None:
    """1つの手順をフォールバック付きで実行する。"""
    for cmd in step["commands"]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                cwd=file_dir,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

        output = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0:
            return {
                "name": step["name"],
                "success": True,
                "output": output,
            }
        if is_missing_tool_output(output):
            continue
        return {
            "name": step["name"],
            "success": False,
            "output": output,
        }
    return None


def run_lint_commands(file_path: str) -> list[dict]:
    """対象ファイル向けの手順を順に実行し、結果を返す。"""
    results = []
    file_dir = str(Path(file_path).parent)

    for step in build_lint_steps(file_path):
        result = run_step(step, file_dir)
        if result is not None:
            results.append(result)

    return results


def main() -> None:
    """PostToolUse hook のエントリポイント。"""
    try:
        data = json.load(sys.stdin)
        tool_name = data.get("tool_name", "")

        if tool_name not in ("Edit", "Write"):
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        file_path = tool_input.get("file_path", "")

        if not build_lint_steps(file_path):
            sys.exit(0)

        results = run_lint_commands(file_path)
        if not results:
            sys.exit(0)

        # 実際に動いたツールだけをユーザーへ通知する。
        messages = []
        has_issues = False
        for result in results:
            if result["success"]:
                if result["output"]:
                    messages.append(f"✓ {result['name']}: {result['output']}")
            else:
                has_issues = True
                messages.append(f"✗ {result['name']}: {result['output']}")

        if messages:
            status = "Issues found" if has_issues else "OK"
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": f"[Lint {status}] {file_path}\n" + "\n".join(messages),
                }
            }
            print(json.dumps(output))

        sys.exit(0)
    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
