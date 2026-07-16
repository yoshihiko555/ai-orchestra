#!/usr/bin/env python3
"""Persistent, content-addressed Docker image lifecycle helpers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import docker_runtime_cli as cli

SubprocessRunner = cli.SubprocessRunner

FILE_MODE = 0o600
DIR_MODE = 0o700
BUILD_TIMEOUT_SECONDS = 900
RECIPE_TAG_LENGTH = 12
_DIGEST_IMAGE_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
_HASH_TAG_RE = re.compile(r"^sha-([0-9a-f]{12})$")
_SAFE_BUILDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$")
_BUILDX_DRIVER_RE = re.compile(r"^Driver:\s*(\S+)", re.MULTILINE)


class DockerImageError(RuntimeError):
    """A required managed-image operation failed."""


def _is_timezone_aware_iso_timestamp(value: str) -> bool:
    """Return True if value parses as a timezone-aware ISO-8601 timestamp.

    Manifest pruning sorts last_used_at as text, so a malformed value (e.g.
    "zzzz") could otherwise outrank valid entries and cause a fresh image to
    be pruned. Requiring timezone-aware ISO timestamps keeps sort order safe.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


@dataclass(frozen=True)
class ImageRecipe:
    family: str
    repository: str
    context_dir: Path
    docker_label: str
    build_args: Mapping[str, str]
    platform: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class ImageCachePolicy:
    manifest_path: Path
    lock_path: Path
    keep_generations: int
    builder_name: str
    buildkit_cache_max_age: str
    buildkit_cache_max_size: str


@dataclass(frozen=True)
class ManifestEntry:
    image_id: str
    built_at: str
    last_used_at: str

    @classmethod
    def from_value(cls, recipe: str, value: object) -> ManifestEntry:
        if not isinstance(value, dict):
            raise DockerImageError(f"invalid image cache manifest entry: {recipe}")
        required = ("image_id", "built_at", "last_used_at")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise DockerImageError(f"invalid image cache manifest entry: {recipe}")
        for key in ("built_at", "last_used_at"):
            if not _is_timezone_aware_iso_timestamp(value[key]):
                raise DockerImageError(f"invalid image cache manifest entry: {recipe}")
        return cls(
            image_id=value["image_id"],
            built_at=value["built_at"],
            last_used_at=value["last_used_at"],
        )

    def to_value(self) -> dict[str, str]:
        return {
            "image_id": self.image_id,
            "built_at": self.built_at,
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True)
class EnsuredImage:
    image_id: str
    tag: str
    recipe_hash: str | None
    built: bool


def recipe_hash(recipe: ImageRecipe) -> str:
    """Hash every input that can change the resulting image."""
    _validate_recipe(recipe)
    value = {
        "build_args": [[key, str(recipe.build_args[key])] for key in sorted(recipe.build_args)],
        "context_hash": cli.context_hash(recipe.context_dir),
        "docker_label": recipe.docker_label,
        "platform": recipe.platform or "",
        "target": recipe.target or "",
    }
    normalized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ensure_recipe_image(
    recipe: ImageRecipe,
    policy: ImageCachePolicy,
    *,
    auto_build: bool = True,
    immutable_image: str | None = None,
    runner: SubprocessRunner = subprocess.run,
    clock: Callable[[], datetime] | None = None,
) -> EnsuredImage:
    """Return a verified image, building and pruning it when required."""
    _validate_recipe(recipe)
    _validate_policy(policy)
    if not auto_build:
        return _ensure_immutable_image(immutable_image, runner=runner)

    digest = recipe_hash(recipe)
    tag = recipe_tag(recipe, digest)
    now = (clock or _utc_now)().astimezone(UTC).isoformat()
    with exclusive_file_lock(policy.lock_path):
        manifest = _load_valid_manifest(policy.manifest_path, runner=runner)
        cached = manifest.get(digest)
        current_image_id = _inspect_image_id(tag, runner=runner)
        if cached is not None and current_image_id == cached.image_id:
            _tag_latest(tag, recipe.repository, runner=runner)
            manifest[digest] = ManifestEntry(cached.image_id, cached.built_at, now)
            _write_manifest(policy.manifest_path, manifest)
            return EnsuredImage(cached.image_id, tag, digest, built=False)

        _ensure_builder(policy.builder_name, runner=runner)
        _build_image(recipe, policy, digest, tag, runner=runner)
        image_id = _inspect_image_id(tag, runner=runner)
        if image_id is None:
            raise DockerImageError(f"could not resolve freshly built Docker image ID: {tag}")
        _tag_latest(tag, recipe.repository, runner=runner)
        manifest[digest] = ManifestEntry(image_id, now, now)
        _write_manifest(policy.manifest_path, manifest)
        manifest = _prune_image_family(recipe, policy, manifest, runner=runner)
        _write_manifest(policy.manifest_path, manifest)
        _prune_buildkit_cache(policy, runner=runner)
        return EnsuredImage(image_id, tag, digest, built=True)


