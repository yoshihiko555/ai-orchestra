from __future__ import annotations

import json
import math
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _LIB_DIR.parent
_SCHEMA_DIR = _PACKAGE_DIR / "schemas"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import evaluator as ev  # noqa: E402
import loop_report  # noqa: E402
import loop_state as state  # noqa: E402
import meta_harness_common as mh  # noqa: E402
import promoter as prm  # noqa: E402
import propose_cli  # noqa: E402
import skill_targets  # noqa: E402

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_VALIDATION_ERROR = 2
EXIT_LOCK_CONFLICT = 3

TARGET_PATTERN = mh.TARGET_PATTERN
LOOP_ID_PATTERN = re.compile(r"^loop-[0-9]{8}-[0-9]{6}-[a-z0-9-]+$")
NORMAL_STOP_REASONS = frozenset({"budget_exhausted", "max_iterations", "divergence", "converged"})


class LoopValidationError(ValueError):
    pass


@dataclass(frozen=True)
class LoopSpec:
    loop_id: str
    target: str
    budget_usd: float | None
    max_iterations: int
    baseline_best_quality: float
    started_index: int


_validate_event = state.validate_event
_iteration_events = state.iteration_events
_safe_iteration_events = state.safe_iteration_events
_next_orphan = state.next_orphan
_best_quality_for_candidates = state.best_quality
_non_holdout_summary = state.non_holdout_summary
_holdout_quality = state.holdout_quality
_iteration_cost = state.iteration_cost
_evaluation_complete = state.evaluation_complete


