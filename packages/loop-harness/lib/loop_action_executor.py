#!/usr/bin/env python3
"""Action-executor selection for host-compatible and fail-closed Docker runs."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import loop_docker_action as docker_action
import loop_docker_config as docker_config

HostChildRunner = docker_action.HostChildRunner
MechanicalRunner = Callable[..., tuple[str, int]]


class HostActionExecutor:
    """Preserve the pre-Phase-4 direct-host execution path."""

    def __init__(self, host_child_runner: HostChildRunner) -> None:
        self._host_child_runner = host_child_runner

    def execute_claude(
        self,
        command: list[str],
        cwd: str,
        timeout_seconds: float,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return self._host_child_runner(command, cwd, timeout_seconds, env)

    @property
    def mechanical_runner(self) -> MechanicalRunner | None:
        return None

    def finish(self, _result: Mapping[str, Any]) -> None:
        return

    def abort(self) -> None:
        return

    def cancel(self) -> None:
        return


class DockerActionExecutor:
    """Execute every untrusted action process inside one hardened container."""

    def __init__(self, runtime: docker_action.DockerActionRuntime) -> None:
        self.runtime = runtime

    def execute_claude(
        self,
        command: list[str],
        cwd: str,
        timeout_seconds: float,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return self.runtime.execute_claude(command, cwd, timeout_seconds, env)

    @property
    def mechanical_runner(self) -> MechanicalRunner:
        return self._run_mechanical

    def _run_mechanical(
        self,
        command: str,
        cwd: str,
        timeout_seconds: float,
        *,
        env: Mapping[str, str] | None = None,
        on_start: Callable[[int | None], None] | None = None,
    ) -> tuple[str, int]:
        del env, on_start
        return self.runtime.execute_mechanical(command, cwd, timeout_seconds)

    def finish(self, result: Mapping[str, Any]) -> None:
        action_succeeded = bool(result) and not bool(result.get("infrastructure_failure"))
        self.runtime.finish(action_succeeded=action_succeeded)

    def abort(self) -> None:
        self.runtime.finish(action_succeeded=False)

    def cancel(self) -> None:
        self.runtime.cancel()


def build_action_executor(
    config: dict[str, Any],
    *,
    project_dir: str,
    loop_id: str,
    action_id: str,
    action: str,
    worktree_path: str,
    branch: str,
    remaining_wall_clock_seconds: Callable[[], float],
    host_child_runner: HostChildRunner,
) -> HostActionExecutor | DockerActionExecutor:
    """Select once per dispatch; only execution_backend=docker enables Docker."""
    if not docker_config.docker_execution_enabled(config):
        return HostActionExecutor(host_child_runner)
    isolation = docker_config.validate_isolation_config(config)
    kind_by_action = {
        "run_maker": "maker",
        "run_checker": "checker",
        "wait_external_review": "classifier",
    }
    kind = kind_by_action.get(action)
    if kind is None:
        return HostActionExecutor(host_child_runner)
    request = docker_action.DockerActionRequest(
        config=config,
        isolation=isolation,
        project_dir=Path(project_dir).resolve(),
        loop_id=loop_id,
        action_id=action_id,
        worktree_path=Path(worktree_path).resolve(),
        branch=branch,
        kind=kind,  # type: ignore[arg-type]
        remaining_wall_clock_seconds=remaining_wall_clock_seconds,
    )
    return DockerActionExecutor(
        docker_action.DockerActionRuntime(request, host_child_runner=host_child_runner)
    )
