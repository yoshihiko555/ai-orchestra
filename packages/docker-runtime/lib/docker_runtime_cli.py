#!/usr/bin/env python3
"""Shared Docker CLI, image inspection, and image build helpers.

Also hosts the shared implementation of image_pin semver validation and
matching (`version_token`/`is_bare_semver_pin`/`version_matches`/
`version_from_pin`) used by every Docker-backed harness namespace adapter
(meta-harness, loop-harness) so the comparison semantics stay identical.
"""

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
_DIGEST_IMAGE_RE = re.compile(r"@sha256:([0-9a-f]{64})\Z")
SEMVER_PIN_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$")


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


_ContextStatSignature = tuple[tuple[str, int, int], ...]
_CONTEXT_HASH_CACHE: dict[Path, tuple[_ContextStatSignature, str]] = {}


def _hashed_context_files(context_dir: Path) -> list[Path]:
    """Return the sorted, filtered file list that `context_hash` hashes.

    Shared between the hash computation below and the stat-based
    cache-invalidation signature in `_context_stat_signature` so the two
    file sets never drift apart.
    """
    return [
        path
        for path in sorted(context_dir.rglob("*"))
        if not path.is_symlink()
        and path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    ]


def _context_stat_signature(context_dir: Path, files: list[Path]) -> _ContextStatSignature:
    """Cheap per-file (relative path, mtime_ns, size) signature used to detect
    whether a context directory's contents changed since the last
    `context_hash` call (Issue #307 review). Stat'ing every file is far
    cheaper than reading and re-hashing their bytes, so this preserves the
    memoization's performance goal (avoiding repeated full SHA-256 passes
    over an unchanged context) while still invalidating on any real change.
    """
    return tuple(
        (path.relative_to(context_dir).as_posix(), path.stat().st_mtime_ns, path.stat().st_size)
        for path in files
    )


def context_hash(context_dir: Path) -> str:
    """Hash every tracked file under `context_dir`.

    Memoized per resolved path within this process (Issue #250 review): a
    single `_start_broker` call previously re-walked and re-hashed the same
    build context multiple times (once via `recipe_hash` inside
    `ensure_recipe_image`, again for diagnostic session fields). The cache
    entry also stores a cheap stat signature (relative path, mtime_ns, size)
    of every hashed file; if a context directory's contents change
    mid-process (e.g. a long-lived harness process, or tests that edit fixture
    files), the signature mismatch automatically invalidates the cache and
    recomputes the digest instead of returning a stale value (Issue #307
    review). `clear_context_hash_cache()` remains available for tests that
    want to force a full recompute unconditionally.
    """
    resolved = context_dir.resolve()
    files = _hashed_context_files(context_dir)
    signature = _context_stat_signature(context_dir, files)
    cached = _CONTEXT_HASH_CACHE.get(resolved)
    if cached is not None and cached[0] == signature:
        return cached[1]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(context_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    result = digest.hexdigest()
    _CONTEXT_HASH_CACHE[resolved] = (signature, result)
    return result


def clear_context_hash_cache() -> None:
    """Test-only hook: clear the process-local `context_hash` memoization
    cache so tests that mutate context directories (or run in the same
    process across cases sharing a context path) don't observe stale hashes.

    Not required for correctness anymore -- `context_hash` now detects stale
    entries itself via a stat signature -- but kept so existing tests that
    call it between mutation and the next `context_hash` call keep working
    unchanged.
    """
    _CONTEXT_HASH_CACHE.clear()


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
    if kind == "image":
        return "no such image:" in detail
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


def version_token(value: str | None) -> str:
    """Extract the leading version token so bare semver image_pin values (e.g.
    "2.1.207") compare equal to full `claude --version` output (e.g.
    "2.1.207 (Claude Code)").

    Shared image_pin semver validation/matching implementation: every
    Docker-backed harness (meta-harness, loop-harness) delegates here so the
    comparison semantics stay identical across namespaces.
    """
    if value is None:
        return ""
    stripped = value.strip()
    return stripped.split(maxsplit=1)[0] if stripped else ""


def is_bare_semver_pin(pin: str) -> bool:
    """Shared image_pin semver validation: report whether `pin` is a bare
    semver string (X.Y.Z[-suffix]) with no wrapper text."""
    return SEMVER_PIN_RE.fullmatch(pin.strip()) is not None


def version_matches(actual: str | None, pin: str) -> bool:
    """Compare a reported `claude --version` string against a configured
    image_pin.

    Shared image_pin semver matching implementation. A bare semver pin (e.g.
    "2.1.207") matches via leading-token comparison so it accepts the fuller
    `claude --version` output (e.g. "2.1.207 (Claude Code)"). Any other pin
    format must match the reported version exactly, preserving the strict
    Docker capability contract: a prebuilt image reporting an unexpected
    wrapper (e.g. "2.1.207 (unexpected wrapper)") must fail closed rather
    than pass on a token match.
    """
    if actual is None:
        return False
    if is_bare_semver_pin(pin):
        return version_token(actual) == version_token(pin)
    return actual == pin


def version_from_pin(image_pin: str) -> str:
    """Extract and validate the Claude CLI version encoded in an image_pin,
    for use as a Docker build arg.

    Shared image_pin semver validation implementation. Raises `ValueError`
    (not `DockerCliError`, to avoid a dependency on Docker-operation state)
    if `image_pin` is empty or not a valid semver version; callers own
    translating that into their own image-lifecycle error type.
    """
    version = image_pin.strip().split(maxsplit=1)[0] if image_pin.strip() else ""
    if not version:
        raise ValueError("image_pin must contain a Claude CLI version")
    if SEMVER_PIN_RE.fullmatch(version) is None:
        raise ValueError(f"image_pin version must match semver (X.Y.Z[-suffix]): {version!r}")
    return version


def _trust_immutable_image(
    image: str,
    *,
    runner: SubprocessRunner,
    cache: ImageCache,
) -> None:
    if _DIGEST_IMAGE_RE.search(image) is None:
        raise DockerCliError(
            "prebuilt Docker images must use an immutable @sha256 digest: "
            + image
            + " (get one with: docker inspect --format '{{index .RepoDigests 0}}' <image>:<tag>)"
        )
    image_identifier = _inspect_image_id(image, runner=runner)
    if image_identifier is None:
        raise DockerCliError(
            f"required immutable Docker image is missing: {image} "
            f"(pull it first with: docker pull {image})"
        )
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
