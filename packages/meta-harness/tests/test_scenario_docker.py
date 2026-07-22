"""Docker scenario boundary command and lifecycle tests (EV-46, EV-47)."""

from __future__ import annotations

import copy
import json
import os
import re
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


def _allowlist_probe_stdout(
    status: str, *, server: str = "meta-harness-broker Python/3.99.0", message: str = ""
) -> str:
    """Build a probe stdout line matching `_ALLOWLIST_SMOKE_SCRIPT`'s output contract."""
    return docker._ALLOWLIST_SMOKE_FIELD_SEP.join([status, server, message])


_VALID_ALLOWLIST_REJECTION_STDOUT = _allowlist_probe_stdout(
    "400", message=docker._ALLOWLIST_SMOKE_REJECTION_MESSAGE
)


def _run_smoke_stub(_broker_session, command, **_kwargs):
    """Generic `_run_smoke_container` fake: reports a genuine broker rejection for the
    negative broker model allowlist probe (Issue #261 PR2 review) and a passing claude
    result for every other capability smoke check."""
    if command[:2] == ["/usr/bin/python3", "-c"]:
        return _completed(stdout=_VALID_ALLOWLIST_REJECTION_STDOUT)
    return _completed(stdout='{"type":"result"}')


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
        max_output_tokens=4096,
    )
    rendered = "\n".join(command)

    assert "--network\nmh-run-test-internal" in rendered
    assert "ANTHROPIC_BASE_URL=http://mh-broker:8787" in command
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS=4096" in command
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT=1" in command
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


def test_judge_command_applies_configured_output_token_cap(tmp_path: Path) -> None:
    launch = _launch(tmp_path)

    command = docker.profile.build_judge_command(
        launch,
        ["claude", "-p", "grade", "--bare"],
        container_name="mh-run-judge",
        max_output_tokens=8192,
    )

    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192" in command


def test_resolve_max_output_tokens_default_treats_null_as_default() -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    config["scenario_run"]["max_output_tokens_default"] = None
    assert docker.profile.resolve_max_output_tokens_default(config) == 4096

    config["scenario_run"]["max_output_tokens_default"] = 8192
    assert docker.profile.resolve_max_output_tokens_default(config) == 8192

    config["scenario_run"] = None
    assert docker.profile.resolve_max_output_tokens_default(config) == 4096


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


