#!/usr/bin/env python3
"""Persistent, content-addressed Docker image lifecycle helpers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import docker_runtime_cli as cli

SubprocessRunner = cli.SubprocessRunner

_LOGGER = logging.getLogger(__name__)

FILE_MODE = 0o600
DIR_MODE = 0o700
BUILD_TIMEOUT_SECONDS = 900
RECIPE_TAG_LENGTH = 12
# Issue #231: throttle for the opportunistic stale-image cleanup that runs on
# every `ensure_recipe_image` call (see `_cleanup_stale_owned_images`). A
# pending record that is a genuine *stale* candidate (past the liveness
# checks below) always forces a cleanup regardless of this TTL; a pending
# record that still looks like it could be an in-flight build does not.
CLEANUP_TTL_SECONDS = 6 * 60 * 60
# Extra grace period beyond BUILD_TIMEOUT_SECONDS before a pending record
# with no live family-lock holder is treated as abandoned rather than
# possibly still finishing up post-build steps (clock skew, slow `docker
# tag`/manifest write) (Issue #231 review, defense in depth alongside the
# family-lock liveness probe in `_family_build_in_progress`).
PENDING_LIVENESS_GRACE_SECONDS = 5 * 60
# Sidecar journal recording in-flight builds, kept next to `manifest_path`
# (e.g. `docker-image-cache.json` -> `docker-image-cache.pending.json`) so a
# crash between a successful `--load` and the manifest write still leaves
# proof of ownership behind (Issue #231 scenario 2).
PENDING_JOURNAL_SUFFIX = ".pending.json"
# Sidecar ledger recording a short-lived "pin lease" for every `image_id`
# `ensure_recipe_image()` hands back to a caller (e.g.
# `docker-image-cache.json` -> `docker-image-cache.pins.json`). A same-tag
# rebuild by a concurrent `ensure_recipe_image()` caller can make *this*
# image_id dangling in the window between a caller resolving it and actually
# starting a container from it (meta-harness: `image_id` is captured up
# front, containers are started later). While an image_id's lease is
# unexpired, opportunistic dangling cleanup must never remove it (Issue #231
# review, PR #320).
PIN_LEDGER_SUFFIX = ".pins.json"
IMAGE_ID_LEASE_TTL_SECONDS = 6 * 60 * 60
_DANGLING_MARKER = "<none>"
_DIGEST_IMAGE_RE = re.compile(r"@sha256:[0-9a-f]{64}\Z")
_HASH_TAG_RE = re.compile(r"^sha-([0-9a-f]{12})$")
_SAFE_BUILDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$")
# Docker repository name grammar (simplified): lowercase alnum segments,
# optionally separated by `.`, `_`/`__`, or `-`, with `/`-separated
# namespace components, plus an optional leading `registry-host[:port]/`
# prefix (the only place a `:` is ever valid outside the tag separator --
# e.g. `registry.example:5000/team/scenario`, already supported by the
# loop-harness `image` config; Issue #231 review). The registry host may
# also be a bracketed IPv6 literal (`[2001:db8::1]:5000`), which Docker's
# own reference grammar accepts (PR #320 review). Matches every shape
# `recipe_tag()`/`image_repository` can ever legitimately produce; used to
# reject a corrupted or adversarial pending-journal tag before it is ever
# passed to `docker image rm` (Issue #231 review).
_REPOSITORY_COMPONENT_RE = r"[a-z0-9]+(?:(?:\.|_{1,2}|-+)[a-z0-9]+)*"
_REGISTRY_HOST_RE = (
    r"(?:\[[0-9a-fA-F:]+\]|[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)"
    r"(?::[0-9]+)?"
)
_REPOSITORY_RE = re.compile(
    rf"^(?:{_REGISTRY_HOST_RE}/)?{_REPOSITORY_COMPONENT_RE}(?:/{_REPOSITORY_COMPONENT_RE})*$"
)
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
    # Populated only when the caller already resolved a verified `claude
    # --version` output for this image (currently: the meta-harness scenario
    # image adapter, when `image_pin` is configured). Lets callers reuse an
    # already-known version instead of launching another container just to
    # look it up again (Issue #307 review).
    claude_version: str | None = None


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
    """Return a verified image, building and pruning it when required.

    Locking is split in two scopes (Issue #250):

    - The manifest is protected by a short-held lock on `policy.lock_path`,
      taken only while reading/validating it or while writing it back. This
      keeps concurrent reads/writes from different families consistent
      without serializing on the build.
    - The build itself (`docker buildx build`, up to `BUILD_TIMEOUT_SECONDS`)
      is serialized only against other builds of the *same* `recipe.family`,
      via a lock file derived from `policy.lock_path` and `recipe.family`.
      Unrelated families (e.g. "scenario" and "broker") sharing the same
      `policy` never block each other's build.
    """
    _validate_recipe(recipe)
    _validate_policy(policy)
    now_dt = _now(clock)

    # Best-effort, throttled reclaim of images this recipe's label owns but
    # no longer has a live claim on (Issue #231). Runs on every call --
    # including the fast reuse-cache-hit path below and, since PR #320
    # review, the immutable-image (`auto_build=False`) path too -- so
    # residue from a prior auto-build (before a config switched to
    # immutable digests) still gets reclaimed instead of leaking forever,
    # matching the ADR's "cleanup always runs at the top of
    # ensure_recipe_image" invariant.
    _cleanup_stale_owned_images_best_effort(recipe, policy, now_dt, runner=runner)

    if not auto_build:
        return _ensure_immutable_image(immutable_image, runner=runner)

    digest = recipe_hash(recipe)
    tag = recipe_tag(recipe, digest)
    now = now_dt.isoformat()

    image_id = _reuse_cached_image(recipe, policy, digest, tag, now, runner=runner)
    if image_id is not None:
        # Lease expiry is computed from a *fresh* clock read (PR #320
        # review), not the `now_dt` captured before cleanup/build: cleanup,
        # an unbounded family-lock wait, and up to BUILD_TIMEOUT_SECONDS of
        # build time can all elapse before a return statement is reached,
        # and a lease computed from a stale timestamp could already be
        # expired by the time it's written.
        _lease_image_id(policy, image_id, _now(clock))
        return EnsuredImage(image_id, tag, digest, built=False)

    with exclusive_file_lock(_family_lock_path(policy.lock_path, recipe.family)):
        # Another process building the same family may have already produced
        # this recipe while this process waited for the family build lock.
        image_id = _reuse_cached_image(recipe, policy, digest, tag, now, runner=runner)
        if image_id is not None:
            _lease_image_id(policy, image_id, _now(clock))
            return EnsuredImage(image_id, tag, digest, built=False)

        _ensure_builder(policy.builder_name, runner=runner)
        # Record the tag as in-flight *before* invoking buildx, so a crash
        # between a successful `--load` and the manifest write (Issue #231
        # scenario 2) still leaves proof of ownership behind for the next
        # `ensure_recipe_image` call's opportunistic cleanup to reclaim.
        _record_pending_build(policy, recipe, tag, digest, now)
        # Capture whatever `tag` already resolves to *before* attempting the
        # build (PR #320 review): the same content-addressed tag can already
        # exist in the shared Docker daemon (e.g. built by a different
        # checkout) even though it is unknown to this checkout's local
        # manifest. If the build then fails, this lets the failure handler
        # tell "we just created/replaced this tag" apart from "this tag
        # already belonged to someone else and our failed build never
        # touched it" -- only the former may ever be deleted.
        pre_build_image_id = _inspect_image_id(tag, runner=runner)
        try:
            _build_image(recipe, policy, digest, tag, runner=runner)
            image_id = _inspect_image_id(tag, runner=runner)
            if image_id is None:
                raise DockerImageError(f"could not resolve freshly built Docker image ID: {tag}")
            _tag_latest(tag, recipe.repository, runner=runner)
        except Exception:
            # Never mask the original failure with a cleanup error.
            _best_effort_remove_pending_tag(policy, tag, pre_build_image_id, runner=runner)
            raise
        with exclusive_file_lock(policy.lock_path):
            # Re-read from disk (rather than reusing an earlier in-memory
            # snapshot) so a concurrent write by a different family is
            # merged into, not clobbered by, this write.
            manifest = _load_valid_manifest(policy.manifest_path, runner=runner)
            manifest[digest] = ManifestEntry(image_id, now, now)
            manifest = _prune_image_family(recipe, policy, manifest, runner=runner)
            _write_manifest(policy.manifest_path, manifest)
            _clear_pending_entry(policy, tag)
        _prune_buildkit_cache(policy, runner=runner)
        _lease_image_id(policy, image_id, _now(clock))
        return EnsuredImage(image_id, tag, digest, built=True)


def _now(clock: Callable[[], datetime] | None) -> datetime:
    return (clock or _utc_now)().astimezone(UTC)


def _reuse_cached_image(
    recipe: ImageRecipe,
    policy: ImageCachePolicy,
    digest: str,
    tag: str,
    now: str,
    *,
    runner: SubprocessRunner,
) -> str | None:
    """Return the cached image ID if `digest` is still a valid cache hit,
    refreshing `last_used_at` and the `:latest` alias; otherwise return None.

    Holds only the short-lived manifest lock (`policy.lock_path`), never the
    per-family build lock, so a cache-hit check never waits behind an
    unrelated family's in-progress build.
    """
    with exclusive_file_lock(policy.lock_path):
        manifest = _load_valid_manifest(policy.manifest_path, runner=runner, verify_digest=digest)
        cached = manifest.get(digest)
        if cached is None:
            return None
        current_image_id = _inspect_image_id(tag, runner=runner)
        if current_image_id != cached.image_id:
            return None
        # Skip the `docker tag` mutation entirely when `:latest` already
        # resolves to this exact image (the common case on repeated cache
        # hits) -- avoids a needless Docker CLI write on every cache hit
        # (Issue #307 review).
        latest_ref = f"{recipe.repository}:latest"
        if _inspect_image_id(latest_ref, runner=runner) != current_image_id:
            _tag_latest(tag, recipe.repository, runner=runner)
        manifest[digest] = ManifestEntry(cached.image_id, cached.built_at, now)
        _write_manifest(policy.manifest_path, manifest)
        return cached.image_id


def _family_lock_path(lock_path: Path, family: str) -> Path:
    """Derive a per-family build-lock path from the shared manifest lock.

    `family` is validated (`_SAFE_BUILDER_RE`, same charset as builder names)
    before being interpolated into a filesystem path.
    """
    if _SAFE_BUILDER_RE.fullmatch(family) is None:
        raise DockerImageError(f"invalid image family name: {family}")
    return lock_path.with_name(f"{lock_path.name}.{family}")


# --- Shared namespace-adapter config helpers (Issue #250 Fix A) ---
#
# `scenario_docker_image.py` (meta-harness) and `loop_docker_image.py`
# (loop-harness) each translate a harness-specific config block into
# `ImageRecipe` / `ImageCachePolicy`. The path-safety and cache-policy
# construction logic below used to be duplicated verbatim in both adapters;
# it now lives here once, with each adapter supplying only its own
# namespace's defaults.


def mapping(value: object) -> dict[str, Any]:
    """Coerce a config value to a dict, defaulting to `{}` for anything else."""
    return dict(value) if isinstance(value, dict) else {}


def image_repository(image: str) -> str:
    """Strip an optional `:tag` and/or `@sha256:...` digest suffix, returning
    the bare repository name recipes/tags are keyed on."""
    if "@" in image:
        image = image.split("@", 1)[0]
    prefix, separator, suffix = image.rpartition(":")
    if separator and "/" not in suffix:
        return prefix
    return image


def resolve_cache_path(main_root: Path, relative: object) -> Path:
    """Resolve an `image_cache` config path under `main_root`.

    Rejects absolute paths, paths that resolve outside `main_root`, and any
    symlinked path component between `main_root` and the leaf (checked via
    `lstat`, i.e. without following symlinks, before `resolve()` is ever
    called on the full path). Otherwise a symlinked ancestor -- e.g. an
    `.claude/<namespace>` directory replaced with a symlink -- would be
    silently followed by `resolve()`, letting the path escape to an
    arbitrary location (still passing `is_relative_to(root)` because
    `resolve()` already followed the symlink) and later be overwritten by
    the manifest/lock writer.
    """
    rel_path = Path(str(relative))
    if rel_path.is_absolute():
        raise DockerImageError(f"image cache path must be relative to main root: {rel_path}")
    root = main_root.resolve()
    candidate = root / rel_path
    _reject_symlinked_ancestors(root, rel_path)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise DockerImageError(f"image cache path escapes main root: {rel_path}")
    return resolved


def _reject_symlinked_ancestors(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DockerImageError(f"image cache path must not be a symlink: {relative}")


def build_cache_policy(
    main_root: Path,
    cache: Mapping[str, Any],
    *,
    defaults: Mapping[str, Any],
) -> ImageCachePolicy:
    """Construct an `ImageCachePolicy` from a namespace's `image_cache` config
    block, applying that namespace's own fallback defaults.

    `defaults` must provide: manifest_path, lock_path, keep_generations,
    builder_name, buildkit_cache_max_age, buildkit_cache_max_size.
    """
    return ImageCachePolicy(
        manifest_path=resolve_cache_path(
            main_root, cache.get("manifest_path", defaults["manifest_path"])
        ),
        lock_path=resolve_cache_path(main_root, cache.get("lock_path", defaults["lock_path"])),
        keep_generations=_positive_int(cache.get("keep_generations"), defaults["keep_generations"]),
        builder_name=str(cache.get("builder_name") or defaults["builder_name"]),
        buildkit_cache_max_age=str(
            cache.get("buildkit_cache_max_age") or defaults["buildkit_cache_max_age"]
        ),
        buildkit_cache_max_size=str(
            cache.get("buildkit_cache_max_size") or defaults["buildkit_cache_max_size"]
        ),
    )


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def recipe_tag(recipe: ImageRecipe, digest: str) -> str:
    return f"{recipe.repository}:sha-{digest[:RECIPE_TAG_LENGTH]}"


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize manifest check/build/update across driver processes.

    `os.O_NOFOLLOW` makes the `open()` fail closed (`ELOOP`, wrapped below
    into `DockerImageError`) if `path` has been replaced with a symlink
    between calls (TOCTOU), instead of transparently following it into
    locking and `chmod`-ing an attacker-controlled target.
    """
    _ensure_private_directory(path.parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, FILE_MODE)
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
    if _SAFE_BUILDER_RE.fullmatch(recipe.family) is None:
        # `family` is interpolated into the per-family build-lock filename
        # (see `_family_lock_path`), so it must be filesystem-safe.
        raise DockerImageError(f"invalid image family name: {recipe.family}")
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
    _validate_distinct_cache_paths(policy)


def _validate_distinct_cache_paths(policy: ImageCachePolicy) -> None:
    """Reject a `policy` whose `manifest_path`/`lock_path` config override
    collides with either sidecar file `_pending_journal_path`/
    `_pin_ledger_path` derive from `manifest_path`'s stem (PR #320 review).

    `_atomic_write_json` publishes sidecar writes via a temp-file-then-
    `os.replace` swap. If `lock_path` happened to equal one of those sidecar
    paths, a sidecar write would silently replace the very inode
    `exclusive_file_lock` is holding a lock on: a concurrent process could
    then lock the *new* inode `os.replace` just swapped in, and mutual
    exclusion between the two processes would be lost without either side
    ever observing an error. Fail closed instead.
    """
    journal_path = _pending_journal_path(policy.manifest_path)
    pin_ledger_path = _pin_ledger_path(policy.manifest_path)
    candidates = (
        ("manifest_path", policy.manifest_path),
        ("lock_path", policy.lock_path),
        ("pending journal path (derived from manifest_path)", journal_path),
        ("pin ledger path (derived from manifest_path)", pin_ledger_path),
    )
    seen: dict[Path, str] = {}
    for label, path in candidates:
        if path in seen:
            raise DockerImageError(
                f"image cache path conflict: {seen[path]} and {label} both resolve to {path}"
            )
        seen[path] = label


def _ensure_immutable_image(
    image: str | None,
    *,
    runner: SubprocessRunner,
) -> EnsuredImage:
    if image is None or _DIGEST_IMAGE_RE.search(image) is None:
        raise DockerImageError(
            "auto-build disabled images must use an immutable @sha256 digest "
            "(get one with: docker inspect --format '{{index .RepoDigests 0}}' <image>:<tag>)"
        )
    image_id = _inspect_image_id(image, runner=runner)
    if image_id is None:
        raise DockerImageError(
            f"required immutable Docker image is missing: {image} "
            f"(pull it first with: docker pull {image})"
        )
    return EnsuredImage(image_id, image, None, built=False)


def _load_valid_manifest(
    path: Path,
    *,
    runner: SubprocessRunner,
    verify_digest: str | None = None,
) -> dict[str, ManifestEntry]:
    """Load and schema-validate every manifest entry.

    `docker image inspect` is only run for `verify_digest` (the recipe
    currently being resolved), instead of every entry on every call, to
    avoid an O(entries) Docker CLI fan-out on each `ensure_recipe_image`
    (Issue #250). Other schema-valid entries are trusted without
    re-verifying Docker image existence: pruning (`_prune_image_family`)
    independently cross-checks manifest tags against `docker image ls`, and
    drift on any individual recipe is still caught the next time that
    recipe's digest is passed here as `verify_digest`.
    """
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
        if verify_digest is None or digest != verify_digest:
            manifest[digest] = entry
            continue
        if _inspect_image_id(entry.image_id, runner=runner) == entry.image_id:
            manifest[digest] = entry
    return manifest


def _write_manifest(path: Path, manifest: Mapping[str, ManifestEntry]) -> None:
    payload = {digest: manifest[digest].to_value() for digest in sorted(manifest)}
    _atomic_write_json(
        path, payload, error_message=f"could not write Docker image cache manifest: {path}"
    )


def _atomic_write_json(path: Path, payload: object, *, error_message: str) -> None:
    """Write `payload` as JSON to `path` via a temp-file-then-`os.replace`
    swap, so readers never observe a partially written file.

    `tempfile.mkstemp()` itself is wrapped too (PR #320 review): a raw,
    unwrapped `OSError` from it (e.g. `ENOSPC`) would otherwise slip past
    every caller's `except DockerImageError` (best-effort cleanup after a
    build failure, the pin-lease writer, ...), replacing an already-in-
    flight exception -- or failing an otherwise-successful `ensure_recipe_image`
    call outright -- instead of being handled the same way as every other
    I/O failure in this module.
    """
    _ensure_private_directory(path.parent)
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    except OSError as exc:
        raise DockerImageError(error_message) from exc
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
        raise DockerImageError(error_message) from exc


def _pending_journal_path(manifest_path: Path) -> Path:
    """Sidecar journal path for `manifest_path` (Issue #231).

    Kept separate from the manifest file itself so the manifest's on-disk
    schema never has to change: `docker-image-cache.json` gets a
    `docker-image-cache.pending.json` sibling.
    """
    return manifest_path.with_name(f"{manifest_path.stem}{PENDING_JOURNAL_SUFFIX}")


def _load_pending_journal(path: Path) -> dict[str, Any]:
    """Load the pending-build journal, tolerating a missing or corrupt file.

    Unlike the manifest (whose integrity failures must be surfaced, since a
    corrupt manifest could otherwise hide a stale cache hit), the journal is
    purely an optimization for reclaiming leaked images; a corrupt or
    missing journal simply means "nothing known to be pending".
    """
    empty: dict[str, Any] = {"entries": {}, "last_cleanup_at": None}
    if not path.exists():
        return empty
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return empty
    if not isinstance(value, dict):
        return empty
    entries = value.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    cleaned = {
        tag: entry
        for tag, entry in entries.items()
        if isinstance(tag, str) and isinstance(entry, dict)
    }
    last_cleanup_at = value.get("last_cleanup_at")
    return {
        "entries": cleaned,
        "last_cleanup_at": last_cleanup_at if isinstance(last_cleanup_at, str) else None,
    }


def _write_pending_journal(path: Path, journal: Mapping[str, Any]) -> None:
    _atomic_write_json(
        path, journal, error_message=f"could not write Docker image pending-build journal: {path}"
    )


def _record_pending_build(
    policy: ImageCachePolicy,
    recipe: ImageRecipe,
    tag: str,
    digest: str,
    now: str,
) -> None:
    """Record `tag` as an in-flight build (Issue #231 scenario 2).

    Must be called *before* `_build_image`, while only the per-family build
    lock is held; this self-acquires the (short-lived) manifest lock to keep
    journal writes serialized the same way manifest writes are.
    """
    path = _pending_journal_path(policy.manifest_path)
    with exclusive_file_lock(policy.lock_path):
        journal = _load_pending_journal(path)
        journal["entries"][tag] = {
            "digest": digest,
            "family": recipe.family,
            "repository": recipe.repository,
            "docker_label": recipe.docker_label,
            "started_at": now,
        }
        _write_pending_journal(path, journal)


def _clear_pending_entry(policy: ImageCachePolicy, tag: str) -> None:
    """Consume a pending-build record once `tag` is safely registered in the
    manifest.

    Callers must already hold `policy.lock_path` (e.g. immediately after
    `_write_manifest`, inside the same locked block) so the manifest write
    and the journal consumption stay consistent with concurrent cleanup.
    """
    path = _pending_journal_path(policy.manifest_path)
    journal = _load_pending_journal(path)
    if journal["entries"].pop(tag, None) is not None:
        _write_pending_journal(path, journal)


def _best_effort_remove_pending_tag(
    policy: ImageCachePolicy,
    tag: str,
    pre_build_image_id: str | None,
    *,
    runner: SubprocessRunner,
) -> None:
    """Reclaim `tag` after a build failure (Issue #231).

    Called from the `except` branch around `_build_image`/post-build steps
    in `ensure_recipe_image`, while only the per-family build lock is held.
    Never raises: the caller always re-raises the original build failure
    unchanged, and a cleanup problem here must not replace it.

    `tag` is only ever removed if this failed build attempt actually
    mutated it -- i.e. `tag` now resolves to a *different* image than
    `pre_build_image_id` (captured immediately before `_build_image` ran).
    The same content-addressed tag can already exist in the shared Docker
    daemon before this attempt even starts (e.g. built by a different
    checkout sharing the same repository), invisible to this checkout's
    local manifest; if this build failed without ever touching that
    pre-existing tag, deleting it would destroy someone else's valid cache
    (PR #320 review). The pending journal entry -- this process's own
    bookkeeping for an attempt that is now over either way -- is always
    dropped best-effort, but only once the Docker-image side is resolved:
    if removal was warranted and `_remove_image_best_effort` fails for a
    reason other than "already gone" (daemon hiccup, in-use), the pending
    record is kept so a later opportunistic cleanup can retry it -- dropping
    it unconditionally would otherwise permanently orphan a tagged,
    manifest-unregistered image with no other reclaim path (Issue #231
    review, PR #320).
    """
    current_image_id = _inspect_image_id(tag, runner=runner)
    tag_was_mutated_by_this_attempt = (
        current_image_id is not None and current_image_id != pre_build_image_id
    )
    if tag_was_mutated_by_this_attempt and not _remove_image_best_effort(tag, runner=runner):
        return
    path = _pending_journal_path(policy.manifest_path)
    try:
        with exclusive_file_lock(policy.lock_path):
            journal = _load_pending_journal(path)
            if journal["entries"].pop(tag, None) is not None:
                _write_pending_journal(path, journal)
    except DockerImageError as exc:
        _LOGGER.warning("could not update pending-build journal for %s: %s", tag, exc)


def _pin_ledger_path(manifest_path: Path) -> Path:
    """Sidecar pin-lease ledger path for `manifest_path` (Issue #231, PR
    #320). Kept separate from both the manifest and the pending journal so
    neither's on-disk schema has to change."""
    return manifest_path.with_name(f"{manifest_path.stem}{PIN_LEDGER_SUFFIX}")


def _load_pin_ledger(path: Path) -> dict[str, Any]:
    """Load the pin-lease ledger, tolerating a missing or corrupt file (same
    defensive stance as `_load_pending_journal`: this is purely a cleanup
    safety net, never a correctness-critical source of truth)."""
    empty: dict[str, Any] = {"leases": {}}
    if not path.exists():
        return empty
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return empty
    if not isinstance(value, dict):
        return empty
    leases = value.get("leases")
    if not isinstance(leases, dict):
        leases = {}
    cleaned = {
        image_id: expires_at
        for image_id, expires_at in leases.items()
        if isinstance(image_id, str) and isinstance(expires_at, str)
    }
    return {"leases": cleaned}


def _write_pin_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    _atomic_write_json(
        path, ledger, error_message=f"could not write Docker image pin ledger: {path}"
    )


def _lease_image_id(policy: ImageCachePolicy, image_id: str, now: datetime) -> None:
    """Record a pin lease for `image_id`, valid for `IMAGE_ID_LEASE_TTL_SECONDS`
    (Issue #231 review, PR #320).

    Called right before `ensure_recipe_image()` hands `image_id` back to its
    caller, at every return point. Self-acquires the (short-lived) manifest
    lock; safe to call while holding at most the per-family build lock (see
    call sites in `ensure_recipe_image`), matching the same family-then-
    manifest ordering used elsewhere in this module. Best-effort: a failure
    to record the lease is logged and does not fail `ensure_recipe_image`
    (the caller still gets its `EnsuredImage` either way; a missed lease
    only means one fewer opportunistic-cleanup guard, not incorrect
    behavior).
    """
    path = _pin_ledger_path(policy.manifest_path)
    expires_at = (now + timedelta(seconds=IMAGE_ID_LEASE_TTL_SECONDS)).isoformat()
    try:
        with exclusive_file_lock(policy.lock_path):
            ledger = _load_pin_ledger(path)
            ledger["leases"][image_id] = expires_at
            _write_pin_ledger(path, ledger)
    except DockerImageError as exc:
        _LOGGER.warning("could not record pin lease for %s: %s", image_id, exc)


def _purge_expired_pin_leases(path: Path, now: datetime) -> set[str]:
    """Return the set of image IDs with a still-active pin lease, purging
    any expired entries from the ledger on disk (Issue #231 review, PR
    #320). Caller must already hold `policy.lock_path`."""
    ledger = _load_pin_ledger(path)
    active = {
        image_id: expires_at
        for image_id, expires_at in ledger["leases"].items()
        if _is_timezone_aware_iso_timestamp(expires_at) and datetime.fromisoformat(expires_at) > now
    }
    if active != ledger["leases"]:
        _write_pin_ledger(path, {"leases": active})
    return set(active)


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
    if _inspect_builder(builder, runner=runner):
        return
    created = cli.run(
        ["docker", "buildx", "create", "--name", builder, "--driver", "docker-container"],
        runner=runner,
        timeout=60,
    )
    if created.returncode == 0:
        return
    # Two projects (different lock files) can race to create the same
    # global builder name on first use. The loser's `create` fails, but the
    # winner's builder is now usable; re-inspect before treating this as
    # fatal.
    if _inspect_builder(builder, runner=runner):
        return
    raise DockerImageError(f"could not create dedicated buildx builder: {builder}")


def _inspect_builder(builder: str, *, runner: SubprocessRunner) -> bool:
    """Return True if `builder` exists with the expected driver.

    Raises DockerImageError if it exists with an incompatible driver.
    """
    inspected = cli.run(
        ["docker", "buildx", "inspect", builder],
        runner=runner,
        timeout=30,
    )
    if inspected.returncode != 0:
        return False
    match = _BUILDX_DRIVER_RE.search(inspected.stdout)
    driver = match.group(1) if match else None
    if driver != "docker-container":
        raise DockerImageError(
            f"buildx builder {builder!r} already exists with driver "
            f"{driver!r}, expected 'docker-container'; rename or remove "
            "the existing builder before reusing this name"
        )
    return True


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
            # Best-effort: an old generation may still be in use (e.g. by a
            # running scenario container). The requested image was already
            # built and recorded, so a stale cleanup conflict must not fail
            # the whole `ensure_recipe_image` call; retry on the next call.
            _LOGGER.warning(
                "could not prune managed Docker image %s (left for a later attempt): %s",
                image_ref,
                (removed.stderr or removed.stdout or "").strip(),
            )
            continue
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


def _cleanup_stale_owned_images_best_effort(
    recipe: ImageRecipe,
    policy: ImageCachePolicy,
    now: datetime,
    *,
    runner: SubprocessRunner,
) -> None:
    """Outer safety net around `_cleanup_stale_owned_images` (Issue #231).

    `_cleanup_stale_owned_images` is already internally best-effort for every
    Docker call it makes; this just guarantees an unexpected error anywhere
    in it (e.g. an unreadable lock directory) can never fail the
    `ensure_recipe_image` call it's opportunistically piggybacking on.
    """
    try:
        _cleanup_stale_owned_images(recipe, policy, now, runner=runner)
    except Exception as exc:  # noqa: BLE001 - defense in depth, must never propagate
        _LOGGER.warning(
            "stale Docker image cleanup failed for label %s (continuing): %s",
            recipe.docker_label,
            exc,
        )


def _cleanup_stale_owned_images(
    recipe: ImageRecipe,
    policy: ImageCachePolicy,
    now: datetime,
    *,
    runner: SubprocessRunner,
) -> None:
    """Reclaim images owned by `recipe.docker_label` that no longer have a
    live claim on them (Issue #231):

    - tags left behind by a pending build whose manifest registration never
      happened (e.g. the process died right after `--load` succeeded,
      scenario 2), or whose manifest write did happen but the pending
      journal was never consumed (just drop the stale journal entry, no
      image to remove)
    - dangling (untagged) images left behind when a same-tag rebuild
      replaced an existing image (scenario 3); `_family_candidates` only
      ever considers *tagged* images, so a rebuild's predecessor is
      otherwise never revisited

    A pending record is only ever a *candidate*, never automatically
    "stale": `_partition_pending` cross-checks each one against the
    corresponding family's build lock (`_family_build_in_progress`) and a
    `BUILD_TIMEOUT_SECONDS`-based grace period (`_pending_entry_is_recent`)
    before it can be treated as abandoned, so an in-flight build's tag is
    never removed out from under it (Issue #231 review).

    Runs on every `ensure_recipe_image` call, but when there are no genuine
    stale candidates, throttled to once per `CLEANUP_TTL_SECONDS`: with
    nothing removable and the TTL not yet elapsed, this returns without
    issuing a single Docker command (Issue #231 review) -- a build merely
    being in flight for the same label must not turn every concurrent
    `ensure_recipe_image` caller's hot reuse path into a `docker image ls` +
    `buildx du` sweep.

    Candidate determination (journal read + `docker image ls`) happens under
    the manifest lock, but the `docker image rm` calls themselves run after
    releasing it, re-acquiring the lock only briefly afterward to consume
    the journal entries that were actually removed -- keeping the lock's
    hold time short even when there is real cleanup work to do.
    """
    journal_path = _pending_journal_path(policy.manifest_path)
    pin_ledger_path = _pin_ledger_path(policy.manifest_path)
    with exclusive_file_lock(policy.lock_path):
        journal = _load_pending_journal(journal_path)
        pending = {
            tag: entry
            for tag, entry in journal["entries"].items()
            if entry.get("docker_label") == recipe.docker_label
        }
        manifest = _load_valid_manifest(policy.manifest_path, runner=runner)
        stale_tags, resolved_tags, malformed_tags = _partition_pending(
            pending, manifest, policy, now, runner=runner
        )
        journal_changed = False
        for tag in (*resolved_tags, *malformed_tags):
            if journal["entries"].pop(tag, None) is not None:
                journal_changed = True
        for tag in malformed_tags:
            _LOGGER.warning("dropping malformed pending-journal tag reference: %s", tag)

        due = _cleanup_due(journal["last_cleanup_at"], now)
        if not stale_tags and not due:
            if journal_changed:
                _write_pending_journal(journal_path, journal)
            return

        try:
            images = _list_label_owned_images(recipe.docker_label, runner=runner)
            scan_succeeded = True
        except DockerImageError as exc:
            _LOGGER.warning(
                "could not list Docker images owned by %s for stale cleanup: %s",
                recipe.docker_label,
                exc,
            )
            images = []
            scan_succeeded = False
        dangling_ids = _dangling_image_ids(images)
        # Never remove a dangling image another `ensure_recipe_image()`
        # caller in *this* checkout has an unexpired pin lease on (Issue
        # #231 review, PR #320): a same-tag rebuild can make an already-
        # ensured image_id dangling before its caller gets around to
        # starting a container from it.
        active_pin_ids = _purge_expired_pin_leases(pin_ledger_path, now)
        dangling_ids = [image_id for image_id in dangling_ids if image_id not in active_pin_ids]

        if journal_changed:
            _write_pending_journal(journal_path, journal)

    removed_stale_tags: list[str] = []
    removed_count = 0
    for tag in stale_tags:
        family = pending.get(tag, {}).get("family")
        if not isinstance(family, str):
            continue
        # Acquire (and, crucially, *hold*) the family build lock for the
        # duration of the ownership check and the removal itself (Issue
        # #231 review, PR #320): the liveness judgment above ran under the
        # manifest lock, but `docker image rm` itself runs after releasing
        # it. A bare probe-then-release still leaves a TOCTOU window in
        # which a new build for this family could start, acquire the family
        # lock, and `--load` this very tag in between the probe and the
        # removal; holding the lock across both closes that window (a new
        # build for the same family cannot proceed past `_ensure_builder`
        # until this `with` block releases it).
        with _hold_family_lock_if_free(policy, family) as lock_held:
            if not lock_held:
                continue
            # Cross-checkout guard (Issue #231 review, partial mitigation):
            # a different checkout sharing this repository/tag has its own,
            # invisible-to-us family lock and pending journal. If the tag
            # was created inside the liveness window, treat it as that
            # checkout's recent rebuild/`--load` rather than our stale
            # candidate.
            if _image_recently_created(tag, now, runner=runner):
                continue
            if _remove_image_best_effort(tag, runner=runner):
                removed_stale_tags.append(tag)
                removed_count += 1
    for image_id in dangling_ids:
        if _remove_image_best_effort(image_id, runner=runner):
            removed_count += 1

    with exclusive_file_lock(policy.lock_path):
        journal = _load_pending_journal(journal_path)
        for tag in removed_stale_tags:
            # Only consume the journal entry if it still matches the stale
            # snapshot this removal decision was based on (Issue #231
            # review, PR #320): the manifest lock was released for the
            # entire removal above, so a new build for this same tag could
            # have already re-recorded a fresh pending entry
            # (`_record_pending_build`) in the meantime. An unconditional
            # `pop` would destroy that new build's bookkeeping; if it later
            # crashes after `--load`, its orphaned tag would then have no
            # record left to be reclaimed by.
            if _pending_entry_matches(journal["entries"].get(tag), pending.get(tag)):
                journal["entries"].pop(tag, None)
        # Only advance the TTL clock when the scan that backs it actually
        # succeeded (Issue #231 review): otherwise a transient `docker image
        # ls` failure would suppress the next real dangling-image sweep for
        # up to CLEANUP_TTL_SECONDS even after the daemon recovers.
        if scan_succeeded:
            journal["last_cleanup_at"] = now.isoformat()
        _write_pending_journal(journal_path, journal)

    if removed_count:
        _LOGGER.info(
            "opportunistic cleanup pruned %d stale Docker image(s) owned by %s",
            removed_count,
            recipe.docker_label,
        )
    _log_cleanup_usage(policy, runner=runner)


def _cleanup_due(last_cleanup_at: object, now: datetime) -> bool:
    if not isinstance(last_cleanup_at, str) or not _is_timezone_aware_iso_timestamp(
        last_cleanup_at
    ):
        return True
    elapsed = now - datetime.fromisoformat(last_cleanup_at)
    return elapsed.total_seconds() >= CLEANUP_TTL_SECONDS


def _partition_pending(
    pending: Mapping[str, dict[str, Any]],
    manifest: Mapping[str, ManifestEntry],
    policy: ImageCachePolicy,
    now: datetime,
    *,
    runner: SubprocessRunner,
) -> tuple[list[str], list[str], list[str]]:
    """Split label-scoped pending-journal tags into `stale` (a safe removal
    candidate), `resolved` (the manifest demonstrably already accounts for
    this exact build -- only the journal entry needs dropping, the image
    itself is now tracked normally), and `malformed` (does not even look
    like a tag `recipe_tag()` could have produced -- dropped from the
    journal, never passed to `docker image rm`).

    A candidate only ever becomes `stale` after clearing two independent
    liveness checks (Issue #231 review) -- either one being unable to
    positively rule out an in-flight build leaves the entry untouched in the
    journal for the next cleanup attempt to re-evaluate:

    - `_pending_entry_is_recent`: still within `BUILD_TIMEOUT_SECONDS +
      PENDING_LIVENESS_GRACE_SECONDS` of `started_at` (or `started_at` is
      unparsable -- fails safe toward "recent")
    - `_family_build_in_progress`: the recorded family's build lock could
      not be acquired non-blocking (or the family value is invalid -- fails
      safe toward "in progress")
    """
    stale: list[str] = []
    resolved: list[str] = []
    malformed: list[str] = []
    for tag, entry in pending.items():
        if not _is_removable_tag_reference(tag):
            malformed.append(tag)
            continue
        if _pending_entry_is_resolved(tag, entry, manifest, runner=runner):
            resolved.append(tag)
            continue
        if _pending_entry_is_recent(entry, now):
            continue
        family = entry.get("family")
        if not isinstance(family, str) or _family_build_in_progress(policy, family):
            continue
        stale.append(tag)
    return stale, resolved, malformed


def _pending_entry_is_resolved(
    tag: str,
    entry: Mapping[str, Any],
    manifest: Mapping[str, ManifestEntry],
    *,
    runner: SubprocessRunner,
) -> bool:
    """A pending record is only genuinely `resolved` -- safe to drop from
    the journal without ever touching Docker -- once the manifest entry it
    references demonstrably corresponds to *this* build, not merely to some
    other (possibly stale) entry that happens to share the same digest key
    (Issue #231 review, PR #320 second round).

    Content-addressing means the manifest can still hold an older image A
    under a digest while a *newer* build of the identical recipe produces
    image B, `--load`s it (replacing A's tag), and then crashes before the
    manifest write. Checking `digest in manifest` alone would see A's
    pre-existing entry and drop B's pending record as "resolved" -- even
    though B, not A, is now the tag's actual content, and the only proof of
    B's existence would just have been deleted. Requiring the manifest's
    recorded `image_id` to match what `tag` *currently* resolves to closes
    that gap: it only holds when the manifest genuinely describes the image
    the tag points to right now.
    """
    digest = entry.get("digest")
    if not isinstance(digest, str):
        return False
    manifest_entry = manifest.get(digest)
    if manifest_entry is None:
        return False
    return _inspect_image_id(tag, runner=runner) == manifest_entry.image_id


def _pending_entry_matches(
    current: Mapping[str, Any] | None, original: Mapping[str, Any] | None
) -> bool:
    """True if `current` (freshly re-read from the journal) is still the
    same pending record as `original` (the stale snapshot a removal
    decision was based on), compared on `digest` and `started_at` -- the
    fields that together identify a specific build attempt (Issue #231
    review, PR #320 second round).

    Used to guard journal-entry consumption after a stale tag's removal:
    the manifest lock is released for the entire removal, so a new build
    could have re-recorded this same tag (`_record_pending_build`) in the
    meantime. `current` would then differ from `original`, and the caller
    must leave it alone rather than destroying the new build's bookkeeping.
    """
    if current is None or original is None:
        return False
    return current.get("digest") == original.get("digest") and current.get(
        "started_at"
    ) == original.get("started_at")


def _pending_entry_is_recent(entry: Mapping[str, Any], now: datetime) -> bool:
    """Fail-safe defense in depth alongside `_family_build_in_progress`
    (Issue #231 review): even if the lock probe races or the recorded
    family is corrupted, a pending record younger than
    `BUILD_TIMEOUT_SECONDS + PENDING_LIVENESS_GRACE_SECONDS` is never
    treated as abandoned. An unparsable `started_at` fails safe (treated as
    recent, i.e. still possibly in-flight)."""
    started_at = entry.get("started_at")
    if not isinstance(started_at, str) or not _is_timezone_aware_iso_timestamp(started_at):
        return True
    elapsed = (now - datetime.fromisoformat(started_at)).total_seconds()
    return elapsed < BUILD_TIMEOUT_SECONDS + PENDING_LIVENESS_GRACE_SECONDS


def _family_build_in_progress(policy: ImageCachePolicy, family: str) -> bool:
    """Best-effort liveness probe (Issue #231 review): `ensure_recipe_image`
    holds `family`'s per-family build lock for the *entire* build, so a
    successful non-blocking (`LOCK_NB`) acquire here proves no build for
    `family` is currently in flight. Any failure to even probe the lock --
    an invalid `family` value, a permissions error, or the lock genuinely
    being held -- fails safe toward "still in progress", so a pending
    record is never removed out from under a build this probe could not
    positively rule out. Never blocks: this is called while the caller
    already holds the manifest lock, and a build waiting on that same
    manifest lock (e.g. inside `_record_pending_build`) must not deadlock
    against a blocking probe here.
    """
    try:
        lock_path = _family_lock_path(policy.lock_path, family)
        _ensure_private_directory(lock_path.parent)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, FILE_MODE)
    except (DockerImageError, OSError):
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextmanager
def _hold_family_lock_if_free(policy: ImageCachePolicy, family: str) -> Iterator[bool]:
    """Attempt to acquire `family`'s per-family build lock non-blocking and,
    if successful, hold it for the duration of the `with` block (Issue #231
    review, PR #320) -- unlike `_family_build_in_progress`, which is a bare
    probe used only to help decide *whether* a pending record is a
    candidate at all. This is used once a tag has already been judged a
    stale removal candidate, to keep a new build for the same family from
    starting (and `--load`ing the very tag about to be removed) partway
    through the ownership check and the removal itself.

    Yields `True` while holding the lock, or `False` (holding nothing) if it
    could not be acquired -- either because a build is already in progress,
    or because the probe itself failed (invalid `family`, permissions
    error); both fail safe toward "treat as in-flight, do not touch this
    tag". Never blocks: this is only ever called with no other lock held by
    this process at the same time (candidate determination, which does hold
    the manifest lock, has already finished and released it by this point).
    """
    try:
        lock_path = _family_lock_path(policy.lock_path, family)
        _ensure_private_directory(lock_path.parent)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, FILE_MODE)
    except (DockerImageError, OSError):
        yield False
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _is_removable_tag_reference(reference: str) -> bool:
    """Validate a journal-sourced tag before it is ever passed to `docker
    image rm` (Issue #231 review): must be shaped like `recipe_tag()`'s
    exact output, `<repository>:sha-<12 hex>`, so a corrupted or
    adversarial journal entry can never smuggle an arbitrary Docker CLI
    argument through."""
    repository, separator, tag = reference.rpartition(":")
    if not separator or not repository:
        return False
    return (
        _REPOSITORY_RE.fullmatch(repository) is not None and _HASH_TAG_RE.fullmatch(tag) is not None
    )


