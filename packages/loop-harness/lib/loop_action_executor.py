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

    def discard(self) -> None:
        # Local pre-push review (round 9): the host executor's finish()/abort() were already
        # no-ops -- there is no ephemeral git session or container lifecycle on the host path
        # for a lease-lost Maker to misclassify against `baseline_sha` -- so the quiet-teardown
        # contract `DockerActionExecutor.discard()` provides is trivially satisfied here too.
        # Kept as its own method (not an alias for `abort`) so both executors expose the same
        # lease-lost teardown surface `loop_driver._dispatch()` calls unconditionally.
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
        # Codex review, PR #262, High: `env` is `run_mechanical_checks()`'s sanitized,
        # push-credential-stripped `checker_env` (loop_driver._run_checker's SEC-P1 env, the same
        # one the host executor already honors) -- discarding it here made mechanical commands
        # unable to redirect tool caches (e.g. `RUFF_CACHE_DIR`) only under Docker, where the
        # default `.ruff_cache`-in-project-root falls back to the read-only checker worktree.
        # `on_start` has no Docker-executor equivalent (no per-command host pid to track).
        del on_start
        return self.runtime.execute_mechanical(command, cwd, timeout_seconds, env=env)

    def finish(self, result: Mapping[str, Any]) -> None:
        action_succeeded = bool(result) and not bool(result.get("infrastructure_failure"))
        self.runtime.finish(action_succeeded=action_succeeded)

    def abort(self) -> None:
        self.runtime.finish(action_succeeded=False)

    def discard(self) -> None:
        self.runtime.discard_after_lease_loss()

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
    params: Mapping[str, Any] | None = None,
    lease_lost: Callable[[], bool] | None = None,
) -> HostActionExecutor | DockerActionExecutor:
    """Select once per dispatch; only execution_backend=docker enables Docker."""
    kind_by_action = {
        "run_maker": "maker",
        "run_checker": "checker",
        "wait_external_review": "classifier",
    }
    kind = kind_by_action.get(action)
    if kind is None:
        # Codex review, PR #262, High (round 3) / P2 (round 9): host-only actions
        # (advance_phase/stop/exit_*) never dispatch into a container regardless of isolation
        # config validity, so this lookup must run before *any* Docker config read -- including
        # `docker_config.docker_execution_enabled()`'s own minimal switch validation just below,
        # which can itself raise `DockerConfigError` for a bad `lp2.isolation.execution_backend`/
        # `backend` combination. `_dispatch()` only translates `docker_config.DockerConfigError`
        # into an infrastructure result for run_maker/run_checker/wait_external_review; any other
        # action reaching that path raises `InvalidStateError` instead of just running on the
        # host as it always has.
        return HostActionExecutor(host_child_runner)
    if not docker_config.docker_execution_enabled(config):
        return HostActionExecutor(host_child_runner)
    isolation = docker_config.validate_isolation_config(config)
    needs_broker = True
    if kind == "checker" and params is not None:
        # Codex review, PR #262, High: only a "checker" action can skip the broker -- "maker"
        # always needs it for the Claude coding step, "classifier" for wait_external_review's own
        # classification call. A checker action whose resolved params have no `llm_review` block
        # (mirrors loop_driver's own `has_llm_review` gating on the same `params`) never calls
        # execute_claude(), so it does not need a Claude credential broker. `params is None`
        # (test call sites that omit it) preserves today's always-True behavior exactly.
        needs_broker = isinstance(params.get("llm_review"), dict)
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
        needs_broker=needs_broker,
        # Codex review, PR #262, P1 (round 9): `loop_driver._dispatch()` now calls this
        # builder with `lease_lost=self._lease_lost.is_set` (round 8, fence #2), but this
        # parameter did not exist here, raising `TypeError` on every dispatch. Thread it
        # straight through to `DockerActionRequest.lease_lost` so `DockerActionRuntime.finish()`
        # (and `abort()`, which routes through it) can still re-check lease loss itself.
        lease_lost=lease_lost,
    )
    return DockerActionExecutor(
        docker_action.DockerActionRuntime(request, host_child_runner=host_child_runner)
    )
