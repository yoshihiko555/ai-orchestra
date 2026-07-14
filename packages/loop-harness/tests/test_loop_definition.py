"""Unit tests for loop_definition."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

ld = load_module("loop_definition", "packages/loop-harness/lib/loop_definition.py")


def _implementation_on_success_exec(definition: ld.LoopDefinition) -> list[str]:
    phase = next(p for p in definition.phases if p.name == "implementation")
    return list(phase.on_success["exec"])


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test User"], path)
    _write(path / "README.md", "root\n")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "initial"], path)


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


def _issue_loop_implementation_definition(*, critical: int, high: int) -> str:
    return _definition(loop_id="issue-loop", phase_name="implementation").replace(
        "        analyzer: failure_detector.analyze",
        f"""        analyzer: failure_detector.analyze
      llm_review:
        baseline: code-reviewer
        selection: skill-review-policy
        pass_criteria:
          critical: {critical}
          high: {high}""",
    )


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


def test_load_and_validate_rejects_non_mapping_trigger(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    _write(path, _definition().replace("trigger:\n  lp1:\n    skill: loop-issue", 'trigger: "bad"'))
    with pytest.raises(ld.DefinitionValidationError, match="trigger must be a mapping"):
        ld.load_and_validate(path)


def test_load_and_validate_rejects_non_mapping_maker(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    _write(
        path,
        _definition().replace(
            "maker:\n      agent: auto\n      prompt_template: x.md#maker",
            "maker: [not, a, mapping]",
        ),
    )
    with pytest.raises(ld.DefinitionValidationError, match="maker must be a mapping"):
        ld.load_and_validate(path)


def test_load_and_validate_rejects_checker_without_mechanical_or_external(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    _write(path, _definition().replace("mechanical:", "noop:"))
    with pytest.raises(ld.DefinitionValidationError, match="checker requires"):
        ld.load_and_validate(path)


def test_load_and_validate_rejects_denylisted_mechanical_command(tmp_path: Path) -> None:
    """SEC-M1: mechanical.commands must not run a denylisted binary directly."""
    path = tmp_path / "bad.yaml"
    _write(path, _definition().replace("commands: [pytest -q]", "commands: [git push origin main]"))
    with pytest.raises(ld.DefinitionValidationError, match="denylisted binary"):
        ld.load_and_validate(path)


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/git push origin main",
        "git\tpush origin main",
        "env git push origin main",
        "command git push origin main",
        "nice -n 10 git push origin main",
        "timeout 30 git push origin main",
        "timeout 30s git push origin main",
        "timeout 1.5m git push origin main",
        "timeout 2h git push origin main",
        'bash -c "git push origin main"',
        'sh -c "git push origin main"',
        "pytest -q ; git push origin main",
        "pytest -q && git push origin main",
        "pytest -q || git push origin main",
        "pytest -q | git push origin main",
        "$(git push origin main)",
        "`git push origin main`",
        "FOO=bar git push origin main",
        "(git push origin main)",
        "exec git push origin main",
        "env -u FOO git push origin main",
        "find . -exec git push origin main \\;",
        "find . -execdir git push origin main +",
        "env -S 'git push origin main'",
        "g\\it push origin main",
    ],
)
def test_load_and_validate_rejects_mechanical_command_denylist_bypass(
    tmp_path: Path, command: str
) -> None:
    """SEC-M1/SN3-extra: normalization must catch path/whitespace/wrapper/shell-construct/
    find-exec/env-split-string/backslash-escape bypasses."""
    path = tmp_path / "bad.yaml"
    _write(
        path,
        _definition().replace("commands: [pytest -q]", f"commands: [{json.dumps(command)}]"),
    )
    with pytest.raises(ld.DefinitionValidationError, match="denylisted binary"):
        ld.load_and_validate(path)


def test_load_and_validate_accepts_mechanical_command_with_denylisted_word_as_argument(
    tmp_path: Path,
) -> None:
    """The scan only checks command-position binaries, not arbitrary argument text."""
    path = tmp_path / "ok.yaml"
    _write(path, _definition().replace("commands: [pytest -q]", "commands: [pytest -k not_git]"))
    definition = ld.load_and_validate(path)
    assert definition.phases[0].checker["mechanical"]["commands"] == ["pytest -k not_git"]


def test_issue_loop_implementation_requires_llm_review(tmp_path: Path) -> None:
    path = tmp_path / "issue-loop.yaml"
    _write(path, _definition(loop_id="issue-loop", phase_name="implementation"))
    with pytest.raises(ld.DefinitionValidationError, match="requires llm_review"):
        ld.load_and_validate(path)


@pytest.mark.parametrize(("critical", "high"), [(1, 0), (0, 1)])
def test_issue_loop_implementation_rejects_relaxed_pass_criteria(
    tmp_path: Path, critical: int, high: int
) -> None:
    path = tmp_path / "issue-loop.yaml"
    _write(
        path,
        _issue_loop_implementation_definition(critical=critical, high=high),
    )

    with pytest.raises(ld.DefinitionValidationError, match="pass_criteria"):
        ld.load_and_validate(path)


def test_checker_pass_criteria_rejects_non_mapping_llm_review() -> None:
    with pytest.raises(ld.DefinitionValidationError, match="llm_review"):
        ld.checker_pass_criteria({"llm_review": ["not", "a", "mapping"]})


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
    assert config["maker"]["fallback_agent"] in config["maker"]["allowed_agents"]
    assert "general-purpose" in config["maker"]["allowed_agents"]
    assert "debugger" in config["maker"]["allowed_agents"]
    assert "requirements" not in config["maker"]["allowed_agents"]
    assert "planner" not in config["maker"]["allowed_agents"]
    assert "docs-writer" not in config["maker"]["allowed_agents"]
    assert "specialized-mcp-builder" not in config["maker"]["allowed_agents"]


def test_load_config_resolves_local_override_from_root_worktree(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _init_repo(root)
    linked = tmp_path / "linked"
    _git(["worktree", "add", str(linked), "-b", "loop/issue-1"], root)
    local = root / ".claude" / "config" / "loop-harness" / "loop-harness.local.yaml"
    _write(local, "guards:\n  no_progress:\n    repeat: 9\n")

    config = ld.load_config(str(linked))

    assert config["guards"]["no_progress"]["repeat"] == 9
    assert config["guards"]["max_iterations"] == 3


def test_load_config_applies_local_override_in_ordinary_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    local = repo / ".claude" / "config" / "loop-harness" / "loop-harness.local.yaml"
    _write(local, "guards:\n  no_progress:\n    repeat: 7\n")

    config = ld.load_config(str(repo))

    assert config["guards"]["no_progress"]["repeat"] == 7
    assert config["guards"]["max_iterations"] == 3


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


def test_bundled_issue_loop_records_baseline_after_push_and_pr_create() -> None:
    """code J2 (source): the packaged issue-loop's `implementation.on_success.exec` must run
    `push` and `pr_create` before `record_baseline` -- recording the baseline before the push
    lands would let the next iteration's reviewer diff against a baseline that predates the
    just-pushed commits."""
    source_path = REPO_ROOT / "packages" / "loop-harness" / "config" / "loops" / "issue-loop.yaml"
    definition = ld.load_and_validate(source_path)
    exec_order = _implementation_on_success_exec(definition)
    assert exec_order.index("push") < exec_order.index("record_baseline")
    assert exec_order.index("pr_create") < exec_order.index("record_baseline")


def test_project_override_issue_loop_is_not_shadowing_stale_exec_order() -> None:
    """code J2 (shadow guard): `load_all_definitions()` full-replaces the packaged issue-loop
    by id with this repo's own project override at
    `.claude/config/loop-harness/loops/issue-loop.yaml` (this repo self-installs loop-harness
    from its own `packages/` tree, see `project_worktree_test_env` memory). Before the fix,
    that checked-in override still had the pre-I3 `record_baseline` before `push` ordering,
    silently shadowing the source fix in every run against this project_dir. This asserts the
    *effective* definition `load_all_definitions()` returns for this repo matches the packaged
    source's exec order, so a future re-sort of the source can never again go stale in the
    override without failing this test."""
    override_path = REPO_ROOT / ".claude" / "config" / "loop-harness" / "loops" / "issue-loop.yaml"
    if not override_path.exists():
        pytest.skip("no project-local issue-loop override present in this checkout")
    effective = ld.load_all_definitions(str(REPO_ROOT))["issue-loop"]
    assert effective.source_path == str(override_path)
    exec_order = _implementation_on_success_exec(effective)
    assert exec_order.index("push") < exec_order.index("record_baseline")
    assert exec_order.index("pr_create") < exec_order.index("record_baseline")