def _list_label_owned_images(
    docker_label: str, *, runner: SubprocessRunner
) -> list[dict[str, Any]]:
    # `--all` is required so dangling (`<none>:<none>`) images are included:
    # real `docker image ls` excludes them by default, which otherwise makes
    # `_dangling_image_ids` permanently see zero candidates (Issue #231 E2E
    # finding). `--no-trunc` is required so the returned `ID` is the full
    # `sha256:<64 hex>` form: without it, `docker image ls` truncates to 12
    # hex chars, which never matches the full IDs `_lease_image_id` records
    # in the pin ledger (from `docker image inspect`), silently defeating
    # the pin-lease protection below (PR #320 review).
    completed = cli.run(
        [
            "docker",
            "image",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            f"label={docker_label}=image",
            "--format",
            "{{json .}}",
        ],
        runner=runner,
        timeout=30,
    )
    if completed.returncode != 0:
        raise DockerImageError(f"could not list Docker images owned by label: {docker_label}")
    images: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            value: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DockerImageError("invalid output from docker image ls") from exc
        if isinstance(value, dict):
            images.append(value)
    return images


def _dangling_image_ids(images: list[dict[str, Any]]) -> list[str]:
    """Dangling images (`<none>:<none>`) that still carry the owner label are
    unreferenced by any tag, so they can be removed unconditionally -- no
    manifest/pending-journal cross-check is needed (Issue #231 scenario 3)."""
    ids: list[str] = []
    for value in images:
        if value.get("Repository") != _DANGLING_MARKER or value.get("Tag") != _DANGLING_MARKER:
            continue
        image_id = value.get("ID")
        if isinstance(image_id, str) and image_id:
            ids.append(image_id)
    return ids


