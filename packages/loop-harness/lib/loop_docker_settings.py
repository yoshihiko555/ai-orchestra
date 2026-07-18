#!/usr/bin/env python3
"""Create the trusted read-only Claude settings bundle mounted into actions."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

CONTAINER_BUNDLE_DIR = "/opt/loop-harness"
CONTAINER_SETTINGS = f"{CONTAINER_BUNDLE_DIR}/settings.json"
CONTAINER_GUARD = f"{CONTAINER_BUNDLE_DIR}/maker_bash_guard.py"


class DockerSettingsError(RuntimeError):
    """The trusted Docker settings bundle could not be created or validated."""


@dataclass(frozen=True)
class DockerSettingsBundle:
    source_dir: Path
    container_dir: str = CONTAINER_BUNDLE_DIR
    settings_path: str = CONTAINER_SETTINGS


def create_settings_bundle(runtime_dir: Path, guard_source: Path) -> DockerSettingsBundle:
    """Create an action-local bundle containing only settings and the trusted guard."""
    source_dir = runtime_dir / "trusted-settings"
    try:
        if source_dir.exists() or source_dir.is_symlink():
            if source_dir.is_symlink() or not source_dir.is_dir():
                raise DockerSettingsError("trusted settings path is not a regular directory")
            source_dir.chmod(0o700)
            shutil.rmtree(source_dir)
        source_dir.mkdir(parents=True, mode=0o700)
        os.chmod(source_dir, 0o700)
        if guard_source.is_symlink() or not guard_source.is_file():
            raise DockerSettingsError("maker Bash guard is not a trusted regular file")
        guard_target = source_dir / "maker_bash_guard.py"
        shutil.copyfile(guard_source, guard_target)
        guard_target.chmod(0o555)
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash|Edit|Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"/usr/bin/python3 {CONTAINER_GUARD}",
                            }
                        ],
                    }
                ]
            }
        }
        settings_target = source_dir / "settings.json"
        settings_target.write_text(json.dumps(settings), encoding="utf-8")
        settings_target.chmod(0o444)
        source_dir.chmod(0o555)
    except DockerSettingsError:
        raise
    except OSError as exc:
        raise DockerSettingsError("could not create trusted Docker settings bundle") from exc
    return DockerSettingsBundle(source_dir.resolve())


def rewrite_claude_settings(command: list[str], bundle: DockerSettingsBundle) -> list[str]:
    """Replace the host-only settings path with the mounted container path."""
    rewritten = list(command)
    try:
        index = rewritten.index("--settings")
        rewritten[index + 1] = bundle.settings_path
    except (ValueError, IndexError) as exc:
        raise DockerSettingsError("claude command has no valid --settings argument") from exc
    if rewritten:
        rewritten[0] = "claude"
    return rewritten


def cleanup_settings_bundle(bundle: DockerSettingsBundle | None) -> None:
    if bundle is None:
        return
    try:
        if bundle.source_dir.is_symlink():
            raise DockerSettingsError("trusted settings cleanup path became a symlink")
        bundle.source_dir.chmod(0o700)
        shutil.rmtree(bundle.source_dir, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DockerSettingsError("could not remove trusted Docker settings bundle") from exc
