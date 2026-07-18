#!/usr/bin/env python3
"""One-action Docker lifecycle for isolated loop-harness execution."""

from __future__ import annotations

import math
import secrets
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_LIB_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _LIB_DIR.parent
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
for _path in (_LIB_DIR, _DOCKER_RUNTIME_LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import docker_runtime_cli as runtime_cli  # noqa: E402
import docker_runtime_lifecycle as runtime_lifecycle  # noqa: E402
import loop_docker_broker as broker_runtime  # noqa: E402
import loop_docker_config as docker_config  # noqa: E402
import loop_docker_image as docker_image  # noqa: E402
import loop_docker_profile as profile  # noqa: E402
import loop_docker_settings as docker_settings  # noqa: E402
import loop_git_ephemeral as git_ephemeral  # noqa: E402

ActionKind = Literal["maker", "checker", "classifier"]
IdleProcessSnapshot = tuple[tuple[int, str, str], ...]
SubprocessRunner = Callable[..., subprocess.CompletedProcess]
HostChildRunner = Callable[
    [list[str], str, float, dict[str, str]], subprocess.CompletedProcess[str]
]

DOCKER_LABEL = "ai.orchestra.loop-harness"
CONTAINER_LIFETIME_MARGIN_SECONDS = 60
DOCKER_EXEC_CLIENT_FAILURE_EXIT_CODE = 125
_ALLOWED_IDLE_COMMANDS = frozenset({"docker-init", "tini", "timeout", "sleep"})
_RUNTIME_LABELS = runtime_lifecycle.RuntimeLabels(DOCKER_LABEL)


class DockerActionError(RuntimeError):
    """An isolated action could not execute or clean up safely."""

    def __init__(self, message: str, *, container_removed: bool = False) -> None:
        super().__init__(message)
        self.container_removed = container_removed


class DockerActionSafetyStop(DockerActionError):
    """A Docker action detected state that requires a durable loop safe-stop."""

    def __init__(
        self,
        stop_reason: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason
        self.details = dict(details or {})


@dataclass(frozen=True)
class DockerActionRequest:
    config: dict[str, Any]
    isolation: docker_config.DockerIsolationConfig
    project_dir: Path
    loop_id: str
    action_id: str
    worktree_path: Path
    branch: str
    kind: ActionKind
    remaining_wall_clock_seconds: Callable[[], float]


class DockerActionRuntime:
    """Lazily starts, executes in, and destroys one hardened action container."""

    def __init__(
        self,
        request: DockerActionRequest,
        *,
        host_child_runner: HostChildRunner,
        runner: SubprocessRunner = subprocess.run,
    ) -> None:
        self.request = request
        self.host_child_runner = host_child_runner
        self.runner = runner
        self.owner_id = runtime_lifecycle.owner_id(request.project_dir)
        self.owner_labels = runtime_lifecycle.resource_labels(_RUNTIME_LABELS, self.owner_id)
        self.container_name = ""
        self.broker: broker_runtime.LoopBrokerSession | None = None
        self.git_session: git_ephemeral.EphemeralGitSession | None = None
        self.settings_bundle: docker_settings.DockerSettingsBundle | None = None
        self._started = False
        self._scenario_start_attempted = False
        self._scenario_removed = False
        self._idle_process_baseline: IdleProcessSnapshot | None = None
        self._finished = False
        self._cancel_requested = threading.Event()
        self._lifecycle_lock = threading.RLock()

    @property
    def started(self) -> bool:
        return self._started

    def execute_claude(
        self,
        command: list[str],
        cwd: str,
        timeout_seconds: float,
        _env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        self._ensure_started()
        if self.request.kind == "classifier":
            command = _without_settings(command)
        else:
            if self.settings_bundle is None:
                raise DockerActionError("trusted Docker settings bundle is unavailable")
            command = docker_settings.rewrite_claude_settings(command, self.settings_bundle)
        return self._execute(
            command,
            cwd="/tmp" if self.request.kind == "classifier" else cwd,
            timeout_seconds=timeout_seconds,
            env=self._broker_exec_env(),
        )

    def execute_mechanical(
        self,
        command: str,
        cwd: str,
        timeout_seconds: float,
    ) -> tuple[str, int]:
        self._ensure_started()
        completed = self._execute(
            ["/bin/bash", "-lc", command],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env={},
        )
        output = completed.stdout
        if completed.stderr:
            output += ("\n" if output else "") + completed.stderr
        return output, completed.returncode

    def finish(self, *, action_succeeded: bool) -> None:
        with self._lifecycle_lock:
            if self._finished:
                return
            self._finished = True
            scenario_error, cleanup_errors = self._cleanup_containers()
            primary_error: BaseException | None = scenario_error
            try:
                if primary_error is None:
                    self._finish_git(action_succeeded=action_succeeded)
            except BaseException as exc:
                primary_error = exc
            finally:
                self._cleanup_local_runtime(cleanup_errors)
            if primary_error is not None:
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(f"action cleanup also failed: {cleanup_error}")
                raise primary_error
            if cleanup_errors:
                raise DockerActionSafetyStop(
                    "action_cleanup_failed",
                    "isolated action cleanup failed",
                    details={"cleanup_errors": cleanup_errors},
                )

    def cancel(self) -> None:
        """Latch cancellation and destroy a started scenario without leaking thread errors."""
        self._cancel_requested.set()
        # Cancellation owns the untrusted scenario cgroup only. The dispatch thread remains
        # the sole owner of broker/network/settings/Git cleanup through finish()/abort(); doing
        # that work from the heartbeat thread would race result finalization and state fencing.
        with self._lifecycle_lock:
            if not self._scenario_start_attempted or self._scenario_removed:
                return
            try:
                self._destroy_scenario_locked()
            except DockerActionSafetyStop:
                # The dispatch thread retries cleanup and turns persistent failure into a
                # typed safety-stop. Heartbeat threads must never mutate loop state directly.
                return

    def _ensure_started(self) -> None:
        if self._started:
            return
        try:
            self._start()
        except (DockerActionError, DockerActionSafetyStop):
            raise
        except (
            docker_config.DockerConfigError,
            docker_image.DockerImageError,
            broker_runtime.LoopDockerBrokerError,
            profile.DockerProfileError,
            docker_settings.DockerSettingsError,
            git_ephemeral.EphemeralGitInfrastructureError,
            git_ephemeral.EphemeralGitSafetyStop,
        ) as exc:
            self._raise_normalized(exc)

    def _start(self) -> None:
        self._raise_if_cancelled()
        if not runtime_cli.docker_daemon_available(runner=self.runner):
            raise DockerActionError("Docker daemon unavailable")
        broker_runtime.sweep_stale_resources(self.owner_id, runner=self.runner)
        scenario_image = docker_image.ensure_scenario_image(
            self.request.config, self.request.project_dir, runner=self.runner
        )
        broker_image = docker_image.ensure_broker_image(
            self.request.config, self.request.project_dir, runner=self.runner
        )
        self._raise_if_cancelled()
        mounts, container_env, workdir = self._prepare_mounts()
        max_lifetime = _max_lifetime_seconds(self.request.remaining_wall_clock_seconds())
        self.broker = broker_runtime.start_broker(
            self.request.config,
            scope=f"{self.request.loop_id}-{self.request.action_id}",
            owner_id=self.owner_id,
            scenario_image_id=scenario_image.image_id,
            broker_image_id=broker_image.image_id,
            max_lifetime_seconds=max_lifetime,
            runner=self.runner,
        )
        self._raise_if_cancelled()
        self.container_name = (
            f"lh-{profile.runtime.safe_name(self.request.loop_id)}-"
            f"{profile.runtime.safe_name(self.request.action_id)}-{secrets.token_hex(3)}"
        )
        spec = profile.ScenarioContainerSpec(
            container_name=self.container_name,
            image_id=scenario_image.image_id,
            internal_network=self.broker.internal_network,
            workdir=workdir,
            mounts=mounts,
            env=container_env,
            resources=profile.resources_config(self.request.isolation.resources),
            max_lifetime_sec=max_lifetime,
            owner_labels=self.owner_labels,
        )
        # docker run plus the trusted idle-baseline capture is one atomic start section.
        # cancel() latches its Event immediately but may wait for this bounded section's Docker
        # calls; the post-start check removes the container before any docker exec can begin.
        with self._lifecycle_lock:
            self._raise_if_cancelled_locked()
            self._scenario_start_attempted = True
            start_scenario_container(spec, runner=self.runner)
            self._idle_process_baseline = capture_scenario_idle_baseline(
                self.container_name,
                runner=self.runner,
            )
            self._started = True
            self._raise_if_cancelled_locked()

    def _prepare_mounts(self) -> tuple[tuple[Any, ...], dict[str, str], Path]:
        if self.request.kind == "classifier":
            return (), {}, Path("/tmp")
        self.git_session = git_ephemeral.prepare_ephemeral_git(
            project_dir=self.request.project_dir,
            loop_id=self.request.loop_id,
            action_id=self.request.action_id,
            worktree_path=self.request.worktree_path,
            branch=self.request.branch,
        )
        runtime_dir = self.git_session.runtime_dir
        self.settings_bundle = docker_settings.create_settings_bundle(
            runtime_dir,
            _LIB_DIR / "maker_bash_guard.py",
        )
        if self.request.kind == "maker":
            git_mounts = git_ephemeral.build_maker_git_mount_spec(self.git_session)
        else:
            # validate_isolation_config() already enforces this for config-built requests.
            # Keep the runtime assertion as defense-in-depth for directly constructed requests.
            if not self.request.isolation.checker_read_only_worktree:
                raise DockerActionError("Checker worktree must be read-only")
            git_mounts = git_ephemeral.build_checker_git_mount_spec(self.git_session)
        trusted_mount = git_ephemeral.BindMountSpec(
            self.settings_bundle.source_dir,
            Path(self.settings_bundle.container_dir),
            True,
        )
        return (
            (*git_mounts.mounts, trusted_mount),
            dict(git_mounts.env),
            self.request.worktree_path,
        )

    def _execute(
        self,
        command: list[str],
        *,
        cwd: str,
        timeout_seconds: float,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        self._raise_if_cancelled()
        docker_command = profile.build_exec_command(
            self.container_name,
            command,
            workdir=cwd,
            env=env,
        )
        try:
            completed = self.host_child_runner(
                docker_command,
                str(self.request.project_dir),
                timeout_seconds,
                runtime_cli.host_env(),
            )
        except Exception as exc:
            self._destroy_scenario_or_raise()
            raise DockerActionError("docker exec did not complete") from exc
        except BaseException:
            self._destroy_scenario_or_raise()
            raise
        self._raise_if_cancelled()
        if completed.returncode == DOCKER_EXEC_CLIENT_FAILURE_EXIT_CODE:
            self._destroy_scenario_or_raise()
            raise DockerActionError("docker exec failed before the action command ran")
        self._assert_idle_or_destroy()
        return completed

    def _broker_exec_env(self) -> dict[str, str]:
        if self.broker is None:
            raise DockerActionError("credential broker is unavailable")
        return {
            "ANTHROPIC_BASE_URL": self.broker.base_url,
            "ANTHROPIC_API_KEY": self.broker.run_token,
            "CLAUDE_CONFIG_DIR": f"{profile.CONTAINER_HOME}/.claude",
            "NO_PROXY": broker_runtime.BROKER_ALIAS,
        }

    def _assert_idle_or_destroy(self) -> None:
        try:
            enforce_scenario_container_idle(
                self.container_name,
                expected_snapshot=self._idle_process_baseline,
                runner=self.runner,
            )
        except DockerActionError as exc:
            self._scenario_removed = exc.container_removed
            raise

    def _destroy_scenario_or_raise(self) -> None:
        with self._lifecycle_lock:
            self._destroy_scenario_locked()

    def _destroy_scenario_locked(self) -> None:
        if self._scenario_removed:
            return
        if not self.container_name:
            return
        if not remove_scenario_container(self.container_name, runner=self.runner):
            raise DockerActionSafetyStop(
                "maker_container_cleanup_unconfirmed"
                if self.request.kind == "maker"
                else "container_cleanup_unconfirmed",
                "could not confirm action container removal",
                details={"container_name": self.container_name},
            )
        self._scenario_removed = True

    def _raise_if_cancelled(self) -> None:
        if not self._cancel_requested.is_set():
            return
        with self._lifecycle_lock:
            self._raise_if_cancelled_locked()

    def _raise_if_cancelled_locked(self) -> None:
        if not self._cancel_requested.is_set():
            return
        if self._scenario_start_attempted and not self._scenario_removed:
            self._destroy_scenario_locked()
        raise DockerActionError("Docker action was cancelled")

    def _cleanup_containers(self) -> tuple[DockerActionSafetyStop | None, list[str]]:
        scenario_error: DockerActionSafetyStop | None = None
        errors: list[str] = []
        try:
            self._destroy_scenario_or_raise()
        except DockerActionSafetyStop as exc:
            scenario_error = exc
        if self.broker is not None:
            try:
                self.broker.cleanup()
            except broker_runtime.LoopDockerBrokerError as exc:
                errors.append(str(exc))
        return scenario_error, errors

    def _finish_git(self, *, action_succeeded: bool) -> None:
        if self.git_session is None or self.request.kind != "maker":
            return
        if self._scenario_start_attempted and not self._scenario_removed:
            raise DockerActionSafetyStop(
                "maker_container_cleanup_unconfirmed",
                "Maker finalize forbidden because container cleanup was not confirmed",
            )
        if action_succeeded:
            git_ephemeral.finalize_ephemeral_git(self.git_session)
            return
        git_ephemeral.verify_failed_maker_worktree(self.git_session)

    def _cleanup_local_runtime(self, errors: list[str]) -> None:
        try:
            docker_settings.cleanup_settings_bundle(self.settings_bundle)
        except docker_settings.DockerSettingsError as exc:
            errors.append(str(exc))
        if self.git_session is not None:
            try:
                git_ephemeral.cleanup_ephemeral_git(self.git_session)
            except git_ephemeral.EphemeralGitInfrastructureError as exc:
                errors.append(str(exc))

    @staticmethod
    def _raise_normalized(exc: BaseException) -> None:
        if isinstance(exc, git_ephemeral.EphemeralGitSafetyStop):
            raise DockerActionSafetyStop(
                exc.stop_reason,
                str(exc),
                details=exc.details,
            ) from exc
        raise DockerActionError(str(exc)) from exc


def _max_lifetime_seconds(remaining_seconds: float) -> int:
    if not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
        raise DockerActionError("action wall-clock budget is exhausted")
    return math.ceil(remaining_seconds) + CONTAINER_LIFETIME_MARGIN_SECONDS


def start_scenario_container(
    spec: profile.ScenarioContainerSpec,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> None:
    """Start one scenario using the production hardened profile builder."""
    completed = runtime_cli.run(
        profile.build_scenario_container_command(spec),
        runner=runner,
        timeout=30,
    )
    if completed.returncode != 0:
        raise DockerActionError("could not start hardened scenario container")


def capture_scenario_idle_baseline(
    container_name: str,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> IdleProcessSnapshot:
    """Capture the trusted supervisor identity immediately after container startup."""
    completed = runtime_cli.run(
        ["docker", "top", container_name, "-eo", "pid,comm,args"],
        runner=runner,
        timeout=10,
    )
    snapshot = _process_snapshot(completed.stdout) if completed.returncode == 0 else None
    if snapshot is None or not _only_idle_snapshot(snapshot):
        raise DockerActionError("docker exec left non-idle processes in the action container")
    return snapshot


def assert_scenario_container_idle(
    container_name: str,
    *,
    expected_snapshot: IdleProcessSnapshot | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> None:
    """Fail unless current processes exactly match the trusted startup supervisor."""
    current = capture_scenario_idle_baseline(container_name, runner=runner)
    if expected_snapshot is not None and current != expected_snapshot:
        raise DockerActionError("docker exec left non-idle processes in the action container")


def enforce_scenario_container_idle(
    container_name: str,
    *,
    expected_snapshot: IdleProcessSnapshot | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> None:
    """Destroy the action cgroup fail-closed when an exec leaves residual processes."""
    try:
        assert_scenario_container_idle(
            container_name,
            expected_snapshot=expected_snapshot,
            runner=runner,
        )
    except DockerActionError as exc:
        if not remove_scenario_container(container_name, runner=runner):
            raise DockerActionError("non-idle action container could not be removed") from exc
        raise DockerActionError(str(exc), container_removed=True) from exc


def remove_scenario_container(
    container_name: str,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> bool:
    """Remove and confirm absence using the shared production cleanup primitive."""
    return runtime_cli.remove_container(container_name, runner=runner)


def _without_settings(command: list[str]) -> list[str]:
    rewritten = list(command)
    try:
        index = rewritten.index("--settings")
    except ValueError:
        return rewritten
    if index + 1 >= len(rewritten):
        raise DockerActionError("claude command has an invalid --settings argument")
    del rewritten[index : index + 2]
    if rewritten:
        rewritten[0] = "claude"
    return rewritten


def _only_idle_processes(output: str) -> bool:
    snapshot = _process_snapshot(output)
    return snapshot is not None and _only_idle_snapshot(snapshot)


def _process_snapshot(output: str) -> IdleProcessSnapshot | None:
    lines = [line.strip() for line in output.splitlines()[1:] if line.strip()]
    if not lines:
        return None
    processes: list[tuple[int, str, str]] = []
    for line in lines:
        fields = line.split(maxsplit=2)
        if len(fields) < 2 or not fields[0].isdigit():
            return None
        processes.append(
            (
                int(fields[0]),
                fields[1],
                fields[2] if len(fields) == 3 else "",
            )
        )
    return tuple(sorted(processes))


def _only_idle_snapshot(snapshot: IdleProcessSnapshot) -> bool:
    if not snapshot:
        return False
    for _pid, command, arguments in snapshot:
        if command not in _ALLOWED_IDLE_COMMANDS or not _is_idle_process(command, arguments):
            return False
    return any(command == "sleep" for _pid, command, _arguments in snapshot)


def _is_idle_process(command: str, arguments: str) -> bool:
    if command == "sleep":
        return arguments.split()[-2:] == ["/usr/bin/sleep", "infinity"]
    if command == "timeout":
        return "/usr/bin/timeout " in f" {arguments}" and arguments.endswith(
            "/usr/bin/sleep infinity"
        )
    if command in {"docker-init", "tini"}:
        return "docker-init" in arguments or "/usr/bin/timeout" in arguments
    return False
