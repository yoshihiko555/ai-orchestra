#!/usr/bin/env python3
"""fail-logs の教訓候補の CLI（エージェント・/review-respond・/lessons が使う。読み取りと候補の追加だけ）。

状態を変える操作（昇格・却下・Issue の作成など）は ``lessons_admin.py`` にあり、この CLI には無い
（設計 docs/design/fail-logs.md §10.1）。この CLI は許可リストに入れてよい。

終了コード: 0 成功 / 1 その他の失敗 / 2 伏せ字が使えない / 3 spool に退避した /
4 導入されていない・無効 / 5 入力の検査に失敗した。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid

_script_dir = os.path.dirname(os.path.abspath(__file__))
_package_dir = os.path.dirname(_script_dir)
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
for _candidate in (
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    os.path.abspath(os.path.join(_package_dir, "..", "core", "hooks")),
    os.path.join(_package_dir, "lib"),
):
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import lessons_context as ctx  # noqa: E402
import lessons_model as model  # noqa: E402
import lessons_store as store_lib  # noqa: E402
import masking  # noqa: E402
import promotion  # noqa: E402
from sanitize import ValidationError, frame, neutralize_json, one_line  # noqa: E402

DESCRIPTION = "fail-logs の教訓候補の CLI（読み取りと候補の追加だけ）"
MAX_STDIN_BYTES = 64 * 1024
REVIEW_SOURCE = "review"
AGENT_SOURCE = "agent"
ACTORS = ("agent", "review")
EXTERNAL_ACTORS = frozenset({"agent", "review", model.ACTOR_SPOOL})
TITLE_DISPLAY_CHARS = 80
_SESSION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


def _print_json(payload: dict) -> None:
    print(json.dumps(neutralize_json({"untrusted": True, **payload}), ensure_ascii=False, indent=2))


def _error(message: str, code: int) -> int:
    print(f"[fail-logs] {message}", file=sys.stderr)
    return code


def _read_stdin_json() -> dict:
    data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(data) > MAX_STDIN_BYTES:
        raise ValidationError("stdin JSON is too large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise ValidationError("stdin is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError("stdin JSON must be an object")
    return payload


def _summary(lesson: dict) -> dict:
    return {
        "lid": lesson["lid"],
        "title": lesson.get("title", ""),
        "recurrence": lesson.get("recurrence", 0),
        "severity": lesson.get("severity"),
        "scope": lesson.get("scope"),
        "state": lesson.get("state"),
        "source": lesson.get("source"),
        "updated_at": lesson.get("updated_at"),
    }


def _spooled_lessons(context: ctx.LessonsContext, masker: model.Masker | None) -> list[dict]:
    """自分の worktree の spool に退避した追加を、表示用の候補として読む（検査と伏せ字を通す）。"""
    if masker is None:
        return []
    items: list[dict] = []
    for eid, raw in store_lib.read_spool(context.spool_dir):
        try:
            event = store_lib.normalize_spool_event(raw, eid, masker, [])
        except Exception:  # noqa: BLE001 - 表示用。壊れたファイルは飛ばす
            event = None
        if event is None:
            continue
        data = event["data"]
        items.append(
            {
                "lid": event["lid"],
                "title": data["title"],
                "recurrence": 0,
                "severity": data["severity"],
                "scope": data["scope"],
                "state": "spooled",
                "source": data["source"],
                "updated_at": event["ts"],
            }
        )
    return items


def _changed_since(lesson: dict, since: str) -> bool:
    return any(
        change.get("actor") in EXTERNAL_ACTORS and str(change.get("ts", "")) > since
        for change in lesson.get("changes", [])
    )


def cmd_list(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    snapshot = context.store.read()
    if snapshot is None:
        return _error(
            "台帳の写像を読めない（lessons_admin.py rebuild で作り直せる）", ctx.EXIT_ERROR
        )
    present_cfg = context.lessons["present"]
    reasons = {
        lesson["lid"]: reason
        for lesson, reason in model.presentable(
            snapshot, present_cfg["threshold"], present_cfg["promoting_stale_days"]
        )
    }
    lessons = list(snapshot.get("lessons", {}).values())
    if args.presentable:
        lessons = [lesson for lesson in lessons if lesson["lid"] in reasons]
    if args.state:
        lessons = [lesson for lesson in lessons if lesson.get("state") == args.state]
    if args.changed_since:
        lessons = [lesson for lesson in lessons if _changed_since(lesson, args.changed_since)]
    items = [
        {**_summary(lesson), "presentable": reasons.get(lesson["lid"], "")} for lesson in lessons
    ]
    if not args.presentable and not args.state:
        items += _spooled_lessons(context, masking.load_masker())
    if args.json:
        _print_json({"lessons": items})
        return ctx.EXIT_OK
    lines = [
        f"[candidate] {one_line(item['lid'], 12)} ×{item['recurrence']} {item['severity']} "
        f"{item['scope']} {item['state']} {item['source']}"
        f"{' presentable=' + item['presentable'] if item.get('presentable') else ''}"
        f" | {one_line(item['title'], TITLE_DISPLAY_CHARS)}"
        for item in items
    ]
    print(frame(lines or ["（候補なし）"]))
    return ctx.EXIT_OK


def _find_lesson(snapshot: dict, lid: str) -> dict | None:
    return snapshot.get("lessons", {}).get(model.resolve_lid(snapshot, lid))


def cmd_show(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    snapshot = context.store.read()
    if snapshot is None:
        return _error("台帳の写像を読めない", ctx.EXIT_ERROR)
    lesson = _find_lesson(snapshot, args.lid)
    if lesson is None:
        return _error(f"候補 {args.lid} が見つからない", ctx.EXIT_INVALID)
    if args.json:
        _print_json({"lesson": lesson})
        return ctx.EXIT_OK
    lines = [
        f"[candidate] {one_line(lesson['lid'], 12)} 状態={lesson['state']} 再発回数={lesson['recurrence']} "
        f"昇格後の再発={lesson.get('post_promotion_recurrence', 0)} 重大度={lesson['severity']} "
        f"対象範囲={lesson['scope']} 追加元={lesson['source']}",
        f"見出し: {one_line(lesson['title'], 120)}",
        f"事象: {one_line(lesson['event'], 400)}",
        f"原因: {one_line(lesson['cause'], 400)}",
        f"回避策: {one_line(lesson['avoidance'], 600)}",
        "根拠:",
    ]
    for evidence in lesson.get("evidence", []):
        counted = "数える" if evidence.get("counted") else "数えない"
        lines.append(
            f"  - {evidence['kind']} {one_line(evidence['ref'], 80)} {evidence['ts']}（{counted}）"
        )
    if lesson.get("target"):
        lines.append(f"昇格先: {one_line(json.dumps(lesson['target'], ensure_ascii=False), 300)}")
    print(frame(lines))
    return ctx.EXIT_OK


def _evidence_for(actor: str, payload: dict) -> dict:
    """根拠を CLI が決める（設計 §8.3）。

    agent は ``CLAUDE_CODE_SESSION_ID`` のセッションの記録が実在するときだけ ``kind: session``（再発回数に数える）にし、
    無ければ ``kind: agent``（数えない）にする。``kind: pr`` は review のときだけ受け付ける（自己申告。受け入れるリスク）。
    """
    if actor == "review":
        evidence = payload.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("kind") != "pr":
            raise ValidationError("--actor review requires evidence {kind: pr, ref: owner/repo#N}")
        return {"kind": "pr", "ref": evidence.get("ref"), "failures": []}
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if _SESSION_ID.fullmatch(session_id) and store_lib.transcript_exists(
        session_id, store_lib.transcript_roots()
    ):
        return {"kind": "session", "ref": session_id, "failures": []}
    return {"kind": "agent", "ref": "agent-" + uuid.uuid4().hex[:12], "failures": []}


def _check_daily_limit(snapshot: dict, source: str, limit: int) -> None:
    daily = snapshot.get("daily_adds") or {}
    if (
        daily.get("date") == store_lib.local_date()
        and daily.get("counts", {}).get(source, 0) >= limit
    ):
        raise ValidationError(f"daily add limit reached for source {source}")


def _build_add_event(
    snapshot: dict, payload: dict, actor: str, masker: model.Masker, *, eid: str = ""
) -> dict:
    source = REVIEW_SOURCE if actor == "review" else AGENT_SOURCE
    data = model.build_lesson_data(
        payload, masker, source=source, evidence=_evidence_for(actor, payload)
    )
    merge_into = payload.get("merge_into")
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:64]
    if merge_into is not None and model.is_lid(merge_into):
        target = model.resolve_lid(snapshot, merge_into)
        if target in snapshot.get("lessons", {}):
            return model.make_event(model.EV_EVIDENCE, target, actor, sid, data, eid=eid)
    return model.make_event(model.EV_CREATED, model.new_lid(), actor, sid, data, eid=eid)


def cmd_add(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    masker = masking.load_masker()
    if masker is None:
        return _error("伏せ字（強化版マスキング）が使えないため追加しない", ctx.EXIT_NO_MASKING)
    payload = _read_stdin_json()
    limit = context.lessons["add_daily_limit"]
    created: list[dict] = []

    def build(snapshot: dict) -> list[dict]:
        source = REVIEW_SOURCE if args.actor == "review" else AGENT_SOURCE
        _check_daily_limit(snapshot, source, limit)
        event = _build_add_event(snapshot, payload, args.actor, masker)
        created.append(event)
        return [event]

    try:
        context.store.append_events(build)
    except store_lib.LedgerUnwritable:
        if store_lib.count_spool(context.spool_dir) >= limit:
            raise ValidationError("spool already holds the daily add limit") from None
        snapshot = context.store.read() or model.empty_snapshot()
        event = _build_add_event(snapshot, payload, args.actor, masker, eid=uuid.uuid4().hex)
        store_lib.write_spool(context.spool_dir, event)
        _print_json({"lid": event["lid"], "action": "spooled"})
        print(
            "[fail-logs] root の台帳に書けないため、作業中の worktree の spool に退避した",
            file=sys.stderr,
        )
        return ctx.EXIT_SPOOLED
    event = created[-1]
    action = "created" if event["type"] == model.EV_CREATED else "merged"
    _print_json({"lid": event["lid"], "action": action})
    return ctx.EXIT_OK


def cmd_check_text(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    masker = masking.load_masker()
    if masker is None:
        return _error("伏せ字（強化版マスキング）が使えない", ctx.EXIT_NO_MASKING)
    snapshot = context.store.read()
    if snapshot is None:
        return _error("台帳の写像を読めない", ctx.EXIT_ERROR)
    lesson = _find_lesson(snapshot, args.lid)
    if lesson is None:
        return _error(f"候補 {args.lid} が見つからない", ctx.EXIT_INVALID)
    visibility = promotion.repo_visibility(context.project_dir)
    result = promotion.check_text(lesson, masker, visibility)
    findings = [neutralize_json(finding) for finding in result["findings"]]
    payload = {"untrusted": True, "target_kind": args.target_kind, **result, "findings": findings}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return ctx.EXIT_OK


def cmd_render(context: ctx.LessonsContext, _args: argparse.Namespace) -> int:
    try:
        context.store.render()
    except store_lib.LedgerUnwritable:
        return _error(
            "root の台帳に書けない（サンドボックスの外で実行する必要がある）", ctx.EXIT_SPOOLED
        )
    print(context.store.markdown_path)
    return ctx.EXIT_OK


def cmd_path(context: ctx.LessonsContext, _args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "ledger": context.store.ledger_path,
                "state": context.store.state_path,
                "markdown": context.store.markdown_path,
                "spool": context.spool_dir,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return ctx.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lessons_cli.py", description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="候補の一覧")
    p_list.add_argument("--state", choices=model.STATES)
    p_list.add_argument("--presentable", action="store_true")
    p_list.add_argument("--changed-since", dest="changed_since")
    p_list.add_argument("--json", action="store_true")
    p_show = sub.add_parser("show", help="1 件の全項目と根拠")
    p_show.add_argument("lid")
    p_show.add_argument("--json", action="store_true")
    p_add = sub.add_parser("add", help="標準入力の JSON で候補を追加・合流する")
    p_add.add_argument("--json", dest="stdin", required=True, choices=["-"])
    p_add.add_argument("--actor", choices=ACTORS, default="agent")
    p_check = sub.add_parser("check-text", help="昇格する文面と検査の結果")
    p_check.add_argument("lid")
    p_check.add_argument("--target-kind", required=True, choices=sorted(model.TARGET_KINDS))
    sub.add_parser("render", help="lessons.md を作り直す")
    sub.add_parser("path", help="台帳・写像・一覧のパス")
    return parser


COMMANDS = {
    "list": cmd_list,
    "show": cmd_show,
    "add": cmd_add,
    "check-text": cmd_check_text,
    "render": cmd_render,
    "path": cmd_path,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    context = ctx.build_context(ctx.resolve_project_dir())
    if context is None:
        return _error(
            "fail-logs が導入されていない、または無効（enabled: false）", ctx.EXIT_DISABLED
        )
    try:
        return COMMANDS[args.command](context, args)
    except ValidationError as exc:
        return _error(f"入力の検査に失敗した: {exc}", ctx.EXIT_INVALID)
    except store_lib.LockTimeout:
        return _error("台帳のロックを取れなかった", ctx.EXIT_ERROR)
    except OSError as exc:
        return _error(f"台帳を読み書きできなかった: {exc.strerror or exc}", ctx.EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
