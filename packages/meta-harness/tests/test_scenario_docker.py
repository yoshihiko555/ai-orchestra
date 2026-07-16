"""Docker scenario boundary command and lifecycle tests (EV-46, EV-47)."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_scenario_docker_tests",
    "packages/meta-harness/lib/meta_harness_common.py",
)
docker = load_module(
    "meta_harness_scenario_docker_tests",
    "packages/meta-harness/lib/scenario_docker.py",
)
siso = load_module(
    "meta_harness_scenario_isolation_docker_tests",
    "packages/meta-harness/lib/scenario_isolation.py",
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _prepare_git_snapshot(**kwargs):
    return siso._prepare_isolated_git(**kwargs)


def _broker(tmp_path: Path):
    return docker.DockerBrokerSession(
        container_name="mh-run-test-broker",
        internal_network="mh-run-test-internal",
        external_network="mh-run-test-external",
        run_token="per-run-dummy-token",
        port=8787,
        scenario_image="scenario:test",
        broker_image="broker:test",
        image_id="sha256:scenario",
        broker_image_id="sha256:broker",
        broker_settings_sha256="a" * 64,
        scenario_context_sha256="b" * 64,
        broker_context_sha256="c" * 64,
        scenario_base_image="node:test@sha256:" + "d" * 64,
        broker_base_image="broker:test@sha256:" + "e" * 64,
        owner_labels={
            docker.OWNER_LABEL: "owner-test",
            docker.PARENT_PID_LABEL: str(os.getpid()),
            docker.CREATED_AT_LABEL: str(int(time.time())),
        },
        runner=lambda *_args, **_kwargs: _completed(),
    )


def _launch(tmp_path: Path):
    worktree = tmp_path / "worktree"
    runtime = tmp_path / "runtime"
    instruction = tmp_path / "instruction.md"
    worktree.mkdir()
    runtime.mkdir()
    (runtime / "git-link-mask").write_text("")
    instruction.write_text("report\n")
    return docker.DockerScenarioLaunch(
        backend="docker",
        env={"PATH": "/usr/bin:/bin"},
        metadata={
            "resources": {
                "pids_limit": 128,
                "memory": "2g",
                "cpus": 2.0,
                "workspace_size": "512m",
                "workspace_max_files": 10000,
                "max_lifetime_sec": 660,
            }
        },
        broker=_broker(tmp_path),
        runtime_state_dir=runtime,
        worktree_dir=worktree,
        instruction_path=instruction,
        scenario_container_name="mh-run-test-scenario",
        owned_runtime_state_dir=None,
    )


@pytest.mark.parametrize(
    ("remove", "missing_error"),
    [
        (
            lambda runner: docker.dcli.remove_container("gone", runner=runner),
            "No such object: gone",
        ),
        (
            lambda runner: docker.dcli.remove_network("gone", runner=runner),
            "network gone not found",
        ),
    ],
)
def test_docker_resource_cleanup_requires_explicit_missing_object(remove, missing_error) -> None:
    daemon_error = iter(
        [
            _completed(1, stderr="Cannot connect to the Docker daemon"),
            _completed(1, stderr="Cannot connect to the Docker daemon"),
        ]
    )
    assert remove(lambda *_args, **_kwargs: next(daemon_error)) is False

    explicit_missing = iter(
        [
            _completed(1, stderr="remove failed"),
            _completed(1, stderr=missing_error),
        ]
    )
    assert remove(lambda *_args, **_kwargs: next(explicit_missing)) is True


def test_docker_host_env_preserves_client_connection_settings(monkeypatch) -> None:
    expected = {
        "DOCKER_API_VERSION": "1.47",
        "DOCKER_CERT_PATH": "/tmp/docker-certs",
        "DOCKER_CONFIG": "/tmp/docker-config",
        "DOCKER_CONTEXT": "remote-context",
        "DOCKER_HOST": "tcp://docker.example:2376",
        "DOCKER_TLS": "1",
        "DOCKER_TLS_VERIFY": "1",
    }
    for key, value in expected.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PARENT_SECRET", "must-not-cross")

    env = docker.dcli.host_env()

    for key, value in expected.items():
        assert env[key] == value
    assert "PARENT_SECRET" not in env


def test_broker_session_keepalive_uses_health_endpoint(tmp_path: Path) -> None:
    session = _broker(tmp_path)
    commands: list[list[str]] = []
    session.runner = lambda command, **_kwargs: commands.append(command) or _completed()

    class StopAfterOneProbe:
        calls = 0

        def wait(self, _interval: float) -> bool:
            self.calls += 1
            return self.calls > 1

    docker._broker_keepalive_loop(session, StopAfterOneProbe(), interval_seconds=1)

    assert commands == [
        [
            "docker",
            "exec",
            session.container_name,
            "/usr/bin/python3",
            docker.CONTAINER_BROKER_SCRIPT,
            "--health",
            "--port",
            str(session.port),
        ]
    ]


def test_execution_boundary_reports_only_matching_docker_backend() -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    assert siso.execution_boundary_available(config) is True
    config["evaluate"]["isolation"]["execution_backend"] = "none"
    assert siso.execution_boundary_available(config) is False
    config["evaluate"]["isolation"] = {
        "backend": "srt",
        "execution_backend": "docker",
    }
    assert siso.execution_boundary_available(config) is False


def test_candidate_command_has_only_allowlisted_mounts_and_internal_network(tmp_path: Path) -> None:
    launch = _launch(tmp_path)

    command = docker.profile.build_scenario_container_command(launch)
    rendered = "\n".join(command)

    assert "--network\nmh-run-test-internal" in rendered
    assert f"src={launch.worktree_dir.resolve()},dst=/input,readonly" in rendered
    assert f"src={launch.runtime_state_dir.resolve()},dst=/runtime,readonly" in rendered
    assert (
        f"src={launch.instruction_path.resolve()},dst=/meta/self-report-instruction.md,readonly"
        in rendered
    )
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert len(mounts) == 3
    assert any(value.startswith("/workspace:") and "size=512m" in value for value in command)
    assert "/var/run/docker.sock" not in rendered
    assert "/run/docker.sock" not in rendered
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "ANTHROPIC_API_KEY=per-run-dummy-token" in command
    assert "ANTHROPIC_BASE_URL=http://mh-broker:8787" in command
    assert command[-6:] == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "660s",
        "/usr/bin/sleep",
        "infinity",
    ]


def test_oracle_is_separate_no_network_read_only_container(tmp_path: Path) -> None:
    launch = _launch(tmp_path)

    command = docker.build_oracle_command(launch, "pytest -q", container_name="mh-run-oracle")
    rendered = "\n".join(command)

    assert "--network\nnone" in rendered
    assert f"src={launch.worktree_dir.resolve()},dst=/workspace,readonly" in rendered
    assert "ANTHROPIC_BASE_URL" not in rendered
    assert f"src={launch.runtime_state_dir.resolve()},dst=/runtime,readonly" not in rendered
    assert (
        f"src={(launch.runtime_state_dir / 'git-snapshot').resolve()},"
        "dst=/runtime/git-snapshot,readonly" in rendered
    )
    assert (
        f"src={(launch.runtime_state_dir / 'bin').resolve()},dst=/runtime/bin,readonly" in rendered
    )
    assert "dst=/workspace/.git,readonly" in rendered
    assert "GIT_DIR=/runtime/git-snapshot" in command
    assert "GIT_WORK_TREE=/workspace" in command
    assert "PATH=/runtime/bin:/usr/local/bin:/usr/bin:/bin" in command
    assert command[-7:] == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "660s",
        "/bin/sh",
        "-c",
        "pytest -q",
    ]


def test_judge_has_broker_but_no_candidate_worktree_mount(tmp_path: Path) -> None:
    launch = _launch(tmp_path)

    command = docker.build_judge_command(
        launch,
        ["claude", "-p", "grade", "--bare"],
        container_name="mh-run-judge",
    )
    rendered = "\n".join(command)

    assert "--network\nmh-run-test-internal" in rendered
    assert "ANTHROPIC_BASE_URL=http://mh-broker:8787" in command
    assert str(launch.worktree_dir.resolve()) not in rendered
    assert str(launch.runtime_state_dir.resolve()) not in rendered
    assert command[-8:] == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "660s",
        "claude",
        "-p",
        "grade",
        "--bare",
    ]


def test_broker_token_is_injected_via_stdin_not_argv_or_env(tmp_path: Path, monkeypatch) -> None:
    commands: list[tuple[list[str], dict]] = []
    oauth_token = "real-oauth-token-never-in-command"

    def runner(command, **kwargs):
        commands.append((list(command), kwargs))
        if "--print-metrics" in command:
            return _completed(stdout="{}")
        return _completed(stdout="ok")

    monkeypatch.setattr(
        docker, "ensure_images", lambda *_args, **_kwargs: ("scenario:test", "broker:test")
    )
    monkeypatch.setattr(docker, "_image_id", lambda image, **_kwargs: f"sha256:{image}")
    monkeypatch.setattr(
        docker, "_image_claude_version", lambda *_args, **_kwargs: "2.1.207 (Claude Code)"
    )
    monkeypatch.setattr(docker, "_wait_for_broker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        docker.credentials,
        "load_claude_oauth_credential",
        lambda **_kwargs: docker.credentials.ClaudeOAuthCredential(
            access_token=oauth_token,
            expires_at_epoch=time.time() + 3600,
        ),
    )

    session = docker._start_broker(
        copy.deepcopy(mh.DEFAULTS), "test", owner_id="owner-test", runner=runner
    )
    try:
        flattened = "\n".join("\n".join(command) for command, _kwargs in commands)
        assert oauth_token not in flattened
        injection = next(
            kwargs for command, kwargs in commands if command[:3] == ["docker", "exec", "-i"]
        )
        assert injection["input"] == oauth_token
        broker_run = next(
            command for command, _kwargs in commands if command[:3] == ["docker", "run", "-d"]
        )
        assert "--network" in broker_run
        assert "--read-only" in broker_run
        assert "--cap-drop=ALL" in broker_run
        assert all(oauth_token not in value for value in broker_run)
        assert "sha256:broker:test" in broker_run
        assert "broker:test" not in broker_run
    finally:
        session.cleanup()

    cleanup_commands = [command for command, _kwargs in commands]
    assert ["docker", "rm", "-f", session.container_name] in cleanup_commands
    assert ["docker", "network", "rm", session.internal_network] in cleanup_commands
    assert ["docker", "network", "rm", session.external_network] in cleanup_commands


def test_daemon_absence_fails_capability_without_fallback() -> None:
    result = docker.check_docker_capabilities(
        copy.deepcopy(mh.DEFAULTS),
        runner=lambda *_args, **_kwargs: _completed(returncode=1),
    )

    assert result.ok is False
    assert result.checks["docker_daemon"] is False
    assert "daemon" in (result.reason or "").lower()


def test_image_pin_mismatch_fails_capability_before_smoke(tmp_path: Path, monkeypatch) -> None:
    session = _broker(tmp_path)
    session.cleaned = True
    monkeypatch.setattr(docker, "_docker_daemon_available", lambda **_kwargs: True, raising=False)
    monkeypatch.setattr(docker.dcli, "docker_daemon_available", lambda **_kwargs: True)
    monkeypatch.setattr(docker, "sweep_stale_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        docker, "docker_broker_session", lambda *_args, **_kwargs: docker._BrokerContext(session)
    )
    monkeypatch.setattr(docker, "_image_claude_version", lambda *_args, **_kwargs: "9.9.9")
    monkeypatch.setattr(
        docker,
        "_run_smoke_container",
        lambda *_args, **_kwargs: pytest.fail("smoke must not run after pin mismatch"),
    )

    result = docker.check_docker_capabilities(
        copy.deepcopy(mh.DEFAULTS), main_root=tmp_path, runner=session.runner
    )

    assert result.ok is False
    assert result.version_pin_match is False
    assert "mismatch" in (result.reason or "")


def test_capability_smoke_uses_configured_evaluate_model(tmp_path: Path, monkeypatch) -> None:
    session = _broker(tmp_path)
    session.cleaned = True
    commands: list[list[str]] = []
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["model"] = "claude-custom-model"
    monkeypatch.setattr(docker.dcli, "docker_daemon_available", lambda **_kwargs: True)
    monkeypatch.setattr(docker, "sweep_stale_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        docker, "docker_broker_session", lambda *_args, **_kwargs: docker._BrokerContext(session)
    )
    monkeypatch.setattr(
        docker,
        "_image_claude_version",
        lambda *_args, **_kwargs: "2.1.207 (Claude Code)",
    )

    def run_smoke(_broker_session, command, **_kwargs):
        commands.append(command)
        return _completed(stdout='{"type":"result"}')

    monkeypatch.setattr(docker, "_run_smoke_container", run_smoke)

    result = docker.check_docker_capabilities(config, main_root=tmp_path, runner=session.runner)

    assert result.ok is True
    assert len(commands) == 3
    for command in commands[:2]:
        model_index = command.index("--model")
        assert command[model_index + 1] == "claude-custom-model"


def test_broker_startup_cleanup_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    del tmp_path
    monkeypatch.setattr(
        docker, "ensure_images", lambda *_args, **_kwargs: ("scenario:test", "broker:test")
    )
    monkeypatch.setattr(docker, "_image_id", lambda image, **_kwargs: f"sha256:{image}")
    monkeypatch.setattr(
        docker, "_image_claude_version", lambda *_args, **_kwargs: "2.1.207 (Claude Code)"
    )
    monkeypatch.setattr(
        docker.credentials,
        "load_claude_oauth_credential",
        lambda **_kwargs: docker.credentials.ClaudeOAuthCredential(
            access_token="oauth-token", expires_at_epoch=time.time() + 3600
        ),
    )
    monkeypatch.setattr(
        docker,
        "_wait_for_broker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            docker.DockerScenarioError("forced health failure")
        ),
    )

    def runner(command, **_kwargs):
        if command[:3] == ["docker", "rm", "-f"]:
            return _completed(returncode=1)
        if command[:2] == ["docker", "inspect"]:
            return _completed(stdout="still-running")
        return _completed()

    with pytest.raises(docker.DockerScenarioError, match="could not remove failed"):
        docker._start_broker(
            copy.deepcopy(mh.DEFAULTS), "health-failure", owner_id="owner-test", runner=runner
        )


def test_stale_cleanup_removes_prefixed_containers_and_networks() -> None:
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["docker", "ps", "-aq"]:
            return _completed(stdout="container-a\ncontainer-b\n")
        if command[:3] == ["docker", "network", "ls"]:
            return _completed(stdout="network-a\n")
        if command[:2] == ["docker", "inspect"]:
            return _completed(
                stdout=json.dumps(
                    [
                        {
                            "Config": {
                                "Labels": {
                                    docker.OWNER_LABEL: "owner-test",
                                    docker.PARENT_PID_LABEL: "999999999",
                                    docker.CREATED_AT_LABEL: str(int(time.time())),
                                }
                            },
                            "State": {"Running": True},
                        }
                    ]
                )
            )
        if command[:3] == ["docker", "network", "inspect"]:
            return _completed(
                stdout=json.dumps(
                    [{"Labels": {docker.OWNER_LABEL: "owner-test"}, "Containers": {}}]
                )
            )
        return _completed()

    docker.sweep_stale_resources("owner-test", runner=runner)

    assert ["docker", "rm", "-f", "container-a"] in commands
    assert ["docker", "rm", "-f", "container-b"] in commands
    assert ["docker", "network", "rm", "network-a"] in commands


def test_broker_cleanup_fails_closed_when_container_still_exists(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        if "--print-metrics" in command:
            return _completed(stdout="{}")
        if command[:3] == ["docker", "rm", "-f"]:
            return _completed(returncode=1)
        if command[:2] == ["docker", "inspect"]:
            return _completed(returncode=0, stdout="still exists")
        return _completed()

    session = _broker(tmp_path)
    session.runner = runner

    with pytest.raises(docker.DockerScenarioError, match="could not remove"):
        session.cleanup()

    assert session.cleaned is False


@pytest.mark.parametrize("name", ["../secret", "/absolute", "dir/.git/config"])
def test_workspace_export_rejects_unsafe_paths(name: str) -> None:
    with pytest.raises(docker.DockerScenarioError, match="unsafe"):
        docker._safe_archive_path(name)


def test_active_same_owner_and_other_owner_resources_are_not_swept(monkeypatch) -> None:
    owner = "owner-test"
    active = {
        "Config": {
            "Labels": {
                docker.OWNER_LABEL: owner,
                docker.PARENT_PID_LABEL: "42",
                docker.CREATED_AT_LABEL: str(int(time.time())),
            }
        },
        "State": {"Running": True},
    }
    other = copy.deepcopy(active)
    other["Config"]["Labels"][docker.OWNER_LABEL] = "other-owner"
    monkeypatch.setattr(docker, "_pid_alive", lambda _pid: True)

    assert docker._container_is_stale(active, owner) is False
    assert docker._container_is_stale(other, owner) is False


def test_preparation_uses_named_bounded_no_network_container(tmp_path: Path, monkeypatch) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    captured: dict = {}
    docker_commands: list[list[str]] = []
    monkeypatch.setattr(
        docker, "ensure_images", lambda *_args, **_kwargs: ("scenario:test", "broker:test")
    )
    monkeypatch.setattr(docker, "_image_id", lambda *_args, **_kwargs: "sha256:scenario")
    monkeypatch.setattr(
        docker, "_image_claude_version", lambda *_args, **_kwargs: "2.1.207 (Claude Code)"
    )
    monkeypatch.setattr(docker, "_remove_container", lambda *_args, **_kwargs: True)

    def bounded(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _completed()

    monkeypatch.setattr(docker.sproc, "run_bounded_capture", bounded)

    def runner(command, **_kwargs):
        docker_commands.append(command)
        return _completed()

    docker.run_preparation_command(
        config=copy.deepcopy(mh.DEFAULTS),
        main_root=tmp_path,
        worktree_dir=worktree,
        source_commit="a" * 40,
        prepare_git_snapshot=_prepare_git_snapshot,
        raw_command=["python3", "-c", "print('ok')"],
        timeout_seconds=5,
        runner=runner,
    )

    start_command = next(command for command in docker_commands if command[:2] == ["docker", "run"])
    exec_commands = [command for command in docker_commands if command[:2] == ["docker", "exec"]]
    rendered = "\n".join(start_command)
    assert "--network\nnone" in rendered
    assert f"src={worktree.resolve()},dst=/input,readonly" in rendered
    assert "dst=/runtime/git-snapshot,readonly" in rendered
    assert "dst=/runtime/bin,readonly" in rendered
    assert "dst=/workspace/.git,readonly" in rendered
    assert "GIT_DIR=/runtime/git-snapshot" in rendered
    assert "GIT_WORK_TREE=/workspace" in rendered
    assert f"src={worktree.resolve()},dst=/workspace" not in rendered
    assert "--name" in start_command
    assert captured["kwargs"]["max_output_bytes"] == 10_000_000
    assert exec_commands[0][:2] == ["docker", "exec"]
    assert captured["command"][:2] == ["docker", "exec"]
    assert callable(captured["kwargs"]["success_callback"])
    assert captured["kwargs"]["cleanup_args"][:3] == ["docker", "rm", "-f"]
    assert start_command[-6:] == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "365s",
        "/usr/bin/sleep",
        "infinity",
    ]


def test_preparation_preserves_primary_error_when_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(
        docker, "ensure_images", lambda *_args, **_kwargs: ("scenario:test", "broker:test")
    )
    monkeypatch.setattr(docker, "_image_id", lambda *_args, **_kwargs: "sha256:scenario")
    monkeypatch.setattr(
        docker, "_image_claude_version", lambda *_args, **_kwargs: "2.1.207 (Claude Code)"
    )
    monkeypatch.setattr(docker, "_remove_container", lambda *_args, **_kwargs: False)

    def fail_checked(*_args, **_kwargs) -> None:
        raise docker.DockerScenarioError("detailed preparation start failure")

    monkeypatch.setattr(docker, "_checked", fail_checked)

    with pytest.raises(docker.DockerScenarioError, match="detailed preparation start failure"):
        docker.run_preparation_command(
            config=copy.deepcopy(mh.DEFAULTS),
            main_root=tmp_path,
            worktree_dir=worktree,
            source_commit="a" * 40,
            prepare_git_snapshot=_prepare_git_snapshot,
            raw_command=["python3", "-c", "print('ok')"],
            timeout_seconds=5,
        )

    assert "preserving the in-flight preparation error" in caplog.text


def test_container_lifetime_matches_broker_absolute_bound() -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["timeout_ms_default"] = 2234
    config["evaluate"]["isolation"]["broker"]["idle_timeout_sec"] = 7

    resources = docker.profile.resources_config(config)
    broker_env = docker.profile.broker_env(config, "run-token", 8787)

    assert resources["max_lifetime_sec"] == 70
    assert broker_env["MH_BROKER_MAX_LIFETIME_SEC"] == "70"


def test_prebuilt_images_require_digest_and_accept_multiarch_reference() -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["auto_build_images"] = False
    config["evaluate"]["isolation"]["image"] = "scenario@sha256:" + "1" * 64
    config["evaluate"]["isolation"]["broker"]["image"] = "broker@sha256:" + "2" * 64

    images = docker.dcli.ensure_images(
        config,
        runner=lambda *_args, **_kwargs: _completed(stdout="sha256:" + "3" * 64),
    )

    assert images[0].endswith("1" * 64)
    config["evaluate"]["isolation"]["image"] = "scenario:mutable"
    with pytest.raises(docker.dcli.DockerCliError, match="immutable"):
        docker.dcli.ensure_images(config, runner=lambda *_args, **_kwargs: _completed())


def test_image_pin_semver_versions_produce_validated_build_args(monkeypatch) -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return _completed(stdout="sha256:" + "3" * 64)

    for version_pin in [
        "2.1.207",
        "2.1.207 (Claude Code)",
        "2.1.207-beta.1",
    ]:
        monkeypatch.setattr(docker.dcli, "_IMAGE_CACHE", docker.dcli.runtime.ImageCache())
        config["evaluate"]["isolation"]["image_pin"] = version_pin
        docker.dcli.ensure_images(config, runner=runner)

    scenario_builds = [
        command
        for command in commands
        if command[:2] == ["docker", "build"]
        and command[command.index("-t") + 1] == docker.dcli.DEFAULT_SCENARIO_IMAGE
    ]
    assert [command[command.index("--build-arg") + 1] for command in scenario_builds] == [
        "CLAUDE_CODE_VERSION=2.1.207",
        "CLAUDE_CODE_VERSION=2.1.207",
        "CLAUDE_CODE_VERSION=2.1.207-beta.1",
    ]


def test_image_pin_rejects_invalid_versions_before_build() -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    runner_calls = []
    injection_pin = "".join(['2.1.207";', "cu", "rl${IFS}evil|", "s", 'h;"'])

    def runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        raise AssertionError("invalid image_pin reached the Docker build command")

    for version_pin in [injection_pin, "2.1", "v2.1.207", "2.1.207.9"]:
        config["evaluate"]["isolation"]["image_pin"] = version_pin
        with pytest.raises(docker.dcli.DockerCliError, match="semver"):
            docker.dcli.ensure_images(config, runner=runner)

    config["evaluate"]["isolation"]["image_pin"] = ""
    with pytest.raises(docker.dcli.DockerCliError, match="Claude CLI version"):
        docker.dcli.ensure_images(config, runner=runner)

    assert runner_calls == []
