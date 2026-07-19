"""Phase-4 hardened loop-harness Docker profile tests."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.module_loader import load_module

profile = load_module(
    "loop_docker_profile_tests",
    "packages/loop-harness/lib/loop_docker_profile.py",
)

IMAGE_ID = "sha256:" + "a" * 64


@dataclass(frozen=True)
class Mount:
    source: Path
    target: Path
    read_only: bool


def _make_unix_socket(directory: Path, name: str) -> Path:
    """Bind a real AF_UNIX socket file at ``directory/name`` and return its path.

    Binds using a relative filename after `chdir`-ing into ``directory`` rather than an absolute
    path, since `AF_UNIX` socket paths are capped at ~104-108 bytes and pytest's `tmp_path`
    fixture routinely produces absolute paths longer than that.
    """
    previous_cwd = Path.cwd()
    os.chdir(directory)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(name)
        finally:
            sock.close()
    finally:
        os.chdir(previous_cwd)
    return directory / name


def _spec(tmp_path: Path, mounts: tuple[Mount, ...]) -> object:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return profile.ScenarioContainerSpec(
        container_name="lh-run-action",
        image_id=IMAGE_ID,
        internal_network="lh-run-action-internal",
        workdir=worktree,
        mounts=mounts,
        env={"ANTHROPIC_BASE_URL": "http://lh-broker:8787"},
        resources={"pids_limit": 64, "memory": "1g", "cpus": 1.0},
        max_lifetime_sec=300,
        owner_labels={"ai.orchestra.loop-harness.owner": "owner"},
    )


def test_scenario_command_applies_complete_security_profile_and_mount_order(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    pinned_git = tmp_path / "pinned-dotgit"
    pinned_git.write_text("gitdir: elsewhere\n", encoding="utf-8")
    mounts = (
        Mount(worktree, worktree, False),
        Mount(pinned_git, worktree / ".git", True),
    )

    command = profile.build_scenario_container_command(_spec(tmp_path, mounts))
    rendered = " ".join(command)
    mount_values = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]

    assert "--network lh-run-action-internal" in rendered
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "--init" in command
    assert "--user" in command
    assert "--pids-limit 64" in rendered
    assert "--memory 1g" in rendered
    assert "--cpus 1.0" in rendered
    assert "/home/loop:rw,noexec,nosuid,nodev" in rendered
    assert "/tmp:rw,noexec,nosuid,nodev" in rendered
    assert "HOME=/home/loop" in rendered
    assert "TMPDIR=/tmp" in rendered
    assert command[-6:] == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "300s",
        "/usr/bin/sleep",
        "infinity",
    ]
    assert str(worktree.resolve()) in mount_values[0]
    assert "readonly" not in mount_values[0]
    assert str(pinned_git.resolve()) in mount_values[1]
    assert f"dst={worktree / '.git'}" in mount_values[1]
    assert mount_values[1].endswith("readonly")
    assert "/var/run/docker.sock" not in rendered
    assert "/run/docker.sock" not in rendered
    # Codex review, PR #262, High: sweep_stale_resources() filters on
    # `label={docker_label}=run`; without this label a crashed driver's scenario container is
    # invisible to the next run's stale-resource sweep.
    assert "--label ai.orchestra.loop-harness=run" in rendered


def test_classifier_profile_supports_no_mounts_and_tmp_workdir(tmp_path: Path) -> None:
    spec = profile.ScenarioContainerSpec(
        **{
            **_spec(tmp_path, ()).__dict__,
            "workdir": Path("/tmp"),
        }
    )

    command = profile.build_scenario_container_command(spec)

    assert "--mount" not in command
    assert command[command.index("--workdir") + 1] == "/tmp"


@pytest.mark.parametrize("network", ["", "bridge", "default", "host", "none"])
def test_profile_rejects_non_internal_network_names(tmp_path: Path, network: str) -> None:
    spec = profile.ScenarioContainerSpec(
        **{
            **_spec(tmp_path, ()).__dict__,
            "internal_network": network,
        }
    )

    with pytest.raises(profile.DockerProfileError, match="dedicated internal network"):
        profile.build_scenario_container_command(spec)


@pytest.mark.parametrize("socket_path", ["/run/docker.sock", "/var/run/docker.sock"])
def test_profile_rejects_docker_socket_mounts(tmp_path: Path, socket_path: str) -> None:
    source = tmp_path / "ordinary-file"
    source.write_text("not a socket", encoding="utf-8")
    mount = Mount(source, Path(socket_path), True)

    with pytest.raises(profile.DockerProfileError, match="Docker socket mounts are forbidden"):
        profile.build_scenario_container_command(_spec(tmp_path, (mount,)))


def test_profile_rejects_renamed_docker_socket_source(tmp_path: Path) -> None:
    source = tmp_path / "docker.sock"
    source.write_text("not a socket", encoding="utf-8")
    mount = Mount(source, tmp_path / "mounted-socket", True)

    with pytest.raises(profile.DockerProfileError, match="Docker socket mounts are forbidden"):
        profile.build_scenario_container_command(_spec(tmp_path, (mount,)))


def test_profile_rejects_direct_unrenamed_socket_bind_mount_source(tmp_path: Path) -> None:
    """A bind mount source that is itself a real Unix socket file is rejected regardless of
    its name (not just the hardcoded `docker.sock` name/path checks above)."""
    socket_dir = tmp_path / "socket-dir"
    socket_dir.mkdir()
    socket_path = _make_unix_socket(socket_dir, "app.sock")
    mount = Mount(socket_path, tmp_path / "mounted-socket", True)

    with pytest.raises(profile.DockerProfileError, match="socket bind mounts are forbidden"):
        profile.build_scenario_container_command(_spec(tmp_path, (mount,)))


def test_profile_rejects_socket_nested_inside_mounted_worktree_directory(tmp_path: Path) -> None:
    """Codex review, PR #262, Critical (round 7): a directory bind mount (e.g. the Maker/Checker
    worktree) that contains a Unix socket anywhere below it must be rejected, not just when the
    mount source itself is a socket -- otherwise the whole bind mount carries that socket (most
    dangerously a `docker.sock`) whole into the scenario container even though the direct-socket
    checks above never see it.
    """
    worktree = tmp_path / "worktree"
    nested = worktree / "nested" / "deeper"
    nested.mkdir(parents=True)
    _make_unix_socket(nested, "docker.sock")
    mount = Mount(worktree, worktree, False)

    with pytest.raises(profile.DockerProfileError, match="socket bind mounts are forbidden"):
        profile.build_scenario_container_command(_spec(tmp_path, (mount,)))


def test_profile_accepts_worktree_mount_with_no_sockets_anywhere_below_it(
    tmp_path: Path,
) -> None:
    """A directory bind mount containing only ordinary files/directories is accepted."""
    worktree = tmp_path / "worktree"
    nested = worktree / "nested"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("ordinary file", encoding="utf-8")
    mount = Mount(worktree, worktree, False)

    command = profile.build_scenario_container_command(_spec(tmp_path, (mount,)))

    assert "--mount" in command


def test_exec_command_is_non_root_and_keeps_workdir(tmp_path: Path) -> None:
    command = profile.build_exec_command(
        "lh-run-action",
        ["bash", "-lc", "pytest -q"],
        workdir=tmp_path,
    )

    assert command[:3] == ["docker", "exec", "--user"]
    assert command[4:6] == ["--workdir", str(tmp_path)]
    assert command[-3:] == ["bash", "-lc", "pytest -q"]


def test_profile_requires_verified_image_id(tmp_path: Path) -> None:
    spec = profile.ScenarioContainerSpec(
        **{
            **_spec(tmp_path, ()).__dict__,
            "image_id": "scenario:latest",
        }
    )

    with pytest.raises(profile.DockerProfileError, match="immutable image ID"):
        profile.build_scenario_container_command(spec)
