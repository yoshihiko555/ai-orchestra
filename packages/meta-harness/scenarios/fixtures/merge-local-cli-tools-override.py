#!/usr/bin/env python3
"""Merge a single override key into cli-tools.local.yaml without erasing a
candidate's already-materialized routing-config patch.

For a `routing-config` candidate's regression run, the evaluator applies the
candidate's config patch to `.claude/config/agent-routing/cli-tools.local.yaml`
*before* scenario `setup:` commands run (see `evaluator._apply_config_patch`
and `_run_attempt_lifecycle`'s `apply_registered_candidate_overlay` ->
`run_setup_commands` ordering). A `setup:` step that blindly overwrites that
file would silently discard the candidate's patch, making the regression
scenario always evaluate the unpatched base config regardless of what the
candidate actually changed (PR #264 review, point 2).

This script instead loads the existing file (if any), sets a single dotted
key path, and writes the merged result back, preserving every other key.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

_LOCAL_CONFIG_RELATIVE = Path(".claude/config/agent-routing/cli-tools.local.yaml")


def _coerce(raw: str) -> bool | str:
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    return raw


def _set_nested(config: dict, segments: tuple[str, ...], value: bool | str) -> None:
    current = config
    for segment in segments[:-1]:
        existing = current.get(segment)
        if not isinstance(existing, dict):
            existing = {}
            current[segment] = existing
        current = existing
    current[segments[-1]] = value


def merge_override(project_root: Path, key: str, value: str) -> None:
    local_path = project_root / _LOCAL_CONFIG_RELATIVE
    local_path.parent.mkdir(parents=True, exist_ok=True)
    loaded = (
        yaml.safe_load(local_path.read_text(encoding="utf-8")) if local_path.is_file() else None
    )
    config = loaded if isinstance(loaded, dict) else {}
    _set_nested(config, tuple(key.split(".")), _coerce(value))
    local_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, help="dotted key path, e.g. codex.enabled")
    parser.add_argument("--value", required=True, help="'true'/'false' or a raw string value")
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)
    merge_override(Path(args.project_root).resolve(), args.key, args.value)


if __name__ == "__main__":
    main()
