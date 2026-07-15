"""Validated ledger folding helpers for meta-harness loops."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
_SCHEMA_DIR = _LIB_DIR.parent / "schemas"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import evaluator as ev  # noqa: E402
import meta_harness_common as mh  # noqa: E402

_PACKAGE_DIR = _LIB_DIR.parent


def validate_event(event: dict, definition: str) -> None:
    schema = mh.load_schema(_SCHEMA_DIR, "ledger.event.schema.json")
    if event.get("event") != definition:
        raise ValueError(f"expected ledger event {definition!r}, got: {event.get('event')!r}")
    errors = mh.validate_against_schema(event, schema, _SCHEMA_DIR)
    if errors:
        raise ValueError("; ".join(errors[:5]))
    _validate_finite_numbers(event)


def _validate_finite_numbers(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite ledger number at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_finite_numbers(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_finite_numbers(child, f"{path}[{index}]")


def iteration_events(events: list[dict], loop_id: str) -> dict[int, dict]:
    registrations = _loop_registrations(events, loop_id)
    result: dict[int, dict] = {}
    for event_index, event in enumerate(events):
        if event.get("event") != "loop_iteration" or event.get("loop_id") != loop_id:
            continue
        validate_event(event, "loop_iteration")
        iteration = int(event["iteration"])
        if iteration in result:
            raise ValueError(f"duplicate loop_iteration {iteration} for {loop_id}")
        if not mh.CAND_ID_PATTERN.fullmatch(str(event["cand_id"])):
            raise ValueError("loop_iteration contains an invalid candidate id")
        registration = registrations.get(iteration)
        if registration is None or registration[0] != str(event["cand_id"]):
            raise ValueError(f"loop_iteration {iteration} has no matching candidate registration")
        if registration[1] >= event_index:
            raise ValueError(f"loop_iteration {iteration} precedes its candidate registration")
        result[iteration] = event
    return result


def safe_iteration_events(events: list[dict], loop_id: str) -> list[dict]:
    registrations = _loop_registrations(events, loop_id, fail_closed=False)
    result: dict[int, dict] = {}
    for event_index, event in enumerate(events):
        if event.get("event") != "loop_iteration" or event.get("loop_id") != loop_id:
            continue
        try:
            validate_event(event, "loop_iteration")
            iteration = int(event["iteration"])
            if not mh.CAND_ID_PATTERN.fullmatch(str(event["cand_id"])):
                continue
            registration = registrations.get(iteration)
            if registration is None or registration[0] != str(event["cand_id"]):
                continue
            if registration[1] >= event_index:
                continue
        except Exception:  # noqa: BLE001 - fail-safe stop ignores corrupt rows
            continue
        result.setdefault(iteration, event)
    return [result[key] for key in sorted(result)]


def next_orphan(
    events: list[dict],
    loop_id: str,
    recorded_iterations: set[int],
    target: str | None = None,
    max_iterations: int | None = None,
) -> tuple[int, str] | None:
    registrations = _loop_registrations(events, loop_id)
    if target is not None and _loop_target(events, loop_id) != target:
        raise ValueError(f"loop target mismatch: expected {target!r}")
    completed = max(recorded_iterations, default=0)
    if recorded_iterations != set(range(1, completed + 1)):
        raise ValueError(f"loop iterations are not contiguous for {loop_id}")
    orphans = {
        iteration: cand_id
        for iteration, (cand_id, _) in registrations.items()
        if iteration not in recorded_iterations
    }
    if not orphans:
        return None
    if len(orphans) != 1:
        raise ValueError(f"expected at most one orphan candidate for {loop_id}")
    orphan = next(iter(orphans.items()))
    if orphan[0] != completed + 1:
        raise ValueError(f"orphan iteration is not the next iteration for {loop_id}")
    if max_iterations is not None and orphan[0] > max_iterations:
        raise ValueError(f"orphan iteration exceeds max_iterations for {loop_id}")
    return orphan


def best_quality(
    main_root: Path,
    config: dict,
    events: list[dict],
    target: str,
    loop_id: str | None,
    *,
    max_iteration: int | None = None,
) -> float:
    states = validated_candidate_states(events, config, target)
    candidate_ids = (
        mh.list_candidate_ids(main_root, config)
        if loop_id is None
        else [cand_id for _, cand_id in _loop_candidates(events, loop_id, target, max_iteration)]
    )
    qualities: list[float] = []
    for cand_id in candidate_ids:
        if not mh.CAND_ID_PATTERN.fullmatch(cand_id):
            raise ValueError(f"invalid candidate id: {cand_id!r}")
        manifest = mh.read_candidate_manifest(main_root, config, cand_id)
        if manifest is None or manifest.get("target") != target:
            continue
        if states.get(cand_id, {}).get("status") == "retired":
            continue
        summary = non_holdout_summary(events, config, cand_id, target)
        if summary:
            qualities.append(float(summary["quality_mean"]))
    return max(qualities, default=0.0)


def _loop_candidates(
    events: list[dict], loop_id: str, target: str, max_iteration: int | None
) -> list[tuple[int, str]]:
    registrations = _loop_registrations(events, loop_id)
    if _loop_target(events, loop_id) != target:
        raise ValueError(f"loop target mismatch: expected {target!r}")
    return sorted(
        (iteration, cand_id)
        for iteration, (cand_id, _) in registrations.items()
        if max_iteration is None or iteration <= max_iteration
    )


def non_holdout_summary(
    events: list[dict], config: dict, cand_id: str, target: str
) -> dict[str, Any] | None:
    runs = current_run_events(events, config, target, cand_id, holdout=False)
    if not runs:
        return None
    evaluation = mh.latest_evaluation_completed(
        events,
        cand_id,
        target,
        holdout=False,
        suite_hash=str(runs[0]["suite_hash"]),
        evaluator_hash=str(runs[0]["evaluator_hash"]),
    )
    if evaluation is None or evaluation.get("verdict") != "pass":
        return None
    return {
        "quality_mean": sum(float(run["quality_score"]) for run in runs) / len(runs),
        "critical_pass": all(float(run.get("critical_pass_rate", 0)) == 1.0 for run in runs),
    }


def holdout_quality(
    events: list[dict], config: dict, cand_id: str | None, target: str
) -> float | None:
    if cand_id is None:
        return None
    runs = current_run_events(events, config, target, cand_id, holdout=True)
    if not runs:
        return None
    return sum(float(run["quality_score"]) for run in runs) / len(runs)


def baseline_candidate_id(
    main_root: Path,
    config: dict,
    events: list[dict],
    target: str,
    started_index: int,
) -> str | None:
    pre_loop = events[:started_index]
    states = validated_candidate_states(pre_loop, config, target)
    candidates: list[tuple[float, str]] = []
    for cand_id in mh.list_candidate_ids(main_root, config):
        manifest = mh.read_candidate_manifest(main_root, config, cand_id)
        if manifest is None or manifest.get("target") != target:
            continue
        if states.get(cand_id, {}).get("status") == "retired":
            continue
        summary = non_holdout_summary(pre_loop, config, cand_id, target)
        if summary:
            candidates.append((float(summary["quality_mean"]), cand_id))
    return max(candidates, default=(0.0, None))[1]


def iteration_cost(
    events: list[dict], loop_id: str, iteration: int, cand_id: str, target: str
) -> float:
    proposer_cost = 0.0
    for event in events:
        proposal = event.get("proposal") or {}
        if (
            event.get("event") == "candidate_registered"
            and event.get("cand_id") == cand_id
            and proposal.get("loop_id") == loop_id
            and int(proposal.get("iteration") or 0) == iteration
        ):
            validate_event(event, "candidate_registered")
            proposer_cost = float(proposal.get("cost_usd") or 0.0)
            break
    run_cost = sum(
        _validated_run_cost(event, target)
        for event in events
        if event.get("event") in {"run_completed", "regression_run_completed"}
        and event.get("cand_id") == cand_id
    )
    return proposer_cost + run_cost


def _validate_run_targets(events: list[dict], cand_id: str, target: str) -> None:
    for event in events:
        validate_event(event, "run_completed")
        if event.get("target") != target or event.get("suite_id") != target:
            raise ValueError(f"run target mismatch for loop candidate: {cand_id}")


def evaluation_complete(
    events: list[dict], config: dict, target: str, cand_id: str, *, holdout: bool
) -> bool:
    expected_ids, _, _, _, _ = _scenario_context(config, target, holdout)
    if not expected_ids:
        return True
    return bool(current_run_events(events, config, target, cand_id, holdout=holdout))


def current_run_events(
    events: list[dict], config: dict, target: str, cand_id: str, *, holdout: bool
) -> list[dict]:
    expected_ids, repeat, suite_hash, evaluator_hash, scenario_hashes = _scenario_context(
        config, target, holdout
    )
    if not expected_ids:
        return []
    matching: list[dict] = []
    for event in events:
        if event.get("event") != "run_completed" or event.get("cand_id") != cand_id:
            continue
        validate_event(event, "run_completed")
        if event.get("target") != target or event.get("suite_id") != target:
            raise ValueError(f"run target mismatch for loop candidate: {cand_id}")
        if bool(event.get("holdout")) != holdout:
            continue
        if (event.get("suite_hash"), event.get("evaluator_hash")) != (
            suite_hash,
            evaluator_hash,
        ):
            continue
        if event.get("scenario_id") not in expected_ids:
            raise ValueError(f"unknown scenario run for loop candidate: {cand_id}")
        if event.get("scenario_hash") != scenario_hashes[event["scenario_id"]]:
            raise ValueError(f"scenario hash mismatch for loop candidate: {cand_id}")
        matching.append(event)
    latest = mh._latest_attempt_groups_per_scenario(matching)
    by_scenario: dict[str, list[dict]] = {}
    for event in latest:
        by_scenario.setdefault(str(event["scenario_id"]), []).append(event)
    complete = all(
        _attempt_group_complete(by_scenario.get(scenario_id, []), repeat)
        for scenario_id in expected_ids
    )
    if not mh.candidate_has_evaluation_completed(events, cand_id):
        # Legacy ledger fallback (pre-b92dd84): this candidate predates the
        # evaluation_completed event and can never gain one retroactively, so require only
        # attempt completeness + no error verdict, matching the pre-cross-skill-regression
        # behavior. Re-running `evaluate` for the candidate records evaluation_completed and
        # moves it onto the strict check below.
        if not complete or any(event.get("verdict") == "error" for event in latest):
            return []
        return latest
    evaluation = mh.latest_evaluation_completed(
        events,
        cand_id,
        target,
        holdout=holdout,
        suite_hash=suite_hash,
        evaluator_hash=evaluator_hash,
    )
    evaluation_run_ids = {str(run_id) for run_id in (evaluation or {}).get("own_run_ids") or []}
    latest_run_ids = {str(event.get("run_id")) for event in latest}
    if (
        not complete
        or any(event.get("verdict") == "error" for event in latest)
        or evaluation is None
        or evaluation.get("verdict") == "error"
        or evaluation_run_ids != latest_run_ids
    ):
        return []
    return latest


def _scenario_context(
    config: dict, target: str, holdout: bool
) -> tuple[set[str], int, str, str, dict[str, str]]:
    paths = ev.discover_scenario_paths(ev.scenario_suite_dir(_PACKAGE_DIR, target))
    scenarios = [ev.load_scenario(path, _SCHEMA_DIR) for path in paths]
    expected_ids = {
        str(scenario["id"]) for scenario in scenarios if bool(scenario["holdout"]) == holdout
    }
    scenario_hashes = {
        str(scenario["id"]): ev.compute_scenario_hash(path)
        for path, scenario in zip(paths, scenarios, strict=True)
        if bool(scenario["holdout"]) == holdout
    }
    evaluate_cfg = config.get("evaluate") or {}
    repeat_key = "repeat_frontier" if holdout else "repeat_default"
    repeat = _positive_int(evaluate_cfg.get(repeat_key, 3 if holdout else 1), repeat_key)
    return (
        expected_ids,
        repeat,
        ev.compute_suite_hash(paths),
        ev.compute_configured_evaluator_hash(config),
        scenario_hashes,
    )


def current_hash_pair(config: dict, target: str) -> tuple[str, str]:
    _, _, suite_hash, evaluator_hash, _ = _scenario_context(config, target, False)
    return suite_hash, evaluator_hash


def current_frontier_events(events: list[dict], config: dict, target: str) -> list[dict]:
    """Return ledger state plus only complete, current non-holdout runs for one target."""
    target_candidates: set[str] = set()
    for event in events:
        if event.get("event") != "candidate_registered" or event.get("target") != target:
            continue
        validate_event(event, "candidate_registered")
        cand_id = str(event["cand_id"])
        if not mh.CAND_ID_PATTERN.fullmatch(cand_id):
            raise ValueError(f"invalid candidate id for target {target}: {cand_id!r}")
        if cand_id in target_candidates:
            raise ValueError(f"duplicate candidate registration: {cand_id}")
        target_candidates.add(cand_id)
    _validate_candidate_state_sequence(events, target, target_candidates)
    candidate_ids: set[str] = set()
    for event in events:
        if event.get("event") != "run_completed" or bool(event.get("holdout")):
            continue
        if event.get("target") == target:
            validate_event(event, "run_completed")
            candidate_ids.add(str(event["cand_id"]))
    selected_run_ids = {
        id(event)
        for cand_id in candidate_ids
        for event in current_run_events(events, config, target, cand_id, holdout=False)
    }
    filtered = [
        event
        for event in events
        if event.get("event") != "run_completed" or id(event) in selected_run_ids
    ]
    return filtered


def validated_candidate_states(events: list[dict], config: dict, target: str) -> dict[str, dict]:
    return mh.fold_candidate_states(current_frontier_events(events, config, target))


def _validate_candidate_state_sequence(
    events: list[dict], target: str, target_candidates: set[str]
) -> None:
    states: dict[str, str] = {}
    state_event_kinds = {
        "status_changed",
        "promotion_reserved",
        "promotion_released",
        "promotion_opened",
    }
    terminal = {"promoted", "retired"}
    for event in events:
        kind = event.get("event")
        cand_id = str(event.get("cand_id") or "")
        if kind == "candidate_registered":
            if event.get("target") == target:
                states[cand_id] = "candidate"
            elif cand_id in target_candidates:
                validate_event(event, "candidate_registered")
                raise ValueError(f"candidate registered for multiple targets: {cand_id}")
            continue
        affects_target = cand_id in target_candidates or (
            kind == "run_completed" and event.get("target") == target
        )
        if not affects_target:
            continue
        if kind == "run_completed":
            validate_event(event, "run_completed")
            if cand_id not in states:
                raise ValueError(f"run precedes candidate registration: {cand_id}")
            if states[cand_id] not in terminal:
                states[cand_id] = "evaluated"
        elif kind in {"regression_run_completed", "evaluation_completed"}:
            validate_event(event, str(kind))
            if event.get("target") != target or cand_id not in states:
                raise ValueError(f"evaluation event target mismatch: {cand_id}")
        elif kind == "status_changed":
            validate_event(event, "status_changed")
            if states.get(cand_id) != event["from"]:
                raise ValueError(f"candidate state transition mismatch: {cand_id}")
            states[cand_id] = str(event["to"])
        elif kind in state_event_kinds:
            validate_event(event, str(kind))
            if cand_id not in states:
                raise ValueError(f"candidate state event precedes registration: {cand_id}")


def _attempt_group_complete(events: list[dict], repeat: int) -> bool:
    return (
        len(events) == repeat
        and {int(event["attempt"]) for event in events} == set(range(1, repeat + 1))
        and all(int(event["attempts_total"]) == repeat for event in events)
    )


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer, got: {value!r}")
    if isinstance(value, int):
        parsed = value
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{label} must be a positive integer, got: {value!r}") from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"{label} must be a positive integer, got: {value!r}")
        parsed = int(numeric)
    if parsed < 1:
        raise ValueError(f"{label} must be >= 1, got: {parsed}")
    return parsed


def _loop_registrations(
    events: list[dict], loop_id: str, *, fail_closed: bool = True
) -> dict[int, tuple[str, int]]:
    result: dict[int, tuple[str, int]] = {}
    candidate_iterations: dict[str, int] = {}
    try:
        started_index, target = _loop_started_context(events, loop_id)
    except Exception:
        if fail_closed:
            raise
        return {}
    for event_index, event in enumerate(events):
        proposal = event.get("proposal") or {}
        if event.get("event") != "candidate_registered" or proposal.get("loop_id") != loop_id:
            continue
        try:
            validate_event(event, "candidate_registered")
            iteration = int(proposal["iteration"])
            cand_id = str(event["cand_id"])
            if not mh.CAND_ID_PATTERN.fullmatch(cand_id):
                raise ValueError("invalid candidate id")
            if event.get("target") != target:
                raise ValueError(
                    f"candidate registration target mismatch for iteration {iteration}"
                )
            if event_index <= started_index:
                raise ValueError(
                    f"candidate registration precedes loop_started for iteration {iteration}"
                )
            if iteration in result:
                raise ValueError(f"duplicate candidate registration for iteration {iteration}")
            if cand_id in candidate_iterations:
                raise ValueError(f"candidate registered for multiple iterations: {cand_id}")
        except Exception:
            if fail_closed:
                raise
            continue
        result[iteration] = (cand_id, event_index)
        candidate_iterations[cand_id] = iteration
    return result


def _loop_target(events: list[dict], loop_id: str) -> str:
    return _loop_started_context(events, loop_id)[1]


def _loop_started_context(events: list[dict], loop_id: str) -> tuple[int, str]:
    started = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("event") == "loop_started" and event.get("loop_id") == loop_id
    ]
    if len(started) != 1:
        raise ValueError(f"expected one loop_started event for {loop_id}, got {len(started)}")
    index, event = started[0]
    validate_event(event, "loop_started")
    return index, str(event["target"])


def _validated_run_cost(event: dict, target: str) -> float:
    definition = str(event.get("event"))
    if definition not in {"run_completed", "regression_run_completed"}:
        raise ValueError(f"unexpected run event: {definition}")
    validate_event(event, definition)
    if event.get("target") != target:
        raise ValueError(f"run target mismatch: {event.get('run_id')}")
    if definition == "run_completed" and event.get("suite_id") != target:
        raise ValueError(f"own run suite mismatch: {event.get('run_id')}")
    if definition == "regression_run_completed" and event.get("suite_id") == target:
        raise ValueError(f"regression run suite mismatch: {event.get('run_id')}")
    return float(event["cost"]["total_cost_usd"])
