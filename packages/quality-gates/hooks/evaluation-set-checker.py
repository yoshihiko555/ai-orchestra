#!/usr/bin/env python3
"""
PostToolUse hook: Remind the agent to reconcile test file changes against the
package's evaluation set (docs/evaluation/<pkg>.md).

When a test file under packages/<pkg>/tests/ or the top-level tests/ directory
is edited, this hook identifies the owning package and nudges the agent to
check docs/evaluation/<pkg>.md's EV-NN criteria per
.claude/rules/evaluation-set-policy.md. Notifications are deduplicated per
session + package (or per session + file when the package could not be
identified) so the same reminder is not repeated on every edit.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

# quality_gate_config.py lives alongside this file under packages/quality-gates/hooks/,
# so add this file's own directory to sys.path before importing it.
_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
else:
    _fallback_core_hooks = Path(__file__).resolve().parents[2] / "core" / "hooks"
    if str(_fallback_core_hooks) not in sys.path:
        sys.path.insert(0, str(_fallback_core_hooks))

from hook_common import (  # noqa: E402
    _find_local_config_path,
    _read_config_file,
    find_package_config,
    is_test_path,
    load_package_config,
    read_hook_input,
    read_json_safe,
    safe_hook_execution,
    write_json,
)

# resolve_state_path() is the single canonical implementation shared across
# the quality-gates package (Issue #154 review: a local duplicate with a
# diverging contract used to live here, which undermined the goal of
# unifying the state-file convention).
from quality_gate_config import resolve_state_path  # noqa: E402

STATE_FILENAME = "evaluation-set-checker.json"

PACKAGES_TEST_PATH_PATTERN = re.compile(r"^packages/([^/]+)/tests/")
TOP_LEVEL_TESTS_PATTERN = re.compile(r"^tests/")

# Package names are always directory names under packages/, so a safe
# identifier is limited to alphanumerics, "-", "_" (no path separators,
# newlines, or other characters that could leak into log/message output).
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Explicit evaluation-set-mapping.yaml config (Issue #237): the package/filename
# heuristics below only recognize SSOT targets that live under packages/<pkg>/.
# Some evaluation sets (e.g. orchex-cli) own tests without any packages/<pkg>/
# directory, so this config lets them opt in to the same reconciliation nudge
# without relying on directory conventions or filename-token guessing.
EVALUATION_SET_MAPPING_PACKAGE = "quality-gates"
EVALUATION_SET_MAPPING_FILENAME = "evaluation-set-mapping.yaml"

DEFAULT_STATE: dict = {"session_id": "", "notified": []}


def to_relative_path(file_path: str, project_dir: str) -> str:
    """Normalize file_path to a forward-slash path relative to project_dir."""
    if not file_path:
        return ""

    path = Path(file_path)
    if not path.is_absolute():
        return str(path).replace("\\", "/")

    if not project_dir:
        return str(path).replace("\\", "/")

    try:
        relative = path.resolve().relative_to(Path(project_dir).resolve())
    except ValueError:
        return str(path).replace("\\", "/")
    return str(relative).replace("\\", "/")


def is_test_filename(basename: str) -> bool:
    """Return True when basename matches test_*.py or *_test.py."""
    # Prefix with top-level tests/ so hook_common applies the scoped filename rule.
    return is_test_path(f"tests/{basename}", scope="scoped")


def is_valid_package_name(name: str) -> bool:
    """Return True when name is a safe package identifier.

    Regex captures derived from attacker-controlled file_path values (e.g.
    ``packages/([^/]+)/tests/``) can contain newlines or other injected
    characters. This is the single validation point all identified package
    names must pass through before being used in messages, dedup keys, or
    filesystem lookups.
    """
    return bool(PACKAGE_NAME_PATTERN.match(name))


def extract_package_from_packages_path(relative_path: str) -> str | None:
    """Return the package name captured from a packages/<pkg>/tests/... path."""
    match = PACKAGES_TEST_PATH_PATTERN.match(relative_path)
    if not match:
        return None
    return match.group(1)


def is_top_level_tests_path(relative_path: str, basename: str) -> bool:
    """Return True when relative_path is under the top-level tests/ directory."""
    if not TOP_LEVEL_TESTS_PATTERN.match(relative_path):
        return False
    return is_test_filename(basename)


def is_target_test_file(relative_path: str) -> bool:
    """Return True when relative_path is a test file this hook should react to."""
    return is_test_path(relative_path, scope="scoped")


def list_package_dirs(project_dir: str) -> list[str]:
    """Return sorted directory names directly under <project_dir>/packages/."""
    packages_dir = Path(project_dir) / "packages"
    if not packages_dir.is_dir():
        return []
    return sorted(entry.name for entry in packages_dir.iterdir() if entry.is_dir())


def _underscore_tokens(text: str) -> list[str]:
    """Split text on "_" into non-empty tokens."""
    return [token for token in text.split("_") if token]


def _is_contiguous_token_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Return True when needle appears as a contiguous run of tokens in haystack."""
    if not needle:
        return False
    window = len(needle)
    return any(
        haystack[start : start + window] == needle for start in range(len(haystack) - window + 1)
    )


