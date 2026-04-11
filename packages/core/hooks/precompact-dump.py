#!/usr/bin/env python3
"""PreCompact hook: コンテキスト圧縮前に重要情報を Markdown ファイルへ退避する。

Claude Code がコンテキスト圧縮を実行する直前に発火する。
圧縮で失われる可能性のある作業コンテキスト・Plans.md スナップショットを
`.claude/context/shared/precompact-{timestamp}.md` に書き出す。

退避内容:
- working-context.json の modified_files / current_phase / decisions
- Plans.md 本体（そのままコピー）
- セッション ID と圧縮トリガー種別（manual / auto）

副作用:
- stdout への JSON 出力は行わない（観測専用）。失敗しても exit 0 を返し、
  圧縮フローをブロックしない。
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

# audit package が有効なら event_logger を読み込む（監査ログへの統一書き込み）。
# 未インストール / import 失敗時は監査出力をスキップし、Markdown ダンプのみ行う。
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _audit_hooks = os.path.join(_orchestra_dir, "packages", "audit", "hooks")
    if os.path.isdir(_audit_hooks) and _audit_hooks not in sys.path:
        sys.path.insert(0, _audit_hooks)

from context_store import get_project_dir, get_shared_dir, read_working_context
from hook_common import read_hook_input, safe_hook_execution

try:
    from event_logger import emit_event as _emit_event  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - audit 未導入時のフォールバック
    _emit_event = None  # type: ignore[assignment]

# 保存するダンプファイルの最大数（古いものから削除）
MAX_DUMP_FILES = 20


def _now_stamp() -> str:
    """ファイル名用のタイムスタンプを返す(UTC, コロン無し)。

    秒精度だけだと同一秒内の複数 PreCompact 実行でファイル名が衝突し、
    古い dump を上書きしてしまうため、マイクロ秒まで含めて一意化する。
    形式: `YYYYMMDDTHHMMSS-ffffffZ`（辞書順 = 時系列順）。
    """
    now = datetime.datetime.now(datetime.UTC)
    return now.strftime("%Y%m%dT%H%M%S") + f"-{now.microsecond:06d}Z"


def _read_plans(project_dir: str) -> str:
    """Plans.md の内容を読み込む。存在しない場合は空文字を返す。"""
    plans_path = os.path.join(project_dir, ".claude", "Plans.md")
    try:
        with open(plans_path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _format_working_context(ctx: dict) -> str:
    """working-context.json を人間可読な Markdown に整形する。"""
    if not ctx:
        return "_(empty)_\n"

    lines: list[str] = []
    for key in ("current_phase", "updated_at"):
        value = ctx.get(key)
        if value:
            lines.append(f"- **{key}**: {value}")

    modified = ctx.get("modified_files")
    if isinstance(modified, list) and modified:
        lines.append(f"- **modified_files** ({len(modified)}):")
        for path in modified[-30:]:
            lines.append(f"  - `{path}`")

    decisions = ctx.get("decisions")
    if isinstance(decisions, list) and decisions:
        lines.append("- **decisions**:")
        for item in decisions[-10:]:
            lines.append(f"  - {item}")

    # 既知キー以外もダンプしておく（将来の拡張に耐える）
    known = {"current_phase", "updated_at", "modified_files", "decisions"}
    extras = {k: v for k, v in ctx.items() if k not in known}
    if extras:
        lines.append("- **other**:")
        for k, v in extras.items():
            lines.append(f"  - `{k}`: {v!r}")

    return "\n".join(lines) + "\n"


def build_dump_text(
    *,
    session_id: str,
    trigger: str,
    working_ctx: dict,
    plans_text: str,
) -> str:
    """圧縮退避用の Markdown 本文を組み立てる。"""
    parts: list[str] = []
    parts.append("# PreCompact Dump")
    parts.append("")
    parts.append(f"- **session_id**: `{session_id or 'unknown'}`")
    parts.append(f"- **trigger**: `{trigger or 'unknown'}`")
    parts.append(f"- **saved_at**: `{datetime.datetime.now(datetime.UTC).isoformat()}`")
    parts.append("")
    parts.append("## Working Context")
    parts.append("")
    parts.append(_format_working_context(working_ctx))
    parts.append("## Plans.md Snapshot")
    parts.append("")
    if plans_text:
        parts.append("```markdown")
        parts.append(plans_text.rstrip())
        parts.append("```")
    else:
        parts.append("_(Plans.md not found)_")
    parts.append("")
    return "\n".join(parts)


def _prune_old_dumps(shared_dir: str, keep: int = MAX_DUMP_FILES) -> None:
    """古いダンプファイルを削除する（最新 keep 件だけ残す）。"""
    try:
        entries = [
            os.path.join(shared_dir, name)
            for name in os.listdir(shared_dir)
            if name.startswith("precompact-") and name.endswith(".md")
        ]
    except OSError:
        return
    entries.sort()
    for path in entries[:-keep]:
        try:
            os.remove(path)
        except OSError:
            pass


def write_dump(project_dir: str, text: str) -> str:
    """ダンプファイルを書き出し、絶対パスを返す。"""
    shared_dir = get_shared_dir(project_dir)
    Path(shared_dir).mkdir(parents=True, exist_ok=True)
    filename = f"precompact-{_now_stamp()}.md"
    dump_path = os.path.join(shared_dir, filename)
    with open(dump_path, "w", encoding="utf-8") as f:
        f.write(text)
    _prune_old_dumps(shared_dir)
    return dump_path


@safe_hook_execution
def main() -> None:
    """PreCompact hook のエントリポイント。"""
    data = read_hook_input()

    project_dir = get_project_dir(data)
    session_id = str(data.get("session_id") or "")
    trigger = str(data.get("trigger") or "")

    working_ctx = read_working_context(project_dir)
    plans_text = _read_plans(project_dir)

    text = build_dump_text(
        session_id=session_id,
        trigger=trigger,
        working_ctx=working_ctx,
        plans_text=plans_text,
    )
    dump_path = write_dump(project_dir, text)

    # 監査ログにも小さく痕跡を残す（audit 未導入時はスキップ）
    if _emit_event is not None and session_id:
        try:
            _emit_event(
                "precompact",
                {
                    "trigger": trigger,
                    "dump_path": os.path.relpath(dump_path, project_dir),
                    "bytes": len(text),
                    "has_plans": bool(plans_text),
                },
                session_id=session_id,
                project_dir=project_dir,
            )
        except Exception as exc:  # pragma: no cover - 診断ログのみ、フックは止めない
            print(
                f"precompact-dump: failed to emit audit event: {exc}",
                file=sys.stderr,
            )

    print(f"precompact-dump: saved to {dump_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
