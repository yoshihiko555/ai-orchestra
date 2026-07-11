#!/usr/bin/env python3
"""Fail-closed placeholder for a future OS-level scenario process container."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

MAX_SCENARIO_OUTPUT_BYTES = 10_000_000


class ScenarioContainmentUnavailable(RuntimeError):
    """No backend can currently contain descendants that escape their process group."""


class ScenarioOutputLimitError(RuntimeError):
    """Reserved error for the future bounded containment backend."""


def run_bounded_process_tree(
    args: list[str],
    *,
    cwd: Path,
    stdin: int,
    stdout: BinaryIO,
    stderr: BinaryIO,
    timeout: int | float,
    env: dict[str, str],
    max_output_bytes: int = MAX_SCENARIO_OUTPUT_BYTES,
):
    """Refuse execution until cgroup/VM-equivalent descendant containment is implemented."""
    del args, cwd, stdin, stdout, stderr, timeout, env, max_output_bytes
    raise ScenarioContainmentUnavailable(
        "scenario process containment unavailable: process groups do not contain setsid children"
    )


def run_bounded_capture(
    args: list[str],
    *,
    cwd: Path,
    timeout: int | float,
    env: dict[str, str],
    max_output_bytes: int = 1_000_000,
):
    """Refuse oracle shell execution under the same incomplete containment boundary."""
    del args, cwd, timeout, env, max_output_bytes
    raise ScenarioContainmentUnavailable(
        "oracle process containment unavailable: process groups do not contain setsid children"
    )