def test_capability_gate_fails_closed_before_docker_when_repin_mismatches_allowlist() -> None:
    """Issue #261 PR2 review round 2: a repinned model missing from model_allowlist
    must fail closed before any Docker/broker work starts (pure config validation),
    not just at the negative-probe smoke check deep inside the gate."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["model"] = "claude-repinned-expensive-model"
    # Round 4: evaluate.model/judge.model must match, or the equal-model check fires
    # first -- keep judge.model in lockstep so this test exercises the menu-mismatch
    # branch specifically.
    config["judge"]["model"] = "claude-repinned-expensive-model"

    def _runner_must_not_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Docker must not be touched when config validation fails closed")

    result = docker.check_docker_capabilities(config, runner=_runner_must_not_run)

    assert result.ok is False
    assert "claude-repinned-expensive-model" in (result.reason or "")
    assert "evaluate.isolation.broker.model_allowlist" in (result.reason or "")
    assert "pricing_upper_bound_usd_per_million" in (result.reason or "")


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


def test_bare_semver_image_pin_passes_capability_check(tmp_path: Path, monkeypatch) -> None:
    """A bare semver pin (e.g. "2.1.207") must not be rejected by the
    capability check just because the actual `claude --version` output is
    the full form (e.g. "2.1.207 (Claude Code)")."""
    session = _broker(tmp_path)
    session.cleaned = True
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["image_pin"] = "2.1.207"
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
    monkeypatch.setattr(docker, "_run_smoke_container", _run_smoke_stub)

    result = docker.check_docker_capabilities(config, main_root=tmp_path, runner=session.runner)

    assert result.ok is True
    assert result.version_pin_match is True


def test_full_image_pin_rejects_matching_version_with_unexpected_suffix(
    tmp_path: Path, monkeypatch
) -> None:
    """A full-format pin (the default "2.1.207 (Claude Code)") must keep the
    exact Docker capability contract: an image reporting the same bare
    version but an unexpected wrapper/suffix must still fail capability
    checks, not pass via bare-token comparison."""
    session = _broker(tmp_path)
    session.cleaned = True
    monkeypatch.setattr(docker.dcli, "docker_daemon_available", lambda **_kwargs: True)
    monkeypatch.setattr(docker, "sweep_stale_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        docker, "docker_broker_session", lambda *_args, **_kwargs: docker._BrokerContext(session)
    )
    monkeypatch.setattr(
        docker,
        "_image_claude_version",
        lambda *_args, **_kwargs: "2.1.207 (unexpected wrapper)",
    )
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


@pytest.mark.parametrize(
    ("actual", "pin", "expected"),
    [
        ("2.1.207 (Claude Code)", "2.1.207", True),
        ("2.1.207-beta.1 (Claude Code)", "2.1.207-beta.1", True),
        ("9.9.9 (Claude Code)", "2.1.207", False),
        ("2.1.207 (Claude Code)", "2.1.207 (Claude Code)", True),
        ("2.1.207 (unexpected wrapper)", "2.1.207 (Claude Code)", False),
        (None, "2.1.207", False),
        (None, "2.1.207 (Claude Code)", False),
    ],
    ids=[
        "bare-pin-matches-full-output",
        "bare-pin-with-suffix-matches-full-output",
        "bare-pin-rejects-mismatch",
        "full-pin-exact-match",
        "full-pin-rejects-unexpected-suffix",
        "missing-actual-rejects-bare-pin",
        "missing-actual-rejects-full-pin",
    ],
)
def test_version_matches_uses_token_compare_only_for_bare_pins(
    actual: str | None, pin: str, expected: bool
) -> None:
    """EV-60/EV-114: Bare semver pins compare via leading-token match; any
    other pin format (including the default full form) must match exactly."""
    assert docker._version_matches(actual, pin) is expected


def test_capability_smoke_uses_configured_evaluate_model(tmp_path: Path, monkeypatch) -> None:
    session = _broker(tmp_path)
    session.cleaned = True
    commands: list[list[str]] = []
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["model"] = "claude-custom-model"
    # Round 4: evaluate.model/judge.model must match.
    config["judge"]["model"] = "claude-custom-model"
    # Issue #261 PR2 review round 2: repinning evaluate.model must be admitted
    # explicitly by the configured allowlist (no more auto-union).
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = [
        "claude-custom-model",
        "claude-sonnet-5",
    ]
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
        return _run_smoke_stub(_broker_session, command, **_kwargs)

    monkeypatch.setattr(docker, "_run_smoke_container", run_smoke)

    result = docker.check_docker_capabilities(config, main_root=tmp_path, runner=session.runner)

    assert result.ok is True
    # stream_json, max_budget_usd, bare judge, allowlist probe (/v1/messages),
    # allowlist probe (/v1/messages/count_tokens).
    assert len(commands) == 5
    for command in commands[:2]:
        model_index = command.index("--model")
        assert command[model_index + 1] == "claude-custom-model"


def test_bare_judge_smoke_uses_pinned_model(tmp_path: Path, monkeypatch) -> None:
    """Issue #261 PR2: judge.model must be pinned on the claude-bare capability smoke
    too. Since round 4 requires evaluate.model == judge.model, this necessarily uses
    the same value as evaluate.model, but the wiring (judge_model_args, separate from
    the evaluate-derived model_args) is still exercised and must not double up
    --model flags."""
    session = _broker(tmp_path)
    session.cleaned = True
    commands: list[list[str]] = []
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["model"] = "claude-custom-model"
    config["judge"]["model"] = "claude-custom-model"
    # Issue #261 PR2 review round 2: repinned model must be explicitly admitted by
    # the configured allowlist (no more auto-union).
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = ["claude-custom-model"]
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
        return _run_smoke_stub(_broker_session, command, **_kwargs)

    monkeypatch.setattr(docker, "_run_smoke_container", run_smoke)

    result = docker.check_docker_capabilities(config, main_root=tmp_path, runner=session.runner)

    assert result.ok is True
    # stream_json, max_budget_usd, bare judge, allowlist probe (/v1/messages),
    # allowlist probe (/v1/messages/count_tokens).
    assert len(commands) == 5
    bare_command = commands[2]
    model_index = bare_command.index("--model")
    assert bare_command[model_index + 1] == "claude-custom-model"
    # evaluate.model must not leak into the bare judge command's --model flag.
    assert bare_command.count("--model") == 1


def _run_capability_gate_with_allowlist_probe_response(
    tmp_path: Path, monkeypatch, *, probe_stdout: str, config: dict | None = None
) -> tuple[docker.DockerCapabilityResult, list[list[str]]]:
    session = _broker(tmp_path)
    session.cleaned = True
    commands: list[list[str]] = []
    effective_config = config if config is not None else copy.deepcopy(mh.DEFAULTS)
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
        if command[:2] == ["/usr/bin/python3", "-c"]:
            return _completed(stdout=probe_stdout)
        return _completed(stdout='{"type":"result"}')

    monkeypatch.setattr(docker, "_run_smoke_container", run_smoke)

    result = docker.check_docker_capabilities(
        effective_config, main_root=tmp_path, runner=session.runner
    )
    return result, commands


def test_capability_gate_passes_when_broker_rejects_disallowed_model(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue #261 PR2 review: negative allowlist smoke proves the broker actually
    enforces model_allowlist (a broker image that ignores it would silently pass
    the capability gate otherwise, since normal smoke checks only send allowed
    models)."""
    result, commands = _run_capability_gate_with_allowlist_probe_response(
        tmp_path, monkeypatch, probe_stdout=_VALID_ALLOWLIST_REJECTION_STDOUT
    )

    assert result.ok is True
    assert result.checks["broker_model_allowlist"] is True
    # Issue #261 PR2 review round 3: count_tokens must be probed too, since it also
    # spends input-token accounting and was previously left unchecked.
    assert result.checks["broker_model_allowlist_count_tokens"] is True
    messages_probe, count_tokens_probe = commands[-2], commands[-1]
    assert messages_probe[:2] == ["/usr/bin/python3", "-c"]
    assert messages_probe[3] == docker._ALLOWLIST_SMOKE_DISALLOWED_MODEL
    assert messages_probe[4] == docker._ALLOWLIST_SMOKE_PATH_MESSAGES
    assert count_tokens_probe[3] == docker._ALLOWLIST_SMOKE_DISALLOWED_MODEL
    assert count_tokens_probe[4] == docker._ALLOWLIST_SMOKE_PATH_COUNT_TOKENS