def cmd_loop(
    project: str,
    target: str | None,
    resume: str | None,
    as_json: bool,
) -> int:
    try:
        ctx = _resolve_context(project)
    except (OSError, ValueError, ev.yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    if ctx is None:
        return EXIT_VALIDATION_ERROR
    main_root, config = ctx
    project_dir = Path(project).resolve()
    try:
        with mh.evaluate_lock(main_root, config):
            exit_code, spec, reason = _execute_locked(
                main_root, config, project_dir, target, resume
            )
    except mh.LockAcquisitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_CONFLICT
    if exit_code != EXIT_OK:
        return exit_code
    assert spec is not None and reason is not None

    payload = _loop_payload(main_root, config, spec, reason)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in (
            f"loop {spec.loop_id} stopped: {reason}",
            f"iterations={payload['iterations']} total_cost_usd={payload['total_cost_usd']:.6f}",
            f"report: {payload['report']}",
        ):
            print(line)
        if reason == "divergence":
            print("notice: no quality improvement was observed for the configured rounds")
    return EXIT_OK


def _execute_locked(
    main_root: Path,
    config: dict,
    project_dir: Path,
    target: str | None,
    resume: str | None,
) -> tuple[int, LoopSpec | None, str | None]:
    spec: LoopSpec | None = None
    try:
        if resume:
            spec = _load_loop_spec(main_root, config, resume, allow_completed=True)
            _validate_target(spec.target)
            if target is not None and target != spec.target:
                raise LoopValidationError(
                    f"resume target mismatch for {resume}: expected {spec.target}, got {target}"
                )
            completed = _normal_stop_event(mh.read_ledger_events_strict(main_root, config), resume)
            if completed is not None:
                report_path = mh.reports_dir(main_root, config) / f"loop-{resume}.md"
                if report_path.is_file():
                    raise LoopValidationError(f"loop already completed: {resume}")
                iterations = state.iteration_events(
                    mh.read_ledger_events_strict(main_root, config), resume
                )
                loop_report.write_report(
                    main_root,
                    config,
                    spec,
                    str(completed["reason"]),
                    [event for _, event in sorted(iterations.items())],
                )
                return EXIT_OK, spec, str(completed["reason"])
        else:
            spec = _start_loop(main_root, config, target)
        return EXIT_OK, spec, _drive_loop(main_root, config, project_dir, spec)
    except mh.LockAcquisitionError as exc:
        _safe_fail_stop(main_root, config, spec, "error")
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_CONFLICT, spec, None
    except KeyboardInterrupt:
        _safe_fail_stop(main_root, config, spec, "interrupted")
        print("error: loop interrupted", file=sys.stderr)
        return EXIT_RUNTIME_ERROR, spec, None
    except propose_cli.pb.ProposerRuntimeError as exc:
        _safe_fail_stop(main_root, config, spec, "error")
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR, spec, None
    except (
        LoopValidationError,
        propose_cli.prop.ProposerError,
        propose_cli.prop.ViewBuildError,
        propose_cli.iso.IsolationError,
        ValueError,
        ev.yaml.YAMLError,
    ) as exc:
        _safe_fail_stop(main_root, config, spec, "error")
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR, spec, None
    except Exception as exc:  # noqa: BLE001 - Sec13-3 fail-safe ledger stop
        _safe_fail_stop(main_root, config, spec, "error")
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR, spec, None


def _resolve_context(project: str) -> tuple[Path, dict] | None:
    project_dir = Path(project).resolve()
    config = mh.load_config(project_dir)
    try:
        return mh.resolve_main_root(project_dir, config), config
    except mh.MetaHarnessRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _start_loop(main_root: Path, config: dict, target: str | None) -> LoopSpec:
    if target is None:
        raise LoopValidationError("--target is required when starting a new loop")
    _validate_target(target)
    events = mh.read_ledger_events_strict(main_root, config)
    baseline = _best_quality_for_candidates(main_root, config, events, target, None)
    proposer_cfg = config.get("proposer") or {}
    max_iterations = _positive_int(proposer_cfg.get("max_iterations", 10), "max_iterations")
    budget_value = (config.get("loop") or {}).get("budget_usd")
    budget = (
        None
        if budget_value is None
        else _finite_float(budget_value, "loop.budget_usd", minimum=0.0)
    )
    loop_id = _generate_loop_id()
    event = {
        "event": "loop_started",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "loop_id": loop_id,
        "target": target,
        "budget_usd": budget,
        "max_iterations": max_iterations,
        "baseline_best_quality": baseline,
    }
    _append_validated_event(main_root, config, event, "loop_started")
    return LoopSpec(loop_id, target, event["budget_usd"], max_iterations, baseline, len(events))


def _restore_loop(
    main_root: Path,
    config: dict,
    loop_id: str,
    requested_target: str | None,
) -> LoopSpec:
    spec = _load_loop_spec(main_root, config, loop_id)
    _validate_target(spec.target)
    if requested_target is not None and requested_target != spec.target:
        raise LoopValidationError(
            f"resume target mismatch for {loop_id}: expected {spec.target}, got {requested_target}"
        )
    return spec


def _load_loop_spec(
    main_root: Path, config: dict, loop_id: str, *, allow_completed: bool = False
) -> LoopSpec:
    if not LOOP_ID_PATTERN.fullmatch(loop_id):
        raise LoopValidationError(f"invalid loop_id: {loop_id!r}")
    events = mh.read_ledger_events_strict(main_root, config)
    matches = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("event") == "loop_started" and event.get("loop_id") == loop_id
    ]
    if len(matches) != 1:
        raise LoopValidationError(
            f"expected one loop_started event for {loop_id}, got {len(matches)}"
        )
    index, started = matches[0]
    _validate_event(started, "loop_started")
    target = str(started["target"])
    stopped = [
        event
        for event in events
        if event.get("event") == "loop_stopped" and event.get("loop_id") == loop_id
    ]
    for event in stopped:
        state.validate_event(event, "loop_stopped")
    if not allow_completed and any(event.get("reason") in NORMAL_STOP_REASONS for event in stopped):
        raise LoopValidationError(f"loop already completed: {loop_id}")
    return LoopSpec(
        loop_id=loop_id,
        target=target,
        budget_usd=(
            None
            if started["budget_usd"] is None
            else _finite_float(started["budget_usd"], "loop_started.budget_usd", minimum=0.0)
        ),
        max_iterations=_positive_int(started["max_iterations"], "loop_started.max_iterations"),
        baseline_best_quality=_finite_float(
            started["baseline_best_quality"], "loop_started.baseline_best_quality"
        ),
        started_index=index,
    )


