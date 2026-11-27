"""Codex スキルの解決結果を、外部 CLI を起動せずに検証する。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _resolve_final_tool(merged: dict[str, Any], project_root: Path) -> str:
    """Resolve ``agents.debugger.tool`` to a concrete engine.

    Mirrors ``assert-routing-behavior.py``'s ``_train_behavior``: an ``auto``
    value is resolved via ``build_aliases``' enabled-CLI alias list, using the
    task-appropriate priority order for the debugger, falling back to
    ``claude-direct`` when no preferred CLI is enabled.
    """
    sys.path.insert(0, str(project_root / "packages/agent-routing/hooks"))
    from route_config import build_aliases, get_agent_tool

    resolved = get_agent_tool("debugger", merged)
    if resolved != "auto":
        return resolved
    aliases = build_aliases(merged)
    auto_aliases = tuple(str(alias) for alias in aliases.get("auto", []))
    return "codex" if "bash:codex" in auto_aliases else "claude-direct"


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


def _expected_route(merged: dict[str, Any], project_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import is_cli_enabled

    context = {
        "codex_enabled": is_cli_enabled("codex", merged),
    }
    final_tool = _resolve_final_tool(merged, project_root)
    if final_tool == "codex":
        return {
            "engine": "codex",
            "resolved_tool": "codex",
            **context,
            **_codex_engine_fields(merged, project_root),
        }
    return {
        "engine": "claude-direct",
        "resolved_tool": "claude-direct",
        **context,
    }


def assert_route(project_root: Path, artifact: Path) -> None:
    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import load_cli_tools_config

    merged = load_cli_tools_config(str(project_root))

    expected = _expected_route(merged, project_root)

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
    parser.add_argument("--tool", choices=("codex",), required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    assert_route(project_root, args.artifact)


if __name__ == "__main__":
    main()