def test_capability_gate_fails_when_broker_accepts_disallowed_model(
    tmp_path: Path, monkeypatch
) -> None:
    """A broker that answers 200 (or anything but 400) to the disallowed-model probe
    is not actually enforcing the allowlist, so the gate must fail closed."""
    result, _commands = _run_capability_gate_with_allowlist_probe_response(
        tmp_path, monkeypatch, probe_stdout=_allowlist_probe_stdout("200")
    )

    assert result.ok is False
    assert result.checks["broker_model_allowlist"] is False
    assert result.checks["broker_model_allowlist_count_tokens"] is False


@pytest.mark.parametrize(
    "probe_stdout",
    [
        # Round-2 P1 fix: a bare 400 alone (e.g. from an allowlist-blind broker that
        # rejected for an unrelated reason, or a transparent upstream proxy) must NOT
        # be mistaken for allowlist enforcement.
        pytest.param(
            _allowlist_probe_stdout("400", message="some unrelated upstream 400"),
            id="right_status_wrong_message",
        ),
        pytest.param(
            _allowlist_probe_stdout(
                "400",
                server="not-the-broker",
                message=docker._ALLOWLIST_SMOKE_REJECTION_MESSAGE,
            ),
            id="right_status_wrong_server_header",
        ),
    ],
)
def test_capability_gate_fails_when_400_lacks_broker_rejection_signal(
    tmp_path: Path, monkeypatch, probe_stdout: str
) -> None:
    """A generic/foreign 400 must not satisfy the check: only the broker's own
    allowlist-rejection message plus its Server header identity count as proof."""
    result, _commands = _run_capability_gate_with_allowlist_probe_response(
        tmp_path, monkeypatch, probe_stdout=probe_stdout
    )

    assert result.ok is False
    assert result.checks["broker_model_allowlist"] is False


