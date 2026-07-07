"""Unit tests for loop_definition."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.module_loader import load_module

ld = load_module("loop_definition", "packages/loop-harness/lib/loop_definition.py")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _definition(loop_id: str = "custom-loop", phase_name: str = "build") -> str:
    return f"""
id: {loop_id}
trigger:
  lp1:
    skill: loop-issue
phases:
  - name: {phase_name}
    maker:
      agent: auto
      prompt_template: x.md#maker
    checker:
      mechanical:
        commands: [pytest -q]
        analyzer: failure_detector.analyze
    guards:
      max_iterations: 3
      no_progress:
        signature: implementation
        repeat: 2
    on_success:
      disposition: exit_success
    on_failure:
      disposition: exit_failure
"""


def test_load_and_validate_accepts_valid_definition(tmp_path: Path) -> None:
    path = tmp_path / "loop.yaml"
    _write(path, _definition())
    definition = ld.load_and_validate(path)
    assert definition.id == "custom-loop"
    assert definition.phases[0].checker["mechanical"]["commands"] == ["pytest -q"]


def test_load_and_validate_rejects_missing_required_key(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    _write(path, "id: bad-loop\ntrigger: {}\n")
    with pytest.raises(ld.DefinitionValidationError, match="Missing required key 'phases'"):
        ld.load_and_validate(path)


def test_load_and_validate_rejects_checker_without_mechanical_or_external(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    _write(path, _definition().replace("mechanical:", "noop:"))
    with pytest.raises(ld.DefinitionValidationError, match="checker requires"):
        ld.load_and_validate(path)


def test_issue_loop_implementation_requires_llm_review(tmp_path: Path) -> None:
    path = tmp_path / "issue-loop.yaml"
    _write(path, _definition(loop_id="issue-loop", phase_name="implementation"))
    with pytest.raises(ld.DefinitionValidationError, match="requires llm_review"):
        ld.load_and_validate(path)


def test_same_phase_name_in_other_loop_does_not_require_llm_review(tmp_path: Path) -> None:
    path = tmp_path / "other.yaml"
    _write(path, _definition(loop_id="other-loop", phase_name="implementation"))
    definition = ld.load_and_validate(path)
    assert definition.id == "other-loop"


def test_advance_phase_requires_existing_next(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    content = _definition().replace(
        "disposition: exit_success", "disposition: advance_phase\n      next: missing"
    )
    _write(path, content)
    with pytest.raises(ld.DefinitionValidationError, match="Unknown next phase"):
        ld.load_and_validate(path)


def test_load_config_applies_local_deep_merge(tmp_path: Path) -> None:
    local = tmp_path / ".claude" / "config" / "loop-harness" / "loop-harness.local.yaml"
    _write(local, "guards:\n  no_progress:\n    repeat: 9\n")
    config = ld.load_config(str(tmp_path))
    assert config["guards"]["max_iterations"] == 3
    assert config["guards"]["no_progress"]["repeat"] == 9
    assert config["lock"]["ttl_seconds"]["lp1"] == 3600
    assert config["lock"]["ttl_seconds"]["lp2"] == 300


def test_load_all_definitions_project_definition_replaces_by_id(tmp_path: Path) -> None:
    project_def = tmp_path / ".claude" / "config" / "loop-harness" / "loops" / "issue-loop.yaml"
    _write(project_def, _definition(loop_id="issue-loop", phase_name="replacement"))
    definitions = ld.load_all_definitions(str(tmp_path))
    assert definitions["issue-loop"].source_path == str(project_def)
    assert definitions["issue-loop"].phases[0].name == "replacement"


def test_load_all_definitions_adds_second_loop_without_core_change(tmp_path: Path) -> None:
    project_def = tmp_path / ".claude" / "config" / "loop-harness" / "loops" / "second.yaml"
    _write(project_def, _definition(loop_id="second-loop"))
    definitions = ld.load_all_definitions(str(tmp_path))
    assert "issue-loop" in definitions
    assert "second-loop" in definitions
