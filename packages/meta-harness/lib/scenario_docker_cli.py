#!/usr/bin/env python3
"""Docker CLI, image build, inspection, and resource-removal helpers."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_DOCKER_DIR = _PACKAGE_DIR / "docker"
DOCKER_LABEL = "ai.orchestra.meta-harness"
DOCKER_CONTEXT_HASH_LABEL = f"{DOCKER_LABEL}.context-sha256"
DEFAULT_SCENARIO_IMAGE = "ai-orchestra/meta-harness-scenario:2.1.207"
DEFAULT_BROKER_IMAGE = "ai-orchestra/meta-harness-broker:0.1.0"
DEFAULT_CLAUDE_VERSION_PIN = "2.1.207 (Claude Code)"
CHECKED_COMMAND_TIMEOUT_SECONDS = 120
_DIGEST_IMAGE_RE = re.compile(r"@sha256:([0-9a-f]{64})$")
# Image preparation is serialized by the single evaluate process; these caches are not
# shared across threads and must be protected if image preparation becomes concurrent.
_TRUSTED_IMAGE_IDS: dict[str, str] = {}
_BUILT_CONTEXTS: set[tuple[str, str, tuple[str, ...]]] = set()


class DockerCliError(RuntimeError):
    """A required Docker CLI operation failed."""


def ensure_images(
    config: dict,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> tuple[str, str]:
    isolation = (config.get("evaluate") or {}).get("isolation") or {}
    broker_cfg = isolation.get("broker") or {}
    scenario_image = str(isolation.get("image", DEFAULT_SCENARIO_IMAGE))
    broker_image = str(broker_cfg.get("image", DEFAULT_BROKER_IMAGE))
    auto_build = bool(isolation.get("auto_build_images", True))
    version_pin = str(isolation.get("image_pin") or DEFAULT_CLAUDE_VERSION_PIN)
    _ensure_image(
        scenario_image,
        _DOCKER_DIR / "scenario",
        auto_build=auto_build,
        build_args=["--build-arg", f"CLAUDE_CODE_VERSION={version_pin.split()[0]}"],
        runner=runner,
    )
    _ensure_image(
        broker_image,
        _DOCKER_DIR / "broker",
        auto_build=auto_build,
        build_args=[],
        runner=runner,
    )
    return scenario_image, broker_image


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


def image_id(image: str, *, runner: SubprocessRunner) -> str:
    trusted = _TRUSTED_IMAGE_IDS.get(image)
    if trusted is not None:
        return trusted
    completed = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        runner=runner,
        timeout=20,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise DockerCliError(f"could not resolve Docker image ID: {image}")
    return completed.stdout.strip()


def context_hash(kind: str) -> str:
    if kind not in {"scenario", "broker"}:
        raise ValueError(f"unknown Docker build context: {kind}")
    return _context_hash(_DOCKER_DIR / kind)


def base_image_reference(kind: str) -> str:
    if kind not in {"scenario", "broker"}:
        raise ValueError(f"unknown Docker build context: {kind}")
    first_line = (_DOCKER_DIR / kind / "Dockerfile").read_text(encoding="utf-8").splitlines()[0]
    if not first_line.startswith("FROM ") or "@sha256:" not in first_line:
        raise DockerCliError(f"Docker base image is not digest-pinned: {kind}")
    return first_line.removeprefix("FROM ").strip()


def remove_container(name: str, *, runner: SubprocessRunner) -> bool:
    removed = run(["docker", "rm", "-f", name], runner=runner, timeout=20)
    if removed.returncode == 0:
        return True
    if _reports_missing_resource(removed, kind="container"):
        return True
    inspected = run(["docker", "inspect", name], runner=runner, timeout=10)
    return inspected.returncode != 0 and _reports_missing_resource(inspected, kind="container")


def remove_network(name: str, *, runner: SubprocessRunner) -> bool:
    removed = run(["docker", "network", "rm", name], runner=runner, timeout=20)
    if removed.returncode == 0:
        return True
    if _reports_missing_resource(removed, kind="network"):
        return True
    inspected = run(["docker", "network", "inspect", name], runner=runner, timeout=10)
    return inspected.returncode != 0 and _reports_missing_resource(inspected, kind="network")


def _reports_missing_resource(completed: subprocess.CompletedProcess, *, kind: str) -> bool:
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
    allowed = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR", "TMPDIR")
    return {key: os.environ[key] for key in allowed if os.environ.get(key)}


def _ensure_image(
    image: str,
    context_dir: Path,
    *,
    auto_build: bool,
    build_args: list[str],
    runner: SubprocessRunner,
) -> None:
    expected_hash = _context_hash(context_dir)
    build_key = (image, expected_hash, tuple(build_args))
    if auto_build and build_key in _BUILT_CONTEXTS:
        return
    if not auto_build:
        digest_match = _DIGEST_IMAGE_RE.search(image)
        if digest_match is None:
            raise DockerCliError(
                "prebuilt Docker images must use an immutable @sha256 digest: " + image
            )
        inspected = run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            runner=runner,
            timeout=20,
        )
        image_identifier = inspected.stdout.strip()
        if (
            inspected.returncode != 0
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_identifier) is None
        ):
            raise DockerCliError(f"required immutable Docker image is missing: {image}")
        _TRUSTED_IMAGE_IDS[image] = image_identifier
        return
    completed = run(
        [
            "docker",
            "build",
            "--no-cache",
            "--label",
            f"{DOCKER_CONTEXT_HASH_LABEL}={expected_hash}",
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
    inspected = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        runner=runner,
        timeout=20,
    )
    image_identifier = inspected.stdout.strip()
    if inspected.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_identifier) is None:
        raise DockerCliError(f"could not resolve freshly built Docker image ID: {image}")
    _TRUSTED_IMAGE_IDS[image] = image_identifier
    _BUILT_CONTEXTS.add(build_key)


def _context_hash(context_dir: Path) -> str:
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
