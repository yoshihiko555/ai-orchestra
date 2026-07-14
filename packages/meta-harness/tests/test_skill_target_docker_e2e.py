"""Opt-in real Docker + Max OAuth E2E for the skill-target bootstrap path."""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_skill_target_docker_e2e",
    "packages/meta-harness/lib/meta_harness_common.py",
)
ev = load_module(
    "meta_harness_evaluator_skill_target_docker_e2e",
    "packages/meta-harness/lib/evaluator.py",
)
cli = load_module(
    "meta_harness_cli_skill_target_docker_e2e",
    "packages/meta-harness/scripts/meta_harness.py",
)
propose_cli = load_module(
    "meta_harness_propose_skill_target_docker_e2e",
    "packages/meta-harness/lib/propose_cli.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO_ROOT / "packages" / "meta-harness" / "schemas"
TARGET = "skill:handoff"

pytestmark = pytest.mark.docker


def _require_live_subscription_e2e() -> None:
    if os.environ.get("META_HARNESS_RUN_SUBSCRIPTION_E2E") != "1":
        pytest.skip("set META_HARNESS_RUN_SUBSCRIPTION_E2E=1 for the Max OAuth E2E")
    if shutil.which("docker") is None:
        pytest.fail("Docker CLI is required for the live skill-target E2E")
    completed = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=20, check=False
    )
    if completed.returncode != 0:
        pytest.fail("Docker daemon is required for the live skill-target E2E")


def _prepare_project(tmp_path: Path) -> tuple[Path, str]:
    project = tmp_path / "project"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(REPO_ROOT), str(project)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    for relative in ("facets/instructions/handoff.md", "facets/scripts/handoff.py"):
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, destination)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "meta-harness-e2e",
        "GIT_AUTHOR_EMAIL": "meta-harness-e2e@example.invalid",
        "GIT_COMMITTER_NAME": "meta-harness-e2e",
        "GIT_COMMITTER_EMAIL": "meta-harness-e2e@example.invalid",
    }
    subprocess.run(
        ["git", "add", "facets/instructions/handoff.md", "facets/scripts/handoff.py"],
        cwd=project,
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "commit", "--quiet", "--allow-empty", "-m", "prepare skill e2e"],
        cwd=project,
        check=True,
        capture_output=True,
        env=env,
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return project, source_commit


def _write_suite(package_dir: Path) -> None:
    config_dir = package_dir / "config"
    config_dir.mkdir(parents=True)
    shutil.copy2(
        REPO_ROOT / "packages" / "meta-harness" / "config" / "self-report-instruction.md",
        config_dir / "self-report-instruction.md",
    )
    suite = package_dir / "scenarios" / "skill" / "handoff"
    suite.mkdir(parents=True)
    (suite / "bootstrap.yaml").write_text(
        """schema_version: "1.0"
id: handoff-bootstrap
target: skill:handoff
description: Minimal real handoff bootstrap.
prompt: |
  /handoff --message "Record that bootstrap validation is complete and no work remains."

  This is a cost-bounded headless acceptance run. Do not collect extra data and
  do not use Write. Run exactly one Bash command and then stop:
  python3 -c "from pathlib import Path; Path('.claude/handoffs/live-e2e.md').write_text('# Task Handoff\\n\\n## Current Task State\\n\\n- Bootstrap validation complete.\\n')"
setup:
  - "mkdir -p .claude/handoffs"
  - "printf '# Plans\\n\\n- `cc:done` Bootstrap validation\\n' > .claude/Plans.md"
allowed_tools:
  - "Bash(python3 *)"
critical:
  - id: handoff-created
    text: A handoff file is created.
    oracle: command_exit
    command: "find .claude/handoffs -type f -name '*.md' -print -quit | grep -q ."
holdout: false
timeout_ms: 300000
budget:
  max_turns: 3
  max_budget_usd: 3.0
  max_output_tokens: 1024
repeat: 1
""",
        encoding="utf-8",
    )
    (suite / "not-invoked.yaml").write_text(
        """schema_version: "1.0"
id: handoff-not-invoked
target: skill:handoff
description: A skill target run that deliberately omits the slash command.
prompt: "Reply with exactly OK."
allowed_tools: []
critical:
  - id: unreachable
    text: The activation gate must fail before this can pass.
    oracle: command_exit
    command: "true"
holdout: false
timeout_ms: 180000
budget:
  max_turns: 1
  max_budget_usd: 3.0
  max_output_tokens: 64
repeat: 1
""",
        encoding="utf-8",
    )
    (suite / "holdout.yaml").write_text(
        """schema_version: "1.0"
id: handoff-holdout-placeholder
target: skill:handoff
description: Suite ownership placeholder; not executed by this E2E.
prompt: "/handoff test"
allowed_tools: []
critical:
  - id: placeholder
    text: Placeholder holdout.
    oracle: command_exit
    command: "true"
holdout: true
budget:
  max_turns: 1
  max_budget_usd: 3.0
  max_output_tokens: 64
repeat: 1
""",
        encoding="utf-8",
    )


def test_handoff_bootstrap_and_missing_activation_fail_closed(tmp_path: Path) -> None:
    _require_live_subscription_e2e()
    project, source_commit = _prepare_project(tmp_path)
    package_dir = tmp_path / "package"
    _write_suite(package_dir)
    overlay = tmp_path / "empty-overlay"
    overlay.mkdir()

    before = set(mh.list_candidate_ids(project, mh.DEFAULTS))
    assert (
        cli.cmd_register(
            str(project),
            str(overlay),
            TARGET,
            None,
            "live Docker baseline",
            "live-docker-baseline",
            source_commit,
            False,
        )
        == cli.EXIT_OK
    )
    candidate_ids = set(mh.list_candidate_ids(project, mh.DEFAULTS)) - before
    assert len(candidate_ids) == 1
    cand_id = candidate_ids.pop()
    manifest = mh.read_candidate_manifest(project, mh.DEFAULTS, cand_id)
    assert manifest is not None

    config = copy.deepcopy(mh.DEFAULTS)
    passing = ev.evaluate_candidate(
        main_root=project,
        config=config,
        schema_dir=SCHEMA_DIR,
        package_dir=package_dir,
        project_dir=project,
        cand_id=cand_id,
        manifest=manifest,
        scenario_ids=["handoff-bootstrap"],
        repeat_override=1,
        cli_capabilities={"claude_version": "2.1.207"},
    )
    assert len(passing) == 1
    assert passing[0]["verdict"] == "pass", passing[0]["errors"]

    frontier = cli._compute_frontier(project, config, TARGET)
    assert frontier["frontier"] == [cand_id]
    mh.write_frontier_cache(project, config, frontier, TARGET)
    snapshot = propose_cli._snapshot_propose_store(project, config, TARGET)
    assert propose_cli._citable_run_ids(snapshot, TARGET) == (passing[0]["run_id"],)

    rejected = ev.evaluate_candidate(
        main_root=project,
        config=config,
        schema_dir=SCHEMA_DIR,
        package_dir=package_dir,
        project_dir=project,
        cand_id=cand_id,
        manifest=manifest,
        scenario_ids=["handoff-not-invoked"],
        repeat_override=1,
        cli_capabilities={"claude_version": "2.1.207"},
    )
    assert len(rejected) == 1
    assert rejected[0]["verdict"] == "error"
    assert any(
        error["stage"] == "run"
        and error["type"] == "run_error"
        and "prompt must start with /handoff" in error["message"]
        for error in rejected[0]["errors"]
    )
