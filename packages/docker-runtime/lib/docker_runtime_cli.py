#!/usr/bin/env python3
"""Shared Docker CLI, image inspection, and image build helpers."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

CHECKED_COMMAND_TIMEOUT_SECONDS = 120
_DIGEST_IMAGE_RE = re.compile(r"@sha256:([0-9a-f]{64})$")


class DockerCliError(RuntimeError):
    """A required Docker CLI operation failed."""


@dataclass
class ImageCache:
    """Process-local cache for resolved image IDs and completed builds."""

    trusted_image_ids: dict[str, str] = field(default_factory=dict)
    built_contexts: set[tuple[str, str, tuple[str, ...]]] = field(default_factory=set)


_DEFAULT_IMAGE_CACHE = ImageCache()


def ensure_image(
    image: str,
    context_dir: Path,
    *,
    context_hash_label: str,
    auto_build: bool,
    build_args: list[str],
    runner: SubprocessRunner,
    cache: ImageCache = _DEFAULT_IMAGE_CACHE,
) -> None:
    """Ensure one image using the existing context-hash build semantics."""
    expected_hash = context_hash(context_dir)
    build_key = (image, expected_hash, tuple(build_args))
    if auto_build and build_key in cache.built_contexts:
        return
    if not auto_build:
        _trust_immutable_image(image, runner=runner, cache=cache)
        return
    completed = run(
        [
            "docker",
            "build",
            "--no-cache",
            "--label",
            f"{context_hash_label}={expected_hash}",
            "-t",
            image,
            *build_args,
            str(context_dir),
        ],
        runner=runner,
        timeout=900,
    )
    if completed.returncode != 0:
        raise DockerCliError(f"could not build required Docker image: {image}")
    image_identifier = _inspect_image_id(image, runner=runner)
    if image_identifier is None:
        raise DockerCliError(f"could not resolve freshly built Docker image ID: {image}")
    cache.trusted_image_ids[image] = image_identifier
    cache.built_contexts.add(build_key)


def docker_daemon_available(*, runner: SubprocessRunner) -> bool:
    return (
        run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            runner=runner,
            timeout=20,
        ).returncode
        == 0
    )


def image_claude_version(image: str, *, runner: SubprocessRunner) -> str | None:
    completed = run(
        ["docker", "run", "--rm", "--network", "none", image, "claude", "--version"],
        runner=runner,
        timeout=30,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def image_id(
    image: str,
    *,
    runner: SubprocessRunner,
    cache: ImageCache = _DEFAULT_IMAGE_CACHE,
) -> str:
    trusted = cache.trusted_image_ids.get(image)
    if trusted is not None:
        return trusted
    inspected = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        runner=runner,
        timeout=20,
    )
    image_identifier = inspected.stdout.strip()
    if inspected.returncode != 0 or not image_identifier:
        raise DockerCliError(f"could not resolve Docker image ID: {image}")
    return image_identifier


def context_hash(context_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(context_dir.rglob("*")):
        if (
            path.is_symlink()
            or not path.is_file()
            or "__pycache__" in path.parts
            or path.suffix == ".pyc"
        ):
            continue
        digest.update(path.relative_to(context_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def base_image_reference(context_dir: Path, *, kind: str) -> str:
    first_line = (context_dir / "Dockerfile").read_text(encoding="utf-8").splitlines()[0]
    if not first_line.startswith("FROM ") or "@sha256:" not in first_line:
        raise DockerCliError(f"Docker base image is not digest-pinned: {kind}")
    return first_line.removeprefix("FROM ").strip()


def remove_container(name: str, *, runner: SubprocessRunner) -> bool:
    removed = run(["docker", "rm", "-f", name], runner=runner, timeout=20)
    if removed.returncode == 0 or reports_missing_resource(removed, kind="container"):
        return True
    inspected = run(["docker", "inspect", name], runner=runner, timeout=10)
    return inspected.returncode != 0 and reports_missing_resource(inspected, kind="container")


def remove_network(name: str, *, runner: SubprocessRunner) -> bool:
    removed = run(["docker", "network", "rm", name], runner=runner, timeout=20)
    if removed.returncode == 0 or reports_missing_resource(removed, kind="network"):
        return True
    inspected = run(["docker", "network", "inspect", name], runner=runner, timeout=10)
    return inspected.returncode != 0 and reports_missing_resource(inspected, kind="network")


def reports_missing_resource(completed: subprocess.CompletedProcess, *, kind: str) -> bool:
    detail = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
    if "no such object:" in detail:
        return True
    if kind == "container":
        return "no such container:" in detail
    if kind == "network":
        return "no such network:" in detail or bool(
            re.search(r"(?:error response from daemon:\s*)?network\s+\S+\s+not found", detail)
        )
    raise ValueError(f"unknown Docker resource kind: {kind}")


def checked(
    command: list[str],
    *,
    runner: SubprocessRunner,
    message: str,
) -> subprocess.CompletedProcess:
    completed = run(command, runner=runner, timeout=CHECKED_COMMAND_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise DockerCliError(message)
    return completed


def best_effort(command: list[str], *, runner: SubprocessRunner) -> None:
    run(command, runner=runner, timeout=20)


def run(
    command: list[str],
    *,
    runner: SubprocessRunner,
    timeout: int | float,
) -> subprocess.CompletedProcess:
    try:
        return runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=host_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(exc))


def host_env() -> dict[str, str]:
    allowed = (
        "PATH",
        "HOME",
        "DOCKER_API_VERSION",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
    )
    return {key: os.environ[key] for key in allowed if os.environ.get(key)}


def _trust_immutable_image(
    image: str,
    *,
    runner: SubprocessRunner,
    cache: ImageCache,
) -> None:
    if _DIGEST_IMAGE_RE.search(image) is None:
        raise DockerCliError(
            "prebuilt Docker images must use an immutable @sha256 digest: " + image
        )
    image_identifier = _inspect_image_id(image, runner=runner)
    if image_identifier is None:
        raise DockerCliError(f"required immutable Docker image is missing: {image}")
    cache.trusted_image_ids[image] = image_identifier


def _inspect_image_id(image: str, *, runner: SubprocessRunner) -> str | None:
    inspected = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        runner=runner,
        timeout=20,
    )
    image_identifier = inspected.stdout.strip()
    if inspected.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_identifier) is None:
        return None
    return image_identifier
