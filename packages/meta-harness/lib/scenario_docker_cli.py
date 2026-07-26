#!/usr/bin/env python3
"""Meta-harness compatibility wrapper for shared Docker CLI helpers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _LIB_DIR.parent
_DOCKER_DIR = _PACKAGE_DIR / "docker"
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
for _path in (_LIB_DIR, _DOCKER_RUNTIME_LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import docker_runtime_cli as runtime
import meta_harness_common as mh
import scenario_docker_image as simg

SubprocessRunner = runtime.SubprocessRunner

DOCKER_LABEL = "ai.orchestra.meta-harness"
# Re-exported from meta_harness_common (Issue #307 review): single Python-side
# source of truth, previously duplicated as identical literals here, in
# scenario_docker_image.py, and in meta_harness_common.DEFAULTS.
DEFAULT_SCENARIO_IMAGE = mh.DEFAULT_SCENARIO_IMAGE
DEFAULT_BROKER_IMAGE = mh.DEFAULT_BROKER_IMAGE
DEFAULT_CLAUDE_VERSION_PIN = mh.DEFAULT_CLAUDE_VERSION_PIN
CHECKED_COMMAND_TIMEOUT_SECONDS = runtime.CHECKED_COMMAND_TIMEOUT_SECONDS
DockerCliError = runtime.DockerCliError

_IMAGE_CACHE = runtime.ImageCache()


def ensure_images(
    config: dict,
    *,
    runner: SubprocessRunner = subprocess.run,
    main_root: Path | None = None,
) -> tuple[str, str]:
    """Ensure the scenario and broker images, returning their tags only.

    Back-compat wrapper (Issue #307 review): this is the public contract
    every existing caller relies on (a plain `(scenario_tag, broker_tag)`
    pair passed straight into Docker argv). Use `ensure_images_detailed()`
    when the richer `EnsuredImage` metadata (image_id / claude_version) is
    needed.
    """
    scenario, broker = ensure_images_detailed(config, runner=runner, main_root=main_root)
    return scenario.tag, broker.tag


def ensure_images_detailed(
    config: dict,
    *,
    runner: SubprocessRunner = subprocess.run,
    main_root: Path | None = None,
) -> tuple[simg.EnsuredImage, simg.EnsuredImage]:
    """Ensure the scenario and broker images, returning full `EnsuredImage`
    metadata (image_id / recipe_hash / claude_version) for callers that need
    to avoid a redundant `docker image inspect` or container launch
    (Issue #307)."""
    try:
        root = main_root if main_root is not None else mh.resolve_main_root(Path.cwd(), config)
        scenario = simg.ensure_scenario_image(config, root, runner=runner)
        broker = simg.ensure_broker_image(config, root, runner=runner)
    except (simg.DockerImageError, mh.MetaHarnessRootError) as exc:
        raise DockerCliError(str(exc)) from exc
    return scenario, broker


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


def _context_hash(context_dir: Path) -> str:
    return runtime.context_hash(context_dir)


def _context_dir(kind: str) -> Path:
    if kind not in {"scenario", "broker"}:
        raise ValueError(f"unknown Docker build context: {kind}")
    if kind == "broker":
        return _PACKAGE_DIR.parent / "docker-runtime" / "docker" / "broker"
    return _DOCKER_DIR / kind
