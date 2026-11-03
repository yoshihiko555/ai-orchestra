#!/usr/bin/env python3
"""SessionStart hook: 昇格の基準を超えた教訓候補を知らせる（設計 docs/design/fail-logs.md §9）。

既存の inject-failure-summary.py とは別の hook で、既存の出力を変えない。例外時も終了コード 0 で戻る。
ロックは LOCK_NB で取り、取れなければ spool の取り込みと追いつきを飛ばして写像のまま読む
（セッションの開始を待たせない）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

_hook_dir = os.path.dirname(os.path.abspath(__file__))
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
for _candidate in (
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    os.path.abspath(os.path.join(_hook_dir, "..", "..", "core", "hooks")),
    os.path.abspath(os.path.join(_hook_dir, "..", "lib")),
):
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:
    import lessons_context as ctx  # noqa: E402
    import lessons_model as model  # noqa: E402
    import lessons_store as store_lib  # noqa: E402
    import masking  # noqa: E402
    from hook_common import read_hook_input, safe_hook_execution, sanitized_git_env  # noqa: E402
    from sanitize import frame, one_line  # noqa: E402

    _AVAILABLE = True
except Exception:  # noqa: BLE001 - 部品が欠けてもセッションの開始を止めない（無出力で戻る）
    _AVAILABLE = False

PRESENT_STATE_FILE = "lessons-present.json"
DISTILL_ENV = "FAIL_LOGS_DISTILL"
NONINTERACTIVE_PREFIX = "sdk-"
TITLE_DISPLAY_CHARS = 60

REASON_LABELS = {
    "recurring": "繰り返し現れている",
    "not_effective": "昇格したのに再発している",
    "promoting_stale": "昇格中のまま反映を確かめていない",
}


def _resolve_project_dir(data: dict) -> str:
    cwd = str(data.get("cwd") or "")
    if cwd and os.path.isdir(os.path.join(cwd, ".claude")):
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _should_stop() -> bool:
    """教訓化セッションの中・非対話のセッションでは示さない（設計 §9 の 1）。"""
    if os.environ.get(DISTILL_ENV):
        return True
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith(NONINTERACTIVE_PREFIX)


def _import_spool(context: ctx.LessonsContext) -> None:
    """全 worktree の spool を取り込む。伏せ字が使えない・ロックが取れないときは飛ばす。"""
    masker = masking.load_masker()
    if masker is None:
        return
    worktrees = store_lib.list_worktree_dirs(context.log_root, sanitized_git_env())
    spool_dirs: list[str] = []
    for worktree in worktrees:
        log_dir = ctx.resolve_log_dir(worktree, context.config)
        if log_dir:
            spool_dirs.append(os.path.join(log_dir, store_lib.SPOOL_DIR))
    try:
        store_lib.import_spool(context.store, spool_dirs, masker, nonblocking=True)
    except (store_lib.LockTimeout, store_lib.LedgerUnwritable, OSError):
        return


def _digest(items: list) -> str:
    """提示する内容（ID・再発回数・理由）の要約値。内容が変わったときだけ同じ日に示し直す。"""
    keys = sorted(
        f"{lesson['lid']}:{lesson.get('recurrence', 0)}:{reason}" for lesson, reason in items
    )
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _load_present_state(path: str) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _should_show(path: str, digest: str, today: str) -> bool:
    """前回と内容が違えば示す。同じなら、前回示した日が今日でなければ示す（設計 §9 の 4）。"""
    state = _load_present_state(path)
    return state.get("digest") != digest or state.get("date") != today


def _format(items: list, max_items: int, max_chars: int) -> str:
    """提示文を作る。(A) の候補は文面を出さず件数と ID だけにする（設計 §9 の 5）。"""
    detailed = [(lesson, reason) for lesson, reason in items if reason not in model.ID_ONLY_REASONS]
    id_only = [lesson for lesson, reason in items if reason in model.ID_ONLY_REASONS]
    lines = [f"[fail-logs] 昇格候補 {len(items)} 件（`/lessons` で確認できる）"]
    used = len(lines[0])
    shown = 0
    for lesson, reason in detailed:
        if shown >= max_items:
            break
        severity = "重大 " if lesson.get("severity") == "critical" else ""
        line = (
            f"  - {one_line(lesson['lid'], 12)} ×{lesson.get('recurrence', 0)} {severity}"
            f"[{one_line(str(lesson.get('source', '')), 12)}] {one_line(lesson.get('title', ''), TITLE_DISPLAY_CHARS)}"
            f"（{REASON_LABELS.get(reason, reason)}）"
        )
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
        shown += 1
    hidden = len(detailed) - shown
    if hidden > 0:
        line = f"  - ほか {hidden} 件"
        if used + len(line) <= max_chars:
            lines.append(line)
            used += len(line)
    if id_only:
        ids = ", ".join(one_line(lesson["lid"], 12) for lesson in id_only[:max_items])
        line = f"  - 重大度の確認が要る候補（文面は表示しない）: {ids}"
        if used + len(line) <= max_chars:
            lines.append(line)
    return frame(lines)


def _run() -> None:
    data = read_hook_input()
    if _should_stop():
        return
    project_dir = _resolve_project_dir(data)
    context = ctx.build_context(project_dir, require_installed=False)
    if context is None or not context.lessons["present"]["enabled"]:
        return
    _import_spool(context)
    snapshot = context.store.read()
    if not snapshot:
        return
    present = context.lessons["present"]
    items = model.presentable(snapshot, present["threshold"], present["promoting_stale_days"])
    if not items:
        return
    state_path = os.path.join(context.log_dir, PRESENT_STATE_FILE)
    digest = _digest(items)
    today = store_lib.local_date()
    if not _should_show(state_path, digest, today):
        return
    print(_format(items, present["max_items"], present["max_chars"]))
    try:
        store_lib.atomic_write(state_path, json.dumps({"digest": digest, "date": today}))
    except OSError:
        return


def main() -> None:
    """hook のエントリポイント。部品が読めないときは何もせずに戻る。"""
    if not _AVAILABLE:
        return
    safe_hook_execution(_run)()


if __name__ == "__main__":
    main()
