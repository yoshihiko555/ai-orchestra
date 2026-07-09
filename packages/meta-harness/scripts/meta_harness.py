#!/usr/bin/env python3
"""meta-harness CLI（`orchex meta <sub>`、Phase 1a/1b/2）。

docs/design/meta-harness-detailed.md が正本。実装済みのサブコマンド:
- init       store ディレクトリ一式を冪等に初期化する（Phase 1a）
- register   overlay を検証し候補を immutable に登録する（Phase 1a）
- frontier   Pareto frontier を算出する（--rebuild で frontier.json を再生成、Phase 1a）
- status     候補群の畳み込み状態を表示する（Phase 1a）
- purge      古い世代・retired 候補を削除する（frontier/promoted/予約中は保護、Phase 1a）
- evaluate   CLI capability gate → worktree ライフサイクル → oracle 判定を実行する（Phase 1b）
- propose    filtered view から候補 overlay を提案・登録する（Phase 2 M4）

`promote` / `loop` は Phase 2/3 のスタブ（exit 2）。

exit code（Sec6）: 0 成功 / 1 実行時エラー / 2 入力・スキーマ検証エラー / 3 lock 取得失敗・排他競合。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _SCRIPT_DIR.parent
_LIB_DIR = _PACKAGE_DIR / "lib"
_SCHEMA_DIR = _PACKAGE_DIR / "schemas"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import evaluator as ev  # noqa: E402
import isolation as iso  # noqa: E402
import meta_harness_common as mh  # noqa: E402
import proposer as prop  # noqa: E402
import proposer_backend as pb  # noqa: E402

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_VALIDATION_ERROR = 2
EXIT_LOCK_CONFLICT = 3

_PHASE_2_3_STUBS = ("promote", "loop")


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
    created_by: str = "human",
) -> dict:
    return {
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": parent_id,
        "generation": generation,
        "created_at": _now_iso(),
        "created_by": created_by,
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
    # 入ることは正常な運用）し、末尾の run_completed が holdout の場合もある。points の
    # 比較スコープ選定（mh.aggregate_run_points）と全く同じ「最新の non-holdout
    # run_completed」（mh.latest_non_holdout_run_completed）を hash メタデータにも使う。
    # これを揃えないと、points は non-holdout の hash ペアで計算されているのに
    # suite_hash/evaluator_hash だけ holdout（または末尾イベント）のものになる不整合が生じる。
    latest = mh.latest_non_holdout_run_completed(events)
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

    if rebuild:
        # 【判断】PR #162 レビュー指摘 (FIX P2): frontier 計算（ledger 読み込み込み）を
        # lock 取得前に行うと、lock 待ちの間に別 writer が run_completed を追記した場合、
        # 「古い points + （lock 内で再読した）新しい ledger_line_count」という不整合な
        # キャッシュができてしまい、しかも ledger_line_count が一致してしまうため
        # status の陳腐化警告も出ない。修正: ledger 読み込み + frontier 計算 +
        # frontier_updated 追記 + キャッシュ書き込みを、すべて同一の store_lock ブロック
        # 内で行う。
        try:
            with mh.store_lock(main_root, config):
                frontier_doc = _compute_frontier(main_root, config)
                event = {
                    "event": "frontier_updated",
                    "ts": mh.now_iso(),
                    "schema_version": "1.0",
                    "frontier": frontier_doc["frontier"],
                    "dominated": frontier_doc["dominated"],
                }
                mh.append_ledger_event(main_root, config, event)
                frontier_doc["ledger_line_count"] = len(mh.read_ledger_events(main_root, config))
                mh.write_frontier_cache(main_root, config, frontier_doc)
        except mh.LockAcquisitionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_LOCK_CONFLICT
    else:
        frontier_doc = _compute_frontier(main_root, config)

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
    cand_dir = mh.candidates_dir(main_root, config) / cand_id
    if cand_dir.is_dir():
        shutil.rmtree(cand_dir)


def cmd_propose(
    project: str,
    target: str,
    focus_run: str | None,
    focus_candidate: str | None,
    as_json: bool,
) -> int:
    """filtered view を構築し proposer を 1 回起動して候補登録する（Sec11）。"""
    ctx = _resolve_context(project)
    if ctx is None:
        return EXIT_VALIDATION_ERROR
    main_root, config = ctx
    project_dir = Path(project).resolve()

    try:
        snapshot = _snapshot_propose_store(main_root, config)
        cand_id = _run_propose_pipeline(
            main_root=main_root,
            config=config,
            project_dir=project_dir,
            target=target,
            focus_run=focus_run,
            focus_candidate=focus_candidate,
            snapshot=snapshot,
        )
    except pb.ProposerRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except (
        prop.ProposerError,
        prop.ViewBuildError,
        iso.IsolationError,
        ValueError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    except mh.LockAcquisitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_CONFLICT

    _emit({"status": "ok", "cand_id": cand_id}, as_json, [f"proposed candidate {cand_id}"])
    return EXIT_OK


def _snapshot_propose_store(main_root: Path, config: dict) -> prop.FilteredStoreSnapshot:
    with mh.store_lock(main_root, config):
        return prop.snapshot_filtered_store(main_root, config)


def _run_propose_pipeline(
    *,
    main_root: Path,
    config: dict,
    project_dir: Path,
    target: str,
    focus_run: str | None,
    focus_candidate: str | None,
    snapshot: prop.FilteredStoreSnapshot,
) -> str:
    proposer_cfg = config.get("proposer") or {}
    tool = proposer_cfg.get("tool", "codex")
    parent_id = _select_proposal_parent(main_root, config, snapshot.frontier_doc, focus_candidate)
    source_commit = _proposal_source_commit(main_root, config, project_dir, parent_id)
    focus_run_ids = _select_focus_run_ids(
        snapshot.ledger_events,
        target=target,
        focus_run=focus_run,
        max_focus_runs=_proposer_max_focus_runs(config),
    )
    with _temporary_proposer_home(tool) as home:
        view = prop.build_filtered_view(
            main_root=main_root,
            config=config,
            source_commit=source_commit,
            snapshot=snapshot,
        )
        try:
            return _launch_and_register_proposal(
                main_root=main_root,
                config=config,
                target=target,
                parent_id=parent_id,
                source_commit=source_commit,
                focus_run_ids=focus_run_ids,
                focus_candidate=focus_candidate,
                view=view,
                home=home,
                tool=tool,
                frontier_doc=snapshot.frontier_doc,
            )
        finally:
            view.cleanup()


@contextmanager
def _temporary_proposer_home(tool: str):
    if tool == "codex":
        with pb.temporary_codex_home() as home:
            yield home
        return
    with _temporary_empty_dir() as home:
        yield home


def _launch_and_register_proposal(
    *,
    main_root: Path,
    config: dict,
    target: str,
    parent_id: str | None,
    source_commit: str,
    focus_run_ids: tuple[str, ...],
    focus_candidate: str | None,
    view: prop.FilteredView,
    home: Path,
    tool: str,
    frontier_doc: dict,
) -> str:
    proposal_obj = None
    try:
        prompt = prop.render_proposer_prompt(
            view_dir=view.path,
            frontier_doc=frontier_doc,
            config=config,
            package_dir=_PACKAGE_DIR,
            target=target,
            focus_run_ids=focus_run_ids,
            focus_candidate_id=focus_candidate,
        )
        launch = iso.resolve_isolation_backend(
            view_dir=view.path,
            main_root=main_root,
            config=config,
            ephemeral_home=home,
            proposer_tool=tool,
        )
        result = pb.launch_proposer_backend(
            view_dir=view.path,
            prompt=prompt,
            schema_dir=_SCHEMA_DIR,
            config=config,
            isolation_launch=launch,
        )
        proposal_obj = result.proposal
        if result.tokens_used is None:
            print("warning: proposer backend did not report tokens used", file=sys.stderr)
        return _register_proposed_candidate(
            main_root=main_root,
            config=config,
            target=target,
            parent_id=parent_id,
            source_commit=source_commit,
            proposal=result.proposal,
            included_run_ids=view.included_run_ids,
            tokens_used=result.tokens_used,
        )
    except pb.ProposerRuntimeError:
        raise
    except prop.ProposerError as exc:
        raw_output = _safe_read_text(view.path / "proposal-output.json")
        rejected_path = prop.save_rejected_proposal(
            main_root=main_root,
            config=config,
            reason=str(exc),
            proposal=proposal_obj,
            raw_output=raw_output,
        )
        raise pb.ProposalValidationError(f"{exc}; rejected saved to {rejected_path}") from exc


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else None
    except OSError:
        return None


@contextmanager
def _temporary_empty_dir():
    path = Path(tempfile.mkdtemp(prefix="meta-harness-proposer-home-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _select_proposal_parent(
    main_root: Path, config: dict, frontier_doc: dict, focus_candidate: str | None
) -> str | None:
    if focus_candidate:
        if mh.read_candidate_manifest(main_root, config, focus_candidate) is None:
            raise prop.ProposerError(f"unknown focus candidate: {focus_candidate}")
        return focus_candidate
    frontier_ids = [str(cand_id) for cand_id in frontier_doc.get("frontier", [])]
    if not frontier_ids:
        return None
    points = [p for p in frontier_doc.get("points", []) if isinstance(p, dict)]
    quality_by_id = {str(p.get("cand_id")): float(p.get("quality_mean") or 0.0) for p in points}
    return max(frontier_ids, key=lambda cand_id: quality_by_id.get(cand_id, 0.0))


def _select_focus_run_ids(
    events: tuple[dict, ...],
    *,
    target: str,
    focus_run: str | None,
    max_focus_runs: int,
) -> tuple[str, ...]:
    if focus_run is not None:
        event = _find_run_completed_event(events, focus_run)
        if event is None:
            raise prop.ProposerError(f"unknown focus run: {focus_run}")
        if bool(event.get("holdout")):
            raise prop.ProposerError(f"focus run is holdout and cannot be exposed: {focus_run}")
        if event.get("target") != target:
            raise prop.ProposerError(
                f"focus run target mismatch for {focus_run}: {event.get('target')}"
            )
        return (focus_run,)
    if max_focus_runs <= 0:
        return ()
    selected: list[str] = []
    seen: set[str] = set()
    for event in reversed(events):
        if event.get("event") != "run_completed" or bool(event.get("holdout")):
            continue
        if event.get("target") != target or event.get("verdict") not in ("fail", "error"):
            continue
        run_id = event.get("run_id")
        if not run_id or str(run_id) in seen:
            continue
        seen.add(str(run_id))
        selected.append(str(run_id))
        if len(selected) >= max_focus_runs:
            break
    return tuple(selected)


def _find_run_completed_event(events: tuple[dict, ...], run_id: str) -> dict | None:
    for event in events:
        if event.get("event") == "run_completed" and event.get("run_id") == run_id:
            return event
    return None


def _proposer_max_focus_runs(config: dict) -> int:
    value = (config.get("proposer") or {}).get("max_focus_runs", 5)
    try:
        max_focus_runs = int(value)
    except (TypeError, ValueError) as exc:
        raise prop.ProposerError(
            f"proposer.max_focus_runs must be an integer, got: {value!r}"
        ) from exc
    if max_focus_runs < 0:
        raise prop.ProposerError(f"proposer.max_focus_runs must be >= 0, got: {max_focus_runs}")
    return max_focus_runs


def _proposal_source_commit(
    main_root: Path, config: dict, project_dir: Path, parent_id: str | None
) -> str:
    if parent_id is None:
        head = _git_head(project_dir)
        if head is None:
            raise prop.ProposerError("could not resolve source_commit (git rev-parse HEAD failed)")
        return head
    manifest = mh.read_candidate_manifest(main_root, config, parent_id)
    if manifest is None:
        raise prop.ProposerError(f"parent candidate not found: {parent_id}")
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str):
        raise prop.ProposerError(f"parent candidate missing source_commit: {parent_id}")
    return source_commit


def _register_proposed_candidate(
    *,
    main_root: Path,
    config: dict,
    target: str,
    parent_id: str | None,
    source_commit: str,
    proposal: dict,
    included_run_ids: frozenset[str],
    tokens_used: int | None,
) -> str:
    max_overlay_bytes = (config.get("proposer") or {}).get("max_overlay_bytes", 200000)
    with tempfile.TemporaryDirectory(prefix="meta-harness-proposal-overlay-") as raw_overlay:
        overlay_dir = Path(raw_overlay)
        overlay_files = prop.materialize_overlay_from_proposal(
            proposal, overlay_dir, max_overlay_bytes=max_overlay_bytes
        )
        violations = mh.validate_overlay(overlay_dir, config)
        if violations:
            raise prop.ProposerError("; ".join(violations[:5]))
        with mh.store_lock(main_root, config):
            events = mh.read_ledger_events(main_root, config)
            run_errors = prop.validate_based_on_runs(
                proposal,
                events=events,
                target=target,
                included_run_ids=included_run_ids,
            )
            if run_errors:
                raise prop.ProposerError("; ".join(run_errors[:5]))
            cand_id = mh.generate_cand_id(str(proposal["theme"]))
            generation = mh.next_generation(main_root, config, parent_id)
            manifest = _build_manifest(
                cand_id=cand_id,
                parent_id=parent_id,
                generation=generation,
                target=target,
                source_commit=source_commit,
                config_hash=mh.compute_config_hash(overlay_dir, config),
                overlay_files=overlay_files,
                description=str(proposal["hypothesis"]),
                created_by="proposer",
            )
            _validate_proposer_registration(manifest, overlay_files, proposal, tokens_used)
            mh.register_candidate(
                main_root,
                config,
                cand_id=cand_id,
                manifest=manifest,
                overlay_dir=overlay_dir,
                overlay_files=overlay_files,
            )
            mh.append_ledger_event(
                main_root,
                config,
                _proposer_registered_event(
                    cand_id, parent_id, generation, target, proposal, tokens_used
                ),
            )
            return cand_id


def _validate_proposer_registration(
    manifest: dict, overlay_files: list[str], proposal: dict, tokens_used: int | None
) -> None:
    manifest_schema = mh.load_schema(_SCHEMA_DIR, "candidate.manifest.schema.json")
    overlay_schema = mh.load_schema(_SCHEMA_DIR, "overlay.schema.json")
    ledger_schema = mh.load_schema(_SCHEMA_DIR, "ledger.event.schema.json")
    errors = mh.validate_against_schema(manifest, manifest_schema, _SCHEMA_DIR)
    errors += mh.validate_against_schema(
        {"schema_version": "1.0", "files": overlay_files}, overlay_schema, _SCHEMA_DIR
    )
    event = _proposer_registered_event(
        manifest["cand_id"],
        manifest["parent_id"],
        manifest["generation"],
        manifest["target"],
        proposal,
        tokens_used,
    )
    errors += mh.validate_against_schema(
        event, ledger_schema["$defs"]["candidate_registered"], _SCHEMA_DIR
    )
    if errors:
        raise prop.ProposerError("; ".join(errors[:5]))


def _proposer_registered_event(
    cand_id: str,
    parent_id: str | None,
    generation: int,
    target: str,
    proposal: dict,
    tokens_used: int | None,
) -> dict:
    proposal_event = {
        "theme": str(proposal["theme"]),
        "based_on_runs": [str(run_id) for run_id in proposal["based_on_runs"]],
        # codex stdout から USD は得られないため、捏造せず tokens_used を別記録する。
        "cost_usd": 0.0,
    }
    if tokens_used is not None:
        proposal_event["tokens_used"] = tokens_used
    return {
        "event": "candidate_registered",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": parent_id,
        "generation": generation,
        "target": target,
        "created_by": "proposer",
        "proposal": proposal_event,
    }


def cmd_phase23_stub(sub: str) -> int:
    """Phase 2/3 未実装サブコマンド。"""
    print(
        f"'{sub}' is not implemented yet. See docs/design/meta-harness-detailed.md Sec9"
        " for the phase boundary.",
        file=sys.stderr,
    )
    return EXIT_VALIDATION_ERROR


def cmd_evaluate(
    project: str,
    candidate: str,
    scenario_ids: list[str] | None,
    repeat: int | None,
    as_json: bool,
) -> int:
    """候補をシナリオ評価する（Sec2, Sec6 `evaluate`）。"""
    ctx = _resolve_context(project)
    if ctx is None:
        return EXIT_VALIDATION_ERROR
    main_root, config = ctx
    project_dir = Path(project).resolve()

    if not mh.CAND_ID_PATTERN.match(candidate):
        # `candidate` はこの後 `candidates_dir(...) / candidate / "manifest.json"` として
        # パス結合される。`cand-...` の登録済み形式に一致しない値（`../` トラバーサル等）は
        # manifest 読み込み前に拒否する。
        print(f"error: invalid candidate id: {candidate}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    manifest = mh.read_candidate_manifest(main_root, config, candidate)
    if manifest is None:
        print(f"error: unknown candidate: {candidate}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        with mh.evaluate_lock(main_root, config):
            return _run_evaluate_under_lock(
                main_root, config, project_dir, candidate, manifest, scenario_ids, repeat, as_json
            )
    except mh.LockAcquisitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_CONFLICT


def _run_evaluate_under_lock(
    main_root: Path,
    config: dict,
    project_dir: Path,
    candidate: str,
    manifest: dict,
    scenario_ids: list[str] | None,
    repeat: int | None,
    as_json: bool,
) -> int:
    """evaluate.lock 保持下での capability gate + 評価実行本体（`cmd_evaluate` から分離）。"""
    caps = ev.check_cli_capabilities(config)
    if not caps.ok:
        print(f"error: CLI capability gate failed: {caps.reason}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        results = ev.evaluate_candidate(
            main_root=main_root,
            config=config,
            schema_dir=_SCHEMA_DIR,
            package_dir=_PACKAGE_DIR,
            project_dir=project_dir,
            cand_id=candidate,
            manifest=manifest,
            scenario_ids=scenario_ids,
            repeat_override=repeat,
            cli_capabilities=caps.as_dict(),
        )
    except (ValueError, OSError, ev.yaml.YAMLError) as exc:
        # `load_scenario()` の `path.read_text()` / `yaml.safe_load()` 由来の OSError /
        # yaml.YAMLError も ValueError と同様に入力検証エラーとして扱う（traceback を
        # main() まで漏らさない、CodeRabbit 指摘）。
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    _emit(
        {"status": "ok", "runs": results},
        as_json,
        [f"{r['run_id']}: {r['verdict']} (quality={r['quality_score']:.2f})" for r in results],
    )
    return EXIT_OK if all(r["verdict"] != "error" for r in results) else EXIT_RUNTIME_ERROR


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

    p_evaluate = sub.add_parser("evaluate", help="候補をシナリオ評価する")
    _add_common_args(p_evaluate)
    p_evaluate.add_argument("--candidate", required=True, help="評価対象の cand_id")
    p_evaluate.add_argument(
        "--scenario",
        action="append",
        default=None,
        help="評価するシナリオ id（複数指定可、省略時は suite 内の全シナリオ）",
    )
    p_evaluate.add_argument(
        "--repeat", type=int, default=None, help="試行回数（省略時はシナリオの repeat 値）"
    )

    p_propose = sub.add_parser("propose", help="filtered view から候補 overlay を提案・登録する")
    _add_common_args(p_propose)
    p_propose.add_argument("--target", required=True, help="対象（claude-harness | skill:<name>）")
    p_propose.add_argument("--focus-run", default=None, help="重点分析する run_id")
    p_propose.add_argument("--focus-candidate", default=None, help="親候補として使う cand_id")

    for stub_name in _PHASE_2_3_STUBS:
        _add_common_args(sub.add_parser(stub_name, help=f"（Phase 2/3 未実装スタブ: {stub_name}）"))

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    """サブコマンドへ振り分ける。"""
    if args.command in _PHASE_2_3_STUBS:
        return cmd_phase23_stub(args.command)
    if args.command == "propose":
        return cmd_propose(
            args.project, args.target, args.focus_run, args.focus_candidate, args.json
        )
    if args.command == "evaluate":
        return cmd_evaluate(args.project, args.candidate, args.scenario, args.repeat, args.json)
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
