#!/usr/bin/env python3
"""Loop definition and config loading for loop-harness."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PACKAGE_NAME = "loop-harness"
CONFIG_FILENAME = "loop-harness.yaml"
_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SIGNATURE_KINDS = {"implementation", "pr_review"}
ISSUE_LOOP_IMPLEMENTATION_PASS_CRITERIA = {"critical": 0, "high": 0}
# SEC-M1: defense-in-depth denylist for `mechanical.commands` command-position binaries. This
# is independent of layer 3 (`claude -p --disallowedTools`, which only constrains the Maker's
# own tool calls) — it constrains what loop-harness itself will execute directly via
# `bash -lc` in the checker phase. Does not replace layer 2/3; loop definitions are
# trusted-but-verified. The scan below normalizes common `bash -lc` bypasses (absolute paths,
# tab/multi-space separators, `env`/`timeout`/`nice`/`command`/`bash -c`/`sh -c`/`exec` wrappers,
# leading `VAR=value` env-assignment prefixes, surrounding quotes/parentheses, and
# `;`/`&&`/`||`/`|`/`&`/`$(...)`/backtick command boundaries) but is not a full shell parser.
_MECHANICAL_COMMAND_DENYLIST = frozenset({"git", "gh", "ssh", "curl", "wget", "docker", "sudo"})
# SN3: `exec` replaces the current shell with the given command (no subprocess spawned) - a
# command-position wrapper exactly like `command`/`nice`, so `exec git push` must also be
# unwrapped down to `git` instead of resolving to the literal name "exec".
_MECHANICAL_COMMAND_WRAPPERS = frozenset(
    {"env", "nice", "command", "timeout", "bash", "sh", "exec"}
)
_COMMAND_SEGMENT_SPLIT_RE = re.compile(r"\$\(|`|;|&&|\|\||\||&|\n")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# G4: wrapper numeric arguments, e.g. `timeout 30 ...` or `timeout 30s ...` /
# `timeout 1.5m ...`. Plain isdigit() misses duration-suffixed forms
# (`s`/`m`/`h`/`d`), which let e.g. `timeout 30s git push` slip past the
# denylist scan (the unrecognized "30s" token would be resolved as the
# command-position binary instead of `git`).
_WRAPPER_NUMERIC_ARG_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")
# SN3: `env`'s flags that consume a following, space-separated value argument (e.g. `-u NAME`
# to unset a var before exec'ing, `-C dir` to chdir first). The generic dash-prefixed-flag
# skip below only consumes the flag token itself; without this, the flag's *value* token is
# left as the next token and gets mis-resolved as env's command-position binary (e.g.
# `env -u FOO git push` would resolve to "FOO", silently missing the denylisted `git`).
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
}


def _resolve_local_override_root(project_dir: str) -> str:
    """Resolve project_dir to the root worktree for .local.yaml lookup, failing open."""
    lib_dir = Path(__file__).resolve().parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    import loop_common

    try:
        return str(loop_common.resolve_root_worktree(project_dir))
    except loop_common.RootResolutionError:
        return project_dir


class DefinitionValidationError(ValueError):
    """Raised when a loop definition YAML violates the schema."""


@dataclass(frozen=True)
class PhaseDefinition:
    """One phase in a loop definition."""

    name: str
    maker: dict[str, Any]
    checker: dict[str, Any]
    guards: dict[str, Any]
    on_success: dict[str, Any]
    on_failure: dict[str, Any]


@dataclass(frozen=True)
class LoopDefinition:
    """Validated loop definition."""

    id: str
    trigger: dict[str, Any]
    phases: list[PhaseDefinition]
    notifications: dict[str, Any]
    source_path: str


def package_root() -> Path:
    """Return the package root directory."""
    return Path(__file__).resolve().parents[1]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base without mutating either input."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
            continue
        result[key] = value
    return result


def _read_yaml_dict(path: Path) -> dict[str, Any]:
    """Read a YAML mapping, returning an empty mapping for absent files."""
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise DefinitionValidationError(f"Invalid YAML: {path}") from exc
    return data if isinstance(data, dict) else {}


def load_config(project_dir: str) -> dict[str, Any]:
    """Load base config and project-local override using scalar-key deep merge."""
    base = _read_yaml_dict(package_root() / "config" / CONFIG_FILENAME)
    override_root = _resolve_local_override_root(project_dir)
    local = _read_yaml_dict(
        Path(override_root) / ".claude" / "config" / PACKAGE_NAME / "loop-harness.local.yaml"
    )
    return deep_merge(base, local) if local else base


def load_and_validate(path: str | os.PathLike[str]) -> LoopDefinition:
    """Read and validate a loop definition YAML file."""
    source = Path(path)
    raw = _read_yaml_dict(source)
    _validate(raw, str(source))
    phases = [
        PhaseDefinition(
            name=str(phase["name"]),
            maker=dict(phase["maker"]),
            checker=dict(phase["checker"]),
            guards=dict(phase["guards"]),
            on_success=dict(phase["on_success"]),
            on_failure=dict(phase["on_failure"]),
        )
        for phase in raw["phases"]
    ]
    return LoopDefinition(
        id=str(raw["id"]),
        trigger=dict(raw["trigger"]),
        phases=phases,
        notifications=dict(raw.get("notifications") or {}),
        source_path=str(source),
    )


def load_all_definitions(project_dir: str) -> dict[str, LoopDefinition]:
    """Load bundled definitions, then project definitions by id full replacement."""
    definitions: dict[str, LoopDefinition] = {}
    for path in _definition_paths(package_root() / "config" / "loops"):
        definition = load_and_validate(path)
        definitions[definition.id] = definition
    project_loops = Path(project_dir) / ".claude" / "config" / PACKAGE_NAME / "loops"
    for path in _definition_paths(project_loops):
        definition = load_and_validate(path)
        definitions[definition.id] = definition
    return definitions


def phase_by_name(definition: LoopDefinition, name: str) -> PhaseDefinition:
    """Return a phase by name or raise DefinitionValidationError."""
    for phase in definition.phases:
        if phase.name == name:
            return phase
    raise DefinitionValidationError(f"Unknown phase: {name}")


def checker_pass_criteria(checker: dict[str, Any]) -> dict[str, int]:
    """Return a strictly shaped LLM review pass criteria mapping."""
    llm_review = checker.get("llm_review")
    if not isinstance(llm_review, dict):
        raise DefinitionValidationError("checker.llm_review must be a mapping")
    criteria = llm_review.get("pass_criteria")
    expected_keys = set(ISSUE_LOOP_IMPLEMENTATION_PASS_CRITERIA)
    if not isinstance(criteria, dict) or set(criteria) != expected_keys:
        raise DefinitionValidationError(
            "checker.llm_review.pass_criteria must contain critical and high"
        )
    if any(
        not isinstance(criteria[name], int) or isinstance(criteria[name], bool)
        for name in expected_keys
    ):
        raise DefinitionValidationError("checker.llm_review.pass_criteria values must be integers")
    return {name: int(criteria[name]) for name in ISSUE_LOOP_IMPLEMENTATION_PASS_CRITERIA}


def _definition_paths(directory: Path) -> list[Path]:
    """Return sorted loop YAML paths from a directory."""
    if not directory.is_dir():
        return []
    return sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])


def _validate(raw: dict[str, Any], source_path: str) -> None:
    """Validate required keys and cross-field constraints."""
    if not raw:
        raise DefinitionValidationError(f"Empty definition: {source_path}")
    for key in ("id", "trigger", "phases"):
        if key not in raw:
            raise DefinitionValidationError(f"Missing required key '{key}': {source_path}")
    if not isinstance(raw["trigger"], dict):
        raise DefinitionValidationError(f"trigger must be a mapping: {source_path}")
    loop_id = _validate_loop_id(raw["id"], source_path)
    phases = _validate_phase_list(raw["phases"], loop_id, source_path)
    _validate_phase_transitions(phases, source_path)


def _validate_loop_id(value: Any, source_path: str) -> str:
    """Validate and return the loop id."""
    loop_id = str(value or "")
    if not _ID_RE.match(loop_id):
        raise DefinitionValidationError(f"Invalid loop id '{loop_id}': {source_path}")
    return loop_id


def _validate_phase_list(value: Any, loop_id: str, source_path: str) -> list[dict[str, Any]]:
    """Validate the phases array."""
    if not isinstance(value, list) or not value:
        raise DefinitionValidationError(f"'phases' must be a non-empty list: {source_path}")
    names: set[str] = set()
    for phase in value:
        if not isinstance(phase, dict):
            raise DefinitionValidationError(f"Phase must be a mapping: {source_path}")
        _validate_phase(phase, loop_id, source_path)
        name = str(phase["name"])
        if name in names:
            raise DefinitionValidationError(f"Duplicate phase '{name}': {source_path}")
        names.add(name)
    return value


def _validate_phase(phase: dict[str, Any], loop_id: str, source_path: str) -> None:
    """Validate one phase."""
    required = ("name", "maker", "checker", "guards", "on_success", "on_failure")
    for key in required:
        if key not in phase:
            raise DefinitionValidationError(f"Missing phase key '{key}': {source_path}")
    if not isinstance(phase["maker"], dict):
        raise DefinitionValidationError(f"maker must be a mapping: {source_path}")
    _validate_checker(loop_id, str(phase["name"]), phase["checker"], source_path)
    _validate_guards(phase["guards"], source_path)
    _validate_success(phase["on_success"], source_path)
    if (phase["on_failure"] or {}).get("disposition") != "exit_failure":
        raise DefinitionValidationError(
            f"on_failure.disposition must be exit_failure: {source_path}"
        )


def _validate_checker(loop_id: str, phase_name: str, checker: Any, source_path: str) -> None:
    """Validate checker requirements."""
    if not isinstance(checker, dict):
        raise DefinitionValidationError(f"checker must be a mapping: {source_path}")
    has_mechanical = isinstance(checker.get("mechanical"), dict)
    has_external = isinstance(checker.get("external_signal"), dict)
    if not has_mechanical and not has_external:
        raise DefinitionValidationError(
            f"checker requires mechanical or external_signal: {source_path}"
        )
    if loop_id == "issue-loop" and phase_name == "implementation":
        llm_review = checker.get("llm_review")
        if not isinstance(llm_review, dict):
            raise DefinitionValidationError("issue-loop implementation requires llm_review")
        pass_criteria = checker_pass_criteria(checker)
        if pass_criteria != ISSUE_LOOP_IMPLEMENTATION_PASS_CRITERIA:
            raise DefinitionValidationError(
                "issue-loop implementation pass_criteria must be critical=0 and high=0"
            )
    if has_mechanical:
        _validate_mechanical(checker["mechanical"], source_path)


def _validate_mechanical(mechanical: dict[str, Any], source_path: str) -> None:
    """Validate mechanical checker shape."""
    commands = mechanical.get("commands")
    if not isinstance(commands, list) or not commands:
        raise DefinitionValidationError(f"mechanical.commands must be non-empty: {source_path}")
    if mechanical.get("analyzer") != "failure_detector.analyze":
        raise DefinitionValidationError(f"Unsupported analyzer: {source_path}")
    for command in commands:
        for segment in _command_segments(str(command)):
            binary = _segment_command_binary(segment)
            if binary in _MECHANICAL_COMMAND_DENYLIST:
                raise DefinitionValidationError(
                    f"mechanical.commands entry uses a denylisted binary ({binary!r}): "
                    f"{source_path}"
                )


def _command_segments(command: str) -> list[str]:
    """Split a mechanical command string into shell-segment strings for denylist scanning.

    Splits on command separators (`;`, `&&`, `||`, `|`, `&`, newline) and command
    substitution openers (`$(`, backtick) so each gets its own "command position" checked.
    The matching `)` from `$(...)` is left in the following segment's text; harmless since
    it never itself resolves to a denylisted or wrapper binary name.
    """
    return [segment for segment in _COMMAND_SEGMENT_SPLIT_RE.split(command) if segment.strip()]


def _segment_command_binary(segment: str) -> str | None:
    """Return the resolved, wrapper-unwrapped command-position binary name for a segment."""
    tokens = [token.strip("'\"()") for token in re.split(r"\s+", segment.strip()) if token]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ENV_ASSIGNMENT_RE.match(token):
            index += 1
            continue
        name = token.rsplit("/", 1)[-1]
        if name not in _MECHANICAL_COMMAND_WRAPPERS:
            return name or None
        value_flags = _WRAPPER_VALUE_FLAGS.get(name, frozenset())
        index += 1
        while index < len(tokens):
            current = tokens[index]
            if current in value_flags:
                # SN3: consume both the flag and its separate value argument (e.g. `-u FOO`)
                # so the value itself is never mistaken for the wrapped command's binary.
                index += 2
                continue
            if current.startswith("-") or _WRAPPER_NUMERIC_ARG_RE.match(current):
                index += 1
                continue
            break
    return None


def _validate_guards(guards: Any, source_path: str) -> None:
    """Validate guard settings."""
    if not isinstance(guards, dict):
        raise DefinitionValidationError(f"guards must be a mapping: {source_path}")
    no_progress = guards.get("no_progress")
    if not isinstance(no_progress, dict):
        raise DefinitionValidationError(f"guards.no_progress is required: {source_path}")
    if no_progress.get("signature") not in _SIGNATURE_KINDS:
        raise DefinitionValidationError(f"Invalid no_progress.signature: {source_path}")
    for path, value in (
        ("guards.max_iterations", guards.get("max_iterations")),
        ("guards.no_progress.repeat", no_progress.get("repeat")),
    ):
        if not isinstance(value, int) or value < 1:
            raise DefinitionValidationError(f"{path} must be a positive integer: {source_path}")


def _validate_success(on_success: Any, source_path: str) -> None:
    """Validate success transition."""
    if not isinstance(on_success, dict):
        raise DefinitionValidationError(f"on_success must be a mapping: {source_path}")
    disposition = on_success.get("disposition")
    if disposition not in {"advance_phase", "exit_success"}:
        raise DefinitionValidationError(f"Invalid on_success.disposition: {source_path}")
    if disposition == "advance_phase" and not on_success.get("next"):
        raise DefinitionValidationError(f"advance_phase requires next: {source_path}")
    if disposition == "exit_success" and "next" in on_success:
        raise DefinitionValidationError(f"exit_success must not define next: {source_path}")


def _validate_phase_transitions(phases: list[dict[str, Any]], source_path: str) -> None:
    """Validate that advance_phase targets exist."""
    names = {str(phase["name"]) for phase in phases}
    for phase in phases:
        on_success = phase["on_success"]
        if on_success.get("disposition") != "advance_phase":
            continue
        if on_success.get("next") not in names:
            raise DefinitionValidationError(f"Unknown next phase: {source_path}")