def _remove_image_best_effort(reference: str, *, runner: SubprocessRunner) -> bool:
    """Best-effort `docker image rm`, returning whether `reference` is now
    known to be gone.

    An "image not found" failure counts as success (Issue #231 E2E finding):
    without this, a pending record for a tag that was never actually
    created (e.g. the build died before `--load` ever ran) would fail
    removal forever, spamming a warning on every cleanup with no way to
    ever consume the journal entry. Any other failure (e.g. still in use by
    a running container) is left for a later attempt, unchanged.
    """
    removed = cli.run(["docker", "image", "rm", reference], runner=runner, timeout=60)
    if removed.returncode == 0 or cli.reports_missing_resource(removed, kind="image"):
        return True
    _LOGGER.warning(
        "could not prune stale Docker image %s (left for a later attempt): %s",
        reference,
        (removed.stderr or removed.stdout or "").strip(),
    )
    return False


def _image_recently_created(reference: str, now: datetime, *, runner: SubprocessRunner) -> bool:
    """Best-effort cross-checkout liveness guard (Issue #231 review, PR
    #320, partial mitigation): another checkout sharing this repository/tag
    may have just rebuilt or `--load`ed it, invisible to this process's
    local family lock and pending journal. If `reference`'s `CreatedAt` is
    within the same liveness window used for pending records
    (`BUILD_TIMEOUT_SECONDS + PENDING_LIVENESS_GRACE_SECONDS`), treat it as
    live and skip removal.

    If the image plainly no longer exists, there is nothing left to
    protect -- returns False so the caller proceeds to
    `_remove_image_best_effort`, which already treats "already gone" as a
    successful removal. Any other inspect failure (daemon hiccup, an
    unparsable timestamp) fails safe (returns True: treat as recent, skip).
    """
    completed = cli.run(
        ["docker", "image", "inspect", "--format", "{{.Created}}", reference],
        runner=runner,
        timeout=20,
    )
    if completed.returncode != 0:
        return not cli.reports_missing_resource(completed, kind="image")
    created_at = _parse_docker_created_at(completed.stdout.strip())
    if created_at is None:
        return True
    elapsed = (now - created_at).total_seconds()
    return elapsed < BUILD_TIMEOUT_SECONDS + PENDING_LIVENESS_GRACE_SECONDS


def _parse_docker_created_at(value: str) -> datetime | None:
    """Parse `docker image inspect --format {{.Created}}`'s RFC3339Nano
    output (e.g. `2026-07-26T04:00:00.123456789Z`)."""
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _log_cleanup_usage(policy: ImageCachePolicy, *, runner: SubprocessRunner) -> None:
    """Informational BuildKit cache usage check after an opportunistic
    cleanup run. Purely diagnostic: never raises, and does not change
    `_prune_buildkit_cache`'s existing hard-fail behavior on the build path.
    """
    usage = cli.run(
        ["docker", "buildx", "du", "--builder", policy.builder_name],
        runner=runner,
        timeout=60,
    )
    if usage.returncode != 0:
        return
    try:
        used_bytes = _buildkit_total_bytes(usage.stdout)
        max_bytes = parse_size(policy.buildkit_cache_max_size)
    except DockerImageError:
        return
    if used_bytes > max_bytes:
        _LOGGER.warning(
            "BuildKit cache usage for builder %s is %d bytes, exceeding buildkit_cache_max_size (%s)",
            policy.builder_name,
            used_bytes,
            policy.buildkit_cache_max_size,
        )


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