def _strip_test_affixes(stem: str) -> str:
    """Strip a leading "test_" prefix or trailing "_test" suffix from stem."""
    if stem.startswith("test_"):
        return stem[len("test_") :]
    if stem.endswith("_test"):
        return stem[: -len("_test")]
    return stem


def match_package_by_filename(basename: str, package_dirs: list[str]) -> str | None:
    """Find the package whose underscored name token-matches the filename stem.

    Matching requires the package name's tokens (split on "_") to appear as a
    contiguous run within the filename stem's tokens, not just a raw
    substring. This avoids false positives such as "core" matching inside
    "hardcore" (test_hardcore_logic.py must not match package "core").
    """
    stem = _strip_test_affixes(os.path.splitext(basename)[0])
    stem_tokens = _underscore_tokens(stem)

    candidates = sorted(package_dirs, key=lambda name: len(name.replace("-", "_")), reverse=True)
    for package_name in candidates:
        candidate_tokens = _underscore_tokens(package_name.replace("-", "_"))
        if _is_contiguous_token_subsequence(candidate_tokens, stem_tokens):
            return package_name
    return None


def _extract_mapping_entries(config: dict) -> list[dict]:
    """Extract and validate ``mappings`` entries from a single loaded config layer.

    Guards against a malformed root (e.g. a ``.local.yaml`` that omits the
    ``mappings:`` key and defines a bare list at the document root) as well as
    malformed individual entries, so a misconfigured file degrades to "no
    entries from this layer" rather than raising (PR #243 review).
    """
    if not isinstance(config, dict):
        return []
    mappings = config.get("mappings", [])
    if not isinstance(mappings, list):
        return []
    return [
        entry
        for entry in mappings
        if isinstance(entry, dict)
        and isinstance(entry.get("package"), str)
        and isinstance(entry.get("test_globs"), list)
    ]


def load_evaluation_set_mapping(project_dir: str) -> list[dict]:
    """Load the evaluation-set-mapping.yaml explicit package/test-glob mapping.

    A missing or malformed config yields an empty list, so identify_package()
    transparently falls back to the packages/<pkg>/tests/ directory convention
    and the filename-token heuristic below (Issue #237: those two heuristics
    alone cannot recognize SSOT targets, such as orchex CLI, that own tests
    without a packages/<pkg>/ directory).

    Base and local (``*.local.yaml``) entries are merged per ``package`` name
    rather than via the generic load_package_config()/deep_merge() whole-value
    override (PR #243 review): deep_merge() replaces the entire ``mappings``
    list when a local file defines that key at all, so a project adding one
    local mapping would silently drop every shipped entry (e.g. orchex-cli),
    reopening the exact "core" misroute this file exists to prevent. A local
    entry overrides the base entry with the same ``package``; entries present
    in only one layer pass through unchanged.
    """
    base_path = find_package_config(
        EVALUATION_SET_MAPPING_PACKAGE, EVALUATION_SET_MAPPING_FILENAME, project_dir
    )
    base_entries = _extract_mapping_entries(_read_config_file(base_path))
    if not base_path:
        return base_entries

    local_path = _find_local_config_path(
        EVALUATION_SET_MAPPING_PACKAGE, EVALUATION_SET_MAPPING_FILENAME, project_dir, base_path
    )
    local_entries = _extract_mapping_entries(_read_config_file(local_path))
    if not local_entries:
        return base_entries

    merged_by_package = {entry["package"]: entry for entry in base_entries}
    for entry in local_entries:
        merged_by_package[entry["package"]] = entry
    return list(merged_by_package.values())


def match_explicit_mapping(relative_path: str, mappings: list[dict]) -> str | None:
    """Return the package of the first mapping entry whose test_globs matches.

    Uses fnmatch.fnmatchcase (not fnmatch.fnmatch) so glob matching is
    case-sensitive regardless of the host OS, keeping behavior identical
    across contributors' machines and CI.
    """
    for entry in mappings:
        for pattern in entry["test_globs"]:
            if isinstance(pattern, str) and fnmatch.fnmatchcase(relative_path, pattern):
                return entry["package"]
    return None


def identify_package(relative_path: str, project_dir: str) -> str | None:
    """Identify the owning package for a given test file path.

    Explicit evaluation-set-mapping.yaml entries are checked first and take
    priority over the packages/<pkg>/tests/ directory convention and the
    filename-token heuristic below, so SSOT targets without a packages/<pkg>/
    directory can be routed correctly and coincidental token collisions
    (e.g. "test_orchestra_manager_core.py" containing the token "core") don't
    misroute to an unrelated evaluation set (Issue #237).

    The result always passes through is_valid_package_name() here (the single
    validation point) so that regex captures derived from an untrusted
    file_path can never yield an unsafe package identifier.
    """
    pkg = match_explicit_mapping(relative_path, load_evaluation_set_mapping(project_dir))

    if pkg is None:
        pkg = extract_package_from_packages_path(relative_path)
    if pkg is None:
        basename = Path(relative_path).name
        if is_top_level_tests_path(relative_path, basename):
            pkg = match_package_by_filename(basename, list_package_dirs(project_dir))

    if pkg is None or not is_valid_package_name(pkg):
        return None
    return pkg


