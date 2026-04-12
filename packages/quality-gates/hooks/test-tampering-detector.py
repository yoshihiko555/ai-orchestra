#!/usr/bin/env python3
"""
PostToolUse hook: Detect newly introduced test tampering patterns.

Warns when code edits add skip/disable markers, or when shell commands delete
tracked test files.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
else:
    _fallback_core_hooks = Path(__file__).resolve().parents[2] / "core" / "hooks"
    if str(_fallback_core_hooks) not in sys.path:
        sys.path.insert(0, str(_fallback_core_hooks))

from hook_common import read_hook_input, safe_hook_execution  # noqa: E402

SKIP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "it.skip() / test.skip() / describe.skip()",
        re.compile(r"\b(?:it|test|describe)\.skip\s*\("),
    ),
    (
        "@pytest.mark.skip / @unittest.skip",
        re.compile(r"@\s*(?:pytest\.mark\.skip|unittest\.skip)\b"),
    ),
)

SUPPRESSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("eslint-disable", re.compile(r"\beslint-disable(?:-next-line|-line)?\b", re.IGNORECASE)),
    ("noqa", re.compile(r"#\s*noqa(?::[\w,\s-]+)?\b", re.IGNORECASE)),
    ("type: ignore", re.compile(r"#\s*type:\s*ignore\b", re.IGNORECASE)),
)

DELETE_COMMAND_PATTERN = re.compile(r"\b(?:rm|git\s+rm)\b")
RELEVANT_TOOL_NAMES = {"Edit", "Write", "Bash", "Delete", "MultiEdit"}
STATE_FILE = Path("/tmp/claude-test-tampering-state.json")


def is_test_file(file_path: str) -> bool:
    """Return True when the path looks like a test file."""
    normalized = file_path.replace("\\", "/")
    if re.search(r"(^|/)(tests?|__tests__)(/|$)", normalized):
        return True
    if re.search(r"(^|/)test_[^/]+\.py$", normalized):
        return True
    if re.search(r"(^|/)[^/]+_test\.py$", normalized):
        return True
    return bool(re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", normalized))


def extract_added_lines(diff_text: str) -> list[str]:
    """Extract only added lines from a unified diff."""
    added_lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "@@", "diff --git", "---")):
            continue
        if line.startswith("+"):
            added_lines.append(line[1:])
    return added_lines


def scan_added_lines(file_path: str, added_lines: list[str]) -> list[dict[str, str]]:
    """Scan added lines for test tampering patterns."""
    findings: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    patterns = list(SKIP_PATTERNS)
    if is_test_file(file_path):
        patterns.extend(SUPPRESSION_PATTERNS)

    for line in added_lines:
        stripped = line.strip()
        if not stripped:
            continue
        for label, pattern in patterns:
            if not pattern.search(stripped):
                continue
            key = f"{file_path}:{label}:{stripped}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            findings.append(
                {
                    "type": "pattern",
                    "file_path": file_path,
                    "label": label,
                    "snippet": stripped,
                }
            )

    return findings


def _pattern_finding_key(finding: dict[str, str]) -> str:
    return "|".join(
        [
            finding.get("type", ""),
            finding.get("file_path", ""),
            finding.get("label", ""),
            finding.get("snippet", ""),
        ]
    )


def extract_deleted_test_files(name_status_output: str) -> list[str]:
    """Extract deleted test files from `git diff --name-status` output."""
    deleted_files: list[str] = []

    for line in name_status_output.splitlines():
        if not line:
            continue
        parts = line.split("\t", maxsplit=1)
        if len(parts) != 2:
            continue
        status, file_path = parts
        if not status.startswith("D"):
            continue
        if is_test_file(file_path):
            deleted_files.append(file_path)

    return sorted(set(deleted_files))


def load_state() -> dict:
    """Load persisted detector state."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"reported_deleted_files": {}}


def save_state(state: dict) -> None:
    """Persist detector state."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def run_git_command(project_dir: str, *args: str) -> str:
    """Run a git command and return stdout on success."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_dir,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""

    if result.returncode != 0:
        return ""
    return result.stdout


def get_project_state_key(project_dir: str) -> str:
    """Return a stable state key for the current git project."""
    common_dir = run_git_command(project_dir, "rev-parse", "--git-common-dir").strip()
    if common_dir:
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = (Path(project_dir) / common_path).resolve()
        return str(common_path)

    top_level = run_git_command(project_dir, "rev-parse", "--show-toplevel").strip()
    if top_level:
        return str(Path(top_level).resolve())

    return str(Path(project_dir).resolve())