def _normal_stop_event(events: list[dict], loop_id: str) -> dict | None:
    result = None
    for event in events:
        if event.get("event") != "loop_stopped" or event.get("loop_id") != loop_id:
            continue
        state.validate_event(event, "loop_stopped")
        if event.get("reason") in NORMAL_STOP_REASONS:
            result = event
    return result


def _validate_target(target: str) -> None:
    if not TARGET_PATTERN.fullmatch(target):
        raise LoopValidationError(f"invalid target: {target!r}")
    try:
        ev.validate_target_suite(_PACKAGE_DIR, _SCHEMA_DIR, target)
    except (OSError, ValueError, ev.yaml.YAMLError) as exc:
        raise LoopValidationError(f"could not load target scenarios: {exc}") from exc


def _drive_loop(main_root: Path, config: dict, project_dir: Path, spec: LoopSpec) -> str:
    while True:
        events = mh.read_ledger_events_strict(main_root, config)
        iterations = _iteration_events(events, spec.loop_id)
        orphan = _next_orphan(
            events, spec.loop_id, set(iterations), spec.target, spec.max_iterations
        )
        if orphan is None:
            pending_reason = _post_iteration_stop(events, config, spec)
            if pending_reason:
                _stop_loop(main_root, config, spec, pending_reason)
                return pending_reason
            guard_reason = _pre_iteration_guard(spec, iterations)
            if guard_reason:
                _stop_loop(main_root, config, spec, guard_reason)
                return guard_reason
            iteration = max(iterations, default=0) + 1
            cand_id = _propose_candidate(main_root, config, project_dir, spec, iteration)
        else:
            iteration, cand_id = orphan

        _validate_loop_candidate(main_root, config, spec, iteration, cand_id)
        train_complete = orphan is not None and _evaluation_complete(
            events, config, spec.target, cand_id, holdout=False
        )
        if not train_complete:
            _evaluate_candidate(main_root, config, project_dir, cand_id, holdout=False)
            events = mh.read_ledger_events_strict(main_root, config)
            if not _evaluation_complete(events, config, spec.target, cand_id, holdout=False):
                raise LoopValidationError(f"non-holdout evaluation is incomplete: {cand_id}")
        train_evaluation = mh.latest_evaluation_completed(
            events, cand_id, spec.target, holdout=False
        )
        train_evaluation_id = (train_evaluation or {}).get("evaluation_id")
        if not isinstance(train_evaluation_id, str) or not train_evaluation_id:
            train_evaluation_id = None
        entered_frontier = _candidate_on_frontier(main_root, config, spec.target, cand_id)
        if entered_frontier:
            events = mh.read_ledger_events_strict(main_root, config)
            if not _evaluation_complete(events, config, spec.target, cand_id, holdout=True):
                evaluation_kwargs = (
                    {"evaluation_id": train_evaluation_id}
                    if train_evaluation_id is not None
                    else {}
                )
                _evaluate_candidate(
                    main_root,
                    config,
                    project_dir,
                    cand_id,
                    holdout=True,
                    **evaluation_kwargs,
                )
                events = mh.read_ledger_events_strict(main_root, config)
                if not _evaluation_complete(events, config, spec.target, cand_id, holdout=True):
                    raise LoopValidationError(f"holdout evaluation is incomplete: {cand_id}")
            _retire_if_overfit(main_root, config, spec, cand_id)
        _rebuild_frontier(main_root, config, spec.target)
        _record_iteration(main_root, config, spec, iteration, cand_id)
        reason = _post_iteration_stop(mh.read_ledger_events_strict(main_root, config), config, spec)
        if reason:
            _stop_loop(main_root, config, spec, reason)
            return reason


