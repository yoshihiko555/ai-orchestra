#!/usr/bin/env python3
"""fail-logs の教訓候補の人向け CLI（/lessons で人が承認した操作だけに使う）。

状態の変更・重複のまとめ・確認の記録・反映の確認・ガードの Issue の作成・写像の作り直しを行う。
**このスクリプトは Bash の許可リストに入れないこと**（入れると、注入されたエージェントが状態の変更や
公開 Issue の作成をできてしまう。設計 docs/design/fail-logs.md §10.1・§12）。

終了コード: 0 成功 / 1 その他の失敗 / 2 伏せ字が使えない / 3 サンドボックスの外での実行が要る /
4 導入されていない・無効 / 5 入力の検査に失敗した・確認が要る。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

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
from hook_common import sanitized_git_env  # noqa: E402
from sanitize import ValidationError, check_branch, check_target_ref  # noqa: E402

DESCRIPTION = "fail-logs の教訓候補の人向け CLI（状態の変更・反映の確認・ガードの Issue）"
ACTOR = "skill"
MAX_STDIN_BYTES = 64 * 1024
COMMAND_TIMEOUT_SECONDS = 30.0
DEFAULT_INTEGRATION_BRANCH = "main"
_ISSUE_URL = re.compile(r"https://github\.com/\S+/issues/\d+")


def run_command(
    command: list[str], *, cwd: str, input_text: str | None = None
) -> subprocess.CompletedProcess:
    """外部コマンドを引数の配列で起動する（シェルを介さない）。テストではこの関数を差し替える。

    コマンドが無い・時間切れのときは、終了コード -1 の結果にする（呼び出し側で失敗として扱う）。
    """
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=sanitized_git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, -1, "", str(exc))


def _origin_url(project_dir: str) -> str:
    """宛先の remote（``origin``）の URL。verify・issue の出力に含め、人が宛先を確かめられるようにする。"""
    result = run_command(["git", "remote", "get-url", "origin"], cwd=project_dir)
    return (result.stdout or "").strip()[:300] if result.returncode == 0 else ""


def _error(message: str, code: int) -> int:
    print(f"[fail-logs] {message}", file=sys.stderr)
    return code


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _sid() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:64]


def _require_lesson(snapshot: dict, lid: str) -> dict:
    lesson = snapshot.get("lessons", {}).get(model.resolve_lid(snapshot, lid))
    if lesson is None:
        raise ValidationError(f"lesson {lid} not found")
    return lesson


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


def _state_event(snapshot: dict, lid: str, to: str, target: dict | None, note: str) -> dict:
    lesson = _require_lesson(snapshot, lid)
    data = model.build_state_data(lesson["state"], to, target, note)
    return model.make_event(model.EV_STATE, lesson["lid"], ACTOR, _sid(), data)


def _append(context: ctx.LessonsContext, build) -> dict:
    snapshot, _events = context.store.append_events(build)
    return snapshot


def cmd_revise(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    masker = masking.load_masker()
    if masker is None:
        return _error("伏せ字（強化版マスキング）が使えない", ctx.EXIT_NO_MASKING)
    payload = _read_stdin_json()

    def build(snapshot: dict) -> list[dict]:
        lesson = _require_lesson(snapshot, args.lid)
        data = model.build_revised_data(payload, masker)
        return [model.make_event(model.EV_REVISED, lesson["lid"], ACTOR, _sid(), data)]

    snapshot = _append(context, build)
    _print({"lid": args.lid, "lesson": _require_lesson(snapshot, args.lid)})
    return ctx.EXIT_OK


def _target_from_args(args: argparse.Namespace) -> dict | None:
    if not args.target_kind:
        if args.target_ref or args.branch:
            raise ValidationError("--target-ref / --branch require --target-kind")
        return None
    target: dict = {"kind": args.target_kind}
    if args.target_ref:
        target["ref"] = args.target_ref
    if args.branch:
        target["branch"] = args.branch
    return target


def cmd_set_state(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    target = _target_from_args(args)
    note = args.note or ""
    if note:
        masker = masking.load_masker()
        if masker is None:
            return _error(
                "伏せ字（強化版マスキング）が使えないため、メモを記録しない", ctx.EXIT_NO_MASKING
            )
        note = masker(note)
    snapshot = _append(context, lambda snap: [_state_event(snap, args.lid, args.to, target, note)])
    lesson = _require_lesson(snapshot, args.lid)
    _print({"lid": lesson["lid"], "state": lesson["state"], "target": lesson.get("target")})
    return ctx.EXIT_OK


def cmd_merge(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    def build(snapshot: dict) -> list[dict]:
        source = _require_lesson(snapshot, args.lid)
        into = _require_lesson(snapshot, args.into)
        if source["lid"] == into["lid"]:
            raise ValidationError("cannot merge a lesson into itself")
        return [
            model.make_event(model.EV_MERGED, source["lid"], ACTOR, _sid(), {"into": into["lid"]})
        ]

    snapshot = _append(context, build)
    into = _require_lesson(snapshot, args.into)
    _print({"merged": args.lid, "into": into["lid"], "recurrence": into["recurrence"]})
    return ctx.EXIT_OK


def cmd_ack(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    def build(snapshot: dict) -> list[dict]:
        lesson = _require_lesson(snapshot, args.lid)
        data = {"post_promotion_recurrence": int(lesson.get("post_promotion_recurrence", 0))}
        return [model.make_event(model.EV_ACK, lesson["lid"], ACTOR, _sid(), data)]

    snapshot = _append(context, build)
    lesson = _require_lesson(snapshot, args.lid)
    _print({"lid": lesson["lid"], "ack_baseline": lesson["ack_baseline"]})
    return ctx.EXIT_OK


def _integration_branch(project_dir: str) -> str:
    result = run_command(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=project_dir)
    ref = (result.stdout or "").strip()
    prefix = "refs/remotes/origin/"
    if result.returncode == 0 and ref.startswith(prefix):
        branch = ref[len(prefix) :]
        try:
            return check_branch(branch)
        except ValidationError:
            return DEFAULT_INTEGRATION_BRANCH
    return DEFAULT_INTEGRATION_BRANCH


def _pr_states(project_dir: str, branch: str) -> list[str] | None:
    result = run_command(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "state"], cwd=project_dir
    )
    if result.returncode != 0:
        return None
    try:
        items = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    return [str(item.get("state", "")).upper() for item in items if isinstance(item, dict)]


def _verify_decision(context: ctx.LessonsContext, lesson: dict) -> tuple[str, str]:
    """反映を確かめ、(次の状態, 理由) を返す（設計 §10.5）。次の状態が今と同じなら変えない。"""
    target = lesson.get("target") or {}
    ref = target.get("ref")
    branch = target.get("branch")
    if not ref or not branch:
        return model.STATE_PROMOTING, "昇格先のパスかブランチが記録されていない"
    check_target_ref(ref)
    check_branch(branch)
    integration = _integration_branch(context.project_dir)
    fetched = run_command(["git", "fetch", "origin", integration], cwd=context.project_dir)
    if fetched.returncode != 0:
        raise RuntimeError(f"git fetch origin {integration} に失敗した")
    marker = model.marker_for(lesson["lid"])
    found = run_command(
        ["git", "grep", "-F", "-e", marker, f"origin/{integration}", "--", ref],
        cwd=context.project_dir,
    )
    if found.returncode == 0:
        return model.STATE_PROMOTED, f"origin/{integration} の {ref} にマーカーがある"
    if found.returncode != 1:
        raise RuntimeError(f"git grep に失敗した（終了コード {found.returncode}）")
    states = _pr_states(context.project_dir, branch)
    if states is None:
        return model.STATE_PROMOTING, "PR の状態を取得できなかった"
    if "MERGED" in states:
        return (
            model.STATE_PROMOTING,
            "PR はマージ済みだが統合ブランチにマーカーが無い（統合ブランチの名前を確かめて選ぶ）",
        )
    if states and all(state == "CLOSED" for state in states):
        return model.STATE_CANDIDATE, "書き込みを含む PR がマージされずに閉じられた"
    return model.STATE_PROMOTING, "PR が開いている、または PR がまだ無い"


def cmd_verify(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    snapshot = context.store.read() or model.empty_snapshot()
    lesson = _require_lesson(snapshot, args.lid)
    if lesson["state"] != model.STATE_PROMOTING:
        raise ValidationError(f"lesson {lesson['lid']} is not promoting (state: {lesson['state']})")
    remote = _origin_url(context.project_dir)
    base = {"lid": lesson["lid"], "remote": remote}
    try:
        next_state, reason = _verify_decision(context, lesson)
    except RuntimeError as exc:
        _print({**base, "state": lesson["state"], "changed": False, "reason": str(exc)})
        return ctx.EXIT_ERROR
    if next_state == lesson["state"]:
        _print({**base, "state": lesson["state"], "changed": False, "reason": reason})
        return ctx.EXIT_OK
    note = reason[:200]

    def build(snap: dict) -> list[dict]:
        # 外部コマンドの間に別のプロセスが状態を変えていたら、何もしない（古い状態で遷移を決めない）。
        if _require_lesson(snap, lesson["lid"])["state"] != model.STATE_PROMOTING:
            return []
        return [_state_event(snap, lesson["lid"], next_state, None, note)]

    snapshot = _append(context, build)
    current = _require_lesson(snapshot, lesson["lid"])["state"]
    _print({**base, "state": current, "changed": current == next_state, "reason": reason})
    return ctx.EXIT_OK


def cmd_issue(context: ctx.LessonsContext, args: argparse.Namespace) -> int:
    masker = masking.load_masker()
    if masker is None:
        return _error("伏せ字（強化版マスキング）が使えない", ctx.EXIT_NO_MASKING)
    snapshot = context.store.read() or model.empty_snapshot()
    lesson = _require_lesson(snapshot, args.lid)
    if lesson["state"] != model.STATE_CANDIDATE:
        raise ValidationError(
            f"lesson {lesson['lid']} is not a candidate (state: {lesson['state']})"
        )
    visibility = promotion.repo_visibility(context.project_dir, runner=_runner_adapter)
    checked = promotion.check_text(lesson, masker, visibility)
    if checked["findings"] and not args.ack_findings:
        _print({"lid": lesson["lid"], "created": False, "reason": "findings", **checked})
        return ctx.EXIT_INVALID
    if checked["public"] and not args.ack_public:
        _print({"lid": lesson["lid"], "created": False, "reason": "public", **checked})
        return ctx.EXIT_INVALID
    title = " ".join(masker(lesson["title"]).split())[: promotion.MAX_TITLE_CHARS]
    body = promotion.issue_body(lesson, checked["text"])
    remote = _origin_url(context.project_dir)
    result = run_command(
        ["gh", "issue", "create", "--title", title, "--body-file", "-"],
        cwd=context.project_dir,
        input_text=body,
    )
    match = _ISSUE_URL.search(result.stdout or "")
    if result.returncode != 0 or match is None:
        _print({"lid": lesson["lid"], "created": False, "remote": remote, "reason": "gh failed"})
        return _error("Issue を作れなかった（状態は変えていない）", ctx.EXIT_ERROR)
    url = match.group(0)
    target = {"kind": "guard", "ref": url}
    try:
        _append(
            context,
            lambda snap: [_state_event(snap, lesson["lid"], model.STATE_PROMOTED, target, "")],
        )
    except (ValidationError, store_lib.LockTimeout, store_lib.LedgerUnwritable, OSError) as exc:
        # Issue は作れたので URL を必ず返す（再実行で同じ Issue を重複して作らないため）。
        _print(
            {
                "lid": lesson["lid"],
                "created": True,
                "url": url,
                "remote": remote,
                "state_updated": False,
            }
        )
        return _error(
            f"Issue を作ったが状態を更新できなかった（{exc}）。set-state --to promoted --target-kind guard "
            f"--target-ref {url} で記録する",
            ctx.EXIT_ERROR,
        )
    _print(
        {
            "lid": lesson["lid"],
            "created": True,
            "url": url,
            "remote": remote,
            "state": model.STATE_PROMOTED,
            "state_updated": True,
        }
    )
    return ctx.EXIT_OK


def _runner_adapter(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    """promotion.repo_visibility から run_command（差し替え可能）を呼ぶための変換。"""
    return run_command(command, cwd=str(kwargs.get("cwd") or os.getcwd()))


def cmd_rebuild(context: ctx.LessonsContext, _args: argparse.Namespace) -> int:
    snapshot = context.store.rebuild()
    _print(
        {"lessons": len(snapshot.get("lessons", {})), "ledger_offset": snapshot["ledger_offset"]}
    )
    return ctx.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lessons_admin.py", description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)
    p_revise = sub.add_parser("revise", help="文面・対象範囲・重大度を直す（人が確定した値になる）")
    p_revise.add_argument("lid")
    p_revise.add_argument("--json", dest="stdin", required=True, choices=["-"])
    p_state = sub.add_parser("set-state", help="状態を変える（§10.3 の遷移だけ）")
    p_state.add_argument("lid")
    p_state.add_argument("--to", required=True, choices=model.STATES)
    p_state.add_argument("--target-kind", choices=sorted(model.TARGET_KINDS))
    p_state.add_argument("--target-ref")
    p_state.add_argument("--branch")
    p_state.add_argument("--note")
    p_merge = sub.add_parser("merge", help="重複した候補をまとめる")
    p_merge.add_argument("lid")
    p_merge.add_argument("--into", required=True)
    p_ack = sub.add_parser("ack", help="昇格後の再発・重大な合流を確認したことを記録する")
    p_ack.add_argument("lid")
    p_verify = sub.add_parser("verify", help="昇格中の候補の反映を確かめる")
    p_verify.add_argument("lid")
    p_issue = sub.add_parser("issue", help="ガードの Issue を作る")
    p_issue.add_argument("lid")
    p_issue.add_argument("--ack-public", action="store_true")
    p_issue.add_argument("--ack-findings", action="store_true")
    sub.add_parser("rebuild", help="台帳の全体から写像を作り直す")
    return parser


COMMANDS = {
    "revise": cmd_revise,
    "set-state": cmd_set_state,
    "merge": cmd_merge,
    "ack": cmd_ack,
    "verify": cmd_verify,
    "issue": cmd_issue,
    "rebuild": cmd_rebuild,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    context = ctx.build_context(ctx.resolve_project_dir())
    if context is None:
        return _error(
            "fail-logs が導入されていない、または無効（enabled: false）", ctx.EXIT_DISABLED
        )
    try:
        return COMMANDS[args.command](context, args)
    except ValidationError as exc:
        return _error(f"入力の検査に失敗した: {exc}", ctx.EXIT_INVALID)
    except store_lib.LedgerUnwritable:
        return _error(
            "root の台帳に書けない（サンドボックスの外で実行する必要がある）", ctx.EXIT_SPOOLED
        )
    except store_lib.LockTimeout:
        return _error("台帳のロックを取れなかった", ctx.EXIT_ERROR)
    except OSError as exc:
        return _error(f"台帳を読み書きできなかった: {exc.strerror or exc}", ctx.EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