def normalize_path(file_path: str, project_dir: str) -> str:
    """Normalize an absolute path to a path relative to project_dir when possible."""
    if not file_path:
        return ""

    path = Path(file_path)
    if not path.is_absolute():
        return str(path)

    try:
        return str(path.resolve().relative_to(Path(project_dir).resolve()))
    except ValueError:
        return str(path)


def is_tracked_file(project_dir: str, file_path: str) -> bool:
    """Return True when git knows about the file."""
    if not file_path:
        return False
    return bool(run_git_command(project_dir, "ls-files", "--error-unmatch", "--", file_path))


def get_added_lines_for_file(project_dir: str, file_path: str, tool_input: dict) -> list[str]:
    """Get added lines for a file using git diff, with tool_input fallback for new files."""
    normalized_path = normalize_path(file_path, project_dir)
    if not normalized_path:
        return []

    diff_text = run_git_command(
        project_dir, "diff", "--no-color", "--no-ext-diff", "--unified=0", "--", normalized_path
    )
    added_lines = extract_added_lines(diff_text)
    if added_lines:
        return added_lines

    if is_tracked_file(project_dir, normalized_path):
        return []

    content = tool_input.get("content") or tool_input.get("new_string") or ""
    if not isinstance(content, str) or not content.strip():
        return []

    return content.splitlines()


def should_check_deleted_tests(tool_name: str, tool_input: dict) -> bool:
    """Return True when the tool call could have deleted files."""
    if tool_name == "Delete":
        return True
    if tool_name != "Bash":
        return False

    command = str(tool_input.get("command") or "")
    return bool(DELETE_COMMAND_PATTERN.search(command))


def _normalize_candidate_path(path: str, project_dir: str) -> str:
    normalized = normalize_path(path, project_dir)
    if normalized.startswith("./"):
        return normalized[2:]
    return normalized.rstrip("/")


def _extract_targets_from_rm_args(args: list[str], project_dir: str) -> list[str]:
    targets: list[str] = []
    for arg in args:
        if arg in {"&&", "||", ";", "|"}:
            break
        if arg.startswith("-"):
            continue
        normalized = _normalize_candidate_path(arg, project_dir)
        if normalized:
            targets.append(normalized)
    return targets


def _extract_delete_targets_from_command(command: str, project_dir: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []

    if not tokens:
        return []

    targets: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"bash", "sh", "zsh"}:
            if (
                index + 2 < len(tokens)
                and tokens[index + 1].startswith("-")
                and "c" in tokens[index + 1]
            ):
                targets.extend(_extract_delete_targets_from_command(tokens[index + 2], project_dir))
                index += 3
                continue
        if token == "rm":
            targets.extend(_extract_targets_from_rm_args(tokens[index + 1 :], project_dir))
            index += 1
            continue
        if token == "git" and index + 1 < len(tokens) and tokens[index + 1] == "rm":
            targets.extend(_extract_targets_from_rm_args(tokens[index + 2 :], project_dir))
            index += 2
            continue
        index += 1

    return targets


def extract_delete_targets(tool_name: str, tool_input: dict, project_dir: str) -> list[str]:
    """Extract file or directory targets from Delete / rm / git rm operations."""
    if tool_name == "Delete":
        file_path = str(tool_input.get("file_path") or "")
        normalized = _normalize_candidate_path(file_path, project_dir)
        return [normalized] if normalized else []

    if tool_name != "Bash":
        return []

    command = str(tool_input.get("command") or "")
    if not command.strip():
        return []

    return _extract_delete_targets_from_command(command, project_dir)


def _split_path_parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def _match_glob_parts(path_parts: list[str], pattern_parts: list[str]) -> bool:
    if not pattern_parts:
        return not path_parts

    pattern = pattern_parts[0]
    if pattern == "**":
        if _match_glob_parts(path_parts, pattern_parts[1:]):
            return True
        if path_parts:
            return _match_glob_parts(path_parts[1:], pattern_parts)
        return False

    if not path_parts:
        return False

    if not fnmatchcase(path_parts[0], pattern):
        return False

    return _match_glob_parts(path_parts[1:], pattern_parts[1:])


def _match_path_glob(file_path: str, pattern: str) -> bool:
    return _match_glob_parts(_split_path_parts(file_path), _split_path_parts(pattern))


def _matches_delete_target(file_path: str, delete_targets: list[str]) -> bool:
    if not delete_targets:
        return False

    normalized_path = file_path.rstrip("/")
    for target in delete_targets:
        if any(char in target for char in "*?[]"):
            if _match_path_glob(normalized_path, target):
                return True
        if normalized_path == target:
            return True
        if normalized_path.startswith(f"{target}/"):
            return True
    return False


