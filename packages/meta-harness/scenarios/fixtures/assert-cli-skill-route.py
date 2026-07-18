#!/usr/bin/env python3
"""Assert codex-system / antigravity-system routing decisions match cli-tools.yaml.

Both skills must resolve their external-CLI route purely from the layered
``.claude/config/agent-routing/cli-tools.yaml`` (+ ``.local.yaml``) config
instead of ever actually invoking ``codex exec`` / ``agy`` (this harness runs
with egress blocked). This fixture recomputes the expected decision from the
same shared resolution helpers the routing-config behavioral scenarios use
(``route_config.get_agent_tool`` / ``hook_common.is_cli_enabled``) and diffs
it against the JSON artifact the scenario prompt asked Claude to write.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# The scenario asks Claude to resolve routing for a fixed representative agent
# per tool, matching the base cli-tools.yaml defaults (agents.debugger.tool:
# codex, agents.researcher.tool: antigravity).
_CODEX_PROBE_AGENT = "debugger"
_ANTIGRAVITY_PROBE_AGENT = "researcher"


def _expected_codex(merged: dict[str, Any], project_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(project_root / "packages/agent-routing/hooks"))
    from route_config import get_agent_tool

    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import is_cli_enabled

    codex_enabled = is_cli_enabled("codex", merged)
    resolved_tool = get_agent_tool(_CODEX_PROBE_AGENT, merged)
    if resolved_tool == "codex":
        codex_cfg = merged.get("codex", {}) or {}
        return {
            "engine": "codex",
            "codex_enabled": codex_enabled,
            "resolved_tool": "codex",
            "model": codex_cfg.get("model"),
            "sandbox": "analysis",
            "flags": codex_cfg.get("flags"),
        }
    return {
        "engine": "claude-direct",
        "codex_enabled": codex_enabled,
        "resolved_tool": "claude-direct",
    }


def _expected_antigravity(merged: dict[str, Any], project_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(project_root / "packages/agent-routing/hooks"))
    from route_config import get_agent_tool

    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import is_cli_enabled

    antigravity_enabled = is_cli_enabled("antigravity", merged)
    resolved_tool = get_agent_tool(_ANTIGRAVITY_PROBE_AGENT, merged)
    if resolved_tool != "antigravity":
        return {
            "engine": "claude-direct",
            "antigravity_enabled": antigravity_enabled,
            "resolved_tool": "claude-direct",
        }
    antigravity_cfg = merged.get("antigravity", {}) or {}
    model = antigravity_cfg.get("model", "")
    allowlist = antigravity_cfg.get("model_allowlist", []) or []
    return {
        "engine": "antigravity",
        "antigravity_enabled": antigravity_enabled,
        "resolved_tool": "antigravity",
        "model": model,
        "allowlist_warning": bool(model) and model not in allowlist,
    }


def assert_route(project_root: Path, tool: str, artifact: Path) -> None:
    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import load_cli_tools_config

    merged = load_cli_tools_config(str(project_root))

    if tool == "codex":
        expected = _expected_codex(merged, project_root)
    elif tool == "antigravity":
        expected = _expected_antigravity(merged, project_root)
    else:
        raise AssertionError(f"unknown tool: {tool!r}")

    artifact_path = project_root / artifact
    assert artifact_path.is_file() and not artifact_path.is_symlink(), (
        f"missing regular route decision artifact: {artifact}"
    )
    actual = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert actual == expected, (
        "route decision artifact does not match the materialized cli-tools config: "
        f"expected {expected!r}, got {actual!r}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", choices=("codex", "antigravity"), required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    assert_route(project_root, args.tool, args.artifact)


if __name__ == "__main__":
    main()
