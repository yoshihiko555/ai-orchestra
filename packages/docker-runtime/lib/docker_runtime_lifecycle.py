#!/usr/bin/env python3
"""Shared Docker broker and owned-resource lifecycle helpers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SubprocessRunner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class RuntimeLabels:
    docker_label: str
    stale_max_age_seconds: int = 24 * 60 * 60

    @property
    def owner_label(self) -> str:
        return f"{self.docker_label}.owner"

    @property
    def parent_pid_label(self) -> str:
        return f"{self.docker_label}.parent-pid"

    @property
    def created_at_label(self) -> str:
        return f"{self.docker_label}.created-at"


@dataclass(frozen=True)
class BrokerContainerSpec:
    docker_label: str
    broker_alias: str
    container_name: str
    internal_network: str
    external_network: str
    broker_image_id: str
    broker_env: dict[str, str]
    owner_labels: dict[str, str]


def start_broker_container(
    spec: BrokerContainerSpec,
    *,
    runner: SubprocessRunner,
    checked: Callable[..., subprocess.CompletedProcess],
    remove_container: Callable[..., bool],
    remove_network: Callable[..., bool],
    inject_token: Callable[[], None],
    wait_ready: Callable[[], None],
    session_factory: Callable[[], Any],
    error_type: type[RuntimeError],
) -> Any:
    """Start a dual-homed broker and clean partial resources on every failure."""
    label_arguments = label_args(spec.owner_labels)
    created_networks: list[str] = []
    try:
        checked(
            [
                "docker",
                "network",
                "create",
                "--internal",
                "--label",
                f"{spec.docker_label}=run",
                *label_arguments,
                spec.internal_network,
            ],
            runner=runner,
            message="could not create internal Docker network",
        )
        created_networks.append(spec.internal_network)
        checked(
            [
                "docker",
                "network",
                "create",
                "--label",
                f"{spec.docker_label}=run",
                *label_arguments,
                spec.external_network,
            ],
            runner=runner,
            message="could not create broker egress network",
        )
        created_networks.append(spec.external_network)
        checked(
            broker_run_command(spec),
            runner=runner,
            message="could not start credential broker",
        )
        checked(
            ["docker", "network", "connect", spec.external_network, spec.container_name],
            runner=runner,
            message="could not connect broker to egress network",
        )
        inject_token()
        wait_ready()
        session = session_factory()
        session.start_keepalive()  # type: ignore[attr-defined]
        return session
    except Exception as exc:
        cleanup_errors: list[str] = []
        if not remove_container(spec.container_name, runner=runner):
            cleanup_errors.append("could not remove failed credential broker container")
        for network in reversed(created_networks):
            if not remove_network(network, runner=runner):
                cleanup_errors.append(f"could not remove failed Docker network: {network}")
        if cleanup_errors:
            raise error_type(
                f"credential broker startup failed: {exc}; " + "; ".join(cleanup_errors)
            ) from exc
        raise


def broker_run_command(spec: BrokerContainerSpec) -> list[str]:
    return [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        spec.container_name,
        "--network",
        spec.internal_network,
        "--network-alias",
        spec.broker_alias,
        "--label",
        f"{spec.docker_label}=run",
        *label_args(spec.owner_labels),
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "128m",
        "--cpus",
        "0.5",
        "--tmpfs",
        "/run/secrets:rw,noexec,nosuid,nodev,size=64k,uid=65532,gid=65532,mode=0700",
        "--tmpfs",
        "/run/state:rw,noexec,nosuid,nodev,size=1m,uid=65532,gid=65532,mode=0700",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700",
        *container_env_args(spec.broker_env),
        spec.broker_image_id,
    ]


def cleanup_broker_session(
    session: Any,
    *,
    error_type: type[RuntimeError],
    remove_container: Callable[..., bool],
    remove_network: Callable[..., bool],
) -> None:
    if session.cleaned:
        return
    session.stop_keepalive()
    errors: list[str] = []
    try:
        session.refresh_metrics()
    except error_type as exc:
        errors.append(str(exc))
    if not remove_container(session.container_name, runner=session.runner):
        errors.append("could not remove credential broker container")
    for network in (session.internal_network, session.external_network):
        if not remove_network(network, runner=session.runner):
            errors.append(f"could not remove Docker network: {network}")
    session.cleaned = not any("could not remove" in error for error in errors)
    if errors:
        raise error_type("; ".join(errors))


def broker_keepalive_loop(
    session: Any,
    stop: threading.Event,
    *,
    interval_seconds: int,
    broker_script: str,
    run_command: Callable[..., subprocess.CompletedProcess],
) -> None:
    command = [
        "docker",
        "exec",
        session.container_name,
        "/usr/bin/python3",
        broker_script,
        "--health",
        "--port",
        str(session.port),
    ]
    while not stop.wait(interval_seconds):
        if run_command(command, runner=session.runner, timeout=10).returncode != 0:
            return


def sweep_stale_resources(
    labels: RuntimeLabels,
    owner_id: str,
    *,
    runner: SubprocessRunner,
    run_command: Callable[..., subprocess.CompletedProcess],
    best_effort: Callable[..., None],
    container_stale: Callable[[dict[str, Any], str], bool],
    network_stale: Callable[[dict[str, Any], str], bool],
) -> None:
    containers = run_command(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label={labels.docker_label}=run",
            "--filter",
            f"label={labels.owner_label}={owner_id}",
        ],
        runner=runner,
        timeout=20,
    )
    if containers.returncode == 0:
        for container in containers.stdout.split():
            inspected = inspect_resource(container, runner=runner, run_command=run_command)
            is_stale = inspected is not None and container_stale(inspected, owner_id)
            if is_stale:
                best_effort(["docker", "rm", "-f", container], runner=runner)
    networks = run_command(
        [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label={labels.docker_label}=run",
            "--filter",
            f"label={labels.owner_label}={owner_id}",
        ],
        runner=runner,
        timeout=20,
    )
    if networks.returncode == 0:
        for network in networks.stdout.split():
            inspected = inspect_resource(
                network,
                network=True,
                runner=runner,
                run_command=run_command,
            )
            is_stale = inspected is not None and network_stale(inspected, owner_id)
            if is_stale:
                best_effort(["docker", "network", "rm", network], runner=runner)


def owner_id(main_root: Path) -> str:
    return hashlib.sha256(str(main_root.resolve()).encode()).hexdigest()[:16]


def resource_labels(labels: RuntimeLabels, owner: str) -> dict[str, str]:
    return {
        labels.owner_label: owner,
        labels.parent_pid_label: str(os.getpid()),
        labels.created_at_label: str(int(time.time())),
    }


def label_args(labels: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in sorted(labels.items()):
        args.extend(["--label", f"{key}={value}"])
    return args


def container_env_args(env: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in sorted(env.items()):
        args.extend(["--env", f"{key}={value}"])
    return args


def inspect_resource(
    resource: str,
    *,
    network: bool = False,
    runner: SubprocessRunner,
    run_command: Callable[..., subprocess.CompletedProcess],
) -> dict[str, Any] | None:
    command = ["docker"]
    if network:
        command.append("network")
    command.extend(["inspect", resource])
    completed = run_command(command, runner=runner, timeout=10)
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout)
    except (ValueError, json.JSONDecodeError):
        return None
    return value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else None


def container_is_stale(
    inspected: dict[str, Any],
    owner: str,
    *,
    labels: RuntimeLabels,
    pid_checker: Callable[[int], bool] | None = None,
) -> bool:
    resource_labels_value = (inspected.get("Config") or {}).get("Labels") or {}
    if resource_labels_value.get(labels.owner_label) != owner:
        return False
    try:
        created_at = int(resource_labels_value[labels.created_at_label])
    except (KeyError, TypeError, ValueError):
        return True
    if time.time() - created_at >= labels.stale_max_age_seconds:
        return True
    if not bool((inspected.get("State") or {}).get("Running")):
        return True
    try:
        parent_pid = int(resource_labels_value[labels.parent_pid_label])
    except (KeyError, TypeError, ValueError):
        return True
    # PID reuse can delay reclamation until STALE_MAX_AGE_SECONDS, but every run container
    # also has an independent absolute lifetime and cannot remain active indefinitely.
    return not (pid_checker or pid_alive)(parent_pid)


def network_is_stale(
    inspected: dict[str, Any],
    owner: str,
    *,
    labels: RuntimeLabels,
    pid_checker: Callable[[int], bool] | None = None,
) -> bool:
    """A same-owner network with no attached containers is stale, unless its creating
    process is still alive and the network is not yet past its own absolute age cap.

    Codex review, PR #262, High (round 6): concurrent same-project workers share one
    `owner_id` (`owner_id()` hashes only the project's main root), so a worker that just
    created its own broker/internal network -- but has not yet attached its broker or
    scenario container to it -- looks identical to a leaked, truly-orphaned network: same
    owner label, zero `Containers`. Without a liveness check, another worker's concurrent
    `sweep_stale_resources()` call can delete that live startup network out from under it,
    turning a healthy concurrent action into a spurious Docker infrastructure failure.
    Mirroring `container_is_stale()`'s own parent-pid grace, a missing/invalid
    `parent_pid_label` (e.g. a network created before this label existed) still reaps
    immediately -- only a network whose creating process is provably still running is
    spared, matching every other "orphaned by a dead driver" reclaim path.

    Codex review, PR #262, High (round 7): the parent-pid grace above has no time bound of its
    own, unlike `container_is_stale()`'s absolute age cap. If a driver crashes right after
    creating this network (before attaching any container) and the OS later reuses that same PID
    for an unrelated, long-lived process, `pid_alive()` stays true forever and this network would
    never be reclaimed, accumulating leaked Docker networks for the project indefinitely. A
    present, valid `created_at_label` past `stale_max_age_seconds` is reclaimed immediately,
    before the PID check ever runs -- mirroring `container_is_stale()`'s own age-then-liveness
    order-of-checks -- so PID reuse can only delay reclamation, never suspend it forever. A
    missing/invalid `created_at_label` (pre-dating this label) has no age signal to act on and
    falls through to the parent-pid check unchanged.
    """
    resource_labels_value = inspected.get("Labels") or {}
    if resource_labels_value.get(labels.owner_label) != owner:
        return False
    if inspected.get("Containers"):
        return False
    try:
        created_at = int(resource_labels_value[labels.created_at_label])
    except (KeyError, TypeError, ValueError):
        created_at = None
    if created_at is not None and time.time() - created_at >= labels.stale_max_age_seconds:
        return True
    try:
        parent_pid = int(resource_labels_value[labels.parent_pid_label])
    except (KeyError, TypeError, ValueError):
        return True
    return not (pid_checker or pid_alive)(parent_pid)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