def _pre_iteration_guard(spec: LoopSpec, iterations: dict[int, dict]) -> str | None:
    ordered = [iterations[key] for key in sorted(iterations)]
    cumulative = sum(float(event["iteration_cost_usd"]) for event in ordered)
    if spec.budget_usd is not None:
        if not ordered and cumulative >= spec.budget_usd:
            return "budget_exhausted"
        if ordered and cumulative + float(ordered[-1]["iteration_cost_usd"]) > spec.budget_usd:
            return "budget_exhausted"
    if max(iterations, default=0) >= spec.max_iterations:
        return "max_iterations"
    return None


def _propose_candidate(
    main_root: Path,
    config: dict,
    project_dir: Path,
    spec: LoopSpec,
    iteration: int,
) -> str:
    snapshot = propose_cli._snapshot_propose_store(main_root, config, spec.target)
    return propose_cli._run_propose_pipeline(
        main_root=main_root,
        config=config,
        project_dir=project_dir,
        target=spec.target,
        focus_run=None,
        focus_candidate=None,
        snapshot=snapshot,
        loop_id=spec.loop_id,
        iteration=iteration,
    )


def _evaluate_candidate(
    main_root: Path,
    config: dict,
    project_dir: Path,
    cand_id: str,
    *,
    holdout: bool,
    evaluation_id: str | None = None,
) -> list[dict]:
    manifest = mh.read_candidate_manifest(main_root, config, cand_id)
    if manifest is None:
        raise LoopValidationError(f"orphan candidate manifest is missing: {cand_id}")
    scenario_ids = _scenario_ids(manifest["target"], holdout=holdout)
    if not scenario_ids:
        return []
    caps = ev.check_cli_capabilities(config, main_root=main_root)
    if not caps.ok:
        raise LoopValidationError(f"CLI capability gate failed: {caps.reason}")
    evaluate_cfg = config.get("evaluate") or {}
    repeat_key = "repeat_frontier" if holdout else "repeat_default"
    repeat = _positive_int(evaluate_cfg.get(repeat_key, 3 if holdout else 1), repeat_key)
    results = ev.evaluate_candidate(
        main_root=main_root,
        config=config,
        schema_dir=_SCHEMA_DIR,
        package_dir=_PACKAGE_DIR,
        project_dir=project_dir,
        cand_id=cand_id,
        manifest=manifest,
        scenario_ids=scenario_ids,
        repeat_override=repeat,
        cli_capabilities=caps.as_dict(),
        evaluation_id=evaluation_id,
    )
    if any(result.get("verdict") == "error" for result in results):
        raise RuntimeError(f"candidate evaluation failed: {cand_id}")
    return results


def _validate_loop_candidate(
    main_root: Path,
    config: dict,
    spec: LoopSpec,
    iteration: int,
    cand_id: str,
) -> None:
    if not mh.CAND_ID_PATTERN.fullmatch(cand_id):
        raise LoopValidationError(f"invalid loop candidate id: {cand_id!r}")
    manifest = mh.read_candidate_manifest(main_root, config, cand_id)
    if manifest is None:
        raise LoopValidationError(f"loop candidate manifest is missing: {cand_id}")
    errors = mh.validate_against_schema(
        manifest,
        mh.load_schema(_SCHEMA_DIR, "candidate.manifest.schema.json"),
        _SCHEMA_DIR,
    )
    if errors:
        raise LoopValidationError("; ".join(errors[:5]))
    if manifest.get("cand_id") != cand_id:
        raise LoopValidationError(f"loop candidate manifest id mismatch: {cand_id}")
    if manifest.get("target") != spec.target:
        raise LoopValidationError(
            f"loop candidate target mismatch for iteration {iteration}: "
            f"expected {spec.target}, got {manifest.get('target')}"
        )
    overlay_dir = mh.candidates_dir(main_root, config) / cand_id / "overlay"
    if spec.target.startswith("skill:"):
        try:
            with skill_targets.materialized_baseline(
                main_root, str(manifest["source_commit"])
            ) as baseline:
                ev.apply_registered_candidate_overlay(
                    main_root=main_root,
                    config=config,
                    manifest=manifest,
                    worktree_dir=baseline,
                    schema_dir=_SCHEMA_DIR,
                )
        except (OSError, ValueError, ev.EvaluatorStageError) as exc:
            raise LoopValidationError(f"loop candidate overlay is invalid: {exc}") from exc
    else:
        violations = mh.validate_overlay(
            overlay_dir, config, target=spec.target, baseline_root=main_root
        )
        if violations:
            raise LoopValidationError(
                f"loop candidate overlay is invalid: {'; '.join(violations[:5])}"
            )
    if mh.list_overlay_files(overlay_dir) != sorted(manifest.get("overlay_files") or []):
        raise LoopValidationError(f"loop candidate overlay manifest mismatch: {cand_id}")
    if mh.compute_config_hash(overlay_dir, config) != manifest.get("config_hash"):
        raise LoopValidationError(f"loop candidate overlay hash mismatch: {cand_id}")


