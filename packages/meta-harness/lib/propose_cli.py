"""`orchex meta propose` コマンド実装。

トップレベル script は薄い argparse dispatcher に留め、本モジュールが Phase 2 M4 の
propose pipeline を担う。後続 CLI milestone を追加しても `scripts/meta_harness.py` を
肥大化させないための分割。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _LIB_DIR.parent
_SCHEMA_DIR = _PACKAGE_DIR / "schemas"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import isolation as iso  # noqa: E402
import meta_harness_common as mh  # noqa: E402
import proposer as prop  # noqa: E402
import proposer_backend as pb  # noqa: E402

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_VALIDATION_ERROR = 2
EXIT_LOCK_CONFLICT = 3


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


def _resolve_context(project: str) -> tuple[Path, dict] | None:
    project_dir = Path(project).resolve()
    config = mh.load_config(project_dir)
    try:
        main_root = mh.resolve_main_root(project_dir, config)
    except mh.MetaHarnessRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return main_root, config


def _emit(data: dict, as_json: bool, human_lines: list[str] | None = None) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    for line in human_lines or [json.dumps(data, ensure_ascii=False)]:
        print(line)


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
    parent_id = _select_proposal_parent(
        main_root,
        config,
        snapshot.frontier_doc,
        target=target,
        focus_candidate=focus_candidate,
    )
    source_commit = _proposal_source_commit(main_root, config, project_dir, parent_id)
    focus_run_ids = _select_focus_run_ids(
        snapshot.ledger_events,
        target=target,
        focus_run=focus_run,
        max_focus_runs=_proposer_max_focus_runs(config),
    )
    valid_based_on_run_ids = _citable_run_ids(snapshot, target)
    if not valid_based_on_run_ids:
        raise prop.ProposerError(f"no citable non-holdout runs for target: {target}")
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
                valid_based_on_run_ids=valid_based_on_run_ids,
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
    valid_based_on_run_ids: tuple[str, ...],
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
            valid_based_on_run_ids=valid_based_on_run_ids,
            focus_candidate_id=parent_id,
        )
        launch = iso.resolve_isolation_backend(
            view_dir=view.path,
            main_root=main_root,
            config=config,
            ephemeral_home=home,
            proposer_tool=tool,
        )
        try:
            result = pb.launch_proposer_backend(
                view_dir=view.path,
                prompt=prompt,
                schema_dir=_SCHEMA_DIR,
                config=config,
                isolation_launch=launch,
                ephemeral_home=home,
                allowed_based_on_runs=valid_based_on_run_ids,
            )
        finally:
            _cleanup_isolation_launch(launch)
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
            included_run_ids=frozenset(valid_based_on_run_ids),
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


def _cleanup_isolation_launch(launch: iso.IsolationLaunch) -> None:
    if launch.owned_settings_dir is not None:
        shutil.rmtree(launch.owned_settings_dir, ignore_errors=True)


@contextmanager
def _temporary_empty_dir():
    path = Path(tempfile.mkdtemp(prefix="meta-harness-proposer-home-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _select_proposal_parent(
    main_root: Path,
    config: dict,
    frontier_doc: dict,
    *,
    target: str,
    focus_candidate: str | None,
) -> str | None:
    if focus_candidate:
        manifest = mh.read_candidate_manifest(main_root, config, focus_candidate)
        if manifest is None:
            raise prop.ProposerError(f"unknown focus candidate: {focus_candidate}")
        candidate_target = manifest.get("target")
        if candidate_target != target:
            raise prop.ProposerError(
                f"focus candidate target mismatch for {focus_candidate}: "
                f"expected {target}, got {candidate_target}"
            )
        return focus_candidate
    frontier_ids = [
        str(cand_id)
        for cand_id in frontier_doc.get("frontier", [])
        if _candidate_matches_target(main_root, config, str(cand_id), target)
    ]
    if not frontier_ids:
        return None
    points = [p for p in frontier_doc.get("points", []) if isinstance(p, dict)]
    quality_by_id = {str(p.get("cand_id")): float(p.get("quality_mean") or 0.0) for p in points}
    return max(frontier_ids, key=lambda cand_id: quality_by_id.get(cand_id, 0.0))


def _candidate_matches_target(main_root: Path, config: dict, cand_id: str, target: str) -> bool:
    manifest = mh.read_candidate_manifest(main_root, config, cand_id)
    return manifest is not None and manifest.get("target") == target


def _citable_run_ids(snapshot: prop.FilteredStoreSnapshot, target: str) -> tuple[str, ...]:
    visible_run_ids = set(snapshot.non_holdout_run_ids)
    return tuple(
        sorted(
            {
                str(event["run_id"])
                for event in snapshot.ledger_events
                if event.get("event") == "run_completed"
                and not bool(event.get("holdout"))
                and event.get("target") == target
                and event.get("run_id") in visible_run_ids
            }
        )
    )


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
        head = mh.git_head(project_dir)
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
            manifest = mh.build_candidate_manifest(
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
            candidate_dir = mh.register_candidate(
                main_root,
                config,
                cand_id=cand_id,
                manifest=manifest,
                overlay_dir=overlay_dir,
                overlay_files=overlay_files,
            )
            try:
                mh.append_ledger_event(
                    main_root,
                    config,
                    _proposer_registered_event(
                        cand_id, parent_id, generation, target, proposal, tokens_used
                    ),
                )
            except BaseException:
                shutil.rmtree(candidate_dir, ignore_errors=True)
                rollback_status = "rolled back" if not candidate_dir.exists() else "rollback failed"
                print(
                    f"warning: {rollback_status} candidate after ledger append failure: {cand_id}",
                    file=sys.stderr,
                )
                raise
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
