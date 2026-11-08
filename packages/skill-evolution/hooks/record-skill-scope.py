#!/usr/bin/env python3
"""直接起動・ターン開始・サブエージェント開始を記録する。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
try:
    import skill_evolution_common as se
except Exception:
    se = None


def main(payload: dict, project_dir: str, config: dict) -> None:
    event_name = payload.get("hook_event_name")
    if event_name == "UserPromptExpansion":
        if payload.get("expansion_type") == "slash_command":
            se.record_run_started(
                project_dir, payload, payload.get("command_name"), "direct", config
            )
    elif event_name in {"UserPromptSubmit", "SubagentStart"}:
        event_type = "turn.started" if event_name == "UserPromptSubmit" else "agent.started"
        se.append_run_event(project_dir, se.build_scope_event(event_type, payload), config)


if __name__ == "__main__" and se is not None:
    se.execute_hook(main)
