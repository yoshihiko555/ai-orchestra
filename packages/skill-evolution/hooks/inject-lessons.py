#!/usr/bin/env python3
"""PreToolUse(Skill) hook: スキル発火前に lessons を注入し run_id を発行する。

処理フロー:
1. stdin から PreToolUse JSON を読み込む
2. tool_name が "Skill" でなければ何もしない
3. tool_input.skill からスキル名を取得
4. run_id を発行し pending（開始時刻）を記録（完了側 hook が duration を算出）
5. lessons/<skill>.md を読み、注入テキスト（lessons ＋ 自己申告ブロック指示）を構築
6. hookSpecificOutput.additionalContext として出力
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


def _safe(func):
    """例外時は stderr に出して exit(0) するラッパ。"""

    def wrapper() -> None:
        try:
            func()
        except Exception as e:  # noqa: BLE001
            print(f"Hook error (inject-lessons): {e}", file=sys.stderr)
            sys.exit(0)

    return wrapper


def _project_dir(data: dict) -> str:
    return data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def build_injection(skill: str, lessons: str, run_id: str, max_chars: int) -> str:
    """注入テキストを構築する。lessons が空でも自己申告指示は出す。"""
    parts: list[str] = [f"[Skill Evolution] skill='{skill}' run_id={run_id}"]
    if lessons.strip():
        body = lessons if len(lessons) <= max_chars else lessons[:max_chars] + "\n...(truncated)"
        parts.append("## 過去の学び（lessons）\n" + body)
    parts.append(
        "## 完了時の自己申告（必須）\n"
        "スキル完了時に以下のブロックを出力すること（値は実績で置換）:\n"
        "[skill-self-report]\n"
        f'{{"run_id": "{run_id}", "ambiguities": 0, "discretion_fills": 0, '
        '"retries": 0, "critical": {}}\n'
        "[/skill-self-report]\n"
        "critical には lessons の [critical] チェックリスト各項目の達成可否(true/false)を入れる。"
    )
    return "\n\n".join(parts)


@_safe
def main() -> None:
    """PreToolUse(Skill) hook のエントリポイント。"""
    if se is None:
        return
    try:
        data = json.loads(sys.stdin.read())
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

    run_id = se.gen_run_id(skill)
    pending = se.pending_path(project_dir, skill, config)
    os.makedirs(os.path.dirname(pending), exist_ok=True)
    with open(pending, "w", encoding="utf-8") as f:
        json.dump({"run_id": run_id, "start_epoch": time.time(), "ts": se.now_iso()}, f)

    lessons = se.read_lessons(project_dir, skill, config)
    max_chars = int((config.get("lessons") or {}).get("inject_max_chars") or 4000)
    injection = build_injection(skill, lessons, run_id, max_chars)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": injection,
        }
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