def _scenario_ids(target: str, *, holdout: bool) -> list[str]:
    suite_dir = ev.scenario_suite_dir(_PACKAGE_DIR, target)
    result: list[str] = []
    for path in ev.discover_scenario_paths(suite_dir):
        scenario = ev.load_scenario(path, _SCHEMA_DIR)
        if bool(scenario.get("holdout")) == holdout:
            result.append(str(scenario["id"]))
    return result


def _candidate_on_frontier(main_root: Path, config: dict, target: str, cand_id: str) -> bool:
    events = mh.read_ledger_events_strict(main_root, config)
    frontier = prm._compute_current_frontier(
        state.current_frontier_events(events, config, target), config, target
    )
    expected_hashes = state.current_hash_pair(config, target)
    actual_hashes = (frontier.get("suite_hash"), frontier.get("evaluator_hash"))
    if actual_hashes != expected_hashes:
        raise LoopValidationError(
            f"frontier hash scope is stale: expected {expected_hashes}, got {actual_hashes}"
        )
    return cand_id in set(frontier["frontier"])


def _rebuild_frontier(main_root: Path, config: dict, target: str) -> None:
    with mh.store_lock(main_root, config):
        events = mh.read_ledger_events_strict(main_root, config)
        doc = prm._compute_current_frontier(
            state.current_frontier_events(events, config, target), config, target
        )
        expected_hashes = state.current_hash_pair(config, target)
        actual_hashes = (doc.get("suite_hash"), doc.get("evaluator_hash"))
        if actual_hashes != expected_hashes:
            raise LoopValidationError(
                f"frontier hash scope is stale: expected {expected_hashes}, got {actual_hashes}"
            )
        event = {
            "event": "frontier_updated",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "target": target,
            "frontier": doc["frontier"],
            "dominated": doc["dominated"],
        }
        _validate_event(event, "frontier_updated")
        mh.append_ledger_event(main_root, config, event)
        doc["ledger_line_count"] = len(mh.read_ledger_events_strict(main_root, config))
        mh.write_frontier_cache(main_root, config, doc, target)


def _retire_if_overfit(main_root: Path, config: dict, spec: LoopSpec, cand_id: str) -> bool:
    events = mh.read_ledger_events_strict(main_root, config)
    baseline_id = state.baseline_candidate_id(
        main_root, config, events, spec.target, spec.started_index
    )
    baseline_quality = (
        _holdout_quality(events, config, baseline_id, spec.target) if baseline_id else None
    )
    candidate_quality = _holdout_quality(events, config, cand_id, spec.target)
    if baseline_quality is None or candidate_quality is None:
        return False
    threshold = _finite_float(
        (config.get("proposer") or {}).get("overfit_drop_pt", 15),
        "proposer.overfit_drop_pt",
        minimum=0.0,
    )
    if baseline_quality - candidate_quality <= threshold:
        return False
    states = state.validated_candidate_states(events, config, spec.target)
    current = states.get(cand_id, {}).get("status")
    if current != "evaluated":
        raise LoopValidationError(
            f"cannot retire overfit candidate from state {current}: {cand_id}"
        )
    _append_validated_event(
        main_root,
        config,
        {
            "event": "status_changed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": cand_id,
            "from": "evaluated",
            "to": "retired",
            "reason": "overfit",
        },
        "status_changed",
    )
    return True


