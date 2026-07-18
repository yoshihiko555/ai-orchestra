#!/usr/bin/env python3
"""Assert that Claude's scenario behavior matches the effective routing config."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _get(config: dict[str, Any], key_path: str) -> Any:
    current: Any = config
    for segment in key_path.split("."):
        assert isinstance(current, dict) and segment in current, (
            f"missing merged routing key: {key_path}"
        )
        current = current[segment]
    return current


def _train_behavior(value: str, *, auto_aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    behaviors: dict[str, tuple[str, dict[str, Any]]] = {
        "codex": ("delegate-debug-analysis", {"first_duplicate": 1}),
        "antigravity": ("research-sequence-pattern", {"unique_count": 4}),
        "claude-direct": ("solve-sequence-directly", {"sorted": [1, 1, 3, 4, 5]}),
    }
    if value == "auto":
        selected_tool = "claude-direct"
        for alias, tool in (("bash:codex", "codex"), ("bash:agy", "antigravity")):
            if alias in auto_aliases:
                selected_tool = tool
                break
        return {"action": "select-debug-route", "result": {"selected_tool": selected_tool}}
    assert value in behaviors, f"unsupported agents.debugger.tool value: {value!r}"
    action, result = behaviors[value]
    return {"action": action, "result": result}


def _resolve_train_behavior(
    project_root: Path, config: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    sys.path.insert(0, str(project_root / "packages/agent-routing/hooks"))
    from route_config import build_aliases, get_agent_tool

    resolved_tool = get_agent_tool("debugger", config)
    assert isinstance(resolved_tool, str), "resolved debugger tool must be a string"
    aliases = build_aliases(config)
    return resolved_tool, _train_behavior(
        resolved_tool,
        auto_aliases=tuple(str(alias) for alias in aliases.get("auto", [])),
    )


def _holdout_behavior(value: str) -> dict[str, Any]:
    if value.startswith("gemini-") and "flash" in value:
        return {
            "action": "prioritize-latency",
            "result": {"steps": ["scan", "summarize", "verify"]},
        }
    if value.startswith("gemini-") and "pro" in value:
        return {
            "action": "prioritize-depth",
            "result": {"steps": ["inspect", "cross-check", "synthesize"]},
        }
    if value.startswith("claude-"):
        return {
            "action": "prioritize-deliberation",
            "result": {"steps": ["frame", "reason", "review"]},
        }
    if value.startswith("gpt-oss-"):
        return {
            "action": "prioritize-openness",
            "result": {"steps": ["inspect", "test", "document"]},
        }
    raise AssertionError(f"unsupported antigravity.model value: {value!r}")


def assert_behavior(project_root: Path, scenario_id: str, artifact: Path) -> None:
    definitions = {
        "train": ("agents.debugger.tool", _train_behavior),
        "holdout": ("antigravity.model", _holdout_behavior),
    }
    assert scenario_id in definitions, f"unknown routing behavior scenario: {scenario_id!r}"
    key_path, behavior_for = definitions[scenario_id]

    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import load_cli_tools_config

    merged = load_cli_tools_config(str(project_root))
    value = _get(merged, key_path)
    assert isinstance(value, str), f"effective {key_path} must be a string"
    if scenario_id == "train":
        value, expected_behavior = _resolve_train_behavior(project_root, merged)
    else:
        expected_behavior = behavior_for(value)

    artifact_path = project_root / artifact
    assert artifact_path.is_file() and not artifact_path.is_symlink(), (
        f"missing regular behavior artifact: {artifact}"
    )
    actual = json.loads(artifact_path.read_text(encoding="utf-8"))
    expected = {
        "resolved_key": key_path,
        "resolved_value": value,
        **expected_behavior,
    }
    assert actual == expected, (
        "behavior artifact does not match the materialized routing config: "
        f"expected {expected!r}, got {actual!r}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("train", "holdout"), required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    assert_behavior(project_root, args.scenario, args.artifact)


if __name__ == "__main__":
    main()