def get_all_deleted_test_files(project_dir: str) -> list[str]:
    """Return all currently deleted tracked test files in the repo diff."""
    deleted_files: set[str] = set()
    for extra_args in ([], ["--cached"]):
        output = run_git_command(
            project_dir, "diff", *extra_args, "--name-status", "--diff-filter=D"
        )
        deleted_files.update(extract_deleted_test_files(output))
    return sorted(deleted_files)


def _build_delete_pathspec(target: str) -> str:
    if any(char in target for char in "*?[]"):
        return f":(glob){target}"
    return target


def get_deleted_test_files(project_dir: str, delete_targets: list[str]) -> list[str]:
    """Return deleted tracked test files tied to the current delete targets."""
    if not delete_targets:
        return []

    deleted_files: set[str] = set()
    pathspecs = [_build_delete_pathspec(target) for target in delete_targets]
    for extra_args in ([], ["--cached"]):
        output = run_git_command(
            project_dir,
            "diff",
            *extra_args,
            "--name-status",
            "--diff-filter=D",
            "--",
            *pathspecs,
        )
        for file_path in extract_deleted_test_files(output):
            if _matches_delete_target(file_path, delete_targets):
                deleted_files.add(file_path)
    return sorted(deleted_files)


def get_unreported_deleted_test_files(project_dir: str, delete_targets: list[str]) -> list[str]:
    """Return matched deleted test files that have not been warned yet."""
    state = load_state()
    reported_by_project = state.get("reported_deleted_files", {})
    project_key = get_project_state_key(project_dir)
    current_deleted = set(get_all_deleted_test_files(project_dir))
    reported = set(reported_by_project.get(project_key, []))

    reported &= current_deleted
    reported_by_project[project_key] = sorted(reported)
    state["reported_deleted_files"] = reported_by_project

    matched_deleted = get_deleted_test_files(project_dir, delete_targets)
    new_deleted = [file_path for file_path in matched_deleted if file_path not in reported]
    if new_deleted:
        reported_by_project[project_key] = sorted(reported.union(new_deleted))
        state["reported_deleted_files"] = reported_by_project
    save_state(state)

    return new_deleted


def get_unreported_pattern_findings(
    project_dir: str, pattern_findings: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Return pattern findings that have not been warned yet in this project."""
    state = load_state()
    project_key = get_project_state_key(project_dir)
    reported_by_project = state.get("reported_pattern_findings", {})
    reported = set(reported_by_project.get(project_key, []))

    new_findings = [
        finding for finding in pattern_findings if _pattern_finding_key(finding) not in reported
    ]
    if new_findings:
        reported.update(_pattern_finding_key(finding) for finding in new_findings)
        reported_by_project[project_key] = sorted(reported)
        state["reported_pattern_findings"] = reported_by_project
        save_state(state)

    return new_findings


def collect_tampering_findings(data: dict) -> list[dict[str, str]]:
    """Collect test tampering findings for the current PostToolUse payload."""
    tool_name = str(data.get("tool_name") or "")
    if tool_name not in RELEVANT_TOOL_NAMES:
        return []

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    project_dir = str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    findings: list[dict[str, str]] = []

    if tool_name in {"Edit", "Write", "MultiEdit"}:
        file_path = str(tool_input.get("file_path") or "")
        added_lines = get_added_lines_for_file(project_dir, file_path, tool_input)
        pattern_findings = scan_added_lines(file_path, added_lines)
        findings.extend(get_unreported_pattern_findings(project_dir, pattern_findings))

    if should_check_deleted_tests(tool_name, tool_input):
        delete_targets = extract_delete_targets(tool_name, tool_input, project_dir)
        findings.extend(
            {
                "type": "deleted_test_file",
                "file_path": file_path,
                "label": "deleted test file",
                "snippet": "",
            }
            for file_path in get_unreported_deleted_test_files(project_dir, delete_targets)
        )

    return findings


def build_warning_message(findings: list[dict[str, str]]) -> str:
    """Build the warning message shown in hook output."""
    lines = ["[Warning] Potential test tampering detected:"]
    for finding in findings:
        if finding["type"] == "deleted_test_file":
            lines.append(f"- `{finding['file_path']}`: deleted test file")
            continue
        lines.append(
            f"- `{finding['file_path']}`: added `{finding['label']}` -> `{finding['snippet']}`"
        )

    lines.extend(
        [
            "",
            "Explain why this change is necessary and how test coverage remains enforced.",
        ]
    )
    return "\n".join(lines)


@safe_hook_execution
def main() -> None:
    """Entry point for the test tampering detector hook."""
    data = read_hook_input()
    findings = collect_tampering_findings(data)
    if not findings:
        sys.exit(0)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": build_warning_message(findings),
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
