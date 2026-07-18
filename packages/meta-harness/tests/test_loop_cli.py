"""Phase 3 `meta loop` ledger state, stop, resume, and report tests."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tests.module_loader import load_module

helpers = load_module(
    "loop_cli_test_helpers", "packages/meta-harness/tests/loop_cli_test_helpers.py"
)
_HASH = helpers._HASH
_append_run = helpers._append_run
_config = helpers._config
_events = helpers._events
_install_pipeline = helpers._install_pipeline
_register_loop_candidate = helpers._register_loop_candidate
_stub_evaluation_complete = helpers._stub_evaluation_complete
_write_manifest = helpers._write_manifest
loop_cli = helpers.loop_cli
mh = helpers.mh


def _routing_loop_spec(
    project: Path, config: dict, *, max_iterations: int = 10
) -> loop_cli.LoopSpec:
    loop_id = "loop-20260717-120000-routing"
    event = {
        "event": "loop_started",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "loop_id": loop_id,
        "target": "routing-config",
        "budget_usd": None,
        "max_iterations": max_iterations,
        "baseline_best_quality": 0.0,
    }
    mh.append_ledger_event(project, config, event)
    return loop_cli.LoopSpec(loop_id, "routing-config", None, max_iterations, 0.0, 0)


def _append_evaluation(
    project: Path,
    config: dict,
    cand_id: str,
    verdict: str,
    *,
    target: str = "routing-config",
) -> None:
    event = {
        "event": "evaluation_completed",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "evaluation_id": "eval-20260717-120000-deadbeef",
        "cand_id": cand_id,
        "target": target,
        "holdout": False,
        "own_run_ids": [],
        "own_suite_hash": _HASH,
        "evaluator_hash": _HASH,
        "own_critical_pass": verdict == "pass",
        "regression_results": [],
        "verdict": verdict,
        "unverified_impacts": [],
        "evaluation_base_commit": "a" * 40,
        "impacted_targets": [],
        "impact_input_hash": _HASH,
        "regression_cost_usd": 0.0,
    }
    if target == "routing-config":
        event["routing_config_base_hash"] = _HASH
    mh.append_ledger_event(project, config, event)


def test_routing_config_target_reaches_bootstrap_guard_before_proposer(
    git_project: Path, monkeypatch, capsys
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    mh.write_frontier_cache(
        git_project,
        config,
        mh._empty_frontier_doc(config, "routing-config"),
        "routing-config",
    )
    monkeypatch.setattr(loop_cli, "_resolve_context", lambda _project: (git_project, config))

    exit_code = loop_cli.cmd_loop(str(git_project), "routing-config", None, False)

    assert exit_code == loop_cli.EXIT_VALIDATION_ERROR
    assert "no citable non-holdout runs for target: routing-config" in capsys.readouterr().err
    assert any(event.get("event") == "loop_started" for event in _events(git_project, config))


# EV-88
def test_routing_config_rate_limit_allows_only_one_candidate_per_iteration(
    git_project: Path,
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config)
    _register_loop_candidate(git_project, config, spec, 1)

    with pytest.raises(loop_cli.LoopValidationError, match="already has a registered candidate"):
        loop_cli._enforce_routing_config_rate_limits(_events(git_project, config), config, spec, 1)


# EV-88
@pytest.mark.parametrize("verdict", ["fail", "error"])
def test_routing_config_rejected_candidate_starts_ledger_backed_cooldown(
    git_project: Path, verdict: str
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config)
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    _append_evaluation(git_project, config, cand_id, verdict)
    events = _events(git_project, config)

    with pytest.raises(loop_cli.LoopValidationError, match="next allowed iteration is 5"):
        loop_cli._enforce_routing_config_rate_limits(events, config, spec, 4)

    loop_cli._enforce_routing_config_rate_limits(events, config, spec, 5)


# EV-88, EV-89
def test_routing_config_proposal_rejection_maps_to_error_cooldown(
    git_project: Path,
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config)
    event = loop_cli.propose_cli._proposal_rejected_event(
        target="routing-config",
        loop_id=spec.loop_id,
        iteration=1,
    )
    loop_cli.state.validate_event(event, "proposal_rejected")
    mh.append_ledger_event(git_project, config, event)
    events = _events(git_project, config)

    with pytest.raises(loop_cli.LoopValidationError, match="after proposal error"):
        loop_cli._enforce_routing_config_rate_limits(events, config, spec, 4)

    loop_cli._enforce_routing_config_rate_limits(events, config, spec, 5)


# EV-88
def test_routing_config_overfit_retirement_starts_configured_cooldown(
    git_project: Path,
) -> None:
    config = _config(config_patch={"proposer_cooldown_rounds": 1})
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config)
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    mh.append_ledger_event(
        git_project,
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
    )
    events = _events(git_project, config)

    with pytest.raises(loop_cli.LoopValidationError, match="next allowed iteration is 3"):
        loop_cli._enforce_routing_config_rate_limits(events, config, spec, 2)

    loop_cli._enforce_routing_config_rate_limits(events, config, spec, 3)


def test_routing_config_cooldown_default_is_three_rounds() -> None:
    assert mh.DEFAULTS["config_patch"]["proposer_cooldown_rounds"] == 3


def _install_routing_cooldown_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    config: dict,
    proposed_iterations: list[int],
) -> None:
    def propose(_main_root, _config, _project_dir, spec, iteration):
        proposed_iterations.append(iteration)
        return _register_loop_candidate(project, config, spec, iteration)

    monkeypatch.setattr(loop_cli, "_propose_candidate", propose)
    monkeypatch.setattr(loop_cli, "_validate_loop_candidate", lambda *_args: None)
    monkeypatch.setattr(loop_cli, "_evaluate_candidate", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(loop_cli, "_evaluation_complete", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(loop_cli, "_candidate_on_frontier", lambda *_args: False)
    monkeypatch.setattr(loop_cli, "_rebuild_frontier", lambda *_args: None)


def test_routing_config_proposal_rejection_advances_through_cooldown(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(proposer={"divergence_rounds": 10})
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config, max_iterations=5)
    mh.append_ledger_event(
        git_project,
        config,
        loop_cli.propose_cli._proposal_rejected_event(
            target="routing-config",
            loop_id=spec.loop_id,
            iteration=1,
        ),
    )
    proposed_iterations: list[int] = []
    _install_routing_cooldown_pipeline(monkeypatch, git_project, config, proposed_iterations)

    reason = loop_cli._drive_loop(git_project, config, git_project, spec)

    iterations = loop_cli._iteration_events(_events(git_project, config), spec.loop_id)
    assert reason == "max_iterations"
    assert proposed_iterations == [5]
    assert iterations[1]["outcome"] == "proposal_rejected"
    assert [iterations[index]["outcome"] for index in (2, 3, 4)] == [
        "cooldown_wait",
        "cooldown_wait",
        "cooldown_wait",
    ]


def test_routing_config_rejected_proposal_continues_unattended_through_cooldown(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(proposer={"divergence_rounds": 10})
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config, max_iterations=5)
    proposed_iterations: list[int] = []

    def propose(_main_root, _config, _project_dir, active_spec, iteration):
        proposed_iterations.append(iteration)
        if iteration == 1:
            mh.append_ledger_event(
                git_project,
                config,
                loop_cli.propose_cli._proposal_rejected_event(
                    target=active_spec.target,
                    loop_id=active_spec.loop_id,
                    iteration=iteration,
                ),
            )
            raise loop_cli.propose_cli.pb.ProposalValidationError("invalid routing proposal")
        return _register_loop_candidate(git_project, config, active_spec, iteration)

    monkeypatch.setattr(loop_cli, "_propose_candidate", propose)
    monkeypatch.setattr(loop_cli, "_validate_loop_candidate", lambda *_args: None)
    monkeypatch.setattr(loop_cli, "_evaluate_candidate", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(loop_cli, "_evaluation_complete", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(loop_cli, "_candidate_on_frontier", lambda *_args: False)
    monkeypatch.setattr(loop_cli, "_rebuild_frontier", lambda *_args: None)

    reason = loop_cli._drive_loop(git_project, config, git_project, spec)

    iterations = loop_cli._iteration_events(_events(git_project, config), spec.loop_id)
    assert reason == "max_iterations"
    assert proposed_iterations == [1, 5]
    assert iterations[1]["outcome"] == "proposal_rejected"
    assert [iterations[index]["outcome"] for index in (2, 3, 4)] == [
        "cooldown_wait",
        "cooldown_wait",
        "cooldown_wait",
    ]
    assert iterations[5]["outcome"] == "candidate"


def test_routing_config_evaluation_error_continues_unattended_through_cooldown(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(proposer={"divergence_rounds": 10})
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config, max_iterations=5)
    proposed_iterations: list[int] = []
    evaluated_iterations: list[int] = []

    def propose(_main_root, _config, _project_dir, active_spec, iteration):
        proposed_iterations.append(iteration)
        return _register_loop_candidate(git_project, config, active_spec, iteration)

    def evaluate(_main_root, _config, _project_dir, cand_id, *, holdout, **_kwargs):
        assert not holdout
        iteration = int(cand_id.split("-loop-")[1].split("-")[0])
        evaluated_iterations.append(iteration)
        if iteration == 1:
            _append_evaluation(git_project, config, cand_id, "error")
            raise loop_cli.ev.EvaluationBatchError("regression budget exceeded")
        return []

    monkeypatch.setattr(loop_cli, "_propose_candidate", propose)
    monkeypatch.setattr(loop_cli, "_validate_loop_candidate", lambda *_args: None)
    monkeypatch.setattr(loop_cli, "_evaluate_candidate", evaluate)
    monkeypatch.setattr(loop_cli, "_evaluation_complete", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(loop_cli, "_candidate_on_frontier", lambda *_args: False)
    monkeypatch.setattr(loop_cli, "_rebuild_frontier", lambda *_args: None)

    reason = loop_cli._drive_loop(git_project, config, git_project, spec)

    iterations = loop_cli._iteration_events(_events(git_project, config), spec.loop_id)
    assert reason == "max_iterations"
    assert proposed_iterations == [1, 5]
    assert evaluated_iterations == [1, 5]
    assert iterations[1]["outcome"] == "candidate"
    assert [iterations[index]["outcome"] for index in (2, 3, 4)] == [
        "cooldown_wait",
        "cooldown_wait",
        "cooldown_wait",
    ]
    assert iterations[5]["outcome"] == "candidate"


def test_routing_config_evaluation_crash_without_summary_still_fail_stops(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(proposer={"max_iterations": 1})
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config, max_iterations=1)
    monkeypatch.setattr(
        loop_cli,
        "_propose_candidate",
        lambda _main_root, _config, _project_dir, active_spec, iteration: _register_loop_candidate(
            git_project, config, active_spec, iteration
        ),
    )
    monkeypatch.setattr(loop_cli, "_validate_loop_candidate", lambda *_args: None)
    monkeypatch.setattr(
        loop_cli,
        "_evaluate_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            loop_cli.ev.EvaluationBatchError("evaluation machinery crashed")
        ),
    )

    with pytest.raises(loop_cli.ev.EvaluationBatchError, match="machinery crashed"):
        loop_cli._drive_loop(git_project, config, git_project, spec)

    iterations = loop_cli._iteration_events(_events(git_project, config), spec.loop_id)
    assert iterations == {}


@pytest.mark.parametrize("verdict", ["fail", "error"])
def test_routing_config_resume_advances_until_evaluation_cooldown_elapses(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
) -> None:
    config = _config(proposer={"divergence_rounds": 10})
    mh.init_store(git_project, config)
    spec = _routing_loop_spec(git_project, config, max_iterations=5)
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    _append_evaluation(git_project, config, cand_id, verdict)
    loop_cli._record_iteration(git_project, config, spec, 1, cand_id)
    loop_cli._stop_loop(git_project, config, spec, "error")
    proposed_iterations: list[int] = []
    _install_routing_cooldown_pipeline(monkeypatch, git_project, config, proposed_iterations)
    monkeypatch.setattr(loop_cli, "_validate_target", lambda _target: None)

    exit_code, resumed_spec, reason = loop_cli._execute_locked(
        git_project,
        config,
        git_project,
        target=None,
        resume=spec.loop_id,
    )

    iterations = loop_cli._iteration_events(_events(git_project, config), spec.loop_id)
    assert exit_code == loop_cli.EXIT_OK
    assert resumed_spec == spec
    assert reason == "max_iterations"
    assert proposed_iterations == [5]
    assert [iterations[index]["outcome"] for index in (2, 3, 4)] == [
        "cooldown_wait",
        "cooldown_wait",
        "cooldown_wait",
    ]


@pytest.mark.parametrize(
    ("failure_stage", "expected_exit"),
    [
        ("proposal", loop_cli.EXIT_VALIDATION_ERROR),
        ("evaluation", loop_cli.EXIT_RUNTIME_ERROR),
    ],
)
def test_non_routing_config_failures_still_fail_stop(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_exit: int,
) -> None:
    config = _config(proposer={"max_iterations": 1})
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    monkeypatch.setattr(loop_cli, "_validate_target", lambda _target: None)
    monkeypatch.setattr(loop_cli, "_validate_loop_candidate", lambda *_args: None)

    if failure_stage == "proposal":
        monkeypatch.setattr(
            loop_cli,
            "_propose_candidate",
            lambda *_args: (_ for _ in ()).throw(
                loop_cli.propose_cli.pb.ProposalValidationError("invalid proposal")
            ),
        )
    else:
        monkeypatch.setattr(
            loop_cli,
            "_propose_candidate",
            lambda _main_root, _config, _project_dir, active_spec, iteration: (
                _register_loop_candidate(git_project, config, active_spec, iteration)
            ),
        )

        def evaluation_error(_main_root, _config, _project_dir, cand_id, **_kwargs):
            _append_evaluation(
                git_project,
                config,
                cand_id,
                "error",
                target="claude-harness",
            )
            raise loop_cli.ev.EvaluationBatchError("scenario failed")

        monkeypatch.setattr(loop_cli, "_evaluate_candidate", evaluation_error)

    exit_code, resumed_spec, reason = loop_cli._execute_locked(
        git_project,
        config,
        git_project,
        target=None,
        resume=spec.loop_id,
    )

    stopped = [event for event in _events(git_project, config) if event["event"] == "loop_stopped"]
    assert exit_code == expected_exit
    assert resumed_spec == spec
    assert reason is None
    assert stopped[-1]["reason"] == "error"


@pytest.mark.parametrize(
    ("reason", "config", "qualities", "expected_iterations"),
    [
        ("budget_exhausted", _config(loop={"budget_usd": 0}), [], 0),
        ("max_iterations", _config(proposer={"max_iterations": 1}), [50.0], 1),
        (
            "divergence",
            _config(proposer={"divergence_rounds": 2, "max_iterations": 5}),
            [0.0, 0.0],
            2,
        ),
        (
            "converged",
            _config(
                proposer={"divergence_rounds": 3, "max_iterations": 5},
                loop={"convergence": {"enabled": True, "quality_band_pt": 3, "rounds": 2}},
            ),
            [80.0, 81.0],
            2,
        ),
    ],
)
def test_four_stop_conditions_record_loop_stopped(
    git_project: Path,
    monkeypatch,
    reason: str,
    config: dict,
    qualities: list[float],
    expected_iterations: int,
) -> None:
    mh.init_store(git_project, config)
    _install_pipeline(monkeypatch, git_project, config, qualities)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")

    actual = loop_cli._drive_loop(git_project, config, git_project, spec)

    stopped = [event for event in _events(git_project, config) if event["event"] == "loop_stopped"]
    assert actual == reason
    assert stopped[-1]["reason"] == reason
    assert stopped[-1]["iterations"] == expected_iterations
    report = mh.reports_dir(git_project, config) / f"loop-{spec.loop_id}.md"
    assert report.is_file()
    assert f"Stop reason: **{reason}**" in report.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [(KeyboardInterrupt(), "interrupted"), (RuntimeError("boom"), "error")],
)
def test_interrupt_and_error_record_fail_safe_stop(
    git_project: Path,
    monkeypatch,
    failure: BaseException,
    expected_reason: str,
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    monkeypatch.setattr(loop_cli, "_resolve_context", lambda _project: (git_project, config))

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(loop_cli, "_drive_loop", fail)

    result = loop_cli.cmd_loop(str(git_project), "claude-harness", None, False)

    stopped = [event for event in _events(git_project, config) if event["event"] == "loop_stopped"]
    assert result == loop_cli.EXIT_RUNTIME_ERROR
    assert stopped[-1]["reason"] == expected_reason


def test_resume_restores_frozen_values_from_loop_started(git_project: Path) -> None:
    config = _config(loop={"budget_usd": 2.0}, proposer={"max_iterations": 4})
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    config["loop"]["budget_usd"] = 99.0
    config["proposer"]["max_iterations"] = 99

    restored = loop_cli._restore_loop(git_project, config, spec.loop_id, None)

    assert restored.budget_usd == 2.0
    assert restored.max_iterations == 4
    assert restored.baseline_best_quality == spec.baseline_best_quality


def test_resume_rejects_path_traversal_loop_id(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)

    with pytest.raises(loop_cli.LoopValidationError, match="invalid loop_id"):
        loop_cli._restore_loop(git_project, config, "../../outside", None)


def test_resume_orphan_without_run_evaluates_before_recording(
    git_project: Path, monkeypatch
) -> None:
    config = _config(proposer={"max_iterations": 1})
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    loop_cli._stop_loop(git_project, config, spec, "interrupted")
    restored = loop_cli._restore_loop(git_project, config, spec.loop_id, None)
    calls: list[str] = []

    def evaluate(_main_root, _config, _project_dir, candidate, *, holdout):
        if holdout:
            return []
        calls.append(candidate)
        for scenario_id in ("create-version-file", "summarize-readme"):
            _append_run(git_project, config, candidate, 70.0, scenario_id=scenario_id)
        return [{"verdict": "pass"}]

    monkeypatch.setattr(loop_cli, "_evaluate_candidate", evaluate)
    monkeypatch.setattr(loop_cli, "_evaluation_complete", _stub_evaluation_complete)
    monkeypatch.setattr(
        loop_cli,
        "_propose_candidate",
        lambda *_args, **_kwargs: pytest.fail("orphan resume must skip propose"),
    )

    reason = loop_cli._drive_loop(git_project, config, git_project, restored)

    iteration_events = loop_cli._iteration_events(_events(git_project, config), spec.loop_id)
    assert reason == "max_iterations"
    assert calls == [cand_id]
    assert iteration_events[1]["cand_id"] == cand_id


def test_resume_orphan_with_run_records_without_reevaluation(
    git_project: Path, monkeypatch
) -> None:
    config = _config(proposer={"max_iterations": 1})
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(git_project, config, cand_id, 75.0, scenario_id=scenario_id)
    loop_cli._stop_loop(git_project, config, spec, "interrupted")
    restored = loop_cli._restore_loop(git_project, config, spec.loop_id, None)
    monkeypatch.setattr(
        loop_cli,
        "_evaluate_candidate",
        lambda *_args, **_kwargs: pytest.fail("existing run must not be evaluated again"),
    )
    monkeypatch.setattr(loop_cli, "_evaluation_complete", _stub_evaluation_complete)
    monkeypatch.setattr(
        loop_cli,
        "_propose_candidate",
        lambda *_args, **_kwargs: pytest.fail("orphan resume must skip propose"),
    )

    reason = loop_cli._drive_loop(git_project, config, git_project, restored)

    iteration_events = loop_cli._iteration_events(_events(git_project, config), spec.loop_id)
    assert reason == "max_iterations"
    assert iteration_events[1]["cand_id"] == cand_id
    assert iteration_events[1]["quality_best_after"] == 75.0


def test_resume_reuses_train_evaluation_id_for_holdout(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(
        proposer={"max_iterations": 1},
        evaluate={"repeat_default": 1, "repeat_frontier": 1},
    )
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "skill:handoff")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    evaluation_id = "eval-20260715-120000-00000001"
    _append_run(
        git_project,
        config,
        cand_id,
        90.0,
        scenario_id="create-handoff",
        target=spec.target,
        evaluation_id=evaluation_id,
    )
    loop_cli._stop_loop(git_project, config, spec, "interrupted")
    restored = loop_cli._restore_loop(git_project, config, spec.loop_id, None)
    capabilities = loop_cli.ev.CliCapabilities(
        claude_version="2.1.207",
        version_pin="2.1.207",
        version_pin_match=True,
        checks={},
        judge_tool="codex",
        ok=True,
    )
    monkeypatch.setattr(
        loop_cli.ev, "check_cli_capabilities", lambda *_args, **_kwargs: capabilities
    )
    monkeypatch.setattr(loop_cli, "_validate_loop_candidate", lambda *_args: None)

    def evaluate_holdout(**kwargs):
        assert kwargs["evaluation_id"] == evaluation_id
        assert kwargs["scenario_ids"] == ["create-handoff-holdout"]
        _append_run(
            git_project,
            config,
            cand_id,
            90.0,
            holdout=True,
            scenario_id="create-handoff-holdout",
            target=spec.target,
            evaluation_id=kwargs["evaluation_id"],
        )
        return [{"verdict": "pass"}]

    monkeypatch.setattr(loop_cli.ev, "evaluate_candidate", evaluate_holdout)

    reason = loop_cli._drive_loop(git_project, config, git_project, restored)

    evaluations = [
        event
        for event in _events(git_project, config)
        if event.get("event") == "evaluation_completed" and event.get("cand_id") == cand_id
    ]
    ids_by_holdout = {bool(event["holdout"]): event["evaluation_id"] for event in evaluations}
    assert reason == "max_iterations"
    assert ids_by_holdout == {False: evaluation_id, True: evaluation_id}


def test_resume_replays_stop_decision_after_recorded_iteration(
    git_project: Path, monkeypatch
) -> None:
    config = _config(proposer={"divergence_rounds": 2, "max_iterations": 5})
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    for iteration in (1, 2):
        cand_id = _register_loop_candidate(git_project, config, spec, iteration)
        _append_run(git_project, config, cand_id, 0.0)
        loop_cli._record_iteration(git_project, config, spec, iteration, cand_id)
    loop_cli._stop_loop(git_project, config, spec, "interrupted")
    restored = loop_cli._restore_loop(git_project, config, spec.loop_id, None)
    monkeypatch.setattr(
        loop_cli,
        "_propose_candidate",
        lambda *_args, **_kwargs: pytest.fail("pending stop decision must run before propose"),
    )

    reason = loop_cli._drive_loop(git_project, config, git_project, restored)

    assert reason == "divergence"
    stopped = [event for event in _events(git_project, config) if event["event"] == "loop_stopped"]
    assert [event["reason"] for event in stopped] == ["interrupted", "divergence"]


def test_proposer_registration_event_includes_loop_coordinates() -> None:
    event = loop_cli.propose_cli._proposer_registered_event(
        "cand-1",
        None,
        1,
        "claude-harness",
        {
            "theme": "loop",
            "based_on_runs": ["run-1"],
        },
        10,
        loop_id="loop-20260711-120000-abcdef",
        iteration=2,
    )

    assert event["proposal"]["loop_id"] == "loop-20260711-120000-abcdef"
    assert event["proposal"]["iteration"] == 2


def test_overfit_candidate_is_retired_and_excluded_from_improvement(
    git_project: Path, monkeypatch
) -> None:
    config = _config(proposer={"overfit_drop_pt": 15})
    mh.init_store(git_project, config)
    baseline_id = "cand-20260711-110000-baseline-abcd"
    _write_manifest(git_project, config, baseline_id, "claude-harness")
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "candidate_registered",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": baseline_id,
            "parent_id": None,
            "generation": 0,
            "target": "claude-harness",
            "created_by": "human",
        },
    )
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(git_project, config, baseline_id, 90.0, scenario_id=scenario_id)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(git_project, config, cand_id, 95.0, scenario_id=scenario_id)
    monkeypatch.setattr(
        loop_cli,
        "_holdout_quality",
        lambda _events, _config, candidate, _target: 90.0 if candidate == baseline_id else 70.0,
    )

    retired = loop_cli._retire_if_overfit(git_project, config, spec, cand_id)

    events = _events(git_project, config)
    assert retired
    assert mh.fold_candidate_states(events)[cand_id]["status"] == "retired"
    assert (
        loop_cli._best_quality_for_candidates(
            git_project, config, events, spec.target, spec.loop_id
        )
        == 0.0
    )
    iteration = {"cand_id": cand_id, "quality_best_after": 90.0}
    assert not loop_cli._iteration_converged(events, config, iteration, 3.0, spec.target)


def test_partial_train_run_is_not_treated_as_complete(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    paths = loop_cli.ev.discover_scenario_paths(
        loop_cli.ev.scenario_suite_dir(loop_cli._PACKAGE_DIR, spec.target)
    )
    scenarios = [loop_cli.ev.load_scenario(path, loop_cli._SCHEMA_DIR) for path in paths]
    suite_hash = loop_cli.ev.compute_suite_hash(paths)
    evaluator_hash = loop_cli.ev.compute_configured_evaluator_hash(config)
    for scenario in scenarios:
        assert scenario["holdout"] is False
    _append_run(
        git_project,
        config,
        cand_id,
        80.0,
        scenario_id=scenarios[0]["id"],
        suite_hash=suite_hash,
        evaluator_hash=evaluator_hash,
    )
    assert not loop_cli._evaluation_complete(
        _events(git_project, config), config, spec.target, cand_id, holdout=False
    )

    _append_run(
        git_project,
        config,
        cand_id,
        80.0,
        scenario_id=scenarios[1]["id"],
        suite_hash=suite_hash,
        evaluator_hash=evaluator_hash,
    )
    assert loop_cli._evaluation_complete(
        _events(git_project, config), config, spec.target, cand_id, holdout=False
    )


def test_evaluate_candidate_hash_matches_loop_current_scope(git_project: Path, monkeypatch) -> None:
    config = _config()
    config["evaluate"].update(
        {
            "allowed_tools": ["Read"],
            "permission_mode": "dontAsk",
            "model": "scope-test-model",
        }
    )
    # Issue #261 PR2 review round 2: a repinned evaluate.model must be explicitly
    # admitted by the configured broker model_allowlist (no auto-union), or
    # evaluator_execution_snapshot() fails closed before a hash can be computed.
    config["evaluate"]["isolation"]["broker"]["model_allowlist"] = [
        "scope-test-model",
        config["judge"]["model"],
    ]
    config["scenario_run"]["max_output_tokens_default"] = 1234
    captured_hashes: list[str] = []

    monkeypatch.setattr(loop_cli.ev.siso, "execution_boundary_available", lambda _config: True)

    def fake_run_single_attempt(**kwargs):
        captured_hashes.append(kwargs["evaluator_hash"])
        return {
            "run_id": "run-scope-test",
            "cand_id": kwargs["cand_id"],
            "scenario_id": kwargs["scenario"]["id"],
            "evaluator_hash": kwargs["evaluator_hash"],
            "verdict": "pass",
            "quality_score": 100.0,
            "critical_pass_rate": 1.0,
            "cost": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "tool_uses": 0,
                "duration_ms": 1,
                "total_cost_usd": 0.0,
                "num_turns": 1,
            },
            "attempt": kwargs["attempt"],
            "attempts_total": kwargs["attempts_total"],
        }

    monkeypatch.setattr(loop_cli.ev, "run_single_attempt", fake_run_single_attempt)
    monkeypatch.setattr(
        loop_cli.ev,
        "candidate_impact_context",
        lambda **_kwargs: loop_cli.ev.skill_targets.SkillImpactContext((), "c" * 64),
    )
    monkeypatch.setattr(loop_cli.ev, "_append_evaluation_events", lambda *_args, **_kwargs: None)
    scenario_path = loop_cli.ev.discover_scenario_paths(
        loop_cli.ev.scenario_suite_dir(loop_cli._PACKAGE_DIR, "claude-harness")
    )[0]
    scenario_id = loop_cli.ev.load_scenario(scenario_path, loop_cli._SCHEMA_DIR)["id"]
    cand_id = "candidate"
    manifest = {
        "cand_id": cand_id,
        "parent_id": None,
        "created_by": "human",
        "target": "claude-harness",
        "source_commit": "a" * 40,
    }
    mh.init_store(git_project, config)
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "candidate_registered",
            "cand_id": cand_id,
            "created_by": "human",
            "target": "claude-harness",
        },
    )

    results = loop_cli.ev.evaluate_candidate(
        main_root=git_project,
        config=config,
        schema_dir=loop_cli._SCHEMA_DIR,
        package_dir=loop_cli._PACKAGE_DIR,
        project_dir=git_project,
        cand_id=cand_id,
        manifest=manifest,
        scenario_ids=[scenario_id],
        repeat_override=1,
        cli_capabilities={},
    )

    loop_hash = loop_cli.state.current_hash_pair(config, "claude-harness")[1]
    assert captured_hashes == [loop_hash]
    assert results[0]["evaluator_hash"] == loop_hash


def test_stale_hash_run_does_not_change_loop_quality_or_frontier(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(git_project, config, cand_id, 50.0, scenario_id=scenario_id)
    _append_run(
        git_project,
        config,
        cand_id,
        100.0,
        scenario_id="create-version-file",
        suite_hash=_HASH,
        evaluator_hash=_HASH,
    )

    quality = loop_cli._best_quality_for_candidates(
        git_project, config, _events(git_project, config), spec.target, spec.loop_id
    )

    assert quality == 50.0

    assert loop_cli._candidate_on_frontier(git_project, config, spec.target, cand_id)


def test_wrong_scenario_hash_run_fails_closed_in_frontier(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(
            git_project,
            config,
            cand_id,
            100.0,
            scenario_id=scenario_id,
            scenario_hash=_HASH,
        )

    with pytest.raises(ValueError, match="scenario hash mismatch"):
        loop_cli._candidate_on_frontier(git_project, config, spec.target, cand_id)


def test_malformed_state_event_fails_closed_in_frontier(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(git_project, config, cand_id, 100.0, scenario_id=scenario_id)
    mh.append_ledger_event(
        git_project,
        config,
        {"event": "status_changed", "cand_id": cand_id, "to": "retired"},
    )

    with pytest.raises(ValueError):
        loop_cli._candidate_on_frontier(git_project, config, spec.target, cand_id)


def test_stale_historical_run_can_support_valid_terminal_transition(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    old_id = "cand-20260711-110000-old-abcd"
    _write_manifest(git_project, config, old_id, "claude-harness")
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "candidate_registered",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": old_id,
            "parent_id": None,
            "generation": 0,
            "target": "claude-harness",
            "created_by": "human",
        },
    )
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(
            git_project,
            config,
            old_id,
            80.0,
            scenario_id=scenario_id,
            suite_hash=_HASH,
            evaluator_hash=_HASH,
        )
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "status_changed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": old_id,
            "from": "evaluated",
            "to": "retired",
            "reason": "historical",
        },
    )
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(git_project, config, cand_id, 90.0, scenario_id=scenario_id)

    assert loop_cli._candidate_on_frontier(git_project, config, spec.target, cand_id)


def test_run_after_terminal_state_preserves_terminal_warning_semantics(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    cand_id = _register_loop_candidate(git_project, config, spec, 1)
    for scenario_id in ("create-version-file", "summarize-readme"):
        _append_run(git_project, config, cand_id, 80.0, scenario_id=scenario_id)
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "status_changed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": cand_id,
            "from": "evaluated",
            "to": "retired",
            "reason": "fixture",
        },
    )
    _append_run(git_project, config, cand_id, 90.0, scenario_id="create-version-file")

    states = loop_cli.state.validated_candidate_states(
        _events(git_project, config), config, spec.target
    )

    assert states[cand_id]["status"] == "retired"
    assert states[cand_id]["warnings"]


def test_multiple_future_orphans_are_rejected(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    _register_loop_candidate(git_project, config, spec, 1)
    _register_loop_candidate(git_project, config, spec, 2)

    with pytest.raises(ValueError, match="at most one orphan"):
        loop_cli._next_orphan(
            _events(git_project, config), spec.loop_id, set(), spec.target, spec.max_iterations
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
def test_loop_numeric_settings_reject_non_finite_values(value) -> None:
    with pytest.raises(loop_cli.LoopValidationError, match="finite number|must be finite"):
        loop_cli._finite_float(value, "loop.setting")


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), float("-inf")])
def test_positive_integer_settings_reject_fractional_and_non_finite_values(value) -> None:
    with pytest.raises(loop_cli.LoopValidationError, match="positive integer"):
        loop_cli._positive_int(value, "loop.rounds")
    with pytest.raises(ValueError, match="positive integer"):
        loop_cli.state._positive_int(value, "evaluate.repeat")


def test_non_finite_ledger_number_is_rejected(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    event = {
        "event": "loop_iteration",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "loop_id": "loop-20260711-120000-finite",
        "iteration": 1,
        "cand_id": "cand-20260711-120000-finite-abcd",
        "quality_best_before": float("nan"),
        "quality_best_after": 0.0,
        "iteration_cost_usd": 0.0,
    }

    with pytest.raises(ValueError, match="non-finite ledger number"):
        loop_cli._validate_event(event, "loop_iteration")
    with pytest.raises(ValueError, match="Out of range float values"):
        mh.append_ledger_event(git_project, config, event)


def test_strict_ledger_reader_rejects_corrupt_nonempty_line(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    path = mh.ledger_path(git_project, config)
    path.write_text('{"event":"loop_stopped"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid ledger JSON at line 1"):
        mh.read_ledger_events_strict(git_project, config)


def test_ledger_writer_rejects_short_write(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    monkeypatch.setattr(mh.os, "write", lambda _fd, data: len(data) - 1)

    with pytest.raises(OSError, match="short ledger write"):
        mh.append_ledger_event(git_project, config, {"event": "test"})

    assert mh.ledger_path(git_project, config).read_bytes() == b""


def test_ledger_writer_retries_real_partial_write_without_corruption(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    real_write = mh.os.write
    calls = 0

    def partial_once(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            partial = bytes(data[:-1])
            return real_write(fd, partial)
        return real_write(fd, data)

    monkeypatch.setattr(mh.os, "write", partial_once)
    mh.append_ledger_event(git_project, config, {"event": "test"})

    assert mh.read_ledger_events_strict(git_project, config) == [{"event": "test"}]


def test_strict_reader_waits_for_partial_writer_snapshot(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    real_write = mh.os.write
    partial_written = threading.Event()
    continue_write = threading.Event()
    reader_done = threading.Event()
    read_result: list[dict] = []
    calls = 0

    def paused_partial_write(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            partial = bytes(data[:-1])
            count = real_write(fd, partial)
            partial_written.set()
            assert continue_write.wait(2)
            return count
        return real_write(fd, data)

    monkeypatch.setattr(mh.os, "write", paused_partial_write)
    writer = threading.Thread(
        target=mh.append_ledger_event,
        args=(git_project, config, {"event": "test"}),
    )

    def read_strictly() -> None:
        read_result.extend(mh.read_ledger_events_strict(git_project, config))
        reader_done.set()

    writer.start()
    assert partial_written.wait(2)
    reader = threading.Thread(target=read_strictly)
    reader.start()
    assert not reader_done.wait(0.05)
    continue_write.set()
    writer.join(2)
    reader.join(2)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert read_result == [{"event": "test"}]


def test_fail_safe_stop_survives_duplicate_iteration_rows(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    event = {
        "event": "loop_iteration",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "loop_id": spec.loop_id,
        "iteration": 1,
        "cand_id": "cand-20260711-120000-safe-abcd",
        "quality_best_before": 0.0,
        "quality_best_after": 0.0,
        "iteration_cost_usd": 0.0,
    }
    mh.append_ledger_event(git_project, config, event)
    mh.append_ledger_event(git_project, config, event)

    loop_cli._safe_fail_stop(git_project, config, spec, "error")

    stopped = [item for item in _events(git_project, config) if item["event"] == "loop_stopped"]
    assert stopped[-1]["reason"] == "error"


def test_iteration_without_matching_registration_is_rejected(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "loop_iteration",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "loop_id": spec.loop_id,
            "iteration": 1,
            "cand_id": "cand-20260711-120000-forged-abcd",
            "quality_best_before": 0.0,
            "quality_best_after": 100.0,
            "iteration_cost_usd": 0.0,
        },
    )

    with pytest.raises(ValueError, match="matching candidate registration"):
        loop_cli._iteration_events(_events(git_project, config), spec.loop_id)


def test_registration_before_loop_started_is_rejected(git_project: Path) -> None:
    config = _config()
    mh.init_store(git_project, config)
    loop_id = "loop-20260711-120000-order"
    cand_id = "cand-20260711-120000-order-abcd"
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "candidate_registered",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": cand_id,
            "parent_id": None,
            "generation": 1,
            "target": "claude-harness",
            "created_by": "proposer",
            "proposal": {
                "theme": "out of order",
                "based_on_runs": ["run-seed"],
                "cost_usd": 0.0,
                "loop_id": loop_id,
                "iteration": 1,
            },
        },
    )
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "loop_started",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "loop_id": loop_id,
            "target": "claude-harness",
            "budget_usd": None,
            "max_iterations": 1,
            "baseline_best_quality": 0.0,
        },
    )

    with pytest.raises(ValueError, match="precedes loop_started"):
        loop_cli._next_orphan(_events(git_project, config), loop_id, set())


def test_report_failure_does_not_append_conflicting_error_stop(
    git_project: Path, monkeypatch
) -> None:
    config = _config()
    mh.init_store(git_project, config)
    spec = loop_cli._start_loop(git_project, config, "claude-harness")

    def fail_report(*_args, **_kwargs):
        raise loop_cli.loop_report.LoopReportError("disk full")

    monkeypatch.setattr(loop_cli.loop_report, "write_report", fail_report)
    with pytest.raises(loop_cli.loop_report.LoopReportError, match="disk full"):
        loop_cli._stop_loop(git_project, config, spec, "max_iterations")
    loop_cli._safe_fail_stop(git_project, config, spec, "error")

    stopped = [item for item in _events(git_project, config) if item["event"] == "loop_stopped"]
    assert [item["reason"] for item in stopped] == ["max_iterations"]
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "frontier_updated",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "target": "claude-harness",
            "frontier": ["cand-20260711-130000-later-abcd"],
            "dominated": [],
        },
    )
    monkeypatch.undo()

    exit_code, _, reason = loop_cli._execute_locked(
        git_project, config, git_project, None, spec.loop_id
    )

    assert exit_code == loop_cli.EXIT_OK
    assert reason == "max_iterations"
    report = mh.reports_dir(git_project, config) / f"loop-{spec.loop_id}.md"
    assert report.is_file()
    assert "- After: (empty)" in report.read_text(encoding="utf-8")


def test_skill_target_path_rejects_traversal() -> None:
    with pytest.raises(ValueError, match="unknown target"):
        loop_cli.ev.scenario_suite_dir(Path("/package"), "skill:../secret")


def test_skill_target_path_uses_allowlisted_suite_directory() -> None:
    assert loop_cli.ev.scenario_suite_dir(Path("/package"), "skill:review") == Path(
        "/package/scenarios/skill/review"
    )


def test_skill_target_is_accepted_when_scenario_suite_exists(tmp_path: Path, monkeypatch) -> None:
    suite = tmp_path / "scenarios" / "skill" / "review"
    suite.mkdir(parents=True)
    (suite / "review.yaml").write_text(
        """schema_version: "1.0"
id: review
target: skill:review
description: review skill scenario
prompt: review the fixture
critical:
  - id: output
    text: output exists
    oracle: artifact_exists
    path: output.md
""",
        encoding="utf-8",
    )
    (suite / "review-holdout.yaml").write_text(
        """schema_version: "1.0"
id: review-holdout
target: skill:review
description: review skill holdout scenario
prompt: review the holdout fixture
holdout: true
critical:
  - id: output
    text: output exists
    oracle: artifact_exists
    path: output.md
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(loop_cli, "_PACKAGE_DIR", tmp_path)

    loop_cli._validate_target("skill:review")