def test_capability_gate_opens_a_dedicated_broker_session_for_the_allowlist_probe(
    tmp_path: Path, monkeypatch
) -> None:
    """Local adversarial review High (round 5): the existing tests replace
    `docker_broker_session` with a single scope-agnostic mock, so a regression that
    reuses the main session for the negative probe (undoing the round 4 budget
    isolation fix) would go undetected. Capture the scope argument on every call and
    assert the gate opens exactly two sessions: the main "capability" session for
    the legitimate smoke checks, and a dedicated "capability-allowlist-probe"
    session for the negative probes."""
    session = _broker(tmp_path)
    session.cleaned = True
    scopes: list[str] = []
    configs: list[dict] = []

    def fake_docker_broker_session(config: dict, scope: str, **_kwargs: object):
        scopes.append(scope)
        configs.append(config)
        return docker._BrokerContext(session)

    monkeypatch.setattr(docker, "docker_broker_session", fake_docker_broker_session)
    monkeypatch.setattr(docker.dcli, "docker_daemon_available", lambda **_kwargs: True)
    monkeypatch.setattr(docker, "sweep_stale_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        docker,
        "_image_claude_version",
        lambda *_args, **_kwargs: "2.1.207 (Claude Code)",
    )
    monkeypatch.setattr(docker, "_run_smoke_container", _run_smoke_stub)

    result = docker.check_docker_capabilities(
        copy.deepcopy(mh.DEFAULTS), main_root=tmp_path, runner=session.runner
    )

    assert result.ok is True
    assert scopes == ["capability", "capability-allowlist-probe"]
    main_config, probe_config = configs
    assert (
        main_config["evaluate"]["isolation"]["broker"]["max_requests"]
        == mh.DEFAULTS["evaluate"]["isolation"]["broker"]["max_requests"]
    )
    assert (
        main_config["scenario_run"]["max_budget_usd_default"]
        == (mh.DEFAULTS["scenario_run"]["max_budget_usd_default"])
    )


