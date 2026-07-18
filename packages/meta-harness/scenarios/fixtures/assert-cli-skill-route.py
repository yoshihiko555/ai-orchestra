#!/usr/bin/env python3
"""Assert codex-system / antigravity-system routing decisions match cli-tools.yaml.

Both skills must resolve their external-CLI route purely from the layered
``.claude/config/agent-routing/cli-tools.yaml`` (+ ``.local.yaml``) config
instead of ever actually invoking ``codex exec`` / ``agy`` (this harness runs
with egress blocked). This fixture recomputes the expected decision from the
same shared resolution helpers the routing-config behavioral scenarios use
(``route_config.get_agent_tool`` / ``route_config.build_aliases`` /
``hook_common.is_cli_enabled``) and diffs it against the JSON artifact the
scenario prompt asked Claude to write.

``agents.<name>.tool`` can resolve to ``auto`` (e.g. via a routing-config
candidate patch). ``auto`` picks the first enabled CLI from ``build_aliases``'
alias list, but the priority order is task-dependent (see
``.claude/rules/orchestra-usage.md`` / ``facets/instructions/antigravity-system.md``):
deep-reasoning/debugging tasks prefer Codex first, research tasks prefer
Antigravity first. So the ``debugger`` probe (codex-system) resolves ``auto``
as ``codex`` -> ``antigravity`` -> ``claude-direct`` (matching
``assert-routing-behavior.py``'s ``_train_behavior``, PR #257), while the
``researcher`` probe (antigravity-system) resolves it as
``antigravity`` -> ``codex`` -> ``claude-direct``. This fixture must be able to
expect a ``codex`` engine for the antigravity-system probe (or an
``antigravity`` engine for the codex-system probe) and not just collapse every
non-primary resolution to ``claude-direct``.
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

# `auto` priority order is task-dependent, not a single global order (PR #264
# review round 2): deep-reasoning/debugging tasks (debugger probe) prefer
# Codex first; research tasks (researcher probe) prefer Antigravity first.
_AUTO_PRIORITY_BY_PROBE: dict[str, tuple[tuple[str, str], ...]] = {
    _CODEX_PROBE_AGENT: (("bash:codex", "codex"), ("bash:agy", "antigravity")),
    _ANTIGRAVITY_PROBE_AGENT: (("bash:agy", "antigravity"), ("bash:codex", "codex")),
}


def _resolve_final_tool(probe_agent: str, merged: dict[str, Any], project_root: Path) -> str:
    """Resolve ``agents.<probe_agent>.tool`` to a concrete engine.

    Mirrors ``assert-routing-behavior.py``'s ``_train_behavior``: an ``auto``
    value is resolved via ``build_aliases``' enabled-CLI alias list, using the
    task-appropriate priority order for ``probe_agent``, falling back to
    ``claude-direct`` when no preferred CLI is enabled.
    """
    sys.path.insert(0, str(project_root / "packages/agent-routing/hooks"))
    from route_config import build_aliases, get_agent_tool

    resolved = get_agent_tool(probe_agent, merged)
    if resolved != "auto":
        return resolved
    aliases = build_aliases(merged)
    auto_aliases = tuple(str(alias) for alias in aliases.get("auto", []))
    priority = _AUTO_PRIORITY_BY_PROBE[probe_agent]
    for alias, tool in priority:
        if alias in auto_aliases:
            return tool
    return "claude-direct"


def _codex_engine_fields(merged: dict[str, Any], project_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import DEFAULT_CODEX_SANDBOX_ANALYSIS

    codex_cfg = merged.get("codex", {}) or {}
    sandbox_cfg = codex_cfg.get("sandbox", {}) or {}
    return {
        "model": codex_cfg.get("model"),
        # Effective `codex.sandbox.analysis` value (including its fallback
        # default), never a hardcoded literal like "analysis".
        "sandbox": sandbox_cfg.get("analysis", DEFAULT_CODEX_SANDBOX_ANALYSIS),
        "flags": codex_cfg.get("flags"),
    }


def _antigravity_engine_fields(merged: dict[str, Any]) -> dict[str, Any]:
    antigravity_cfg = merged.get("antigravity", {}) or {}
    model = antigravity_cfg.get("model", "")
    allowlist = antigravity_cfg.get("model_allowlist", []) or []
    return {
        "model": model,
        "allowlist_warning": bool(model) and model not in allowlist,
    }


def _expected_for_probe(
    probe_agent: str, merged: dict[str, Any], project_root: Path
) -> dict[str, Any]:
    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import is_cli_enabled

    context = {
        "codex_enabled": is_cli_enabled("codex", merged),
        "antigravity_enabled": is_cli_enabled("antigravity", merged),
    }
    final_tool = _resolve_final_tool(probe_agent, merged, project_root)
    if final_tool == "codex":
        return {
            "engine": "codex",
            "resolved_tool": "codex",
            **context,
            **_codex_engine_fields(merged, project_root),
        }
    if final_tool == "antigravity":
        return {
            "engine": "antigravity",
            "resolved_tool": "antigravity",
            **context,
            **_antigravity_engine_fields(merged),
        }
    return {
        "engine": "claude-direct",
        "resolved_tool": "claude-direct",
        **context,
    }


def assert_route(project_root: Path, tool: str, artifact: Path) -> None:
    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import load_cli_tools_config

    merged = load_cli_tools_config(str(project_root))

    if tool == "codex":
        expected = _expected_for_probe(_CODEX_PROBE_AGENT, merged, project_root)
    elif tool == "antigravity":
        expected = _expected_for_probe(_ANTIGRAVITY_PROBE_AGENT, merged, project_root)
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
