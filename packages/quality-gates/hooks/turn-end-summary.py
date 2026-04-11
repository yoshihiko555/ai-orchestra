#!/usr/bin/env python3
"""Stop hook: ターン終了時にサマリーと軽量リマインダーを出力する。

責務（A + B + E1 合成）:
- A: audit v1 ログに `turn_end` イベントを追記（ファイル編集数、Plans 件数等の集計）
- B: working-context の modified_files から lint/test 未実行の可能性があるファイルを抽出
- E1: Plans.md の WIP / TODO / blocked 件数表示（SSOT への気付きを促す）

制約:
- `decision: block` は一切使わない（UX 安全）
- `stop_hook_active=true` の時は一切処理しない（再入ループ防止）
- transcript_path は読まない（コスト削減）
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if os.path.isdir(_core_hooks) and _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if os.path.isdir(_audit_hooks) and _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)

try:
    from hook_common import read_hook_input, safe_hook_execution
except ImportError:  # pragma: no cover - core 未導入時のフォールバック
    import functools

    def read_hook_input() -> dict:  # type: ignore[misc]
        try:
            return json.loads(sys.stdin.read())
        except (json.JSONDecodeError, ValueError):
            return {}

    def safe_hook_execution(func: Callable[[], None]) -> Callable[[], None]:  # type: ignore[misc]
        @functools.wraps(func)
        def wrapper() -> None:
            try:
                func()
            except Exception as e:
                print(f"Hook error ({func.__module__}): {e}", file=sys.stderr)
                sys.exit(0)

        return wrapper


try:
    from context_store import get_project_dir, read_working_context
except ImportError:  # pragma: no cover

    def get_project_dir(data: dict) -> str:  # type: ignore[misc]
        return data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    def read_working_context(_project_dir: str) -> dict:  # type: ignore[misc]
        return {}


try:
    from event_logger import emit_event as _emit_event  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - audit 未導入時のフォールバック
    _emit_event = None  # type: ignore[assignment]


def _load_parse_tasks() -> Callable[[str], dict] | None:
    """`load-task-state.py` から `parse_tasks` を動的に取得する。

    通常の `from load_task_state import parse_tasks` はファイル名にハイフンが
    含まれるため成立しない。importlib で直接ロードし、`sys.modules` にも
    登録しておくことで内部の自己参照 import にも対応する。
    失敗時は `None` を返し、stderr に診断ログを残す。
    """
    import importlib.util

    if not _orchestra_dir:
        return None
    plans_path = os.path.join(_orchestra_dir, "packages", "core", "hooks", "load-task-state.py")
    if not os.path.isfile(plans_path):
        return None

    module_name = "orchestra_load_task_state"
    spec = importlib.util.spec_from_file_location(module_name, plans_path)
    if spec is None or spec.loader is None:
        print("turn-end-summary: cannot create spec for load-task-state.py", file=sys.stderr)
        return None

    module = importlib.util.module_from_spec(spec)
    # 実行前に sys.modules へ登録（モジュール内部の自己参照 import に備える）
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # pragma: no cover - 診断ログのみ
        print(f"turn-end-summary: failed to load parse_tasks: {e}", file=sys.stderr)
        sys.modules.pop(module_name, None)
        return None

    return getattr(module, "parse_tasks", None)


parse_tasks = _load_parse_tasks()


# 拡張子 → lint/test 対象判定に使うコード拡張子
_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".sh",
    ".bash",
    ".zsh",
}


def _read_plans(project_dir: str) -> str:
    """Plans.md の内容を読み込む。存在しない場合は空文字を返す。"""
    plans_path = os.path.join(project_dir, ".claude", "Plans.md")
    try:
        with open(plans_path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _count_plans(project_dir: str) -> dict[str, int]:
    """Plans.md を parse してタスク件数を返す。"""
    if parse_tasks is None:
        return {}
    content = _read_plans(project_dir)
    if not content:
        return {}
    try:
        tasks = parse_tasks(content)
    except Exception as exc:  # pragma: no cover - 診断ログのみ
        print(f"turn-end-summary: failed to parse Plans.md: {exc}", file=sys.stderr)
        return {}
    return {state: len(items) for state, items in tasks.items()}


def _extract_code_files(working_ctx: dict) -> list[str]:
    """working-context から lint 対象となりえるコードファイルを抽出する。"""
    modified = working_ctx.get("modified_files")
    if not isinstance(modified, list):
        return []
    result: list[str] = []
    for item in modified:
        if not isinstance(item, str):
            continue
        ext = Path(item).suffix.lower()
        if ext in _CODE_EXTENSIONS:
            result.append(item)
    return result


def build_summary_text(
    *,
    code_files: list[str],
    plans_counts: dict[str, int],
    total_modified: int,
) -> str:
    """turn-end サマリーの Markdown を組み立てる。空なら空文字を返す。"""
    lines: list[str] = []

    if total_modified > 0 or code_files:
        lines.append(f"- Modified: {total_modified} files (code: {len(code_files)})")

    if plans_counts:
        parts = []
        for state in ("WIP", "TODO", "blocked"):
            count = plans_counts.get(state, 0)
            if count:
                parts.append(f"{state} {count}")
        if parts:
            lines.append(f"- Plans.md: {', '.join(parts)}")

    if code_files:
        preview = ", ".join(code_files[-5:])
        lines.append(f"- Reminder: 編集済みコードファイルの lint/test 未実行の可能性 ({preview})")

    if not lines:
        return ""
    return "[Turn Summary]\n" + "\n".join(lines)


def _emit_turn_end(
    *,
    session_id: str,
    project_dir: str,
    code_files: list[str],
    plans_counts: dict[str, int],
    total_modified: int,
) -> None:
    """audit v1 ログに turn_end イベントを追記する。audit 未導入なら何もしない。"""
    if _emit_event is None or not session_id:
        return
    try:
        _emit_event(
            "turn_end",
            {
                "modified_files": total_modified,
                "code_files": len(code_files),
                "plans_counts": plans_counts,
            },
            session_id=session_id,
            project_dir=project_dir,
        )
    except Exception as exc:  # pragma: no cover - 診断ログのみ、フックは止めない
        print(f"turn-end-summary: audit emit skipped: {exc}", file=sys.stderr)


@safe_hook_execution
def main() -> None:
    """Stop hook のエントリポイント。"""
    data = read_hook_input()

    # 再入ループ防止（Stop hook が block した後の再発火をスキップする安全弁）
    if data.get("stop_hook_active") is True:
        return

    project_dir = get_project_dir(data)
    session_id = str(data.get("session_id") or "")

    working_ctx = read_working_context(project_dir)
    modified = working_ctx.get("modified_files") or []
    total_modified = len(modified) if isinstance(modified, list) else 0

    code_files = _extract_code_files(working_ctx)
    plans_counts = _count_plans(project_dir)

    _emit_turn_end(
        session_id=session_id,
        project_dir=project_dir,
        code_files=code_files,
        plans_counts=plans_counts,
        total_modified=total_modified,
    )

    summary = build_summary_text(
        code_files=code_files,
        plans_counts=plans_counts,
        total_modified=total_modified,
    )
    if not summary:
        return

    # Stop hook では hookSpecificOutput は許可されていないため systemMessage を使う
    output = {"systemMessage": summary}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