def test_capability_gate_pins_a_fixed_safe_budget_for_the_allowlist_probe_session(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue #261 PR2 review round 6 (High): the negative-probe session must not
    inherit the user's configured max_requests/budget -- a tight `max_requests: 1`
    would let the first probe consume the only slot, rejecting the second
    (count_tokens) probe via the request envelope before it ever reaches the
    allowlist check. The probe session's env must carry fixed, safe values
    independently of the user's (here, deliberately tight) configuration."""
    session = _broker(tmp_path)
    session.cleaned = True
    configs: list[dict] = []

    def fake_docker_broker_session(config: dict, scope: str, **_kwargs: object):
        configs.append(config)
        return docker._BrokerContext(session)

    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["broker"]["max_requests"] = 1
    config["scenario_run"]["max_budget_usd_default"] = 0.01

    monkeypatch.setattr(docker, "docker_broker_session", fake_docker_broker_session)
    monkeypatch.setattr(docker.dcli, "docker_daemon_available", lambda **_kwargs: True)
    monkeypatch.setattr(docker, "sweep_stale_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        docker,
        "_image_claude_version",
        lambda *_args, **_kwargs: "2.1.207 (Claude Code)",
    )
    monkeypatch.setattr(docker, "_run_smoke_container", _run_smoke_stub)

    result = docker.check_docker_capabilities(config, main_root=tmp_path, runner=session.runner)

    assert result.ok is True
    main_config, probe_config = configs
    # The main session keeps the user's (tight) configured values.
    assert main_config["evaluate"]["isolation"]["broker"]["max_requests"] == 1
    assert main_config["scenario_run"]["max_budget_usd_default"] == 0.01
    # The probe session's env is pinned to fixed, safe values instead.
    probe_env = docker.profile.broker_env(probe_config, "probe-run-token", 8787)
    assert probe_env["DR_BROKER_MAX_REQUESTS"] == str(docker.profile.ALLOWLIST_PROBE_MAX_REQUESTS)
    assert probe_env["DR_BROKER_BUDGET_USD"] == str(docker.profile.ALLOWLIST_PROBE_BUDGET_USD)
    assert probe_config["evaluate"]["isolation"]["broker"]["max_requests"] == (
        docker.profile.ALLOWLIST_PROBE_MAX_REQUESTS
    )
    assert probe_config["scenario_run"]["max_budget_usd_default"] == (
        docker.profile.ALLOWLIST_PROBE_BUDGET_USD
    )
    # Everything else in the probe config must be untouched (e.g. the pinned model).
    assert probe_config["evaluate"]["model"] == config["evaluate"]["model"]


def test_capability_gate_fails_closed_before_docker_when_judge_model_unpinned() -> None:
    """Issue #261 PR2 review round 3: leaving judge.model (or evaluate.model) at
    `null` is no longer treated as "no restriction" -- with pricing DEFAULTS now
    calibrated for the pinned Sonnet tier, an unpinned model would run at the
    CLI/session-default price, silently under-counting real cost. The gate must
    fail closed before touching Docker at all, not skip the negative probe."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["judge"]["model"] = None

    def _runner_must_not_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Docker must not be touched when config validation fails closed")

    result = docker.check_docker_capabilities(config, runner=_runner_must_not_run)

    assert result.ok is False
    assert "judge.model" in (result.reason or "")
    assert "not pinned" in (result.reason or "")


@pytest.mark.parametrize(
    ("configured_max_output_tokens", "expected_max_output_tokens"),
    [
        (None, mh.DEFAULTS["scenario_run"]["max_output_tokens_default"]),
        (8192, 8192),
    ],
    ids=["default", "configured"],
)
def test_capability_smoke_uses_configured_max_output_tokens(
    tmp_path: Path,
    monkeypatch,
    configured_max_output_tokens: int | None,
    expected_max_output_tokens: int,
) -> None:
    session = _broker(tmp_path)
    session.cleaned = True
    docker_run_commands: list[list[str]] = []
    config = copy.deepcopy(mh.DEFAULTS)
    if configured_max_output_tokens is not None:
        config["scenario_run"]["max_output_tokens_default"] = configured_max_output_tokens
    original_run_smoke_container = docker._run_smoke_container
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

    def run_smoke(_broker_session, command, *, max_output_tokens, **_kwargs):
        is_allowlist_probe = command[:2] == ["/usr/bin/python3", "-c"]

        def capture_runner(argv, **_runner_kwargs):
            if argv[:2] == ["docker", "run"]:
                docker_run_commands.append(argv)
                stdout = (
                    _VALID_ALLOWLIST_REJECTION_STDOUT if is_allowlist_probe else '{"type":"result"}'
                )
                return _completed(stdout=stdout)
            return _completed()

        return original_run_smoke_container(
            _broker_session,
            command,
            max_output_tokens=max_output_tokens,
            runner=capture_runner,
        )

    monkeypatch.setattr(docker, "_run_smoke_container", run_smoke)

    result = docker.check_docker_capabilities(config, main_root=tmp_path, runner=session.runner)

    assert result.ok is True
    # stream_json, max_budget_usd, bare judge, allowlist probe (/v1/messages),
    # allowlist probe (/v1/messages/count_tokens).
    assert len(docker_run_commands) == 5
    expected_env = f"CLAUDE_CODE_MAX_OUTPUT_TOKENS={expected_max_output_tokens}"
    for command in docker_run_commands:
        env_values = [
            command[index + 1] for index, arg in enumerate(command[:-1]) if arg == "--env"
        ]
        assert expected_env in env_values
        assert "CLAUDE_CODE_DISABLE_1M_CONTEXT=1" in env_values


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


def test_broker_env_sends_generic_and_legacy_contracts() -> None:
    broker_env = docker.profile.broker_env(copy.deepcopy(mh.DEFAULTS), "run-token", 8787)

    assert broker_env["DR_BROKER_NAMESPACE"] == "meta-harness"
    for generic_name, legacy_name in (
        ("DR_BROKER_RUN_TOKEN", "MH_BROKER_RUN_TOKEN"),
        ("DR_BROKER_PORT", "MH_BROKER_PORT"),
        ("DR_BROKER_BUDGET_USD", "MH_BROKER_BUDGET_USD"),
        ("DR_BROKER_IDLE_TIMEOUT_SEC", "MH_BROKER_IDLE_TIMEOUT_SEC"),
        ("DR_BROKER_MAX_LIFETIME_SEC", "MH_BROKER_MAX_LIFETIME_SEC"),
        ("DR_BROKER_STARTUP_TIMEOUT_SEC", "MH_BROKER_STARTUP_TIMEOUT_SEC"),
        ("DR_BROKER_MAX_REQUESTS", "MH_BROKER_MAX_REQUESTS"),
        ("DR_BROKER_MAX_TOTAL_TOKENS", "MH_BROKER_MAX_TOTAL_TOKENS"),
        ("DR_BROKER_MAX_UPSTREAM_BYTES", "MH_BROKER_MAX_UPSTREAM_BYTES"),
        ("DR_PRICE_INPUT", "MH_PRICE_INPUT"),
        ("DR_PRICE_OUTPUT", "MH_PRICE_OUTPUT"),
        ("DR_PRICE_CACHE_CREATION", "MH_PRICE_CACHE_CREATION"),
        ("DR_PRICE_CACHE_READ", "MH_PRICE_CACHE_READ"),
        ("DR_BROKER_MODEL_ALLOWLIST", "MH_BROKER_MODEL_ALLOWLIST"),
    ):
        assert broker_env[generic_name] == broker_env[legacy_name]


@pytest.mark.parametrize(
    ("null_key", "generic_env_key", "legacy_env_key"),
    [
        ("input", "DR_PRICE_INPUT", "MH_PRICE_INPUT"),
        ("output", "DR_PRICE_OUTPUT", "MH_PRICE_OUTPUT"),
        ("cache_creation", "DR_PRICE_CACHE_CREATION", "MH_PRICE_CACHE_CREATION"),
        ("cache_read", "DR_PRICE_CACHE_READ", "MH_PRICE_CACHE_READ"),
    ],
)
def test_broker_env_falls_back_to_defaults_when_pricing_key_is_present_but_null(
    null_key: str, generic_env_key: str, legacy_env_key: str
) -> None:
    """Local adversarial review High (round 5): a `.local.yaml` override that nulls
    out a single pricing key (e.g. `pricing_upper_bound_usd_per_million.input: null`)
    leaves the key *present* with value `None` after config merge. `dict.get(key,
    default)` only falls back when the key is *absent*, so this used to put the
    literal string "None" into the broker env, crashing the broker's `_float_env`
    (`float("None")`) uncaught. The fallback must be an explicit None-check."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["broker"]["pricing_upper_bound_usd_per_million"][null_key] = (
        None
    )

    broker_env = docker.profile.broker_env(config, "run-token", 8787)

    default_pricing = mh.DEFAULTS["evaluate"]["isolation"]["broker"][
        "pricing_upper_bound_usd_per_million"
    ]
    expected = str(default_pricing[null_key])
    assert broker_env[generic_env_key] == expected
    assert broker_env[legacy_env_key] == expected
    # Must never leak the Python None-as-string sentinel to the broker's float parser.
    assert broker_env[generic_env_key] != "None"