def _record_iteration(
    main_root: Path,
    config: dict,
    spec: LoopSpec,
    iteration: int,
    cand_id: str,
) -> None:
    events = mh.read_ledger_events_strict(main_root, config)
    existing = _iteration_events(events, spec.loop_id)
    if iteration in existing:
        return
    before = (
        float(existing[max(existing)]["quality_best_after"])
        if existing
        else spec.baseline_best_quality
    )
    after = _best_quality_for_candidates(
        main_root, config, events, spec.target, spec.loop_id, max_iteration=iteration
    )
    cost = _iteration_cost(events, spec.loop_id, iteration, cand_id, spec.target)
    _append_validated_event(
        main_root,
        config,
        {
            "event": "loop_iteration",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "loop_id": spec.loop_id,
            "iteration": iteration,
            "cand_id": cand_id,
            "quality_best_before": before,
            "quality_best_after": max(spec.baseline_best_quality, after),
            "iteration_cost_usd": cost,
        },
        "loop_iteration",
    )


def _post_iteration_stop(events: list[dict], config: dict, spec: LoopSpec) -> str | None:
    iterations = [event for _, event in sorted(_iteration_events(events, spec.loop_id).items())]
    proposer_cfg = config.get("proposer") or {}
    divergence_rounds = _positive_int(proposer_cfg.get("divergence_rounds", 3), "divergence_rounds")
    epsilon = _finite_float(
        (config.get("loop") or {}).get("quality_epsilon_pt", 0.5),
        "loop.quality_epsilon_pt",
        minimum=0.0,
    )
    if len(iterations) >= divergence_rounds and all(
        float(event["quality_best_after"]) <= float(event["quality_best_before"]) + epsilon
        for event in iterations[-divergence_rounds:]
    ):
        return "divergence"

    convergence = (config.get("loop") or {}).get("convergence") or {}
    if not convergence.get("enabled", True):
        return None
    rounds = _positive_int(convergence.get("rounds", 2), "convergence.rounds")
    band = _finite_float(
        convergence.get("quality_band_pt", 3),
        "loop.convergence.quality_band_pt",
        minimum=0.0,
    )
    if len(iterations) < rounds:
        return None
    if all(
        _iteration_converged(events, config, event, band, spec.target)
        for event in iterations[-rounds:]
    ):
        return "converged"
    return None


def _iteration_converged(
    events: list[dict], config: dict, iteration: dict, band: float, target: str
) -> bool:
    cand_id = str(iteration["cand_id"])
    if (
        state.validated_candidate_states(events, config, target).get(cand_id, {}).get("status")
        != "evaluated"
    ):
        return False
    summary = _non_holdout_summary(events, config, cand_id, target)
    return (
        bool(summary)
        and bool(summary["critical_pass"])
        and abs(float(summary["quality_mean"]) - float(iteration["quality_best_after"])) <= band
    )


def _stop_loop(main_root: Path, config: dict, spec: LoopSpec, reason: str) -> None:
    events = mh.read_ledger_events_strict(main_root, config)
    iterations = _iteration_events(events, spec.loop_id)
    event = {
        "event": "loop_stopped",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "loop_id": spec.loop_id,
        "reason": reason,
        "iterations": max(iterations, default=0),
        "total_cost_usd": sum(float(item["iteration_cost_usd"]) for item in iterations.values()),
    }
    _append_validated_event(main_root, config, event, "loop_stopped")
    loop_report.write_report(
        main_root,
        config,
        spec,
        reason,
        [event for _, event in sorted(iterations.items())],
    )


