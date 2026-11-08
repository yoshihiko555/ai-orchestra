#!/usr/bin/env python3
"""PreToolUse:Skill の実行範囲の始まりを記録する。

v1 では lessons を注入していた。導入先の settings の登録を保つため、ファイル名は v1 のまま据え置く（ADR-20261108-065）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
try:
    import skill_evolution_common as se
except Exception:
    se = None


def main(payload: dict, project_dir: str, config: dict) -> None:
    if payload.get("tool_name") != "Skill":
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    skill = tool_input.get("skill") or tool_input.get("skill_name")
    se.record_run_started(project_dir, payload, skill, "skill_tool", config)


if __name__ == "__main__" and se is not None:
    se.execute_hook(main)