def test_broker_env_sends_configured_model_allowlist() -> None:
    """Issue #261 PR2: config-driven model allowlist is joined and forwarded to the broker."""
    broker_env = docker.profile.broker_env(copy.deepcopy(mh.DEFAULTS), "run-token", 8787)

    assert broker_env["DR_BROKER_MODEL_ALLOWLIST"] == "claude-sonnet-5"
    assert broker_env["MH_BROKER_MODEL_ALLOWLIST"] == "claude-sonnet-5"


def test_broker_env_raises_fail_closed_when_both_models_unpinned() -> None:
    """Issue #261 PR2 review round 3: leaving both evaluate.model and judge.model at
    `null` no longer omits the allowlist restriction (retired backward-compat path).
    With pricing DEFAULTS now calibrated for a pinned Sonnet tier, an unpinned model
    would run at the CLI/session-default price -- silently under-counting cost -- so
    this must fail closed instead."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = []
    config["evaluate"]["model"] = None
    config["judge"]["model"] = None

    with pytest.raises(docker.profile.DockerProfileError) as excinfo:
        docker.profile.broker_env(config, "run-token", 8787)

    message = str(excinfo.value)
    assert "evaluate.model" in message
    assert "judge.model" in message
    assert "not pinned" in message


@pytest.mark.parametrize(
    "unset_key",
    ["evaluate", "judge"],
    ids=["evaluate_model_unpinned", "judge_model_unpinned"],
)
def test_broker_env_raises_fail_closed_when_either_model_is_unpinned(unset_key: str) -> None:
    """Issue #261 PR2 review round 3: if only one of evaluate.model / judge.model is
    pinned (e.g. a project on the old `model: null` default that upgraded config but
    has not pinned both models yet), this must fail closed with an actionable
    message rather than silently omit the allowlist restriction."""
    config = copy.deepcopy(mh.DEFAULTS)
    config[unset_key]["model"] = None
    field = f"{unset_key}.model"

    with pytest.raises(docker.profile.DockerProfileError) as excinfo:
        docker.profile.broker_env(config, "run-token", 8787)

    message = str(excinfo.value)
    assert field in message
    assert "not pinned" in message


def test_broker_env_raises_fail_closed_when_repinned_model_mismatches_allowlist() -> None:
    """Issue #261 PR2 review round 2: repinning evaluate.model (or judge.model) without
    also updating `model_allowlist` must fail closed with an actionable error, not
    silently auto-admit the pricier model (a prior revision auto-unioned the pinned
    model into the allowlist, which defeated the whole point of the guard: repinning
    to a pricier model would be admitted without the operator also updating
    pricing_upper_bound_usd_per_million, under-counting real cost)."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["model"] = "claude-project-override-model"
    # Round 4: evaluate.model/judge.model must match to reach the menu-mismatch
    # branch this test targets (otherwise the equal-model check fires first).
    config["judge"]["model"] = "claude-project-override-model"
    # model_allowlist intentionally left at its default (["claude-sonnet-5"]): the
    # repinned model is not covered.

    with pytest.raises(docker.profile.DockerProfileError) as excinfo:
        docker.profile.broker_env(config, "run-token", 8787)

    message = str(excinfo.value)
    assert "claude-project-override-model" in message
    assert "evaluate.isolation.broker.model_allowlist" in message
    assert "pricing_upper_bound_usd_per_million" in message