def _safe_fail_stop(
    main_root: Path,
    config: dict,
    spec: LoopSpec | None,
    reason: str,
) -> None:
    if spec is None:
        return
    existing_events = mh.read_ledger_events(main_root, config)
    for event in existing_events:
        if event.get("event") != "loop_stopped" or event.get("loop_id") != spec.loop_id:
            continue
        try:
            state.validate_event(event, "loop_stopped")
        except ValueError:
            continue
        if event.get("reason") in NORMAL_STOP_REASONS:
            return
    before = sum(
        1
        for event in existing_events
        if event.get("event") == "loop_stopped"
        and event.get("loop_id") == spec.loop_id
        and event.get("reason") == reason
    )
    try:
        _stop_loop(main_root, config, spec, reason)
    except Exception:  # noqa: BLE001 - original failure must remain visible
        events = mh.read_ledger_events(main_root, config)
        after = sum(
            1
            for event in events
            if event.get("event") == "loop_stopped"
            and event.get("loop_id") == spec.loop_id
            and event.get("reason") == reason
        )
        already_recorded = after > before
        if not already_recorded:
            safe_iterations = _safe_iteration_events(events, spec.loop_id)
            event = {
                "event": "loop_stopped",
                "ts": mh.now_iso(),
                "schema_version": "1.0",
                "loop_id": spec.loop_id,
                "reason": reason,
                "iterations": max((int(item["iteration"]) for item in safe_iterations), default=0),
                "total_cost_usd": sum(
                    float(item["iteration_cost_usd"]) for item in safe_iterations
                ),
            }
            try:
                _validate_event(event, "loop_stopped")
                mh.append_ledger_event(main_root, config, event)
            except Exception:  # noqa: BLE001 - final best-effort fallback
                return
        try:
            safe_iterations = _safe_iteration_events(events, spec.loop_id)
            loop_report.write_report(main_root, config, spec, reason, safe_iterations)
        except Exception:  # noqa: BLE001 - ledger event is the fail-safe contract
            pass


def _append_validated_event(
    main_root: Path,
    config: dict,
    event: dict,
    definition: str,
) -> None:
    _validate_event(event, definition)
    with mh.store_lock(main_root, config):
        mh.append_ledger_event(main_root, config, event)


def _loop_payload(main_root: Path, config: dict, spec: LoopSpec, reason: str) -> dict:
    events = mh.read_ledger_events_strict(main_root, config)
    iterations = _iteration_events(events, spec.loop_id)
    return {
        "status": "ok",
        "loop_id": spec.loop_id,
        "reason": reason,
        "iterations": max(iterations, default=0),
        "total_cost_usd": sum(float(item["iteration_cost_usd"]) for item in iterations.values()),
        "report": str(mh.reports_dir(main_root, config) / f"loop-{spec.loop_id}.md"),
    }


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise LoopValidationError(f"{label} must be a positive integer, got: {value!r}")
    if isinstance(value, int):
        parsed = value
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise LoopValidationError(
                f"{label} must be a positive integer, got: {value!r}"
            ) from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise LoopValidationError(f"{label} must be a positive integer, got: {value!r}")
        parsed = int(numeric)
    if parsed < 1:
        raise LoopValidationError(f"{label} must be >= 1, got: {parsed}")
    return parsed


def _finite_float(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise LoopValidationError(f"{label} must be a finite number, got: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise LoopValidationError(f"{label} must be a finite number, got: {value!r}") from exc
    if not math.isfinite(parsed):
        raise LoopValidationError(f"{label} must be finite, got: {value!r}")
    if minimum is not None and parsed < minimum:
        raise LoopValidationError(f"{label} must be >= {minimum:g}, got: {parsed!r}")
    return parsed


def _generate_loop_id() -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"loop-{timestamp}-{secrets.token_hex(3)}"
