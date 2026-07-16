#!/usr/bin/env python3
"""Assert that every routing-config local override is effective after layering."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml


def _leaves(
    value: dict[str, Any], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    for key, child in value.items():
        path = (*prefix, str(key))
        if isinstance(child, dict):
            yield from _leaves(child, path)
        else:
            yield path, child


def _get(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for segment in path:
        assert isinstance(current, dict) and segment in current, (
            f"missing merged key: {'.'.join(path)}"
        )
        current = current[segment]
    return current


def _is_allowlisted(path: tuple[str, ...]) -> bool:
    return path in (("codex", "model"), ("antigravity", "model")) or (
        len(path) == 3 and path[0] == "agents" and path[2] == "tool"
    )


def main() -> None:
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    local_path = project_root / ".claude/config/agent-routing/cli-tools.local.yaml"
    assert local_path.is_file() and not local_path.is_symlink(), (
        f"missing local config: {local_path}"
    )
    local = yaml.safe_load(local_path.read_text(encoding="utf-8"))
    assert isinstance(local, dict), "local config must be a mapping"
    leaves = list(_leaves(local))
    assert leaves, "local config must contain at least one patched value"
    assert all(_is_allowlisted(path) for path, _value in leaves), (
        "local config contains a non-allowlisted key"
    )

    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import load_cli_tools_config

    merged = load_cli_tools_config(str(project_root))
    for path, expected in leaves:
        actual = _get(merged, path)
        assert actual == expected, (
            f"layering mismatch for {'.'.join(path)}: {actual!r} != {expected!r}"
        )


if __name__ == "__main__":
    main()