def evaluation_set_check_enabled(config: dict) -> bool:
    """Return whether the evaluation_set_check feature flag is enabled.

    Takes an already-loaded audit-flags.json config dict (see
    load_package_config) so callers that need both the enabled flag and
    resolve_state_path()'s paths.state_dir don't read the config file twice.
    """
    feature = config.get("features", {}).get("evaluation_set_check", {})
    return bool(feature.get("enabled", True))


def evaluation_set_doc_exists(pkg: str, project_dir: str) -> bool:
    """Return True when docs/evaluation/<pkg>.md exists for the given package."""
    doc_path = Path(project_dir) / "docs" / "evaluation" / f"{pkg}.md"
    return doc_path.is_file()


def build_message(pkg: str | None, doc_exists: bool) -> str:
    """Build the Japanese reminder message shown to the agent."""
    if pkg is not None and doc_exists:
        return (
            f"[Evaluation Set] テストファイルを変更しました。`docs/evaluation/{pkg}.md` "
            "の評価観点（EV-NN）と突合し、must 観点が covered であることを確認してから"
            "完了してください（詳細: .claude/rules/evaluation-set-policy.md）"
        )
    if pkg is not None:
        return (
            f"[Evaluation Set] パッケージ `{pkg}` の評価セット docs/evaluation/{pkg}.md "
            "が存在しません。docs/evaluation/_template.md に従い新規作成を検討・提案してください"
        )
    return (
        "[Evaluation Set] テストファイルを変更しました。対象パッケージを特定できませんでした。"
        "該当する評価セット（docs/evaluation/）との突合を確認してください"
    )


def load_state(state_path: str) -> dict:
    """Load the dedup state, defaulting missing keys."""
    data = read_json_safe(state_path)
    return {
        "session_id": data.get("session_id", ""),
        "notified": list(data.get("notified", [])),
    }


def already_notified(state: dict, session_id: str, pkg_key: str) -> bool:
    """Return True when this session already received a notification for pkg_key."""
    if not session_id:
        return False
    if state.get("session_id") != session_id:
        return False
    return pkg_key in state.get("notified", [])


def record_notification(state_path: str, session_id: str, pkg_key: str) -> None:
    """Persist that pkg_key has been notified for the current session_id."""
    if not state_path or not session_id:
        return

    state = load_state(state_path)
    if state.get("session_id") != session_id:
        state = {"session_id": session_id, "notified": []}

    notified = state.get("notified", [])
    if pkg_key not in notified:
        notified.append(pkg_key)
    state["notified"] = notified
    state["session_id"] = session_id

    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    write_json(state_path, state)


@safe_hook_execution
def main() -> None:
    data = read_hook_input()
    tool_name = data.get("tool_name", "")
    if tool_name not in ("Edit", "Write"):
        sys.exit(0)

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    file_path = str(tool_input.get("file_path") or "")

    project_dir = str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "") or "")
    if not project_dir:
        sys.exit(0)

    session_id = str(data.get("session_id") or "")

    # Cheap, filesystem/config-free checks first (path pattern matching only)
    # before the more expensive audit-flags.json config read below (mirrors
    # test-gate-checker.py's is_code_file()-before-config-read convention).
    relative_path = to_relative_path(file_path, project_dir)
    if not is_target_test_file(relative_path):
        sys.exit(0)

    # Read audit-flags.json once and reuse it for both the feature flag check
    # and resolve_state_path()'s paths.state_dir lookup.
    config = load_package_config("audit", "audit-flags.json", project_dir)
    if not evaluation_set_check_enabled(config):
        sys.exit(0)

    pkg = identify_package(relative_path, project_dir)
    # A file whose package could not be identified is deduplicated per-file
    # (not under one shared "unknown" bucket) so distinct unidentified files
    # are each still notified once per session.
    pkg_key = pkg if pkg is not None else f"unknown:{relative_path}"

    # config is already loaded above (shared with evaluation_set_check_enabled()),
    # and resolve_state_path() always returns a non-None path (quality_gate_config's
    # canonical contract).
    state_path = resolve_state_path(project_dir, STATE_FILENAME, config=config)
    state = load_state(state_path) if state_path else dict(DEFAULT_STATE)
    if already_notified(state, session_id, pkg_key):
        sys.exit(0)

    doc_exists = pkg is not None and evaluation_set_doc_exists(pkg, project_dir)
    message = build_message(pkg, doc_exists)

    if state_path:
        record_notification(state_path, session_id, pkg_key)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
