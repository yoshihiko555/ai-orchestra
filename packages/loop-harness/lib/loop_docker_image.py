#!/usr/bin/env python3
"""Loop-harness namespace adapter for the shared Docker image lifecycle."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _LIB_DIR.parent
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
for _path in (_LIB_DIR, _DOCKER_RUNTIME_LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import docker_runtime_cli as runtime_cli
import docker_runtime_image as runtime_image
import loop_docker_config as docker_config

SubprocessRunner = runtime_cli.SubprocessRunner
EnsuredImage = runtime_image.EnsuredImage
DockerImageError = runtime_image.DockerImageError

DOCKER_LABEL = "ai.orchestra.loop-harness"
NAME_PREFIX = "lh-"
DEFAULT_SCENARIO_IMAGE = docker_config.DEFAULT_SCENARIO_IMAGE
DEFAULT_BROKER_IMAGE = docker_config.DEFAULT_BROKER_IMAGE
DEFAULT_MANIFEST_PATH = ".claude/loop/docker-image-cache.json"
DEFAULT_LOCK_PATH = ".claude/loop/docker-image-build.lock"
DEFAULT_BUILDER_NAME = "orchex-loop-harness"
DEFAULT_BUILDKIT_CACHE_MAX_AGE = "168h"
DEFAULT_BUILDKIT_CACHE_MAX_SIZE = "10g"
DEFAULT_KEEP_GENERATIONS = 3


def ensure_scenario_image(
    config: dict[str, Any],
    main_root: Path,
    *,
    runner: SubprocessRunner = subprocess.run,
    platform: str | None = None,
    target: str | None = None,
) -> EnsuredImage:
    """Ensure the configured loop-harness scenario image without enabling execution."""
    isolation = _mapping(_mapping(config.get("lp2")).get("isolation"))
    cache = _mapping(isolation.get("image_cache"))
    configured_image = str(isolation.get("image") or DEFAULT_SCENARIO_IMAGE)
    image_pin = isolation.get("image_pin")
    build_args: dict[str, str] = {}
    if image_pin is not None:
        try:
            build_args["CLAUDE_CODE_VERSION"] = runtime_cli.version_from_pin(str(image_pin))
        except ValueError as exc:
            raise DockerImageError(str(exc)) from exc
    recipe = runtime_image.ImageRecipe(
        family="scenario",
        repository=_image_repository(configured_image),
        context_dir=_PACKAGE_DIR / "docker" / "scenario",
        docker_label=DOCKER_LABEL,
        build_args=build_args,
        platform=platform,
        target=target,
    )
    policy = _cache_policy(main_root, cache)
    ensured = runtime_image.ensure_recipe_image(
        recipe,
        policy,
        auto_build=bool(isolation.get("auto_build_images", True)),
        immutable_image=configured_image,
        runner=runner,
    )
    if image_pin is not None:
        actual = runtime_cli.image_claude_version(ensured.image_id, runner=runner)
        if not runtime_cli.version_matches(actual, str(image_pin)):
            raise DockerImageError(f"image_pin mismatch: expected {image_pin!r}, got {actual!r}")
    return ensured


def ensure_broker_image(
    config: dict[str, Any],
    main_root: Path,
    *,
    runner: SubprocessRunner = subprocess.run,
    platform: str | None = None,
    target: str | None = None,
) -> EnsuredImage:
    """Ensure the shared broker image in the loop-harness image-cache namespace."""
    isolation = _mapping(_mapping(config.get("lp2")).get("isolation"))
    broker = _mapping(isolation.get("broker"))
    cache = _mapping(isolation.get("image_cache"))
    configured_image = str(broker.get("image") or DEFAULT_BROKER_IMAGE)
    recipe = runtime_image.ImageRecipe(
        family="broker",
        repository=_image_repository(configured_image),
        context_dir=_PACKAGE_DIR.parent / "docker-runtime" / "docker" / "broker",
        docker_label=DOCKER_LABEL,
        build_args={},
        platform=platform,
        target=target,
    )
    policy = _cache_policy(main_root, cache)
    return runtime_image.ensure_recipe_image(
        recipe,
        policy,
        auto_build=bool(isolation.get("auto_build_images", True)),
        immutable_image=configured_image,
        runner=runner,
    )


def _cache_policy(main_root: Path, cache: dict[str, Any]) -> runtime_image.ImageCachePolicy:
    return runtime_image.build_cache_policy(
        main_root,
        cache,
        defaults={
            "manifest_path": DEFAULT_MANIFEST_PATH,
            "lock_path": DEFAULT_LOCK_PATH,
            "keep_generations": DEFAULT_KEEP_GENERATIONS,
            "builder_name": DEFAULT_BUILDER_NAME,
            "buildkit_cache_max_age": DEFAULT_BUILDKIT_CACHE_MAX_AGE,
            "buildkit_cache_max_size": DEFAULT_BUILDKIT_CACHE_MAX_SIZE,
        },
    )


# Path-safety, cache-policy construction, and repository-name parsing are
# shared across harness namespace adapters; see docker_runtime_image.py
# (Issue #250 Fix A). Aliases below preserve this module's private-name
# surface for existing tests/call sites.
_mapping = runtime_image.mapping
_image_repository = runtime_image.image_repository