def test_broker_env_passes_when_repinned_model_matches_allowlist() -> None:
    """A repin that the operator also reflects in `model_allowlist` (and, implicitly,
    is expected to reflect in pricing) is admitted as configured -- no auto-union.
    Only the pinned model is wired, never the full configured menu (round 4)."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["model"] = "claude-project-override-model"
    config["judge"]["model"] = "claude-project-override-model"
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = [
        "claude-project-override-model",
        "claude-sonnet-5",
    ]

    broker_env = docker.profile.broker_env(config, "run-token", 8787)

    allowed = set(broker_env["DR_BROKER_MODEL_ALLOWLIST"].split(","))
    assert allowed == {"claude-project-override-model"}
    assert broker_env["DR_BROKER_MODEL_ALLOWLIST"] == broker_env["MH_BROKER_MODEL_ALLOWLIST"]


def test_broker_env_raises_when_model_allowlist_is_a_bare_string() -> None:
    """CodeRabbit High (PR #265): a bare string `model_allowlist` (a common YAML typo
    for a single-item list) must not be silently iterated character-by-character into
    a set of single-character "allowed" models."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = "claude-sonnet-5"

    with pytest.raises(docker.profile.DockerProfileError) as excinfo:
        docker.profile.broker_env(config, "run-token", 8787)

    assert "model_allowlist must be a list" in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_allowlist",
    [
        pytest.param(["claude-sonnet-5", "claude-sonnet-5,claude-opus-4-8"], id="comma"),
        pytest.param(["claude-sonnet-5", "claude-\x00-injected"], id="control_char"),
        pytest.param(["claude-sonnet-5", "   "], id="blank"),
        pytest.param(["claude-sonnet-5", 5], id="non_string_element"),
    ],
)
def test_broker_env_raises_when_allowlist_element_is_invalid(bad_allowlist: list) -> None:
    """CodeRabbit High (PR #265): a comma inside one allowlist element would silently
    expand into multiple entries once the allowlist is CSV-joined for the broker env,
    so it (and other malformed elements) must be rejected outright."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = bad_allowlist

    with pytest.raises(docker.profile.DockerProfileError):
        docker.profile.broker_env(config, "run-token", 8787)


def test_broker_env_accepts_a_clean_model_allowlist() -> None:
    """A normal list of clean model slugs must pass through unchanged (no false
    positives from the new type/element validation). The menu may list more than
    the pinned model, but only the pinned model is wired (round 4)."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = [
        "claude-sonnet-5",
        "claude-opus-4-8",
    ]
    config["evaluate"]["model"] = "claude-sonnet-5"
    config["judge"]["model"] = "claude-sonnet-5"

    broker_env = docker.profile.broker_env(config, "run-token", 8787)

    allowed = set(broker_env["DR_BROKER_MODEL_ALLOWLIST"].split(","))
    assert allowed == {"claude-sonnet-5"}


def test_broker_env_raises_when_evaluate_and_judge_models_differ() -> None:
    """Issue #261 PR2 review round 4: the broker pricing table has exactly one
    price point per run. Pinning evaluate.model and judge.model to different
    models (even if both are listed in model_allowlist) must fail closed --
    otherwise the cheaper of the two would silently run under the other's price
    ceiling, under-counting whichever model is actually more expensive."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["model"] = "claude-cheap-model"
    config["judge"]["model"] = "claude-expensive-model"
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = [
        "claude-cheap-model",
        "claude-expensive-model",
    ]

    with pytest.raises(docker.profile.DockerProfileError) as excinfo:
        docker.profile.broker_env(config, "run-token", 8787)

    message = str(excinfo.value)
    assert "claude-cheap-model" in message
    assert "claude-expensive-model" in message
    assert "pricing_upper_bound_usd_per_million" in message


def test_broker_env_allows_evaluate_and_judge_when_pinned_to_the_same_model() -> None:
    """The equal-model requirement (round 4) does not reject valid single-model
    configs, even when model_allowlist carries additional unrelated menu entries."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["model"] = "claude-sonnet-5"
    config["judge"]["model"] = "claude-sonnet-5"
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = [
        "claude-sonnet-5",
        "claude-opus-4-8-experimental",
    ]

    broker_env = docker.profile.broker_env(config, "run-token", 8787)

    allowed = set(broker_env["DR_BROKER_MODEL_ALLOWLIST"].split(","))
    assert allowed == {"claude-sonnet-5"}


