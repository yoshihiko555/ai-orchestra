"""Cross-skill regression batch and ledger semantics (EV-54 through EV-58)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_cross_skill_regression",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_cross_skill_regression",
    "packages/meta-harness/lib/meta_harness_common.py",
)
loop_state = load_module(
    "meta_harness_loop_state_cross_skill_regression",
    "packages/meta-harness/lib/loop_state.py",
)
prm = load_module(
    "meta_harness_promoter_cross_skill_regression",
    "packages/meta-harness/lib/promoter.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "packages" / "meta-harness"
SCHEMA_DIR = PACKAGE_DIR / "schemas"
CAND_ID = "cand-20260715-120000-cross-skill-abcd"
EVALUATION_ID = "eval-20260715-120000-00000001"
TARGET = "skill:handoff"
REGRESSION_TARGET = "skill:issue-create"


def _cost(usd: float, *, tokens: int) -> dict:
    return {
        "input_tokens": tokens // 2,
        "output_tokens": tokens - tokens // 2,
        "total_tokens": tokens,
        "tool_uses": 0,
        "duration_ms": 1,
        "total_cost_usd": usd,
        "num_turns": 1,
    }


def _result(
    *,
    suite_id: str,
    scenario_id: str,
    verdict: str,
    cost_usd: float,
    tokens: int,
) -> dict:
    return {
        "run_id": f"run-{suite_id.split(':')[-1]}-{scenario_id}",
        "cand_id": CAND_ID,
        "scenario_id": scenario_id,
        "verdict": verdict,
        "quality_score": 90.0 if suite_id == TARGET else 5.0,
        "critical_pass_rate": 1.0 if verdict == "pass" else 0.0,
        "cost": _cost(cost_usd, tokens=tokens),
        "attempt": 1,
        "attempts_total": 1,
    }


def _registration(*, proposer_cost: float = 0.0) -> dict:
    return {
        "event": "candidate_registered",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "cand_id": CAND_ID,
        "parent_id": None,
        "generation": 0,
        "target": TARGET,
        "created_by": "proposer",
        "proposal": {
            "theme": "cross-skill regression",
            "based_on_runs": ["run-seed"],
            "cost_usd": proposer_cost,
            "loop_id": "loop-20260715-120000-cross-skill",
            "iteration": 1,
        },
    }


def _suite(target: str, *, holdout: bool = False) -> tuple[list[Path], list[tuple[Path, dict]]]:
    paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, target)
    docs = [(path, ev.load_scenario(path, SCHEMA_DIR)) for path in paths]
    selected = [item for item in docs if bool(item[1]["holdout"]) == holdout]
    return paths, selected[:1]


def _run_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    regression_verdict: str = "pass",
    regression_cost: float = 0.4,
    max_budget: float = 12.0,
    unverified: tuple[str, ...] = (),
) -> tuple[dict, list[dict], list[dict]]:
    config = copy.deepcopy(mh.DEFAULTS)
    config["regression"]["max_budget_usd"] = max_budget
    mh.init_store(tmp_path, config)
    mh.append_ledger_event(tmp_path, config, _registration())
    own_paths, own_docs = _suite(TARGET)
    regression_paths, regression_docs = _suite(REGRESSION_TARGET)
    impacted = (REGRESSION_TARGET,)
    monkeypatch.setattr(
        ev,
        "candidate_impact_context",
        lambda **_kwargs: ev.skill_targets.SkillImpactContext(impacted, "c" * 64),
    )
    monkeypatch.setattr(
        ev,
        "_resolve_regression_suites",
        lambda *_args, **_kwargs: (
            [] if unverified else [(REGRESSION_TARGET, regression_paths, regression_docs)],
            list(unverified),
        ),
    )

    def fake_run_scenario_set(**kwargs):
        scenario_id = str(kwargs["scenario_docs"][0][1]["id"])
        if kwargs["suite_id"] == TARGET:
            return [
                _result(
                    suite_id=TARGET,
                    scenario_id=scenario_id,
                    verdict="pass",
                    cost_usd=0.1,
                    tokens=10,
                )
            ]
        return [
            _result(
                suite_id=REGRESSION_TARGET,
                scenario_id=scenario_id,
                verdict=regression_verdict,
                cost_usd=regression_cost,
                tokens=999,
            )
        ]

    monkeypatch.setattr(ev, "_run_scenario_set", fake_run_scenario_set)
    manifest = {
        "cand_id": CAND_ID,
        "parent_id": None,
        "target": TARGET,
        "source_commit": "a" * 40,
        "overlay_files": ["facets/policies/cli-language.md"],
    }
    results = ev._evaluate_scenario_batch(
        main_root=tmp_path,
        config=config,
        schema_dir=SCHEMA_DIR,
        package_dir=PACKAGE_DIR,
        project_dir=tmp_path,
        cand_id=CAND_ID,
        cand_dir=tmp_path / "candidate",
        manifest=manifest,
        target=TARGET,
        own_suite_paths=own_paths,
        own_scenarios=own_docs,
        holdout=False,
        repeat_override=1,
        cli_capabilities={},
        runner=lambda *_args, **_kwargs: None,
    )
    return config, results, mh.read_ledger_events_strict(tmp_path, config)


def test_regression_failure_is_hard_gate_and_does_not_pollute_frontier_axes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, results, events = _run_batch(tmp_path, monkeypatch, regression_verdict="fail")

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["own_critical_pass"] is True
    assert summary["regression_results"][0]["critical_pass"] is False
    assert summary["verdict"] == "fail"
    assert {event["event"] for event in events} >= {
        "run_completed",
        "regression_run_completed",
        "evaluation_completed",
    }
    assert len(results) == 2

    points = mh.aggregate_run_points(events, config, TARGET)
    assert len(points) == 1
    assert points[0]["eligible"] is False
    assert points[0]["quality_mean"] == 90.0
    assert points[0]["cost_mean"] == 10
    assert points[0]["runs"] == 1
    assert loop_state.non_holdout_summary(events, config, CAND_ID, TARGET) is None


def test_non_holdout_summary_uses_legacy_runs_without_evaluation_summary() -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    target = "claude-harness"
    paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, target)
    scenario_docs = [(path, ev.load_scenario(path, SCHEMA_DIR)) for path in paths]
    results = []
    for quality, (_, scenario) in zip((80.0, 100.0), scenario_docs, strict=True):
        result = _result(
            suite_id=TARGET,
            scenario_id=str(scenario["id"]),
            verdict="pass",
            cost_usd=0.1,
            tokens=10,
        )
        result["run_id"] = f"run-legacy-{scenario['id']}"
        result["quality_score"] = quality
        results.append(result)
    results[-1]["critical_pass_rate"] = 0.0
    events = ev._events_for_results(
        results,
        target=target,
        suite_id=target,
        suite_hash=ev.compute_suite_hash(paths),
        evaluator_hash=ev.compute_configured_evaluator_hash(config),
        scenario_docs=scenario_docs,
        evaluation_id=EVALUATION_ID,
    )

    assert not mh.candidate_has_evaluation_completed(events, CAND_ID)
    assert loop_state.non_holdout_summary(events, config, CAND_ID, target) == {
        "quality_mean": 90.0,
        "critical_pass": False,
    }


def test_unverified_impact_is_recorded_without_failing_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, events = _run_batch(
        tmp_path,
        monkeypatch,
        unverified=(REGRESSION_TARGET,),
    )

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["verdict"] == "pass"
    assert summary["unverified_impacts"] == [REGRESSION_TARGET]
    assert summary["regression_results"] == []
    assert not any(event["event"] == "regression_run_completed" for event in events)


def test_regression_budget_excess_records_error_summary_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ev.EvaluationBatchError, match="max_budget_usd"):
        _run_batch(tmp_path, monkeypatch, regression_cost=0.5, max_budget=0.25)

    events = mh.read_ledger_events_strict(tmp_path, mh.DEFAULTS)
    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["verdict"] == "error"
    assert summary["regression_cost_usd"] == 0.5
    assert any("max_budget_usd" in error for error in summary["errors"])


def test_regression_attempt_uses_remaining_evaluation_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite_paths, scenario_docs = _suite(REGRESSION_TARGET)
    captured: list[float] = []

    def fake_attempt(**kwargs):
        captured.append(float(kwargs["scenario"]["budget"]["max_budget_usd"]))
        return _result(
            suite_id=REGRESSION_TARGET,
            scenario_id=kwargs["scenario"]["id"],
            verdict="pass",
            cost_usd=0.2,
            tokens=20,
        )

    monkeypatch.setattr(ev, "run_single_attempt", fake_attempt)
    results = ev._run_scenario_set(
        main_root=tmp_path,
        config=mh.DEFAULTS,
        schema_dir=SCHEMA_DIR,
        package_dir=PACKAGE_DIR,
        project_dir=tmp_path,
        cand_id=CAND_ID,
        cand_dir=tmp_path / "candidate",
        manifest={"source_commit": "a" * 40},
        target=TARGET,
        routing_config_base_hash=None,
        suite_id=REGRESSION_TARGET,
        scenario_docs=scenario_docs,
        suite_hash=ev.compute_suite_hash(suite_paths),
        evaluator_hash="d" * 64,
        evaluation_id=EVALUATION_ID,
        repeat_override=1,
        cli_capabilities={},
        runner=lambda *_args, **_kwargs: None,
        max_total_cost_usd=0.25,
    )

    assert len(results) == 1
    assert captured == [0.25]

    with pytest.raises(ev.RegressionBudgetExceeded) as exc_info:
        ev._run_scenario_set(
            main_root=tmp_path,
            config=mh.DEFAULTS,
            schema_dir=SCHEMA_DIR,
            package_dir=PACKAGE_DIR,
            project_dir=tmp_path,
            cand_id=CAND_ID,
            cand_dir=tmp_path / "candidate",
            manifest={"source_commit": "a" * 40},
            target=TARGET,
            routing_config_base_hash=None,
            suite_id=REGRESSION_TARGET,
            scenario_docs=scenario_docs,
            suite_hash=ev.compute_suite_hash(suite_paths),
            evaluator_hash="d" * 64,
            evaluation_id=EVALUATION_ID,
            repeat_override=1,
            cli_capabilities={},
            runner=lambda *_args, **_kwargs: None,
            max_total_cost_usd=0.0,
        )
    assert exc_info.value.results == []
    assert captured == [0.25]


def test_broker_cost_includes_judge_and_fail_closed_fallback(tmp_path: Path) -> None:
    cli_cost = _cost(0.2, tokens=20)
    scenario = {"budget": {"max_budget_usd": 1.5}}

    accounted = ev._account_cost_with_broker_metrics(
        cli_cost,
        {"broker": {"metrics": {"estimated_cost_usd": 0.7}}},
        scenario,
        mh.DEFAULTS,
    )
    missing = ev._account_cost_with_broker_metrics(cli_cost, {"broker": {}}, scenario, mh.DEFAULTS)

    assert accounted["total_cost_usd"] == pytest.approx(0.7)
    assert missing["total_cost_usd"] == pytest.approx(1.5)

    (tmp_path / "isolation.json").write_text(
        '{"broker":{"metrics":{"estimated_cost_usd":0.3,"anomaly":false,"anomaly_reasons":[]}}}\n',
        encoding="utf-8",
    )
    ev._mark_isolation_metrics_stale(tmp_path)
    stale = ev._account_cost_with_broker_metrics(
        cli_cost, ev._load_isolation_metadata(tmp_path), scenario, mh.DEFAULTS
    )
    assert stale["total_cost_usd"] == pytest.approx(1.5)


def test_train_and_holdout_batches_share_evaluation_id_and_regression_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    train_path = tmp_path / "train.yaml"
    holdout_path = tmp_path / "holdout.yaml"
    docs = {
        train_path: {"id": "train", "holdout": False},
        holdout_path: {"id": "holdout", "holdout": True},
    }
    calls: list[tuple[str, float]] = []

    monkeypatch.setattr(ev, "validate_target_suite", lambda *_args: list(docs))
    monkeypatch.setattr(ev, "load_scenario", lambda path, _schema: docs[path])
    monkeypatch.setattr(ev.siso, "execution_boundary_available", lambda _config: True)

    def fake_batch(**kwargs):
        budget = kwargs["regression_budget"]
        calls.append((kwargs["evaluation_id"], budget["remaining_usd"]))
        budget["remaining_usd"] -= 1.0
        return []

    monkeypatch.setattr(ev, "_evaluate_scenario_batch", fake_batch)
    config = copy.deepcopy(mh.DEFAULTS)
    config["regression"]["max_budget_usd"] = 2.0
    mh.init_store(tmp_path, config)
    mh.append_ledger_event(tmp_path, config, _registration())
    manifest = {
        "cand_id": CAND_ID,
        "parent_id": None,
        "created_by": "proposer",
        "target": TARGET,
    }

    ev.evaluate_candidate(
        main_root=tmp_path,
        config=config,
        schema_dir=SCHEMA_DIR,
        package_dir=PACKAGE_DIR,
        project_dir=tmp_path,
        cand_id=CAND_ID,
        manifest=manifest,
        scenario_ids=None,
        repeat_override=None,
        cli_capabilities={},
    )

    assert calls[0][0] == calls[1][0]
    assert [remaining for _, remaining in calls] == [2.0, 1.0]


def test_too_many_regression_suites_fails_before_own_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    config["regression"]["max_affected_suites"] = 1
    mh.init_store(tmp_path, config)
    mh.append_ledger_event(tmp_path, config, _registration())
    own_paths, own_docs = _suite(TARGET)
    impacted = ("skill:alpha", "skill:beta")
    monkeypatch.setattr(
        ev,
        "candidate_impact_context",
        lambda **_kwargs: ev.skill_targets.SkillImpactContext(impacted, "c" * 64),
    )
    monkeypatch.setattr(
        ev,
        "_resolve_regression_suites",
        lambda *_args, **_kwargs: (
            [(target, [], []) for target in impacted],
            [],
        ),
    )
    monkeypatch.setattr(
        ev,
        "_run_scenario_set",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("runs must not start")),
    )

    with pytest.raises(ev.EvaluationBatchError, match="max_affected_suites"):
        ev._evaluate_scenario_batch(
            main_root=tmp_path,
            config=config,
            schema_dir=SCHEMA_DIR,
            package_dir=PACKAGE_DIR,
            project_dir=tmp_path,
            cand_id=CAND_ID,
            cand_dir=tmp_path / "candidate",
            manifest={
                "cand_id": CAND_ID,
                "target": TARGET,
                "source_commit": "a" * 40,
                "overlay_files": [],
            },
            target=TARGET,
            own_suite_paths=own_paths,
            own_scenarios=own_docs,
            holdout=False,
            repeat_override=1,
            cli_capabilities={},
            runner=lambda *_args, **_kwargs: None,
        )

    events = mh.read_ledger_events_strict(tmp_path, config)
    assert not any(event["event"] == "run_completed" for event in events)
    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["verdict"] == "error"


def test_incomplete_batch_with_only_own_pass_is_not_frontier_eligible() -> None:
    own = _result(
        suite_id=TARGET,
        scenario_id="create-handoff",
        verdict="pass",
        cost_usd=0.1,
        tokens=10,
    )
    event = ev._build_run_completed_event(
        own,
        target=TARGET,
        suite_id=TARGET,
        suite_hash="a" * 64,
        scenario_hash="b" * 64,
        evaluator_hash="d" * 64,
        holdout=False,
    )
    points = mh.aggregate_run_points([_registration(), event], mh.DEFAULTS, TARGET)

    assert len(points) == 1
    assert points[0]["eligible"] is False


def test_same_scenario_id_is_distinguished_by_suite_and_event_type() -> None:
    own_result = _result(
        suite_id=TARGET,
        scenario_id="shared-id",
        verdict="pass",
        cost_usd=0.1,
        tokens=10,
    )
    regression_result = _result(
        suite_id=REGRESSION_TARGET,
        scenario_id="shared-id",
        verdict="pass",
        cost_usd=0.2,
        tokens=20,
    )
    own_event = ev._build_run_completed_event(
        own_result,
        target=TARGET,
        suite_id=TARGET,
        suite_hash="a" * 64,
        scenario_hash="b" * 64,
        evaluator_hash="d" * 64,
        holdout=False,
    )
    regression_event = ev._build_regression_run_completed_event(
        regression_result,
        evaluation_id=EVALUATION_ID,
        target=TARGET,
        suite_id=REGRESSION_TARGET,
        suite_hash="e" * 64,
        scenario_hash="f" * 64,
        evaluator_hash="d" * 64,
        holdout=False,
    )

    ev._validate_ledger_event(SCHEMA_DIR, own_event)
    ev._validate_ledger_event(SCHEMA_DIR, regression_event)
    assert own_event["scenario_id"] == regression_event["scenario_id"]
    assert (own_event["event"], own_event["suite_id"]) != (
        regression_event["event"],
        regression_event["suite_id"],
    )


def test_regression_cost_is_included_in_loop_iteration_budget() -> None:
    own_result = _result(
        suite_id=TARGET,
        scenario_id="create-handoff",
        verdict="pass",
        cost_usd=0.3,
        tokens=10,
    )
    regression_result = _result(
        suite_id=REGRESSION_TARGET,
        scenario_id="create-task-issue",
        verdict="pass",
        cost_usd=0.4,
        tokens=20,
    )
    own_event = ev._build_run_completed_event(
        own_result,
        target=TARGET,
        suite_id=TARGET,
        suite_hash="a" * 64,
        scenario_hash="b" * 64,
        evaluator_hash="d" * 64,
        holdout=False,
    )
    regression_event = ev._build_regression_run_completed_event(
        regression_result,
        evaluation_id=EVALUATION_ID,
        target=TARGET,
        suite_id=REGRESSION_TARGET,
        suite_hash="e" * 64,
        scenario_hash="f" * 64,
        evaluator_hash="d" * 64,
        holdout=False,
    )

    cost = loop_state.iteration_cost(
        [_registration(proposer_cost=0.2), own_event, regression_event],
        "loop-20260715-120000-cross-skill",
        1,
        CAND_ID,
        TARGET,
    )

    assert cost == pytest.approx(0.9)


def test_regression_config_toggle_changes_evaluator_hash() -> None:
    enabled = copy.deepcopy(mh.DEFAULTS)
    disabled = copy.deepcopy(mh.DEFAULTS)
    disabled["regression"]["enabled"] = False

    assert ev.compute_configured_evaluator_hash(enabled) != ev.compute_configured_evaluator_hash(
        disabled
    )


def _holdout_own_event(
    *, run_id: str, suite_hash: str, evaluator_hash: str, target: str = TARGET
) -> dict:
    result = _result(
        suite_id=target,
        scenario_id="holdout",
        verdict="pass",
        cost_usd=0.1,
        tokens=10,
    )
    result["run_id"] = run_id
    return ev._build_run_completed_event(
        result,
        target=target,
        suite_id=target,
        suite_hash=suite_hash,
        scenario_hash="b" * 64,
        evaluator_hash=evaluator_hash,
        holdout=True,
    )


def _holdout_regression_event(
    *,
    run_id: str,
    suite_id: str,
    verdict: str,
    suite_hash: str,
    evaluator_hash: str,
    target: str = TARGET,
) -> dict:
    result = _result(
        suite_id=suite_id,
        scenario_id="shared-holdout",
        verdict=verdict,
        cost_usd=0.2,
        tokens=20,
    )
    result["run_id"] = run_id
    return ev._build_regression_run_completed_event(
        result,
        evaluation_id=EVALUATION_ID,
        target=target,
        suite_id=suite_id,
        suite_hash=suite_hash,
        scenario_hash="f" * 64,
        evaluator_hash=evaluator_hash,
        holdout=True,
    )


def test_promote_batch_rejects_earlier_regression_fail_even_when_later_suite_passes() -> None:
    suite_hash = "a" * 64
    evaluator_hash = "d" * 64
    own = _holdout_own_event(
        run_id="run-own-holdout",
        suite_hash=suite_hash,
        evaluator_hash=evaluator_hash,
    )
    failed = _holdout_regression_event(
        run_id="run-alpha-fail",
        suite_id="skill:alpha",
        verdict="fail",
        suite_hash="1" * 64,
        evaluator_hash=evaluator_hash,
    )
    passed = _holdout_regression_event(
        run_id="run-beta-pass",
        suite_id="skill:beta",
        verdict="pass",
        suite_hash="2" * 64,
        evaluator_hash=evaluator_hash,
    )
    tampered_summary = {
        "event": "evaluation_completed",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "evaluation_id": EVALUATION_ID,
        "cand_id": CAND_ID,
        "target": TARGET,
        "holdout": True,
        "own_run_ids": [own["run_id"]],
        "own_suite_hash": suite_hash,
        "evaluator_hash": evaluator_hash,
        "own_critical_pass": True,
        "regression_results": [
            {
                "suite_id": "skill:alpha",
                "suite_hash": "1" * 64,
                "run_ids": [failed["run_id"]],
                "verdict": "fail",
                "critical_pass": False,
            },
            {
                "suite_id": "skill:beta",
                "suite_hash": "2" * 64,
                "run_ids": [passed["run_id"]],
                "verdict": "pass",
                "critical_pass": True,
            },
        ],
        "verdict": "pass",
        "unverified_impacts": [],
        "evaluation_base_commit": "a" * 40,
        "impacted_targets": ["skill:alpha", "skill:beta"],
        "impact_input_hash": "c" * 64,
        "regression_cost_usd": 0.4,
    }

    assert not prm._evaluation_runs_are_consistent(
        [own, failed, passed], tampered_summary, CAND_ID, TARGET
    )


def test_promote_checks_each_regression_suite_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    suite_hash = "a" * 64
    evaluator_hash = "d" * 64
    own = _holdout_own_event(
        run_id="run-own-holdout",
        suite_hash=suite_hash,
        evaluator_hash=evaluator_hash,
    )
    regression = _holdout_regression_event(
        run_id="run-issue-holdout",
        suite_id=REGRESSION_TARGET,
        verdict="pass",
        suite_hash="e" * 64,
        evaluator_hash=evaluator_hash,
    )
    holdout_summary = {
        "event": "evaluation_completed",
        "evaluation_id": EVALUATION_ID,
        "cand_id": CAND_ID,
        "target": TARGET,
        "holdout": True,
        "own_run_ids": [own["run_id"]],
        "own_suite_hash": suite_hash,
        "evaluator_hash": evaluator_hash,
        "own_critical_pass": True,
        "regression_results": [
            {
                "suite_id": REGRESSION_TARGET,
                "suite_hash": "e" * 64,
                "run_ids": [regression["run_id"]],
                "verdict": "pass",
                "critical_pass": True,
            }
        ],
        "verdict": "pass",
        "unverified_impacts": [],
        "impacted_targets": [REGRESSION_TARGET],
    }
    train_summary = {
        "event": "evaluation_completed",
        "evaluation_id": EVALUATION_ID,
        "cand_id": CAND_ID,
        "target": TARGET,
        "holdout": False,
        "own_suite_hash": suite_hash,
        "evaluator_hash": evaluator_hash,
        "verdict": "pass",
    }
    events = [own, regression, train_summary, holdout_summary]
    frontier = {"suite_hash": suite_hash, "evaluator_hash": evaluator_hash}
    monkeypatch.setattr(
        prm.ev,
        "validate_target_suite",
        lambda _package, _schema, suite_id: [Path(f"{suite_id}.yaml")],
    )
    monkeypatch.setattr(
        prm.ev,
        "compute_suite_hash",
        lambda paths: ("a" if "handoff" in str(paths[0]) else "e") * 64,
    )
    monkeypatch.setattr(prm.ev, "compute_configured_evaluator_hash", lambda _config: evaluator_hash)
    monkeypatch.setattr(
        prm.ev,
        "load_scenario",
        lambda path, _schema: {
            "id": "holdout" if "handoff" in str(path) else "shared-holdout",
            "holdout": True,
        },
    )
    monkeypatch.setattr(
        prm.ev,
        "compute_scenario_hash",
        lambda path: ("b" if "handoff" in str(path) else "f") * 64,
    )
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["repeat_frontier"] = 1

    assert prm._has_current_hash_pair(
        events,
        CAND_ID,
        TARGET,
        frontier,
        config,
        holdout_evaluation=holdout_summary,
    )

    monkeypatch.setattr(prm.ev, "compute_suite_hash", lambda _paths: "f" * 64)
    assert not prm._has_current_hash_pair(
        events,
        CAND_ID,
        TARGET,
        frontier,
        config,
        holdout_evaluation=holdout_summary,
    )

    monkeypatch.setattr(
        prm.ev,
        "compute_suite_hash",
        lambda paths: ("a" if "handoff" in str(paths[0]) else "e") * 64,
    )
    monkeypatch.setattr(prm.ev, "compute_configured_evaluator_hash", lambda _config: "f" * 64)
    assert not prm._has_current_hash_pair(
        events,
        CAND_ID,
        TARGET,
        frontier,
        config,
        holdout_evaluation=holdout_summary,
    )


def test_promote_rejects_train_holdout_evaluation_id_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """train (non-holdout) の evaluation_completed が holdout batch と別 evaluation_id
    (= 別バッチ) だと、regression 予算の分離を暗黙にバイパスして promote してしまう
    回帰の防止。"""
    suite_hash = "a" * 64
    evaluator_hash = "d" * 64
    own = _holdout_own_event(
        run_id="run-own-holdout",
        suite_hash=suite_hash,
        evaluator_hash=evaluator_hash,
    )
    holdout_summary = {
        "event": "evaluation_completed",
        "evaluation_id": EVALUATION_ID,
        "cand_id": CAND_ID,
        "target": TARGET,
        "holdout": True,
        "own_run_ids": [own["run_id"]],
        "own_suite_hash": suite_hash,
        "evaluator_hash": evaluator_hash,
        "own_critical_pass": True,
        "regression_results": [],
        "verdict": "pass",
        "unverified_impacts": [],
        "impacted_targets": [],
    }
    train_same_batch = {
        "event": "evaluation_completed",
        "evaluation_id": EVALUATION_ID,
        "cand_id": CAND_ID,
        "target": TARGET,
        "holdout": False,
        "own_suite_hash": suite_hash,
        "evaluator_hash": evaluator_hash,
        "verdict": "pass",
    }
    train_other_batch = {**train_same_batch, "evaluation_id": "eval-20260715-120000-99999999"}
    monkeypatch.setattr(
        prm.ev,
        "validate_target_suite",
        lambda _package, _schema, suite_id: [Path(f"{suite_id}.yaml")],
    )
    monkeypatch.setattr(
        prm.ev,
        "load_scenario",
        lambda path, _schema: {"id": "holdout", "holdout": True},
    )
    monkeypatch.setattr(prm.ev, "compute_suite_hash", lambda _paths: suite_hash)
    monkeypatch.setattr(prm.ev, "compute_configured_evaluator_hash", lambda _config: evaluator_hash)
    monkeypatch.setattr(prm.ev, "compute_scenario_hash", lambda _path: "b" * 64)
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["repeat_frontier"] = 1
    frontier = {"suite_hash": suite_hash, "evaluator_hash": evaluator_hash}

    assert prm._has_current_hash_pair(
        [own, train_same_batch, holdout_summary],
        CAND_ID,
        TARGET,
        frontier,
        config,
        holdout_evaluation=holdout_summary,
    )
    assert not prm._has_current_hash_pair(
        [own, train_other_batch, holdout_summary],
        CAND_ID,
        TARGET,
        frontier,
        config,
        holdout_evaluation=holdout_summary,
    )


def test_promote_requires_every_holdout_scenario_at_frontier_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_hash = "a" * 64
    evaluator_hash = "d" * 64
    own = _holdout_own_event(
        run_id="run-own-holdout",
        suite_hash=suite_hash,
        evaluator_hash=evaluator_hash,
    )
    regression = _holdout_regression_event(
        run_id="run-issue-holdout",
        suite_id=REGRESSION_TARGET,
        verdict="pass",
        suite_hash="e" * 64,
        evaluator_hash=evaluator_hash,
    )
    summary = {
        "evaluation_id": EVALUATION_ID,
        "own_run_ids": [own["run_id"]],
        "regression_results": [{"suite_id": REGRESSION_TARGET, "run_ids": [regression["run_id"]]}],
    }
    monkeypatch.setattr(
        prm.ev,
        "validate_target_suite",
        lambda _package, _schema, suite_id: [Path(f"{suite_id}.yaml")],
    )
    monkeypatch.setattr(
        prm.ev,
        "load_scenario",
        lambda path, _schema: {
            "id": "holdout" if "handoff" in str(path) else "shared-holdout",
            "holdout": True,
        },
    )
    monkeypatch.setattr(
        prm.ev,
        "compute_scenario_hash",
        lambda path: ("b" if "handoff" in str(path) else "f") * 64,
    )
    config = copy.deepcopy(mh.DEFAULTS)

    assert not prm._evaluation_covers_current_holdouts([own, regression], summary, TARGET, config)
    config["evaluate"]["repeat_frontier"] = 1
    assert prm._evaluation_covers_current_holdouts([own, regression], summary, TARGET, config)


def test_promote_requires_complete_affected_holdouts_for_claude_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "claude-harness"
    evaluator_hash = "d" * 64
    own = _holdout_own_event(
        run_id="run-claude-holdout",
        suite_hash="a" * 64,
        evaluator_hash=evaluator_hash,
        target=target,
    )
    regression = _holdout_regression_event(
        run_id="run-issue-holdout",
        suite_id=REGRESSION_TARGET,
        verdict="pass",
        suite_hash="e" * 64,
        evaluator_hash=evaluator_hash,
        target=target,
    )
    summary = {
        "evaluation_id": EVALUATION_ID,
        "own_run_ids": [own["run_id"]],
        "regression_results": [{"suite_id": REGRESSION_TARGET, "run_ids": [regression["run_id"]]}],
    }
    monkeypatch.setattr(
        prm.ev,
        "validate_target_suite",
        lambda _package, _schema, suite_id: [Path(f"{suite_id}.yaml")],
    )
    monkeypatch.setattr(
        prm.ev,
        "load_scenario",
        lambda path, _schema: {
            "id": "holdout" if "claude-harness" in str(path) else "shared-holdout",
            "holdout": True,
        },
    )
    monkeypatch.setattr(
        prm.ev,
        "compute_scenario_hash",
        lambda path: ("b" if "claude-harness" in str(path) else "f") * 64,
    )

    assert not prm._evaluation_covers_current_holdouts(
        [own, regression], summary, target, copy.deepcopy(mh.DEFAULTS)
    )


def test_promote_rejects_missing_own_holdout_for_claude_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude-harness (non-``skill:``) ターゲットの own holdout coverage が suites 構築から
    漏れていた回帰を検知する。own_run_ids が指す run が ledger に存在しなくても、regression
    suite の coverage さえ完全なら promote を誤って許可してしまうバグの再発防止テスト。
    """
    target = "claude-harness"
    evaluator_hash = "d" * 64
    regression = _holdout_regression_event(
        run_id="run-issue-holdout",
        suite_id=REGRESSION_TARGET,
        verdict="pass",
        suite_hash="e" * 64,
        evaluator_hash=evaluator_hash,
        target=target,
    )
    summary = {
        "evaluation_id": EVALUATION_ID,
        "own_run_ids": ["run-missing-own-holdout"],
        "regression_results": [{"suite_id": REGRESSION_TARGET, "run_ids": [regression["run_id"]]}],
    }
    monkeypatch.setattr(
        prm.ev,
        "validate_target_suite",
        lambda _package, _schema, suite_id: [Path(f"{suite_id}.yaml")],
    )
    monkeypatch.setattr(
        prm.ev,
        "load_scenario",
        lambda path, _schema: {
            "id": "holdout" if "claude-harness" in str(path) else "shared-holdout",
            "holdout": True,
        },
    )
    monkeypatch.setattr(
        prm.ev,
        "compute_scenario_hash",
        lambda path: ("b" if "claude-harness" in str(path) else "f") * 64,
    )
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["repeat_frontier"] = 1

    assert not prm._evaluation_covers_current_holdouts([regression], summary, target, config)


