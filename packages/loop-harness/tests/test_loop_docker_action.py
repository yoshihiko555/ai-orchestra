"""Phase-4 action executor and one-container lifecycle tests."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

docker_config = load_module(
    "loop_docker_config",
    "packages/loop-harness/lib/loop_docker_config.py",
)
profile = load_module("loop_docker_profile", "packages/loop-harness/lib/loop_docker_profile.py")
git_ephemeral = load_module(
    "loop_git_ephemeral",
    "packages/loop-harness/lib/loop_git_ephemeral.py",
)
docker_settings = load_module(
    "loop_docker_settings",
    "packages/loop-harness/lib/loop_docker_settings.py",
)
broker_runtime = load_module(
    "loop_docker_broker",
    "packages/loop-harness/lib/loop_docker_broker.py",
)
docker_image = load_module(
    "loop_docker_image",
    "packages/loop-harness/lib/loop_docker_image.py",
)
docker_action = load_module(
    "loop_docker_action",
    "packages/loop-harness/lib/loop_docker_action.py",
)
action_executor = load_module(
    "loop_action_executor_tests",
    "packages/loop-harness/lib/loop_action_executor.py",
)

IMAGE_ID = "sha256:" + "a" * 64


def _config(*, execution_backend: str = "docker") -> dict[str, Any]:
    config = yaml.safe_load(
        (REPO_ROOT / "packages/loop-harness/config/loop-harness.yaml").read_text(encoding="utf-8")
    )
    config["lp2"]["isolation"].update({"backend": "docker", "execution_backend": execution_backend})
    return config


def _request(tmp_path: Path, *, kind: str = "maker") -> object:
    config = _config()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return docker_action.DockerActionRequest(
        config=config,
        isolation=docker_config.validate_isolation_config(config),
        project_dir=tmp_path,
        loop_id="loop-211",
        action_id="action-001",
        worktree_path=worktree,
        branch="issue-211",
        kind=kind,
        remaining_wall_clock_seconds=lambda: 600,
    )


def test_backend_docker_without_execution_backend_uses_host_executor(tmp_path: Path) -> None:
    def host_runner(*args: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, "", "")

    executor = action_executor.build_action_executor(
        _config(execution_backend="none"),
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="run_maker",
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=host_runner,
    )

    assert isinstance(executor, action_executor.HostActionExecutor)


def test_host_executor_ignores_unused_docker_profile_fields(tmp_path: Path) -> None:
    config = _config(execution_backend="none")
    config["lp2"]["isolation"]["checker"]["read_only_worktree"] = False
    config["lp2"]["isolation"]["resources"]["pids_limit"] = "invalid"

    executor = action_executor.build_action_executor(
        config,
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="run_checker",
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=lambda *args: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    assert isinstance(executor, action_executor.HostActionExecutor)


def test_docker_executor_never_falls_back_to_host_after_runtime_failure() -> None:
    class FailedRuntime:
        def execute_claude(self, *_args: Any, **_kwargs: Any) -> Any:
            raise docker_action.DockerActionError("daemon unavailable")

    host_calls: list[object] = []
    executor = action_executor.DockerActionExecutor(FailedRuntime())

    with pytest.raises(docker_action.DockerActionError, match="daemon unavailable"):
        executor.execute_claude(["claude", "-p", "prompt"], "/tmp", 30, {})

    assert host_calls == []


def test_empty_action_result_is_not_treated_as_maker_success() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.success: bool | None = None

        def finish(self, *, action_succeeded: bool) -> None:
            self.success = action_succeeded

    runtime = Runtime()

    action_executor.DockerActionExecutor(runtime).finish({})

    assert runtime.success is False


def test_action_executor_cancel_delegates_only_for_docker() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    runtime = Runtime()
    action_executor.HostActionExecutor(lambda *_args: None).cancel()
    action_executor.DockerActionExecutor(runtime).cancel()

    assert runtime.cancelled is True


def test_maker_lifecycle_uses_production_primitives_in_required_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    bundle = docker_settings.DockerSettingsBundle(tmp_path / "trusted")
    mount_spec = SimpleNamespace(mounts=(), env={"GIT_DIR": "/git", "GIT_WORK_TREE": "/work"})

    class Broker:
        internal_network = "lh-internal"
        base_url = "http://lh-broker:8790"
        run_token = "run-token"

        def cleanup(self) -> None:
            events.append("broker_cleanup")

    monkeypatch.setattr(
        docker_action.runtime_cli,
        "docker_daemon_available",
        lambda **_kwargs: events.append("daemon") or True,
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "sweep_stale_resources",
        lambda *_args, **_kwargs: events.append("stale_sweep"),
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_scenario_image",
        lambda *_args, **_kwargs: (
            events.append("scenario_image") or SimpleNamespace(image_id=IMAGE_ID)
        ),
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_broker_image",
        lambda *_args, **_kwargs: (
            events.append("broker_image") or SimpleNamespace(image_id=IMAGE_ID)
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "prepare_ephemeral_git",
        lambda **_kwargs: events.append("git_prepare") or session,
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "create_settings_bundle",
        lambda *_args: events.append("settings") or bundle,
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "build_maker_git_mount_spec",
        lambda *_args: events.append("mount_spec") or mount_spec,
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "start_broker",
        lambda *_args, **_kwargs: events.append("broker") or Broker(),
    )
    monkeypatch.setattr(
        docker_action.profile,
        "build_scenario_container_command",
        lambda *_args: events.append("profile") or ["docker", "run"],
    )

    def docker_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "run"]:
            events.append("scenario")
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[:2] == ["docker", "top"]:
            events.append("idle_check")
            return subprocess.CompletedProcess(
                command,
                0,
                "PID COMMAND COMMAND\n"
                "101 docker-init /usr/bin/docker-init -- /usr/bin/timeout 660s "
                "/usr/bin/sleep infinity\n"
                "102 timeout /usr/bin/timeout 660s /usr/bin/sleep infinity\n"
                "103 sleep /usr/bin/sleep infinity\n",
                "",
            )
        raise AssertionError(command)

    monkeypatch.setattr(docker_action.runtime_cli, "run", docker_run)
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda *_args, **_kwargs: events.append("scenario_cleanup") or True,
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: events.append("git_finalize"),
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "cleanup_settings_bundle",
        lambda *_args: events.append("settings_cleanup"),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: events.append("git_cleanup"),
    )

    def host_child(
        command: list[str], _cwd: str, _timeout: float, _env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        events.append("exec")
        return subprocess.CompletedProcess(command, 0, '{"result":"ok"}', "")

    runtime = docker_action.DockerActionRuntime(_request(tmp_path), host_child_runner=host_child)
    runtime.execute_claude(
        ["host-claude", "--settings", "/host/settings", "-p", "prompt"],
        str(tmp_path / "worktree"),
        30,
        {},
    )
    runtime.finish(action_succeeded=True)

    assert events == [
        "daemon",
        "stale_sweep",
        "scenario_image",
        "broker_image",
        "git_prepare",
        "settings",
        "mount_spec",
        "broker",
        "profile",
        "scenario",
        "idle_check",
        "exec",
        "idle_check",
        "scenario_cleanup",
        "broker_cleanup",
        "git_finalize",
        "settings_cleanup",
        "git_cleanup",
    ]


def test_non_idle_process_forces_container_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.container_name = "lh-action"
    runtime._started = True
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "PID COMMAND COMMAND\n101 sleep /usr/bin/sleep 300\n", ""
        ),
    )
    removed: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    with pytest.raises(docker_action.DockerActionError, match="non-idle"):
        runtime._assert_idle_or_destroy()

    assert removed == ["lh-action"]
    assert runtime._scenario_removed is True


def test_allowed_idle_command_spoof_does_not_match_startup_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_output = (
        "PID COMMAND COMMAND\n"
        "101 docker-init /usr/bin/docker-init -- /usr/bin/timeout 660s "
        "/usr/bin/sleep infinity\n"
        "102 timeout /usr/bin/timeout 660s /usr/bin/sleep infinity\n"
        "103 sleep /usr/bin/sleep infinity\n"
    )
    spoofed_output = baseline_output + "104 sleep /usr/bin/sleep infinity\n"
    outputs = iter((baseline_output, spoofed_output))

    monkeypatch.setattr(
        docker_action.runtime_cli,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, next(outputs), ""),
    )
    removed: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    baseline = docker_action.capture_scenario_idle_baseline("lh-action")
    with pytest.raises(docker_action.DockerActionError, match="non-idle") as caught:
        docker_action.enforce_scenario_container_idle(
            "lh-action",
            expected_snapshot=baseline,
        )

    assert caught.value.container_removed is True
    assert removed == ["lh-action"]


def test_docker_exec_timeout_is_normalized_and_removes_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(*_args: Any) -> subprocess.CompletedProcess[str]:
        raise TimeoutError("docker exec timed out")

    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier"),
        host_child_runner=timed_out,
    )
    runtime.container_name = "lh-action"
    runtime._started = True
    runtime.broker = SimpleNamespace(
        base_url="http://lh-broker:8790",
        run_token="run-token",
    )
    removed: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    with pytest.raises(docker_action.DockerActionError, match="docker exec did not complete"):
        runtime.execute_claude(["claude", "-p", "prompt"], "/tmp", 30, {})

    assert removed == ["lh-action"]
    assert runtime._scenario_removed is True


def test_cancel_before_start_prevents_any_docker_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier"),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    daemon_checks: list[bool] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "docker_daemon_available",
        lambda **_kwargs: daemon_checks.append(True) or True,
    )

    runtime.cancel()

    with pytest.raises(docker_action.DockerActionError, match="cancelled"):
        runtime.execute_mechanical("true", "/tmp", 30)

    assert daemon_checks == []


def test_cancel_removes_a_running_scenario_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier"),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.container_name = "lh-action"
    runtime._scenario_start_attempted = True
    runtime._started = True
    removed: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    runtime.cancel()
    runtime.cancel()

    assert removed == ["lh-action"]
    assert runtime._scenario_removed is True


def test_cancel_racing_scenario_start_removes_container_before_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier"),
        host_child_runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("cancelled scenario must never reach docker exec")
        ),
    )
    entered_start = threading.Event()
    release_start = threading.Event()
    removed: list[str] = []

    class Broker:
        internal_network = "lh-internal"

        def cleanup(self) -> None:
            return

    monkeypatch.setattr(
        docker_action.runtime_cli,
        "docker_daemon_available",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "sweep_stale_resources",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_scenario_image",
        lambda *_args, **_kwargs: SimpleNamespace(image_id=IMAGE_ID),
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_broker_image",
        lambda *_args, **_kwargs: SimpleNamespace(image_id=IMAGE_ID),
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "start_broker",
        lambda *_args, **_kwargs: Broker(),
    )

    def blocking_start(*_args: Any, **_kwargs: Any) -> None:
        entered_start.set()
        assert release_start.wait(timeout=5)

    monkeypatch.setattr(docker_action, "start_scenario_container", blocking_start)
    monkeypatch.setattr(
        docker_action,
        "capture_scenario_idle_baseline",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            runtime.execute_mechanical("true", "/tmp", 30)
        except BaseException as exc:
            failures.append(exc)

    execute_thread = threading.Thread(target=execute)
    execute_thread.start()
    assert entered_start.wait(timeout=5)
    cancel_thread = threading.Thread(target=runtime.cancel)
    cancel_thread.start()
    assert runtime._cancel_requested.wait(timeout=5)
    release_start.set()
    execute_thread.join(timeout=5)
    cancel_thread.join(timeout=5)

    assert not execute_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], docker_action.DockerActionError)
    assert "cancelled" in str(failures[0])
    assert removed == [runtime.container_name]


def test_trusted_settings_bundle_is_readable_by_non_root_container_and_cleanupable(
    tmp_path: Path,
) -> None:
    guard = tmp_path / "maker_bash_guard.py"
    guard.write_text("raise SystemExit(0)\n", encoding="utf-8")

    bundle = docker_settings.create_settings_bundle(tmp_path / "runtime", guard)

    assert bundle.source_dir.stat().st_mode & 0o777 == 0o555
    assert (bundle.source_dir / "settings.json").stat().st_mode & 0o777 == 0o444
    assert (bundle.source_dir / "maker_bash_guard.py").stat().st_mode & 0o777 == 0o555
    docker_settings.cleanup_settings_bundle(bundle)
    assert not bundle.source_dir.exists()


def test_failed_maker_partial_worktree_safe_stop_preserves_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True
    finalized: list[bool] = []
    cleaned: list[bool] = []
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "verify_failed_maker_worktree",
        lambda *_args: (_ for _ in ()).throw(
            git_ephemeral.EphemeralGitSafetyStop("maker_partial_worktree", "partial worktree")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: finalized.append(True),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: cleaned.append(True),
    )

    with pytest.raises(git_ephemeral.EphemeralGitSafetyStop, match="partial worktree") as caught:
        runtime.finish(action_succeeded=False)

    assert caught.value.stop_reason == "maker_partial_worktree"
    assert finalized == []
    assert cleaned == [True]


def test_maker_finalizes_after_scenario_removal_when_broker_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True

    class FailedBroker:
        def cleanup(self) -> None:
            raise broker_runtime.LoopDockerBrokerError("broker cleanup failed")

    runtime.broker = FailedBroker()
    finalized: list[bool] = []
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: finalized.append(True),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: None,
    )

    with pytest.raises(
        docker_action.DockerActionSafetyStop,
        match="isolated action cleanup failed",
    ) as caught:
        runtime.finish(action_succeeded=True)

    assert caught.value.stop_reason == "action_cleanup_failed"
    assert caught.value.details == {"cleanup_errors": ["broker cleanup failed"]}
    assert finalized == [True]


@pytest.mark.parametrize(
    ("kind", "expected_reason"),
    [
        ("maker", "maker_container_cleanup_unconfirmed"),
        ("checker", "container_cleanup_unconfirmed"),
        ("classifier", "container_cleanup_unconfirmed"),
    ],
)
def test_unconfirmed_container_removal_safe_stops_every_action_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_reason: str,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind=kind),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.container_name = "lh-action"
    runtime._scenario_start_attempted = True
    runtime._started = True
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(docker_action.DockerActionSafetyStop) as caught:
        runtime.finish(action_succeeded=False)

    assert caught.value.stop_reason == expected_reason
