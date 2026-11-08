#!/usr/bin/env python3
"""SubagentStop の実行範囲の区切りを記録する。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
try:
    import skill_evolution_common as se
except Exception:
    se = None


def main(payload: dict, project_dir: str, config: dict) -> None:
    se.append_run_event(project_dir, se.build_scope_event("agent.stopped", payload), config)


if __name__ == "__main__" and se is not None:
    se.execute_hook(main)
