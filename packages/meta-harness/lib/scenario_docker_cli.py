#!/usr/bin/env python3
"""Meta-harness compatibility wrapper for shared Docker CLI helpers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_DOCKER_DIR = _PACKAGE_DIR / "docker"
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
if str(_DOCKER_RUNTIME_LIB) not in sys.path:
    sys.path.insert(0, str(_DOCKER_RUNTIME_LIB))

import docker_runtime_cli as runtime

SubprocessRunner = runtime.SubprocessRunner

DOCKER_LABEL = "ai.orchestra.meta-harness"
DOCKER_CONTEXT_HASH_LABEL = f"{DOCKER_LABEL}.context-sha256"
DEFAULT_SCENARIO_IMAGE = "ai-orchestra/meta-harness-scenario:2.1.207"
DEFAULT_BROKER_IMAGE = "ai-orchestra/meta-harness-broker:0.1.0"
DEFAULT_CLAUDE_VERSION_PIN = "2.1.207 (Claude Code)"
CHECKED_COMMAND_TIMEOUT_SECONDS = runtime.CHECKED_COMMAND_TIMEOUT_SECONDS
DockerCliError = runtime.DockerCliError

_IMAGE_CACHE = runtime.ImageCache()
_TRUSTED_IMAGE_IDS = _IMAGE_CACHE.trusted_image_ids
_BUILT_CONTEXTS = _IMAGE_CACHE.built_contexts


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
        _context_dir("scenario"),
        auto_build=auto_build,
        build_args=["--build-arg", f"CLAUDE_CODE_VERSION={version_pin.split()[0]}"],
        runner=runner,
    )
    _ensure_image(
        broker_image,
        _context_dir("broker"),
        auto_build=auto_build,
        build_args=[],
        runner=runner,
    )
    return scenario_image, broker_image


def docker_daemon_available(*, runner: SubprocessRunner) -> bool:
    return runtime.docker_daemon_available(runner=runner)


def image_claude_version(image: str, *, runner: SubprocessRunner) -> str | None:
    return runtime.image_claude_version(image, runner=runner)


def image_id(image: str, *, runner: SubprocessRunner) -> str:
    return runtime.image_id(image, runner=runner, cache=_IMAGE_CACHE)


def context_hash(kind: str) -> str:
    return _context_hash(_context_dir(kind))


def base_image_reference(kind: str) -> str:
    return runtime.base_image_reference(_context_dir(kind), kind=kind)


def remove_container(name: str, *, runner: SubprocessRunner) -> bool:
    return runtime.remove_container(name, runner=runner)


def remove_network(name: str, *, runner: SubprocessRunner) -> bool:
    return runtime.remove_network(name, runner=runner)


def _reports_missing_resource(completed: subprocess.CompletedProcess, *, kind: str) -> bool:
    return runtime.reports_missing_resource(completed, kind=kind)


def checked(
    command: list[str],
    *,
    runner: SubprocessRunner,
    message: str,
) -> subprocess.CompletedProcess:
    return runtime.checked(command, runner=runner, message=message)


def best_effort(command: list[str], *, runner: SubprocessRunner) -> None:
    runtime.best_effort(command, runner=runner)


def run(
    command: list[str],
    *,
    runner: SubprocessRunner,
    timeout: int | float,
) -> subprocess.CompletedProcess:
    return runtime.run(command, runner=runner, timeout=timeout)


def host_env() -> dict[str, str]:
    return runtime.host_env()


def _ensure_image(
    image: str,
    context_dir: Path,
    *,
    auto_build: bool,
    build_args: list[str],
    runner: SubprocessRunner,
) -> None:
    runtime.ensure_image(
        image,
        context_dir,
        context_hash_label=DOCKER_CONTEXT_HASH_LABEL,
        auto_build=auto_build,
        build_args=build_args,
        runner=runner,
        cache=_IMAGE_CACHE,
    )


def _context_hash(context_dir: Path) -> str:
    return runtime.context_hash(context_dir)


def _context_dir(kind: str) -> Path:
    if kind not in {"scenario", "broker"}:
        raise ValueError(f"unknown Docker build context: {kind}")
    if kind == "broker":
        return _PACKAGE_DIR.parent / "docker-runtime" / "docker" / "broker"
    return _DOCKER_DIR / kind
