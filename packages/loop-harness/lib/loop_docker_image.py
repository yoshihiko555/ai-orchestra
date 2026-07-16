#!/usr/bin/env python3
"""Loop-harness namespace adapter for the shared Docker image lifecycle."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
if str(_DOCKER_RUNTIME_LIB) not in sys.path:
    sys.path.insert(0, str(_DOCKER_RUNTIME_LIB))

import docker_runtime_cli as runtime_cli
import docker_runtime_image as runtime_image

SubprocessRunner = runtime_cli.SubprocessRunner
EnsuredImage = runtime_image.EnsuredImage
DockerImageError = runtime_image.DockerImageError

DOCKER_LABEL = "ai.orchestra.loop-harness"
NAME_PREFIX = "lh-"
DEFAULT_SCENARIO_IMAGE = "ai-orchestra/loop-harness-scenario:2.1.207"
DEFAULT_MANIFEST_PATH = ".claude/loop/docker-image-cache.json"
DEFAULT_LOCK_PATH = ".claude/loop/docker-image-build.lock"
DEFAULT_BUILDER_NAME = "loop-harness-builder"
DEFAULT_BUILDKIT_CACHE_MAX_AGE = "168h"
DEFAULT_BUILDKIT_CACHE_MAX_SIZE = "10g"
DEFAULT_KEEP_GENERATIONS = 3
_SEMVER_PIN_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$")


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
        build_args["CLAUDE_CODE_VERSION"] = _version_from_pin(str(image_pin))
    recipe = runtime_image.ImageRecipe(
        family="scenario",
        repository=_image_repository(configured_image),
        context_dir=_PACKAGE_DIR / "docker" / "scenario",
        docker_label=DOCKER_LABEL,
        build_args=build_args,
        platform=platform,
        target=target,
    )
    policy = runtime_image.ImageCachePolicy(
        manifest_path=_main_root_path(
            main_root,
            cache.get("manifest_path", DEFAULT_MANIFEST_PATH),
        ),
        lock_path=_main_root_path(main_root, cache.get("lock_path", DEFAULT_LOCK_PATH)),
        keep_generations=_positive_int(
            cache.get("keep_generations"),
            DEFAULT_KEEP_GENERATIONS,
        ),
        builder_name=str(cache.get("builder_name") or DEFAULT_BUILDER_NAME),
        buildkit_cache_max_age=str(
            cache.get("buildkit_cache_max_age") or DEFAULT_BUILDKIT_CACHE_MAX_AGE
        ),
        buildkit_cache_max_size=str(
            cache.get("buildkit_cache_max_size") or DEFAULT_BUILDKIT_CACHE_MAX_SIZE
        ),
    )
    ensured = runtime_image.ensure_recipe_image(
        recipe,
        policy,
        auto_build=bool(isolation.get("auto_build_images", True)),
        immutable_image=configured_image,
        runner=runner,
    )
    if image_pin is not None:
        actual = runtime_cli.image_claude_version(ensured.image_id, runner=runner)
        if actual != str(image_pin):
            raise DockerImageError(f"image_pin mismatch: expected {image_pin!r}, got {actual!r}")
    return ensured


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _version_from_pin(image_pin: str) -> str:
    version = image_pin.strip().split(maxsplit=1)[0] if image_pin.strip() else ""
    if not version:
        raise DockerImageError("image_pin must contain a Claude CLI version")
    if _SEMVER_PIN_RE.fullmatch(version) is None:
        raise DockerImageError(f"image_pin version must match semver (X.Y.Z[-suffix]): {version!r}")
    return version


def _image_repository(image: str) -> str:
    if "@" in image:
        return image.split("@", 1)[0]
    prefix, separator, suffix = image.rpartition(":")
    if separator and "/" not in suffix:
        return prefix
    return image


def _main_root_path(main_root: Path, value: object) -> Path:
    relative = Path(str(value))
    if relative.is_absolute():
        raise DockerImageError(f"image cache path must be relative to main root: {relative}")
    root = main_root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise DockerImageError(f"image cache path escapes main root: {relative}")
    return resolved


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default
