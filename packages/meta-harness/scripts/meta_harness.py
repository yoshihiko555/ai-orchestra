#!/usr/bin/env python3
"""meta-harness CLI（`orchex meta <sub>`、Phase 1a）。

docs/design/meta-harness-detailed.md が正本。Phase 1a で実装済みのサブコマンド:
- init       store ディレクトリ一式を冪等に初期化する
- register   overlay を検証し候補を immutable に登録する
- frontier   Pareto frontier を算出する（--rebuild で frontier.json を再生成）
- status     候補群の畳み込み状態を表示する
- purge      古い世代・retired 候補を削除する（frontier/promoted/予約中は保護）

`evaluate` / `propose` / `promote` / `loop` は Phase 1b / 2 のスタブ（exit 2）。

exit code（Sec6）: 0 成功 / 1 実行時エラー / 2 入力・スキーマ検証エラー / 3 lock 取得失敗・排他競合。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _SCRIPT_DIR.parent
_LIB_DIR = _PACKAGE_DIR / "lib"
_SCHEMA_DIR = _PACKAGE_DIR / "schemas"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import meta_harness_common as mh  # noqa: E402

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_VALIDATION_ERROR = 2
EXIT_LOCK_CONFLICT = 3

_PHASE_1B_STUBS = ("evaluate", "propose", "promote", "loop")


def _emit(data: dict, as_json: bool, human_lines: list[str] | None = None) -> None:
    """--json 指定時は JSON を、それ以外は human_lines（無ければ data の要約）を出す。"""
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    for line in human_lines or [json.dumps(data, ensure_ascii=False)]:
        print(line)


def _resolve_context(project: str) -> tuple[Path, dict] | None:
    """project_dir から config を読み main_root を解決する。失敗時は None（呼び出し側が exit 2）。"""
    project_dir = Path(project).resolve()
    config = mh.load_config(project_dir)
    try:
        main_root = mh.resolve_main_root(project_dir, config)
    except mh.MetaHarnessRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return main_root, config


def cmd_init(project: str, as_json: bool) -> int:
    """store ディレクトリ一式を冪等に初期化する。"""
    ctx = _resolve_context(project)
    if ctx is None:
        return EXIT_VALIDATION_ERROR
    main_root, config = ctx
    mh.init_store(main_root, config)
    _emit(
        {
            "status": "ok",
            "main_root": str(main_root),
            "store_dir": str(mh.store_dir(main_root, config)),
        },
        as_json,
        [f"initialized store at {mh.store_dir(main_root, config)}"],
    )
    return EXIT_OK


def _git_head(cwd: Path) -> str | None:
    """`cwd` における `git rev-parse HEAD` の結果を返す（source_commit 解決用）。

    【判断】`source_commit` は「overlay の差分先となるコミット」= register の登録元
    （`--project`）の HEAD であり、共有 store のある main_root（feature worktree の
    場合は別ディレクトリ）ではない。呼び出し側は必ず project_dir を渡すこと。
    """
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=10
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _git_is_dirty(cwd: Path) -> bool:
    """`cwd` の working tree が dirty かどうかを返す（`_git_head` と同じ理由で project_dir 必須）。"""
    completed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=10
    )
    return bool(completed.stdout.strip())


def _build_manifest(
    *,
    cand_id: str,
    parent_id: str | None,
    generation: int,
    target: str,
    source_commit: str,
    config_hash: str,
    overlay_files: list[str],
    description: str,
) -> dict:
    return {
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": parent_id,
        "generation": generation,
        "created_at": _now_iso(),
        "created_by": "human",
        "target": target,
        "source_commit": source_commit,
        "config_hash": config_hash,
        "model_versions": {},
        "overlay_files": overlay_files,
        "description": description,
    }


def _now_iso() -> str:
    return mh.now_iso()


def cmd_register(
    project: str,
    overlay: str,
    target: str,
    parent: str | None,
    description: str,
    slug: str | None,
    source_commit: str | None,
    as_json: bool,
) -> int:
    """overlay を検証し候補を登録する（Sec6 `register`）。"""
    ctx = _resolve_context(project)
    if ctx is None:
        return EXIT_VALIDATION_ERROR
    main_root, config = ctx
    overlay_dir = Path(overlay).resolve()

    violations = mh.validate_overlay(overlay_dir, config)
    config_patch_path = overlay_dir / mh.CONFIG_PATCH_FILENAME
    if config_patch_path.is_file():
        try:
            config_patch = json.loads(config_patch_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            violations.append(f"config-patch.json is not valid JSON: {exc}")
        else:
            violations.extend(mh.validate_config_patch(config_patch, config, _SCHEMA_DIR))
    if violations:
        for v in violations:
            print(f"error: {v}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    project_dir = Path(project).resolve()
    if _git_is_dirty(project_dir):
        print(
            "warning: working tree is dirty; uncommitted changes are not part of the candidate",
            file=sys.stderr,
        )
    resolved_source_commit = source_commit or _git_head(project_dir)
    if resolved_source_commit is None:
        print("error: could not resolve source_commit (git rev-parse HEAD failed)", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    slug_value = slug or mh.slugify(description) or "manual"
    cand_id = mh.generate_cand_id(slug_value)
    try:
        generation = mh.next_generation(main_root, config, parent)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    overlay_files = mh.list_overlay_files(overlay_dir)
    config_hash = mh.compute_config_hash(overlay_dir, config)
    manifest = _build_manifest(
        cand_id=cand_id,
        parent_id=parent,
        generation=generation,
        target=target,
        source_commit=resolved_source_commit,
        config_hash=config_hash,
        overlay_files=overlay_files,
        description=description,
    )

    manifest_schema = mh.load_schema(_SCHEMA_DIR, "candidate.manifest.schema.json")
    overlay_schema = mh.load_schema(_SCHEMA_DIR, "overlay.schema.json")
    schema_errors = mh.validate_against_schema(manifest, manifest_schema, _SCHEMA_DIR)
    schema_errors += mh.validate_against_schema(
        {"schema_version": "1.0", "files": overlay_files}, overlay_schema, _SCHEMA_DIR
    )
    if schema_errors:
        for e in schema_errors:
            print(f"error: {e}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    event = {
        "event": "candidate_registered",
        "ts": _now_iso(),
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": parent,
        "generation": generation,
        "target": target,
        "created_by": "human",
    }
    ledger_schema = mh.load_schema(_SCHEMA_DIR, "ledger.event.schema.json")
    event_errors = mh.validate_against_schema(
        event, ledger_schema["$defs"]["candidate_registered"], _SCHEMA_DIR
    )
    if event_errors:
        for e in event_errors:
            print(f"error: {e}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        with mh.store_lock(main_root, config):
            mh.register_candidate(
                main_root,
                config,
                cand_id=cand_id,
                manifest=manifest,
                overlay_dir=overlay_dir,
                overlay_files=overlay_files,
            )
            mh.append_ledger_event(main_root, config, event)
    except mh.LockAcquisitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_CONFLICT
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    _emit({"status": "ok", "cand_id": cand_id}, as_json, [f"registered candidate {cand_id}"])
    return EXIT_OK


def _eligible_and_ineligible(points: list[dict]) -> tuple[list[dict], list[str]]:
    """frontier 判定対象（eligible）と、fail/error により除外される cand_id を分ける。

    【判断】設計書は fail/error 候補を「frontier 候補から除外する」とのみ規定し、
    frontier_updated イベントの `dominated` に含めるかは明記していない。本実装では
    「frontier に載らない評価済み候補」を単一の `dominated` リストとして扱う（Pareto
    敗北・fail/error 除外のいずれも frontier 外という点で意味が同じため）。
    """
    eligible = [p for p in points if p["eligible"]]
    ineligible_ids = [p["cand_id"] for p in points if not p["eligible"]]
    return eligible, ineligible_ids


def _compute_frontier(main_root: Path, config: dict) -> dict:
    events = mh.read_ledger_events(main_root, config)
    points = mh.aggregate_run_points(events, config)
    eligible, ineligible_ids = _eligible_and_ineligible(points)
    frontier_ids, dominated_ids = mh.compute_pareto_frontier(eligible)
    dominated_ids = sorted(set(dominated_ids) | set(ineligible_ids))
    # 【判断】ledger 末尾のイベントが run_completed とは限らない（run 後に register 等が
    # 入ることは正常な運用）。points の比較スコープ選定（mh.aggregate_run_points）と同じ
    # 「最新の run_completed」を hash メタデータにも使う。末尾イベントに限定すると、
    # points は非ゼロの hash ペアで計算されているのに suite_hash/evaluator_hash だけ
    # ゼロ埋めになる不整合が生じる。
    latest = next((e for e in reversed(events) if e.get("event") == "run_completed"), None)
    zero_hash = "0" * 64
    return {
        "schema_version": "1.0",
        "generated_at": mh.now_iso(),
        "ledger_line_count": len(events),
        "suite_hash": (latest or {}).get("suite_hash", zero_hash),
        "evaluator_hash": (latest or {}).get("evaluator_hash", zero_hash),
        "cost_axis": (config.get("frontier") or {}).get("cost_axis", "total_tokens"),
        # 【判断】points の各要素は内部計算用に `eligible` フラグを持つ（_eligible_and_ineligible
        # の判定用）が、frontier.schema.json（Sec1-5）の points item は
        # additionalProperties: false かつ `eligible` を含まない。永続化する frontier.json が
        # schema に適合するよう、書き出し直前に内部専用フィールドを除去する。
        "points": [{k: v for k, v in p.items() if k != "eligible"} for p in points],
        "frontier": sorted(frontier_ids),
        "dominated": dominated_ids,
    }


def cmd_frontier(project: str, rebuild: bool, as_json: bool) -> int:
    """Pareto frontier を算出する（Sec6 `frontier`）。"""
    ctx = _resolve_context(project)
    if ctx is None:
        return EXIT_VALIDATION_ERROR
    main_root, config = ctx
    frontier_doc = _compute_frontier(main_root, config)

    if rebuild:
        event = {
            "event": "frontier_updated",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "frontier": frontier_doc["frontier"],
            "dominated": frontier_doc["dominated"],
        }
        try:
            with mh.store_lock(main_root, config):
                mh.append_ledger_event(main_root, config, event)
                frontier_doc["ledger_line_count"] = len(mh.read_ledger_events(main_root, config))
                mh.write_frontier_cache(main_root, config, frontier_doc)
        except mh.LockAcquisitionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_LOCK_CONFLICT

    _emit(
        frontier_doc,
        as_json,
        [
            f"frontier: {', '.join(frontier_doc['frontier']) or '(none)'}",
            f"dominated: {', '.join(frontier_doc['dominated']) or '(none)'}",
        ],
    )
    return EXIT_OK


def _staleness_warning(main_root: Path, config: dict, current_line_count: int) -> str | None:
    path = mh.frontier_path(main_root, config)
    if not path.is_file():
        return None
    cached = json.loads(path.read_text(encoding="utf-8"))
    if cached.get("ledger_line_count") != current_line_count:
        return "frontier.json may be stale; run `orchex meta frontier --rebuild` to refresh"
    return None


def cmd_status(project: str, candidate: str | None, as_json: bool) -> int:
    """候補群（または指定候補）の畳み込み状態を表示する（Sec6 `status`）。"""
    ctx = _resolve_context(project)
    if ctx is None:
        return EXIT_VALIDATION_ERROR
    main_root, config = ctx
    events = mh.read_ledger_events(main_root, config)
    states = mh.fold_candidate_states(events)
    warning = _staleness_warning(main_root, config, len(events))

    if candidate is not None:
        state = states.get(candidate)
        if state is None:
            print(f"error: unknown candidate: {candidate}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        payload = {"cand_id": candidate, **state}
    else:
        payload = {"candidates": states, "count": len(states)}
    if warning:
        payload["warning"] = warning
        print(f"warning: {warning}", file=sys.stderr)

    _emit(payload, as_json)
    return EXIT_OK


def _purge_eligible_ids(main_root: Path, config: dict, keep_generations: int) -> list[str]:
    """削除対象候補の cand_id を返す（Sec12-3）。

    保護対象（frontier 上・promoted・未解放 reservation）に加え、直近
    `keep_generations` 世代（manifest.json の `generation` の distinct な値の
    降順上位 N 件）に属する候補も保護する。同一世代の候補が複数あっても、その
    世代が保護対象であれば全て残す（「N 世代」であって「N 候補」ではない）。
    manifest が読めない候補は安全側（削除しない）として除外し、警告を出す。
    """
    events = mh.read_ledger_events(main_root, config)
    states = mh.fold_candidate_states(events)
    frontier_ids = set(_compute_frontier(main_root, config)["frontier"])
    all_ids = mh.list_candidate_ids(main_root, config)

    protected = set(frontier_ids)
    for cand_id in all_ids:
        state = states.get(cand_id, {})
        if state.get("status") == "promoted" or state.get("has_active_promotion_hold"):
            protected.add(cand_id)

    deletable_candidates = [c for c in all_ids if c not in protected]
    generations = _read_candidate_generations(main_root, config, deletable_candidates)
    protected_generations = _top_n_generations(list(generations.values()), keep_generations)

    return [
        cand_id
        for cand_id in deletable_candidates
        if cand_id in generations and generations[cand_id] not in protected_generations
    ]


def _read_candidate_generations(
    main_root: Path, config: dict, cand_ids: list[str]
) -> dict[str, int]:
    """purge 候補群の manifest.json から generation を読む（読めないものは除外 + 警告）。"""
    generations: dict[str, int] = {}
    for cand_id in cand_ids:
        manifest = _safe_read_manifest(main_root, config, cand_id)
        if manifest is None or "generation" not in manifest:
            print(
                f"warning: could not read manifest for {cand_id}; "
                "skipping purge eligibility (safe default)",
                file=sys.stderr,
            )
            continue
        generations[cand_id] = int(manifest["generation"])
    return generations


def _safe_read_manifest(main_root: Path, config: dict, cand_id: str) -> dict | None:
    """`mh.read_candidate_manifest` を壊れた JSON にも安全なようラップする。"""
    try:
        return mh.read_candidate_manifest(main_root, config, cand_id)
    except (json.JSONDecodeError, OSError):
        return None


def _top_n_generations(generations: list[int], keep_generations: int) -> set[int]:
    """distinct な generation を降順に並べ、上位 `keep_generations` 件を返す（保護対象世代）。"""
    if keep_generations <= 0:
        return set()
    distinct_sorted = sorted(set(generations), reverse=True)
    return set(distinct_sorted[:keep_generations])


def cmd_purge(project: str, keep_generations: int | None, as_json: bool) -> int:
    """古い世代・retired 候補を削除する（frontier/promoted/予約中は保護、Sec6 `purge`）。"""
    ctx = _resolve_context(project)
    if ctx is None:
        return EXIT_VALIDATION_ERROR
    main_root, config = ctx
    keep = (
        keep_generations
        if keep_generations is not None
        else config["retention"]["keep_generations"]
    )
    if keep < 0:
        print(f"error: --keep-generations must be >= 0, got: {keep}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        with mh.store_lock(main_root, config):
            to_delete = _purge_eligible_ids(main_root, config, keep)
            for cand_id in to_delete:
                _remove_candidate_dir(main_root, config, cand_id)
    except mh.LockAcquisitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_CONFLICT

    _emit(
        {"status": "ok", "purged": to_delete, "count": len(to_delete)},
        as_json,
        [f"purged {len(to_delete)} candidate(s): {', '.join(to_delete) or '(none)'}"],
    )
    return EXIT_OK


def _remove_candidate_dir(main_root: Path, config: dict, cand_id: str) -> None:
    import shutil

    cand_dir = mh.candidates_dir(main_root, config) / cand_id
    if cand_dir.is_dir():
        shutil.rmtree(cand_dir)


def cmd_phase1b_stub(sub: str) -> int:
    """Phase 1b / 2 未実装サブコマンド。"""
    print(
        f"'{sub}' is not implemented in Phase 1a. See docs/design/meta-harness-detailed.md Sec9"
        " for the phase boundary.",
        file=sys.stderr,
    )
    return EXIT_VALIDATION_ERROR


def _add_common_args(parser: argparse.ArgumentParser, *, is_top_level: bool = False) -> None:
    """--project / --json をどのサブコマンドの前後どちらに置いても解釈できるようにする。

    【判断】argparse のサブパーサは、自身の `--project`/`--json` に実値のデフォルト
    （`os.getcwd()` / `False`）を持たせていると、サブコマンド側で明示指定が無い場合に
    親パーサで既に解析済みの値をそのデフォルトで上書きしてしまう（`nargs=PARSER` の
    サブパーサアクションは自身のデフォルト充填を独立に行うため、`hasattr` チェックは
    トップレベルの実引数を保護しない。実測で `meta_harness.py --project X init` が
    `args.project` を X ではなく cwd にしてしまうことを確認済み）。

    そのため実際のデフォルト値は **トップレベルのみ** に持たせ、各サブパーサ側は
    `default=argparse.SUPPRESS` にする。SUPPRESS はサブコマンド側で未指定の場合に
    namespace 属性へ書き込みを行わない（＝トップレベルで設定済みの値がそのまま残る）
    ため、`--project X init` / `init --project X` のどちらの語順でも X が使われ、
    両方省略した場合はトップレベルの既定値（cwd / False）が使われる。
    """
    if is_top_level:
        parser.add_argument("--project", default=os.getcwd(), help="プロジェクトパス（既定: cwd）")
        parser.add_argument("--json", action="store_true", help="機械可読出力")
        return
    parser.add_argument(
        "--project", default=argparse.SUPPRESS, help="プロジェクトパス（既定: cwd）"
    )
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="機械可読出力"
    )


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサを構築する。"""
    parser = argparse.ArgumentParser(prog="meta_harness", description="meta-harness CLI (Phase 1a)")
    _add_common_args(parser, is_top_level=True)
    sub = parser.add_subparsers(dest="command", required=True)

    _add_common_args(sub.add_parser("init", help="store ディレクトリ一式を初期化"))

    p_register = sub.add_parser("register", help="候補を登録")
    _add_common_args(p_register)
    p_register.add_argument("--overlay", required=True, help="overlay ディレクトリ")
    p_register.add_argument("--target", required=True, help="対象（claude-harness | skill:<name>）")
    p_register.add_argument("--parent", default=None, help="親候補の cand_id")
    p_register.add_argument("--description", default="", help="候補の説明")
    p_register.add_argument(
        "--slug", default=None, help="cand_id 用の slug（省略時は description から生成）"
    )
    p_register.add_argument("--source-commit", default=None, help="対象コミット（既定: HEAD）")

    p_frontier = sub.add_parser("frontier", help="Pareto frontier を算出")
    _add_common_args(p_frontier)
    p_frontier.add_argument("--rebuild", action="store_true", help="frontier.json を再生成")

    p_status = sub.add_parser("status", help="候補群の状態表示")
    _add_common_args(p_status)
    p_status.add_argument("--candidate", default=None, help="対象候補の cand_id")

    p_purge = sub.add_parser("purge", help="古い世代・retired 候補を削除")
    _add_common_args(p_purge)
    p_purge.add_argument(
        "--keep-generations", type=int, default=None, help="保持世代数（既定: config 値）"
    )

    for stub_name in _PHASE_1B_STUBS:
        _add_common_args(
            sub.add_parser(stub_name, help=f"（Phase 1b/2 未実装スタブ: {stub_name}）")
        )

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    """サブコマンドへ振り分ける。"""
    if args.command in _PHASE_1B_STUBS:
        return cmd_phase1b_stub(args.command)
    if args.command == "init":
        return cmd_init(args.project, args.json)
    if args.command == "register":
        return cmd_register(
            args.project,
            args.overlay,
            args.target,
            args.parent,
            args.description,
            args.slug,
            args.source_commit,
            args.json,
        )
    if args.command == "frontier":
        return cmd_frontier(args.project, args.rebuild, args.json)
    if args.command == "status":
        return cmd_status(args.project, args.candidate, args.json)
    if args.command == "purge":
        return cmd_purge(args.project, args.keep_generations, args.json)
    return EXIT_VALIDATION_ERROR


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。"""
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except mh.MetaHarnessRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR


if __name__ == "__main__":
    sys.exit(main())
