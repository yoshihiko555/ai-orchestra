#!/usr/bin/env python3
"""Pure hardened Docker command builders for loop-harness actions."""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
if str(_DOCKER_RUNTIME_LIB) not in sys.path:
    sys.path.insert(0, str(_DOCKER_RUNTIME_LIB))

import docker_runtime_profile as runtime  # noqa: E402

CONTAINER_HOME = "/home/loop"
CONTAINER_TMP = "/tmp"
CONTAINER_TIMEOUT_KILL_AFTER_SECONDS = 5
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_NETWORKS = frozenset({"bridge", "default", "host", "none"})
_DOCKER_SOCKET_PATHS = frozenset({"/run/docker.sock", "/var/run/docker.sock"})

DockerProfileError = runtime.DockerProfileError

# Codex review, PR #262, High: must match the base run label docker_runtime_lifecycle's
# `sweep_stale_resources()` filters on (`label={docker_label}=run`, see loop_docker_action.py's
# and loop_docker_broker.py's own `DOCKER_LABEL`). Without it here, a scenario container started
# by this module carries only the owner/parent/created-at labels and is invisible to the
# `docker ps --filter label=...=run` sweep a crashed driver's next run relies on to reclaim it.
DOCKER_LABEL = "ai.orchestra.loop-harness"


class BindMount(Protocol):
    """Structural type shared by Maker/Checker ordered mount specs."""

    source: Path
    target: Path
    read_only: bool


@dataclass(frozen=True)
class ScenarioContainerSpec:
    """Inputs required to start one hardened action container."""

    container_name: str
    image_id: str
    internal_network: str
    workdir: Path
    mounts: Sequence[BindMount]
    env: Mapping[str, str]
    resources: Mapping[str, Any]
    max_lifetime_sec: int
    owner_labels: Mapping[str, str]


def build_scenario_container_command(spec: ScenarioContainerSpec) -> list[str]:
    """Build a detached, broker-only, non-root action container command."""
    _validate_spec(spec)
    uid, gid = runtime.non_root_identity()
    resources = {**spec.resources, "max_lifetime_sec": spec.max_lifetime_sec}
    return [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        spec.container_name,
        "--label",
        f"{DOCKER_LABEL}=run",
        *_label_args(spec.owner_labels),
        "--network",
        spec.internal_network,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--init",
        "--user",
        f"{uid}:{gid}",
        *runtime.resource_args(resources),
        *_mount_args(spec.mounts),
        "--tmpfs",
        runtime.tmpfs(CONTAINER_HOME, uid, gid, size="256m"),
        "--tmpfs",
        runtime.tmpfs(CONTAINER_TMP, uid, gid, size="256m"),
        "--workdir",
        str(spec.workdir),
        *runtime.container_env_args({**spec.env, "HOME": CONTAINER_HOME, "TMPDIR": CONTAINER_TMP}),
        spec.image_id,
        *runtime.bounded_container_command(
            resources,
            ["/usr/bin/sleep", "infinity"],
            kill_after_seconds=CONTAINER_TIMEOUT_KILL_AFTER_SECONDS,
        ),
    ]


def build_exec_command(
    container_name: str,
    command: Sequence[str],
    *,
    workdir: str | Path,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Build one non-root ``docker exec`` command for an action container."""
    if not container_name or not command or isinstance(command, str):
        raise DockerProfileError("container name and exec command must not be empty")
    workdir_text = str(workdir)
    if not Path(workdir_text).is_absolute():
        raise DockerProfileError("container exec workdir must be absolute")
    uid, gid = runtime.non_root_identity()
    return [
        "docker",
        "exec",
        "--user",
        f"{uid}:{gid}",
        "--workdir",
        workdir_text,
        *runtime.container_env_args(dict(env or {})),
        container_name,
        *command,
    ]


def resources_config(resources: Any) -> dict[str, Any]:
    """Translate a validated resource dataclass or mapping for lifecycle metadata."""
    if isinstance(resources, Mapping):
        return {
            "pids_limit": resources["pids_limit"],
            "memory": resources["memory"],
            "cpus": resources["cpus"],
        }
    return {
        "pids_limit": resources.pids_limit,
        "memory": resources.memory,
        "cpus": resources.cpus,
    }


def _validate_spec(spec: ScenarioContainerSpec) -> None:
    if not spec.container_name:
        raise DockerProfileError("container name must not be empty")
    if _IMAGE_ID_RE.fullmatch(spec.image_id) is None:
        raise DockerProfileError("scenario image must be a verified immutable image ID")
    if not spec.internal_network or spec.internal_network in _FORBIDDEN_NETWORKS:
        raise DockerProfileError("scenario container requires a dedicated internal network")
    if not spec.workdir.is_absolute():
        raise DockerProfileError("scenario container workdir must be absolute")
    if not isinstance(spec.max_lifetime_sec, int) or isinstance(spec.max_lifetime_sec, bool):
        raise DockerProfileError("container max_lifetime_sec must be an integer")
    if spec.max_lifetime_sec <= 0:
        raise DockerProfileError("container max_lifetime_sec must be positive")
    for mount in spec.mounts:
        _validate_mount(mount)


def _mount_args(mounts: Sequence[BindMount]) -> list[str]:
    args: list[str] = []
    for mount in mounts:
        args.extend(
            [
                "--mount",
                runtime.bind_mount(
                    mount.source,
                    str(mount.target),
                    read_only=mount.read_only,
                ),
            ]
        )
    return args


def _validate_mount(mount: BindMount) -> None:
    source = mount.source.resolve()
    target = str(mount.target)
    if not mount.target.is_absolute():
        raise DockerProfileError("bind mount target must be absolute")
    if (
        str(mount.source) in _DOCKER_SOCKET_PATHS
        or str(source) in _DOCKER_SOCKET_PATHS
        or target in _DOCKER_SOCKET_PATHS
        or mount.source.name == "docker.sock"
        or mount.target.name == "docker.sock"
    ):
        raise DockerProfileError("Docker socket mounts are forbidden")
    try:
        mode = source.stat().st_mode
    except OSError:
        return
    if stat.S_ISSOCK(mode):
        raise DockerProfileError("socket bind mounts are forbidden")
    if stat.S_ISDIR(mode):
        _reject_socket_descendants(source)


def _reject_socket_descendants(root: Path) -> None:
    """Fail closed if a directory bind mount source contains a Unix socket anywhere below it.

    Codex review, PR #262, Critical (round 7): `_validate_mount()`'s socket checks above only
    stat the mount source itself. When the source is a directory -- e.g. the Maker/Checker
    worktree -- a Unix socket sitting anywhere underneath it (most dangerously a `docker.sock`
    someone bind-mounted into a dev worktree, but any listening socket works the same way) rides
    along whole into the scenario container's bind mount even though the direct socket checks
    above never see it, letting Maker-authored code inside the container reach it and, for a
    Docker socket, fully escape the container/host isolation boundary. `followlinks=False` keeps
    this walk from crossing a symlinked directory into an unrelated part of the filesystem; a
    symlink to a socket is not itself flagged (`lstat` reports it as a symlink), matching what a
    bind mount actually exposes -- the real socket inode, not a same-tree symlink pointing away
    from it.
    """
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            entry_path = Path(dirpath) / name
            try:
                entry_mode = entry_path.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISSOCK(entry_mode):
                raise DockerProfileError(
                    f"socket bind mounts are forbidden: found a socket at {entry_path}"
                )


def _label_args(labels: Mapping[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in sorted(labels.items()):
        if not key or not value:
            raise DockerProfileError("Docker owner labels must not be empty")
        args.extend(["--label", f"{key}={value}"])
    return args