def recipe_tag(recipe: ImageRecipe, digest: str) -> str:
    return f"{recipe.repository}:sha-{digest[:RECIPE_TAG_LENGTH]}"


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize manifest check/build/update across driver processes."""
    _ensure_private_directory(path.parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
        try:
            os.chmod(path, FILE_MODE)
            stream = os.fdopen(fd, "a+", encoding="utf-8")
        except OSError:
            os.close(fd)
            raise
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError:
            stream.close()
            raise
    except OSError as exc:
        raise DockerImageError(f"could not lock Docker image build: {path}") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def parse_size(value: str) -> int:
    """Parse Docker/config byte sizes such as 10g, 12.5GB, or 64MiB."""
    match = _SIZE_RE.fullmatch(value.strip())
    if match is None:
        raise DockerImageError(f"invalid Docker cache size: {value}")
    number = float(match.group(1))
    unit = match.group(2).lower()
    decimal_units = {"b": 1, "k": 1000, "kb": 1000, "m": 1000**2, "mb": 1000**2}
    decimal_units.update({"g": 1000**3, "gb": 1000**3, "t": 1000**4, "tb": 1000**4})
    binary_units = {"kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}
    multiplier = {**decimal_units, **binary_units}.get(unit)
    if multiplier is None:
        raise DockerImageError(f"invalid Docker cache size unit: {value}")
    return int(number * multiplier)


def _validate_recipe(recipe: ImageRecipe) -> None:
    if not recipe.family or not recipe.docker_label:
        raise DockerImageError("image family and Docker label must not be empty")
    if not recipe.repository or "@" in recipe.repository:
        raise DockerImageError(f"invalid managed image repository: {recipe.repository}")
    if not recipe.context_dir.is_dir() or not (recipe.context_dir / "Dockerfile").is_file():
        raise DockerImageError(f"Docker build context is missing: {recipe.context_dir}")


def _validate_policy(policy: ImageCachePolicy) -> None:
    if policy.keep_generations < 1:
        raise DockerImageError("image keep_generations must be at least 1")
    if _SAFE_BUILDER_RE.fullmatch(policy.builder_name) is None:
        raise DockerImageError(f"invalid buildx builder name: {policy.builder_name}")
    if not policy.buildkit_cache_max_age:
        raise DockerImageError("buildkit_cache_max_age must not be empty")
    parse_size(policy.buildkit_cache_max_size)


def _ensure_immutable_image(
    image: str | None,
    *,
    runner: SubprocessRunner,
) -> EnsuredImage:
    if image is None or _DIGEST_IMAGE_RE.search(image) is None:
        raise DockerImageError("auto-build disabled images must use an immutable @sha256 digest")
    image_id = _inspect_image_id(image, runner=runner)
    if image_id is None:
        raise DockerImageError(f"required immutable Docker image is missing: {image}")
    return EnsuredImage(image_id, image, None, built=False)


def _load_valid_manifest(
    path: Path,
    *,
    runner: SubprocessRunner,
) -> dict[str, ManifestEntry]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise DockerImageError(f"could not read Docker image cache manifest: {path}") from exc
    if not isinstance(value, dict):
        raise DockerImageError(f"invalid Docker image cache manifest: {path}")
    manifest: dict[str, ManifestEntry] = {}
    for digest, entry_value in value.items():
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            continue
        try:
            entry = ManifestEntry.from_value(digest, entry_value)
        except DockerImageError:
            continue
        if _inspect_image_id(entry.image_id, runner=runner) == entry.image_id:
            manifest[digest] = entry
    return manifest


def _write_manifest(path: Path, manifest: Mapping[str, ManifestEntry]) -> None:
    _ensure_private_directory(path.parent)
    payload = {digest: manifest[digest].to_value() for digest in sorted(manifest)}
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, FILE_MODE)
        os.replace(temp_path, path)
        os.chmod(path, FILE_MODE)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise DockerImageError(f"could not write Docker image cache manifest: {path}") from exc


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise DockerImageError(f"could not create Docker image cache directory: {path}") from exc


def _inspect_image_id(image: str, *, runner: SubprocessRunner) -> str | None:
    completed = cli.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        runner=runner,
        timeout=20,
    )
    image_id = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        return None
    return image_id


def _tag_latest(tag: str, repository: str, *, runner: SubprocessRunner) -> None:
    latest = f"{repository}:latest"
    completed = cli.run(["docker", "tag", tag, latest], runner=runner, timeout=20)
    if completed.returncode != 0:
        raise DockerImageError(f"could not update Docker image alias: {latest}")


def _ensure_builder(builder: str, *, runner: SubprocessRunner) -> None:
    inspected = cli.run(
        ["docker", "buildx", "inspect", builder],
        runner=runner,
        timeout=30,
    )
    if inspected.returncode == 0:
        match = _BUILDX_DRIVER_RE.search(inspected.stdout)
        driver = match.group(1) if match else None
        if driver != "docker-container":
            raise DockerImageError(
                f"buildx builder {builder!r} already exists with driver "
                f"{driver!r}, expected 'docker-container'; rename or remove "
                "the existing builder before reusing this name"
            )
        return
    created = cli.run(
        ["docker", "buildx", "create", "--name", builder, "--driver", "docker-container"],
        runner=runner,
        timeout=60,
    )
    if created.returncode != 0:
        raise DockerImageError(f"could not create dedicated buildx builder: {builder}")


def _build_image(
    recipe: ImageRecipe,
    policy: ImageCachePolicy,
    digest: str,
    tag: str,
    *,
    runner: SubprocessRunner,
) -> None:
    command = [
        "docker",
        "buildx",
        "build",
        "--builder",
        policy.builder_name,
        "--load",
        "--label",
        f"{recipe.docker_label}=image",
        "--label",
        f"{recipe.docker_label}.recipe-sha256={digest}",
        "-t",
        tag,
    ]
    for key in sorted(recipe.build_args):
        command.extend(["--build-arg", f"{key}={recipe.build_args[key]}"])
    if recipe.platform:
        command.extend(["--platform", recipe.platform])
    if recipe.target:
        command.extend(["--target", recipe.target])
    command.append(str(recipe.context_dir))
    completed = cli.run(command, runner=runner, timeout=BUILD_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise DockerImageError(f"could not build required Docker image: {tag}")


def _prune_image_family(
    recipe: ImageRecipe,
    policy: ImageCachePolicy,
    manifest: dict[str, ManifestEntry],
    *,
    runner: SubprocessRunner,
) -> dict[str, ManifestEntry]:
    completed = cli.run(
        [
            "docker",
            "image",
            "ls",
            "--filter",
            f"label={recipe.docker_label}=image",
            "--format",
            "{{json .}}",
        ],
        runner=runner,
        timeout=30,
    )
    if completed.returncode != 0:
        raise DockerImageError(f"could not list managed Docker images: {recipe.family}")
    candidates = _family_candidates(completed.stdout, recipe.repository, manifest)
    tracked = [candidate for candidate in candidates if candidate[1] is not None]
    retained = sorted(tracked, key=lambda item: item[2], reverse=True)[: policy.keep_generations]
    retained_refs = {item[0] for item in retained}
    updated = dict(manifest)
    for image_ref, digest, _last_used in candidates:
        if digest is None:
            # Not recorded in this manifest (e.g. another project's build
            # sharing the same repository/label). Never delete tags we don't own.
            continue
        if image_ref in retained_refs:
            continue
        removed = cli.run(["docker", "image", "rm", image_ref], runner=runner, timeout=60)
        if removed.returncode != 0:
            raise DockerImageError(f"could not prune managed Docker image: {image_ref}")
        updated.pop(digest, None)
    return updated


def _family_candidates(
    output: str,
    repository: str,
    manifest: Mapping[str, ManifestEntry],
) -> list[tuple[str, str | None, str]]:
    candidates: list[tuple[str, str | None, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            value: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DockerImageError("invalid output from docker image ls") from exc
        if not isinstance(value, dict) or value.get("Repository") != repository:
            continue
        tag = value.get("Tag")
        if not isinstance(tag, str) or _HASH_TAG_RE.fullmatch(tag) is None:
            continue
        prefix = tag.removeprefix("sha-")
        matches = [digest for digest in manifest if digest.startswith(prefix)]
        digest = matches[0] if len(matches) == 1 else None
        last_used = manifest[digest].last_used_at if digest is not None else ""
        candidates.append((f"{repository}:{tag}", digest, last_used))
    return candidates


def _prune_buildkit_cache(policy: ImageCachePolicy, *, runner: SubprocessRunner) -> None:
    _run_buildkit_prune(policy.builder_name, policy.buildkit_cache_max_age, runner=runner)
    usage = cli.run(
        ["docker", "buildx", "du", "--builder", policy.builder_name],
        runner=runner,
        timeout=60,
    )
    if usage.returncode != 0:
        raise DockerImageError(f"could not inspect buildx cache usage: {policy.builder_name}")
    used_bytes = _buildkit_total_bytes(usage.stdout)
    if used_bytes > parse_size(policy.buildkit_cache_max_size):
        _run_buildkit_prune(policy.builder_name, "0", runner=runner)


def _run_buildkit_prune(builder: str, age: str, *, runner: SubprocessRunner) -> None:
    completed = cli.run(
        [
            "docker",
            "buildx",
            "prune",
            "--builder",
            builder,
            "--force",
            "--filter",
            f"until={age}",
        ],
        runner=runner,
        timeout=300,
    )
    if completed.returncode != 0:
        raise DockerImageError(f"could not prune buildx cache: {builder}")


def _buildkit_total_bytes(output: str) -> int:
    for line in reversed(output.splitlines()):
        match = re.match(r"^\s*Total:\s*(\S+)\s*$", line, flags=re.IGNORECASE)
        if match:
            return parse_size(match.group(1))
    raise DockerImageError("could not parse buildx cache usage")


def _utc_now() -> datetime:
    return datetime.now(UTC)
