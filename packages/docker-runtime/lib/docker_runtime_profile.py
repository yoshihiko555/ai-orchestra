#!/usr/bin/env python3
"""Shared pure builders for hardened Docker command profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

_SAFE_NAME_RE = re.compile(r"[^a-z0-9_.-]+")


class DockerProfileError(RuntimeError):
    """A Docker mount/resource profile cannot be represented safely."""


# These defaults are looser than the pre-Phase-0 meta-harness values (40 and "-.").
# packages/meta-harness/lib/scenario_docker_profile.py overrides both to preserve
# historical behavior; new callers that omit them receive these shared defaults.
def safe_name(
    value: str,
    *,
    max_length: int = 48,
    strip_chars: str = "-._",
) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", value.lower()).strip(strip_chars)
    return (cleaned or "run")[:max_length]


def container_env_args(env: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in sorted(env.items()):
        args.extend(["--env", f"{key}={value}"])
    return args


def bind_mount(source: Path, target: str, *, read_only: bool) -> str:
    resolved = str(source.resolve())
    if "," in resolved:
        raise DockerProfileError(f"Docker bind source contains unsupported comma: {source}")
    options = ["type=bind", f"src={resolved}", f"dst={target}"]
    if read_only:
        options.append("readonly")
    return ",".join(options)


def tmpfs(target: str, uid: int, gid: int, *, size: str) -> str:
    return f"{target}:rw,noexec,nosuid,nodev,size={size},uid={uid},gid={gid},mode=0700"


def non_root_identity() -> tuple[int, int]:
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        return 65532, 65532
    return uid, gid


def align_mount_ownership(
    path: Path,
    *,
    exclude: frozenset[Path] | None = None,
    protect_owner_only: bool = True,
) -> None:
    """Re-own a read-write mount source so the forced non-root container identity can write it.

    ``non_root_identity()`` maps a root host process to the fixed ``65532:65532`` container
    identity (the container must never run as root -- that constraint is not negotiable).
    When the host process that prepared a bind-mount source is itself root, every file/directory
    it created keeps the host's default ownership (``root:root``), which the non-root container
    identity cannot write to even though the mount itself is read-write. This recursively
    re-owns ``path`` to ``non_root_identity()`` so the mount stays writable without weakening the
    container's non-root guarantee -- only the exact identity the container already runs as gains
    access; no permission bits are widened. No-op when the host process is not root, because in
    that case ``non_root_identity()`` already returns the host's own uid/gid and ownership already
    matches.

    ``exclude`` (Codex review, PR #262, High, round 4) skips specific leaf entries that must keep
    their original owner even under a root-run host: without it, this re-own could newly grant the
    fixed non-root container identity read access to a file whose stricter-than-usual permission
    bits (e.g. mode 600, owned by root) previously restricted it to the root-trusted host process
    only -- "no permission bits are widened" above only holds when the *owning identity* doesn't
    itself change from a more-trusted one to the less-trusted container identity. Callers pass
    project-local override files (`.claude/config/**/*.local.{yaml,json}`) here; their ancestor
    directories are intentionally still re-owned so the container can traverse them and create
    unrelated sibling entries.

    Codex review, PR #262, P2 (round 8, low severity): a plain ``Path`` membership check only
    excludes the exact paths the caller enumerated. A hardlink to the same excluded file, planted
    at a different path elsewhere under this same recursive `rglob()` walk, is a distinct
    directory entry with its own path but the *same inode* -- `child in excluded` would miss it
    and this function would happily re-own the excluded file's underlying inode through its
    hardlink alias, exposing its contents to the container identity via the alias path. Excluded
    entries are therefore also matched by ``(st_dev, st_ino)`` identity, not just by path, so a
    hardlink alias is skipped the same as the original path would be. (Creating such a hardlink
    requires the source `.local.*` file's inode to already be linkable from within the worktree
    tree, which most modern kernels restrict by default via `fs.protected_hardlinks`; this is
    defense in depth for hosts that have that hardening disabled.)

    Codex review, PR #262, P1 (round 10): the caller-supplied ``exclude`` set above only covers
    the specific `.local.*` override leaves the caller already knows about. A root-run host's
    mount source can also contain root-owned secrets the caller never enumerated -- `.env`,
    `.npmrc`, `.netrc`, credential files, etc. -- deliberately left at a restrictive mode (no
    group/other permission bits at all, e.g. `0600`) so only the root-trusted host process can
    read them. The unconditional recursive chown below used to re-own those too, handing the
    untrusted Maker container read/write access to a secret it never had, purely as a side effect
    of making the mount writable. An entry with no group/other permission bits was already
    restricted to its current owner only; changing that owner to the container identity would
    still grant that identity access it did not have before -- so such entries now keep their
    original owner and are skipped via `_is_owner_only_permission()`, the same way excluded leaf
    entries are skipped. Ordinary worktree content Maker legitimately needs to write (source
    files, `.git/` working data, etc.) is created at the usual `0644`/`0755` modes and is
    unaffected by this check.

    Codex review, PR #262, P1 (round 11): the owner-only skip above only protects a secret from
    *this* re-own -- it does nothing when the host process is already non-root, because
    `non_root_identity()` then maps the container to that same host uid/gid and the two branches
    below are skipped entirely (ownership already "matches"). On that common non-root path the
    scenario container runs as the exact identity that already owns any `0600` secret in the
    bind-mounted tree (`.env`, `.netrc`, a project-local override, etc.), so it is readable by the
    untrusted Maker/Checker regardless of chown. There is no ownership change that can fix this --
    the only fail-closed option is to refuse to start rather than silently mount the secret into
    the container, via `_reject_owner_only_secrets()`.

    ``protect_owner_only=False`` (round 11) opts a caller out of both the round-10 skip above and
    the round-11 reject below. It is used for paths this driver fully generates itself moments
    before mounting (e.g. the ephemeral Git runtime directory) rather than pre-existing worktree
    content: nothing there is a human-placed secret, so a restrictive mode picked up from the
    process umask must not block re-owning (root path) or starting the container (non-root path).
    """
    excluded = exclude or frozenset()
    if os.getuid() != 0:
        if protect_owner_only:
            _reject_owner_only_secrets(path)
        return
    excluded_identities = {
        identity
        for identity in (_stat_identity(entry) for entry in excluded)
        if identity is not None
    }
    uid, gid = non_root_identity()
    if (
        path not in excluded
        and _stat_identity(path) not in excluded_identities
        and not (protect_owner_only and _is_owner_only_permission(path))
    ):
        os.chown(path, uid, gid)
    if not path.is_dir():
        return
    for child in path.rglob("*"):
        if child in excluded:
            continue
        if _stat_identity(child) in excluded_identities:
            continue
        if protect_owner_only and _is_owner_only_permission(child):
            continue
        try:
            os.chown(child, uid, gid, follow_symlinks=False)
        except FileNotFoundError:
            continue


def _reject_owner_only_secrets(path: Path) -> None:
    """Fail closed instead of mounting an owner-only-permission secret into a non-root container.

    Codex review, PR #262, P1 (round 11): see `align_mount_ownership()`'s own docstring. When the
    driver runs as a normal user there is no chown that can protect a `0600` secret already owned
    by that same user -- the forced non-root container identity *is* that user. Refusing to start
    is the only fail-closed option available.
    """
    if _is_owner_only_permission(path):
        raise DockerProfileError(
            f"refusing to mount an owner-only-permission path into a non-root container: {path}"
        )
    if not path.is_dir():
        return
    for child in path.rglob("*"):
        if _is_owner_only_permission(child):
            raise DockerProfileError(
                "refusing to mount an owner-only-permission path into a non-root "
                f"container: {child}"
            )


def _stat_identity(path: Path) -> tuple[int, int] | None:
    """Return ``(st_dev, st_ino)`` for ``path`` (without following a trailing symlink), or
    ``None`` if it does not exist. Used by `align_mount_ownership()`'s `exclude` set to also catch
    hardlink aliases of an excluded path (see that function's round 8 docstring note)."""
    try:
        stat_result = os.lstat(path)
    except OSError:
        return None
    return (stat_result.st_dev, stat_result.st_ino)


_OWNER_ONLY_MODE_MASK = 0o077


def _is_owner_only_permission(path: Path) -> bool:
    """Return True when `path`'s current mode bits grant no access to group or other at all.

    Used by `align_mount_ownership()` to skip re-owning entries that were deliberately left
    restricted to their current owner (e.g. a root-owned `0600` secret file) -- see that
    function's round 10 docstring note. Uses `lstat()` so a symlink's own mode gates the
    decision rather than its target's, matching the `follow_symlinks=False` chown below it
    guards. Returns False (not owner-only, i.e. eligible for re-owning) if `path` no longer
    exists by the time this runs, consistent with `_stat_identity()`'s own not-found handling.
    """
    try:
        stat_result = os.lstat(path)
    except OSError:
        return False
    return stat_result.st_mode & _OWNER_ONLY_MODE_MASK == 0


def resource_args(resources: dict[str, Any]) -> list[str]:
    return [
        "--pids-limit",
        str(resources["pids_limit"]),
        "--memory",
        str(resources["memory"]),
        "--cpus",
        str(resources["cpus"]),
    ]


def bounded_container_command(
    resources: dict[str, Any],
    command: list[str],
    *,
    kill_after_seconds: int = 5,
) -> list[str]:
    try:
        lifetime = int(resources["max_lifetime_sec"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DockerProfileError("container max_lifetime_sec must be an integer") from exc
    if lifetime <= 0:
        raise DockerProfileError("container max_lifetime_sec must be positive")
    return [
        "/usr/bin/timeout",
        "--signal=TERM",
        f"--kill-after={kill_after_seconds}s",
        f"{lifetime}s",
        *command,
    ]


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