def test_prebuilt_images_require_digest_and_accept_multiarch_reference(
    tmp_path: Path,
) -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["auto_build_images"] = False
    config["evaluate"]["isolation"]["image"] = "scenario@sha256:" + "1" * 64
    config["evaluate"]["isolation"]["broker"]["image"] = "broker@sha256:" + "2" * 64
    # This test is about the immutable-digest requirement, not image_pin
    # verification (covered separately above); disable the pin check so the
    # fake runner's synthetic `claude --version` stdout doesn't interfere.
    config["evaluate"]["isolation"]["image_pin"] = None

    images = docker.dcli.ensure_images(
        config,
        runner=lambda *_args, **_kwargs: _completed(stdout="sha256:" + "3" * 64),
        main_root=tmp_path,
    )

    assert images[0].endswith("1" * 64)
    config["evaluate"]["isolation"]["image"] = "scenario:mutable"
    with pytest.raises(docker.dcli.DockerCliError, match="immutable"):
        docker.dcli.ensure_images(
            config,
            runner=lambda *_args, **_kwargs: _completed(),
            main_root=tmp_path,
        )


def test_ensure_images_normalizes_main_root_resolution_error() -> None:
    """`resolve_main_root` failures (e.g. a non-absolute `storage.root`) must
    surface as `DockerCliError` like every other `ensure_images` failure, not
    leak the internal `MetaHarnessRootError` type to callers (Issue #250
    review)."""
    config = copy.deepcopy(mh.DEFAULTS)
    config["storage"]["root"] = "relative/not-absolute"

    def runner(*_args, **_kwargs):
        raise AssertionError("ensure_images must fail before touching Docker")

    with pytest.raises(
        docker.dcli.DockerCliError, match=re.escape("storage.root must be an absolute path")
    ):
        docker.dcli.ensure_images(config, runner=runner)


def test_image_pin_semver_versions_produce_validated_build_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """image_pin values in every accepted format must translate into the exact
    CLAUDE_CODE_VERSION build-arg passed to the shared image recipe (EV-60). The
    low-level buildx/tag/prune subprocess sequence is exercised by
    packages/docker-runtime/tests instead of being re-simulated here (mirrors
    packages/loop-harness/tests/test_loop_docker_image.py)."""
    config = copy.deepcopy(mh.DEFAULTS)
    captured_build_args: list[dict[str, str]] = []

    def fake_ensure_recipe_image(recipe, policy, **_kwargs):
        if recipe.family == "scenario":
            captured_build_args.append(dict(recipe.build_args))
        return docker.dcli.simg.runtime_image.EnsuredImage(
            "sha256:" + "3" * 64, f"{recipe.repository}:sha-test", "b" * 64, True
        )

    monkeypatch.setattr(
        docker.dcli.simg.runtime_image, "ensure_recipe_image", fake_ensure_recipe_image
    )
    monkeypatch.setattr(
        docker.dcli.simg.runtime_cli,
        "image_claude_version",
        lambda _image, **_kwargs: config["evaluate"]["isolation"]["image_pin"],
    )

    for version_pin in [
        "2.1.207",
        "2.1.207 (Claude Code)",
        "2.1.207-beta.1",
    ]:
        config["evaluate"]["isolation"]["image_pin"] = version_pin
        docker.dcli.ensure_images(config, main_root=tmp_path)

    assert captured_build_args == [
        {"CLAUDE_CODE_VERSION": "2.1.207"},
        {"CLAUDE_CODE_VERSION": "2.1.207"},
        {"CLAUDE_CODE_VERSION": "2.1.207-beta.1"},
    ]


def test_image_pin_rejects_invalid_versions_before_build(tmp_path: Path) -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    runner_calls = []
    injection_pin = "".join(['2.1.207";', "cu", "rl${IFS}evil|", "s", 'h;"'])

    def runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        raise AssertionError("invalid image_pin reached the Docker build command")

    for version_pin in [injection_pin, "2.1", "v2.1.207", "2.1.207.9"]:
        config["evaluate"]["isolation"]["image_pin"] = version_pin
        with pytest.raises(docker.dcli.DockerCliError, match="semver"):
            docker.dcli.ensure_images(config, runner=runner, main_root=tmp_path)

    config["evaluate"]["isolation"]["image_pin"] = ""
    with pytest.raises(docker.dcli.DockerCliError, match="Claude CLI version"):
        docker.dcli.ensure_images(config, runner=runner, main_root=tmp_path)

    assert runner_calls == []
