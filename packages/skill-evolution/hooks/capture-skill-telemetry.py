#!/usr/bin/env python3
"""PostToolUse(Skill) hook: スキル完了時に二軸テレメトリを metrics へ記録する。

処理フロー:
1. stdin から PostToolUse JSON を読み込む
2. tool_name が "Skill" でなければ何もしない
3. tool_response から自己申告ブロックをパース
4. pending（発火側 hook が書いた開始時刻）から duration_ms を算出
5. metrics/<skill>.jsonl に 1 行 append
6. 失敗 or 不明瞭点ありのときは lessons に短い学びを追記
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
            print(f"Hook error (capture-skill-telemetry): {e}", file=sys.stderr)
            sys.exit(0)

    return wrapper


def _project_dir(data: dict) -> str:
    return data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


@_safe
def main() -> None:
    """PostToolUse(Skill) hook のエントリポイント。"""
    if se is None:
        return
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES).decode("utf-8", "replace")
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return
    if (data.get("tool_name") or "") != "Skill":
        return
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return
    skill = str(tool_input.get("skill") or tool_input.get("skill_name") or "").strip()
    if not skill:
        return

    project_dir = _project_dir(data)
    config = se.load_config(project_dir)
    if not config.get("enabled", True):
        return

    # tool_response の文字列葉から自己申告ブロックを抽出（json.dumps だと " がエスケープされ読めない）。
    self_report = se.parse_self_report(se.extract_text(data.get("tool_response")))
    sr_run_id = str(self_report.get("run_id") or "") if isinstance(self_report, dict) else ""
    run_id, duration_ms = se.consume_pending(project_dir, sr_run_id, skill, config)

    tool_uses = None
    if isinstance(self_report, dict) and self_report.get("tool_uses") is not None:
        tool_uses = se._safe_int(self_report.get("tool_uses"))

    record = se.build_metric_record(skill, run_id, self_report, duration_ms, tool_uses=tool_uses)
    se.append_metric(project_dir, skill, record, config)

    # 失敗 or 不明瞭点があれば短い学びを追記（オンライン層の即時還元）。
    sr = record.get("self_report") or {}
    if not record.get("success") or int(sr.get("ambiguities") or 0) > 0:
        note = (
            f"run {run_id or 'n/a'}: success={record.get('success')} "
            f"critical_pass_rate={record['machine']['critical_pass_rate']:.2f} "
            f"ambiguities={sr.get('ambiguities', 'n/a')}"
        )
        se.append_lesson(project_dir, skill, note, config)


if __name__ == "__main__":
    main()
