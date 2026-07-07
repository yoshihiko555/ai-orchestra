#!/usr/bin/env python3
"""Loop definition and config loading for loop-harness."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PACKAGE_NAME = "loop-harness"
CONFIG_FILENAME = "loop-harness.yaml"
_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SIGNATURE_KINDS = {"implementation", "pr_review"}


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
    local = _read_yaml_dict(
        Path(project_dir) / ".claude" / "config" / PACKAGE_NAME / "loop-harness.local.yaml"
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
        if not isinstance(checker.get("llm_review"), dict):
            raise DefinitionValidationError("issue-loop implementation requires llm_review")
    if has_mechanical:
        _validate_mechanical(checker["mechanical"], source_path)


def _validate_mechanical(mechanical: dict[str, Any], source_path: str) -> None:
    """Validate mechanical checker shape."""
    commands = mechanical.get("commands")
    if not isinstance(commands, list) or not commands:
        raise DefinitionValidationError(f"mechanical.commands must be non-empty: {source_path}")
    if mechanical.get("analyzer") != "failure_detector.analyze":
        raise DefinitionValidationError(f"Unsupported analyzer: {source_path}")


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