def test_promote_detects_unverified_suite_becoming_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite_dir = tmp_path / "future-suite"
    summary = {
        "impacted_targets": ["skill:future"],
        "unverified_impacts": ["skill:future"],
    }
    monkeypatch.setattr(prm.ev, "scenario_suite_dir", lambda *_args: suite_dir)

    assert prm._current_unverified_impacts(summary) == {"skill:future"}
    suite_dir.mkdir()
    scenario = suite_dir / "holdout.yaml"
    scenario.write_text("placeholder\n", encoding="utf-8")
    monkeypatch.setattr(prm.ev, "discover_scenario_paths", lambda _dir: [scenario])
    assert prm._current_unverified_impacts(summary) == set()


def test_promote_rejects_changed_impact_context(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {
        "cand_id": CAND_ID,
        "source_commit": "a" * 40,
        "target": "claude-harness",
        "overlay_files": [],
    }
    evaluation = {
        "impacted_targets": [REGRESSION_TARGET],
        "impact_input_hash": "c" * 64,
    }
    monkeypatch.setattr(prm, "_ref_exists", lambda *_args: True)
    monkeypatch.setattr(prm, "_is_ancestor", lambda *_args: True)
    monkeypatch.setattr(
        prm.ev,
        "candidate_impact_context",
        lambda **_kwargs: prm.ev.skill_targets.SkillImpactContext(("skill:handoff",), "f" * 64),
    )

    with pytest.raises(prm.PromotionValidationError, match="re-run holdout evaluate"):
        prm._check_freshness(
            REPO_ROOT,
            REPO_ROOT,
            manifest,
            mh.DEFAULTS,
            holdout_evaluation=evaluation,
        )


def test_promote_pr_body_warns_about_unverified_impacts() -> None:
    body = prm._build_pr_body(
        CAND_ID,
        {"description": "shared facet update"},
        {"points": []},
        [],
        holdout_evaluation={"unverified_impacts": [REGRESSION_TARGET]},
    )

    assert "Unverified cross-skill impacts" in body
    assert f"`{REGRESSION_TARGET}`" in body
