"""Scenario process execution remains fail-closed until strong containment exists."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

sproc = load_module(
    "meta_harness_scenario_process_tests",
    "packages/meta-harness/lib/scenario_process.py",
)


def test_process_tree_runner_refuses_process_group_only_containment(tmp_path: Path) -> None:
    with pytest.raises(sproc.ScenarioContainmentUnavailable, match="setsid"):
        sproc.run_bounded_process_tree(
            ["true"],
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            env={"PATH": "/usr/bin:/bin"},
        )


def test_oracle_capture_refuses_same_incomplete_boundary(tmp_path: Path) -> None:
    with pytest.raises(sproc.ScenarioContainmentUnavailable, match="setsid"):
        sproc.run_bounded_capture(["true"], cwd=tmp_path, timeout=1, env={"PATH": "/usr/bin:/bin"})
