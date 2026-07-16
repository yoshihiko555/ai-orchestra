"""Shared Docker runtime behavior tests (docker-runtime EV-01 through EV-08)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

cli = load_module(
    "docker_runtime_cli",
    "packages/docker-runtime/lib/docker_runtime_cli.py",
)
lifecycle = load_module(
    "docker_runtime_lifecycle_tests",
    "packages/docker-runtime/lib/docker_runtime_lifecycle.py",
)
profile = load_module(
    "docker_runtime_profile_tests",
    "packages/docker-runtime/lib/docker_runtime_profile.py",
)

IMAGE_ID = "sha256:" + "a" * 64


def _completed(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_ensure_image_reuses_process_local_build_cache(tmp_path: Path) -> None:
    context = tmp_path / "scenario"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM example@sha256:" + "b" * 64, encoding="utf-8")
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return _completed(stdout=IMAGE_ID)
        return _completed()

    cache = cli.ImageCache()
    for _ in range(2):
        cli.ensure_image(
            "runtime:test",
            context,
            context_hash_label="ai.orchestra.test.context-sha256",
            auto_build=True,
            build_args=["--build-arg", "VERSION=1"],
            runner=runner,
            cache=cache,
        )

    builds = [command for command in commands if command[:2] == ["docker", "build"]]
    assert len(builds) == 1
    assert "--no-cache" in builds[0]
    assert any(value.startswith("ai.orchestra.test.context-sha256=") for value in builds[0])


def test_prebuilt_image_requires_immutable_digest(tmp_path: Path) -> None:
    with pytest.raises(cli.DockerCliError, match="immutable"):
        cli.ensure_image(
            "runtime:mutable",
            tmp_path,
            context_hash_label="ai.orchestra.test.context-sha256",
            auto_build=False,
            build_args=[],
            runner=lambda *_args, **_kwargs: _completed(),
            cache=cli.ImageCache(),
        )


def test_resource_removal_only_accepts_explicit_missing_response() -> None:
    responses = iter(
        [
            _completed(returncode=1, stderr="permission denied"),
            _completed(returncode=1, stderr="permission denied"),
        ]
    )
    assert cli.remove_container("owned", runner=lambda *_args, **_kwargs: next(responses)) is False

    assert (
        cli.remove_network(
            "gone",
            runner=lambda *_args, **_kwargs: _completed(
                returncode=1,
                stderr="Error response from daemon: network gone not found",
            ),
        )
        is True
    )


def test_profile_builders_keep_hardening_and_validate_mounts(tmp_path: Path) -> None:
    assert profile.tmpfs("/tmp", 501, 20, size="64m").startswith("/tmp:rw,noexec,nosuid,nodev")
    assert profile.bounded_container_command(
        {"max_lifetime_sec": 30}, ["command"], kill_after_seconds=5
    ) == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "30s",
        "command",
    ]
    with pytest.raises(profile.DockerProfileError, match="comma"):
        profile.bind_mount(tmp_path / "invalid,path", "/workspace", read_only=True)


def test_broker_command_is_dual_homed_hardened_and_uses_image_id() -> None:
    spec = lifecycle.BrokerContainerSpec(
        docker_label="ai.orchestra.test",
        broker_alias="test-broker",
        container_name="test-broker-1",
        internal_network="test-internal",
        external_network="test-external",
        broker_image_id=IMAGE_ID,
        broker_env={"TOKEN": "run-token"},
        owner_labels={"ai.orchestra.test.owner": "owner"},
    )

    command = lifecycle.broker_run_command(spec)
    rendered = " ".join(command)

    assert "--network test-internal" in rendered
    assert "--network-alias test-broker" in rendered
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert command[-1] == IMAGE_ID
    assert "/var/run/docker.sock" not in rendered


def test_partial_broker_startup_failure_cleans_owned_resources() -> None:
    spec = lifecycle.BrokerContainerSpec(
        docker_label="ai.orchestra.test",
        broker_alias="test-broker",
        container_name="test-broker-1",
        internal_network="test-internal",
        external_network="test-external",
        broker_image_id=IMAGE_ID,
        broker_env={},
        owner_labels={},
    )
    removed: list[str] = []
    checked_count = 0

    def checked(*_args, **_kwargs) -> subprocess.CompletedProcess:
        nonlocal checked_count
        checked_count += 1
        if checked_count == 3:
            raise RuntimeError("broker failed")
        return _completed()

    with pytest.raises(RuntimeError, match="broker failed"):
        lifecycle.start_broker_container(
            spec,
            runner=lambda *_args, **_kwargs: _completed(),
            checked=checked,
            remove_container=lambda name, **_kwargs: not removed.append(name),
            remove_network=lambda name, **_kwargs: not removed.append(name),
            inject_token=lambda: None,
            wait_ready=lambda: None,
            session_factory=lambda: pytest.fail("session must not be created"),
            error_type=RuntimeError,
        )

    assert removed == ["test-broker-1", "test-external", "test-internal"]


def test_runtime_labels_keep_harness_namespaces_independent() -> None:
    meta = lifecycle.RuntimeLabels("ai.orchestra.meta-harness")
    loop = lifecycle.RuntimeLabels("ai.orchestra.loop-harness")

    assert meta.owner_label == "ai.orchestra.meta-harness.owner"
    assert loop.owner_label == "ai.orchestra.loop-harness.owner"
    assert meta.owner_label != loop.owner_label


def test_sweep_stale_resources_removes_only_stale_containers_and_networks() -> None:
    """EV-11: Only resources selected by the injected stale checks are removed."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    stale_container = "container-stale"
    active_container = "container-active"
    stale_network = "network-stale"
    active_network = "network-active"
    removed: list[list[str]] = []

    def run_command(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        if command[:3] == ["docker", "ps", "-aq"]:
            return _completed(stdout=f"{stale_container} {active_container}\n")
        if command[:4] == ["docker", "network", "ls", "-q"]:
            return _completed(stdout=f"{stale_network} {active_network}\n")
        return _completed(stdout='[{"Id": "' + command[-1] + '"}]')

    def container_stale(inspected: dict, _owner: str) -> bool:
        return inspected["Id"] == stale_container

    def network_stale(inspected: dict, _owner: str) -> bool:
        return inspected["Id"] == stale_network

    def best_effort(command: list[str], **_kwargs) -> None:
        removed.append(command)

    lifecycle.sweep_stale_resources(
        labels,
        "owner-test",
        runner=subprocess.run,
        run_command=run_command,
        best_effort=best_effort,
        container_stale=container_stale,
        network_stale=network_stale,
    )

    assert removed == [
        ["docker", "rm", "-f", stale_container],
        ["docker", "network", "rm", stale_network],
    ]
    assert ["docker", "rm", "-f", active_container] not in removed
    assert ["docker", "network", "rm", active_network] not in removed


def test_container_is_stale_returns_false_for_owner_mismatch() -> None:
    """EV-12: A container owned by another caller is never stale."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    inspected = {"Config": {"Labels": {labels.owner_label: "other-owner"}}}

    assert lifecycle.container_is_stale(inspected, "owner-test", labels=labels) is False


def test_network_is_stale_returns_false_for_owner_mismatch() -> None:
    """EV-12: A network owned by another caller is never stale."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    inspected = {"Labels": {labels.owner_label: "other-owner"}}

    assert lifecycle.network_is_stale(inspected, "owner-test", labels=labels) is False
