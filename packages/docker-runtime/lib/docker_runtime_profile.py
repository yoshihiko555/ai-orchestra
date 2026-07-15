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
