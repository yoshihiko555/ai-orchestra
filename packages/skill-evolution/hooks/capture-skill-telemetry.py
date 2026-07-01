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
import time

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


def _response_text(data: dict) -> str:
    """tool_response を文字列化する（dict/list でも探索）。"""
    resp = data.get("tool_response")
    if isinstance(resp, str):
        return resp
    if resp is None:
        return ""
    try:
        return json.dumps(resp, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(resp)


def _consume_pending(project_dir: str, skill: str, config: dict) -> tuple[str, int | None]:
    """pending から run_id と duration_ms を得て pending を削除する。"""
    path = se.pending_path(project_dir, skill, config)
    if not os.path.isfile(path):
        return "", None
    try:
        with open(path, encoding="utf-8") as f:
            pending = json.load(f)
    except (OSError, ValueError):
        return "", None
    run_id = str(pending.get("run_id") or "")
    start = pending.get("start_epoch")
    try:
        # clock skew で負になり得るため max(0, ...) でガード
        duration = max(0, int((time.time() - float(start)) * 1000)) if start is not None else None
    except (TypeError, ValueError):
        duration = None
    try:
        os.remove(path)
    except OSError:
        pass
    return run_id, duration


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

    self_report = se.parse_self_report(_response_text(data))
    run_id, duration_ms = _consume_pending(project_dir, skill, config)
    if not run_id and isinstance(self_report, dict):
        run_id = str(self_report.get("run_id") or "")

    record = se.build_metric_record(skill, run_id, self_report, duration_ms)
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
