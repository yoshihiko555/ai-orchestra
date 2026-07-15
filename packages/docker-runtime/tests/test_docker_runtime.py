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
