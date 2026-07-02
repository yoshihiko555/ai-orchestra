#!/usr/bin/env python3
"""SubagentStop hook: `context: fork` スキル（サブエージェント実行）の完了を捕捉する。

PostToolUse(Skill) では拾えないサブエージェント実行のスキルを補完的に記録する。
SubagentStop の入力形状は不確定なため best-effort:
- 入力 JSON 全体を文字列化して自己申告ブロックを探す
- ブロックに "skill" と "run_id" があり、まだ metrics に無ければ 1 行 append する
"""

from __future__ import annotations

import json
import os
import sys

_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(os.path.dirname(_HOOK_DIR), "lib")
for _p in (_HOOK_DIR, _LIB_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import skill_evolution_common as se
except ImportError:
    se = None  # type: ignore[assignment]

MAX_STDIN_BYTES = 1024 * 1024  # 1 MB。巨大 stdin による OOM を防ぐ


def _safe(func):
    def wrapper() -> None:
        try:
            func()
        except Exception as e:  # noqa: BLE001
            print(f"Hook error (capture-subagent-skill): {e}", file=sys.stderr)
            sys.exit(0)

    return wrapper


def _project_dir(data: dict) -> str:
    return data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def _already_recorded(project_dir: str, skill: str, run_id: str, config: dict) -> bool:
    """同一 run_id が既に metrics にあるか（PostToolUse と二重記録しない）。

    末尾のみを走査する有界チェック（metrics 肥大化時も一定コスト）。
    """
    if not run_id:
        return False
    return run_id in se.recent_run_ids(project_dir, skill, config)


@_safe
def main() -> None:
    """SubagentStop hook のエントリポイント。"""
    if se is None:
        return
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES).decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return

    # デコード済みの文字列葉から抽出（raw JSON だと " がエスケープされ parse できない）。
    self_report = se.parse_self_report(se.extract_text(data))
    if not isinstance(self_report, dict):
        return
    skill = str(self_report.get("skill") or "").strip()
    run_id = str(self_report.get("run_id") or "").strip()
    # skill と run_id の両方が必要（run_id 無しは重複排除できず二重記録になるため捨てる）。
    if not skill or not run_id:
        return

    project_dir = _project_dir(data if isinstance(data, dict) else {})
    config = se.load_config(project_dir)
    if not config.get("enabled", True):
        return
    if _already_recorded(project_dir, skill, run_id, config):
        return

    record = se.build_metric_record(skill, run_id, self_report, duration_ms=None)
    se.append_metric(project_dir, skill, record, config)


if __name__ == "__main__":
    main()
