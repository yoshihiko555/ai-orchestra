"""Cross-skill regression batch and ledger semantics (EV-54 through EV-58)."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml

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
ROUTING_CONFIG_TARGET = "routing-config"


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
    graded: list[dict] | None = None,
) -> dict:
    result = {
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
    if graded is not None:
        result["graded"] = graded
        passed = sum(1 for check in graded if check["passed"])
        result["graded_pass_rate"] = (passed / len(graded)) if graded else 0.0
    return result


def _registration(*, proposer_cost: float = 0.0, target: str = TARGET) -> dict:
    return {
        "event": "candidate_registered",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "cand_id": CAND_ID,
        "parent_id": None,
        "generation": 0,
        "target": target,
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
    max_budget: float = 30.0,
    unverified: tuple[str, ...] = (),
    target: str = TARGET,
    regression_verdicts: tuple[str, ...] | None = None,
    regression_budget_latched: tuple[bool, ...] | None = None,
    regression_graded: tuple[list[dict] | None, ...] | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    config = copy.deepcopy(mh.DEFAULTS)
    config["regression"]["max_budget_usd"] = max_budget
    mh.init_store(tmp_path, config)
    mh.append_ledger_event(tmp_path, config, _registration(target=target))
    own_paths, own_docs = _suite(target)
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
    if target == ROUTING_CONFIG_TARGET:
        monkeypatch.setattr(ev, "compute_routing_config_base_hash", lambda *_args: "b" * 64)

    def fake_run_scenario_set(**kwargs):
        scenario_id = str(kwargs["scenario_docs"][0][1]["id"])
        if kwargs["suite_id"] == target:
            return [
                _result(
                    suite_id=target,
                    scenario_id=scenario_id,
                    verdict="pass",
                    cost_usd=0.1,
                    tokens=10,
                )
            ]
        verdicts = regression_verdicts or (regression_verdict,)
        latched = regression_budget_latched or tuple(False for _ in verdicts)
        graded_per_run = regression_graded or tuple(None for _ in verdicts)
        assert len(verdicts) == len(latched) == len(graded_per_run)
        results = []
        for index, (verdict, budget_latched, graded) in enumerate(
            zip(verdicts, latched, graded_per_run, strict=True), 1
        ):
            result = _result(
                suite_id=REGRESSION_TARGET,
                scenario_id=scenario_id,
                verdict=verdict,
                cost_usd=regression_cost,
                tokens=999,
                graded=graded,
            )
            result["run_id"] = f"{result['run_id']}-{index}"
            if budget_latched:
                result["budget_latched"] = True
            results.append(result)
        return results

    monkeypatch.setattr(ev, "_run_scenario_set", fake_run_scenario_set)
    manifest = {
        "cand_id": CAND_ID,
        "parent_id": None,
        "target": target,
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
        target=target,
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
    assert points[0]["cost_mean"] == 0.1
    assert points[0]["runs"] == 1
    assert loop_state.non_holdout_summary(events, config, CAND_ID, TARGET) is None


def test_regression_graded_fail_is_strict_suite_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-20260814-050 決定5（regression strict mode）: regression 評価文脈では、run 自体の
    verdict が pass でも graded checks に fail があれば suite fail として扱う（converted
    criticals が gate から抜けた分の回帰ゲートの穴を塞ぐ）。"""
    graded = ({"id": "g1", "passed": False, "oracle": "command_exit", "detail": "regressed"},)
    config, _results, events = _run_batch(
        tmp_path,
        monkeypatch,
        regression_verdict="pass",
        regression_graded=(list(graded),),
    )

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    # 元 run 自体の verdict は "pass"（graded は verdict 判定機構に影響しない、決定1）が、
    # regression suite の合成結果（suite_verdict）は strict mode により "fail" へ格上げされる。
    assert summary["regression_results"][0]["verdict"] == "fail"
    assert summary["regression_results"][0]["critical_pass"] is False
    assert summary["verdict"] == "fail"


def test_regression_graded_all_passed_keeps_suite_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graded 宣言 run で全 graded checks が pass していれば、strict mode は介入せず suite は
    従来どおり run の verdict に従って pass のままになる。"""
    graded = ({"id": "g1", "passed": True, "oracle": "command_exit", "detail": "ok"},)
    config, _results, events = _run_batch(
        tmp_path,
        monkeypatch,
        regression_verdict="pass",
        regression_graded=(list(graded),),
    )

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["regression_results"][0]["verdict"] == "pass"
    assert summary["regression_results"][0]["critical_pass"] is True
    assert summary["verdict"] == "pass"


def test_regression_graded_undeclared_run_is_unaffected_by_strict_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graded 未宣言（"graded" キー欠落）の regression run は strict mode の対象外で、従来どおり
    run の verdict のみで suite verdict が決まる（graded 転換前のシナリオとの後方互換）。"""
    config, _results, events = _run_batch(
        tmp_path, monkeypatch, regression_verdict="pass", regression_graded=(None,)
    )

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["regression_results"][0]["critical_pass"] is True
    assert summary["verdict"] == "pass"


def test_routing_config_regression_failure_blocks_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, events = _run_batch(
        tmp_path,
        monkeypatch,
        regression_verdict="fail",
        target=ROUTING_CONFIG_TARGET,
    )

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["target"] == ROUTING_CONFIG_TARGET
    assert summary["own_critical_pass"] is True
    assert summary["regression_results"][0]["critical_pass"] is False
    assert summary["verdict"] == "fail"

    points = mh.aggregate_run_points(events, config, ROUTING_CONFIG_TARGET)
    assert len(points) == 1
    assert points[0]["eligible"] is False
    frontier, _ = mh.compute_pareto_frontier(
        [point for point in points if point["eligible"]], ROUTING_CONFIG_TARGET
    )
    assert CAND_ID not in frontier


def test_routing_config_budget_latch_is_frontier_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, events = _run_batch(
        tmp_path,
        monkeypatch,
        target=ROUTING_CONFIG_TARGET,
        regression_verdicts=("error",),
        regression_budget_latched=(True,),
    )

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["regression_results"][0]["verdict"] == "error"
    assert summary["regression_results"][0]["critical_pass"] is False
    assert summary["budget_latched_suites"] == [REGRESSION_TARGET]
    assert summary["verdict"] == "pass"

    points = mh.aggregate_run_points(events, config, ROUTING_CONFIG_TARGET)
    assert len(points) == 1
    assert points[0]["eligible"] is True
    frontier, _ = mh.compute_pareto_frontier(points, ROUTING_CONFIG_TARGET)
    assert CAND_ID in frontier


def test_regression_graded_fail_defeats_budget_latch_neutralization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-20260814-050 決定5: budget-latch-only 中立化（EV-54）は「suite に実質的な回帰
    シグナルが一切ない」ことが前提。latched error run と graded-fail pass run が同居する
    suite は、graded fail という実質シグナルを持つため中立化されず suite fail のまま。
    （見落とすと、strict mode が意図した回帰ゲートを budget latch 経路から回避できてしまう。）"""
    graded_fail = [{"id": "g1", "passed": False, "oracle": "command_exit", "detail": "regressed"}]
    config, _results, events = _run_batch(
        tmp_path,
        monkeypatch,
        target=ROUTING_CONFIG_TARGET,
        regression_verdicts=("error", "pass"),
        regression_budget_latched=(True, False),
        regression_graded=(None, graded_fail),
    )

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    # latch 中立化が働かないため suite の combined verdict は "error" のまま（latched error
    # run が残っているため）。中立化されていれば "pass" になっていたはずの箇所が、graded fail
    # という実質シグナルにより budget_latched_suites に載らず regression_error=True として
    # ブロックされることを固定する（中立化されると evaluation verdict が誤って "pass" になる）。
    assert summary["budget_latched_suites"] == []
    assert summary["regression_results"][0]["verdict"] == "error"
    assert summary["regression_results"][0]["critical_pass"] is False
    assert summary["verdict"] == "error"

    points = mh.aggregate_run_points(events, config, ROUTING_CONFIG_TARGET)
    assert len(points) == 1
    assert points[0]["eligible"] is False


def test_budget_latch_mixed_with_non_latched_error_remains_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, events = _run_batch(
        tmp_path,
        monkeypatch,
        regression_verdicts=("error", "error"),
        regression_budget_latched=(True, False),
    )

    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["budget_latched_suites"] == []
    assert summary["verdict"] == "error"


def test_routing_config_empty_claude_harness_holdout_is_vacuously_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["repeat_frontier"] = 1
    mh.init_store(tmp_path, config)
    mh.append_ledger_event(
        tmp_path,
        config,
        _registration(target=ROUTING_CONFIG_TARGET),
    )
    own_paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, ROUTING_CONFIG_TARGET)
    own_docs = [
        (path, scenario)
        for path in own_paths
        if (scenario := ev.load_scenario(path, SCHEMA_DIR))["holdout"]
    ]
    monkeypatch.setattr(
        ev,
        "candidate_impact_context",
        lambda **_kwargs: ev.skill_targets.SkillImpactContext(("claude-harness",), "c" * 64),
    )
    monkeypatch.setattr(ev, "compute_routing_config_base_hash", lambda *_args: "b" * 64)
    # ADR-20260817-052 gave claude-harness real holdout scenarios, so it is no longer an
    # example of a suite with zero holdout scenarios in this repo (every skill:/routing-config
    # suite requires >=1 holdout by construction). Mock `_resolve_regression_suites` directly so
    # this test still exercises the "selected holdout scenario_docs is empty" vacuous-pass code
    # path (line ~3914 of evaluator.py) independent of claude-harness's current suite content,
    # while still deriving suite_hash from the real claude-harness suite files below.
    real_claude_harness_paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, "claude-harness")
    monkeypatch.setattr(
        ev,
        "_resolve_regression_suites",
        lambda *_args, **_kwargs: ([("claude-harness", real_claude_harness_paths, [])], []),
    )

    def fake_run_scenario_set(**kwargs):
        if not kwargs["scenario_docs"]:
            return []
        return [
            _result(
                suite_id=kwargs["suite_id"],
                scenario_id=str(scenario["id"]),
                verdict="pass",
                cost_usd=0.1,
                tokens=10,
            )
            for _, scenario in kwargs["scenario_docs"]
        ]

    monkeypatch.setattr(ev, "_run_scenario_set", fake_run_scenario_set)
    manifest = {
        "cand_id": CAND_ID,
        "parent_id": None,
        "target": ROUTING_CONFIG_TARGET,
        "source_commit": "a" * 40,
        "overlay_files": [],
    }

    ev._evaluate_scenario_batch(
        main_root=tmp_path,
        config=config,
        schema_dir=SCHEMA_DIR,
        package_dir=PACKAGE_DIR,
        project_dir=tmp_path,
        cand_id=CAND_ID,
        cand_dir=tmp_path / "candidate",
        manifest=manifest,
        target=ROUTING_CONFIG_TARGET,
        own_suite_paths=own_paths,
        own_scenarios=own_docs,
        holdout=True,
        repeat_override=1,
        cli_capabilities={},
        runner=lambda *_args, **_kwargs: None,
        evaluation_id=EVALUATION_ID,
    )

    events = mh.read_ledger_events_strict(tmp_path, config)
    summary = next(event for event in events if event["event"] == "evaluation_completed")
    assert summary["verdict"] == "pass"
    assert summary["regression_results"] == [
        {
            "suite_id": "claude-harness",
            "suite_hash": ev.compute_suite_hash(
                ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, "claude-harness")
            ),
            "run_ids": [],
            "verdict": "pass",
            "critical_pass": True,
        }
    ]
    assert prm._evaluation_runs_are_consistent(events, summary, CAND_ID, ROUTING_CONFIG_TARGET)
    # `_evaluation_covers_current_holdouts` re-derives expected holdout coverage via `prm.ev`
    # (a separate module load instance from this file's own `ev`, so the
    # `_resolve_regression_suites` mock above does not reach it). ADR-20260817-052 gave
    # claude-harness real holdout scenarios, so the real suite would no longer produce the
    # "zero expected holdout scenarios" vacuous-pass branch this test targets; mock
    # `validate_target_suite` to keep exercising that branch for claude-harness specifically
    # (mirroring `test_promote_requires_complete_affected_holdouts_for_claude_harness` below),
    # while all other suite_ids still resolve via the real function.
    real_validate_target_suite = prm.ev.validate_target_suite

    def _fake_validate_target_suite(package_dir: Path, schema_dir: Path, suite_id: str) -> list:
        if suite_id == "claude-harness":
            return []
        return real_validate_target_suite(package_dir, schema_dir, suite_id)

    monkeypatch.setattr(prm.ev, "validate_target_suite", _fake_validate_target_suite)
    assert prm._evaluation_covers_current_holdouts(events, summary, ROUTING_CONFIG_TARGET, config)


def test_non_holdout_summary_uses_legacy_runs_without_evaluation_summary() -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    target = "claude-harness"
    paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, target)
    all_docs = [(path, ev.load_scenario(path, SCHEMA_DIR)) for path in paths]
    # ADR-20260817-052: claude-harness now legitimately carries more than 2 scenarios (train +
    # holdout mixed). `non_holdout_summary`'s legacy fallback requires coverage of *every*
    # non-holdout scenario in the current real suite (loop_state._attempt_group_complete via
    # `expected_ids`), so this covers all of them rather than a fixed-size subset. A uniform
    # quality per run keeps the expected mean (90.0) independent of how many scenarios exist.
    scenario_docs = [item for item in all_docs if not item[1]["holdout"]]
    results = []
    for _, scenario in scenario_docs:
        result = _result(
            suite_id=TARGET,
            scenario_id=str(scenario["id"]),
            verdict="pass",
            cost_usd=0.1,
            tokens=10,
        )
        result["run_id"] = f"run-legacy-{scenario['id']}"
        result["quality_score"] = 90.0
        results.append(result)
    # verdict 基準（ADR-20260814-049 決定 3、EV-104）: critical_pass は critical_pass_rate
    # ではなく run の verdict で判定するため、critical_pass_rate だけを 0.0 にしても
    # critical_pass には影響しない（下の verdict 基準テストと対比）。
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
        "critical_pass": True,
    }


def test_non_holdout_summary_critical_pass_uses_verdict_not_critical_pass_rate() -> None:
    """EV-104: critical_pass 集約は verdict 基準。critical_pass_rate<1.0 でも
    verdict=pass なら適格（収束判定から恒久的に除外されない）。逆に verdict=fail の run が
    1 件でもあれば critical_pass=False になる。"""
    config = copy.deepcopy(mh.DEFAULTS)
    target = "claude-harness"
    paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, target)
    all_docs = [(path, ev.load_scenario(path, SCHEMA_DIR)) for path in paths]
    # ADR-20260817-052: see test_non_holdout_summary_uses_legacy_runs_without_evaluation_summary
    # above -- cover every non-holdout scenario in the current real suite, uniform quality.
    scenario_docs = [item for item in all_docs if not item[1]["holdout"]]
    results = []
    for _, scenario in scenario_docs:
        result = _result(
            suite_id=TARGET,
            scenario_id=str(scenario["id"]),
            verdict="pass",
            cost_usd=0.1,
            tokens=10,
        )
        result["run_id"] = f"run-verdict-basis-{scenario['id']}"
        result["quality_score"] = 90.0
        results.append(result)
    # verdict=fail だが critical_pass_rate はまだ 1.0（将来の gate/graded 型を想定した合成
    # データ）。critical_pass_rate 基準なら適格と誤判定されるが、verdict 基準では正しく
    # 不適格になる。
    results[-1]["verdict"] = "fail"
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


# Issue #378 (ADR-20260817-051): budget accounting (`_account_cost_with_broker_metrics`,
# `total_cost_usd`) must stay untouched by the cache-neutral cost axis. This test asserts
# that behavior is unchanged while confirming cache-neutral fields pass through the
# broker-metrics accounting step unmodified (it only ever touches `total_cost_usd`).
def test_broker_cost_accounting_leaves_cache_neutral_fields_untouched() -> None:
    cli_cost = {
        **_cost(0.2, tokens=20),
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 200,
        "cache_neutral_cost_usd": 0.05,
        "cache_neutral_source": "cli",
    }
    scenario = {"budget": {"max_budget_usd": 1.5}}

    accounted = ev._account_cost_with_broker_metrics(
        cli_cost,
        {"broker": {"metrics": {"estimated_cost_usd": 0.7}}},
        scenario,
        mh.DEFAULTS,
    )

    assert accounted["total_cost_usd"] == pytest.approx(0.7)
    assert accounted["cache_creation_input_tokens"] == 100
    assert accounted["cache_read_input_tokens"] == 200
    assert accounted["cache_neutral_cost_usd"] == pytest.approx(0.05)
    assert accounted["cache_neutral_source"] == "cli"


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


def test_default_budget_covers_all_registered_routing_config_regression_suites() -> None:
    impacted_targets = tuple(
        sorted(
            [
                "claude-harness",
                *[
                    f"skill:{path.stem}"
                    for path in (REPO_ROOT / "facets" / "compositions" / "skills").glob("*.yaml")
                ],
            ]
        )
    )
    required_budget = 0.0
    suite_counts: list[int] = []
    unverified_by_phase: list[set[str]] = []
    for holdout, repeat_key in ((False, "repeat_default"), (True, "repeat_frontier")):
        suites, unverified = ev._resolve_regression_suites(
            PACKAGE_DIR,
            SCHEMA_DIR,
            impacted_targets,
            holdout=holdout,
        )
        suite_counts.append(len(suites))
        unverified_by_phase.append(set(unverified))
        repeat = int(mh.DEFAULTS["evaluate"][repeat_key])
        for _, _, scenario_docs in suites:
            for _, scenario in scenario_docs:
                required_budget += repeat * float(
                    scenario["budget"].get(
                        "max_budget_usd",
                        mh.DEFAULTS["scenario_run"]["max_budget_usd_default"],
                    )
                )

    assert suite_counts == [7, 7]
    assert unverified_by_phase[0] == unverified_by_phase[1]
    assert unverified_by_phase[0]
    # ADR-20260817-052 added 2 non-holdout + 2 holdout scenarios to claude-harness (each
    # max_budget_usd=3.0): +2*3.0*repeat_default(1) train + 2*3.0*repeat_frontier(3) holdout =
    # +6.0 + 18.0 = +24.0 over the prior 186.0 baseline.
    assert required_budget == pytest.approx(210.0)
    assert mh.DEFAULTS["regression"]["max_affected_suites"] >= max(suite_counts)
    # NOTE (known gap, out of scope for this PR): `mh.DEFAULTS["regression"]["max_budget_usd"]`
    # (packages/meta-harness/lib/meta_harness_common.py) is a config-load-failure fallback that
    # has historically been kept in lockstep with the effective YAML value, but
    # packages/meta-harness/lib/** is frozen for this PR (concurrent Issue #267 work), so it
    # still reads 186.0 here and would under-budget regression evaluation by $24 if config
    # loading ever fails. The *effective* configured ceiling (what real runs actually use) was
    # already bumped to 210.0 in both packages/meta-harness/config/meta-harness.yaml and
    # .claude/config/meta-harness/meta-harness.yaml, so assert against that instead.
    effective_config = yaml.safe_load(
        (PACKAGE_DIR / "config" / "meta-harness.yaml").read_text(encoding="utf-8")
    )
    assert effective_config["regression"]["max_budget_usd"] >= required_budget


def test_current_routing_suite_coverage_allows_promotion_preconditions(
    git_project: Path,
    git_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EV-86: suite-less skills warn, while every current promotion gate remains reachable."""
    shutil.copytree(REPO_ROOT / "facets", git_project / "facets")
    routing_config_path = git_project / ev.ROUTING_CONFIG_SSOT_RELATIVE
    routing_config_path.parent.mkdir(parents=True)
    routing_config_path.write_bytes((REPO_ROOT / ev.ROUTING_CONFIG_SSOT_RELATIVE).read_bytes())
    git_run("add", "facets", str(ev.ROUTING_CONFIG_SSOT_RELATIVE), cwd=git_project)
    git_run("commit", "-m", "add routing promotion baseline", cwd=git_project)
    source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
    git_run("update-ref", "refs/remotes/origin/main", source_commit, cwd=git_project)

    config = copy.deepcopy(mh.DEFAULTS)
    mh.init_store(git_project, config)
    overlay_dir = tmp_path / "routing-promotion-overlay"
    overlay_dir.mkdir()
    patch = [
        {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": "agents.debugger.tool",
            "value": "claude-direct",
        }
    ]
    (overlay_dir / mh.CONFIG_PATCH_FILENAME).write_text(json.dumps(patch), encoding="utf-8")
    manifest = mh.build_candidate_manifest(
        cand_id=CAND_ID,
        parent_id=None,
        generation=0,
        target=ROUTING_CONFIG_TARGET,
        source_commit=source_commit,
        config_hash=mh.compute_config_hash(overlay_dir, config),
        overlay_files=[],
        description="realistic routing-config promotion candidate",
        created_by="proposer",
        config_patch_hash=mh.compute_config_patch_hash(patch),
    )
    mh.register_candidate(
        git_project,
        config,
        cand_id=CAND_ID,
        manifest=manifest,
        overlay_dir=overlay_dir,
        overlay_files=[],
        target=ROUTING_CONFIG_TARGET,
        created_by="proposer",
        schema_dir=SCHEMA_DIR,
    )
    mh.append_ledger_event(
        git_project,
        config,
        _registration(target=ROUTING_CONFIG_TARGET),
    )

    own_paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, ROUTING_CONFIG_TARGET)
    own_docs = [(path, ev.load_scenario(path, SCHEMA_DIR)) for path in own_paths]

    def fake_run_scenario_set(**kwargs):
        repeat = int(kwargs["repeat_override"])
        results = []
        for _, scenario in kwargs["scenario_docs"]:
            for attempt in range(1, repeat + 1):
                suite_slug = str(kwargs["suite_id"]).replace(":", "-")
                results.append(
                    {
                        "run_id": (
                            f"run-{suite_slug}-{scenario['id']}-{attempt}-"
                            f"{'holdout' if scenario['holdout'] else 'train'}"
                        ),
                        "cand_id": CAND_ID,
                        "scenario_id": str(scenario["id"]),
                        "verdict": "pass",
                        "quality_score": 100.0,
                        "critical_pass_rate": 1.0,
                        "cost": _cost(0.01, tokens=2),
                        "attempt": attempt,
                        "attempts_total": repeat,
                    }
                )
        return results

    monkeypatch.setattr(ev, "_run_scenario_set", fake_run_scenario_set)
    regression_budget = {"remaining_usd": config["regression"]["max_budget_usd"]}
    for holdout in (False, True):
        ev._evaluate_scenario_batch(
            main_root=git_project,
            config=config,
            schema_dir=SCHEMA_DIR,
            package_dir=PACKAGE_DIR,
            project_dir=git_project,
            cand_id=CAND_ID,
            cand_dir=mh.candidates_dir(git_project, config) / CAND_ID,
            manifest=manifest,
            target=ROUTING_CONFIG_TARGET,
            own_suite_paths=own_paths,
            own_scenarios=[item for item in own_docs if bool(item[1]["holdout"]) == holdout],
            holdout=holdout,
            repeat_override=None,
            cli_capabilities={},
            runner=lambda *_args, **_kwargs: None,
            evaluation_id=EVALUATION_ID,
            regression_budget=regression_budget,
        )

    events = mh.read_ledger_events_strict(git_project, config)
    preflight = prm._validate_preconditions(
        git_project,
        config,
        git_project,
        CAND_ID,
        events,
        SCHEMA_DIR,
    )

    holdout = preflight.holdout_evaluation
    assert preflight.cand_id == CAND_ID
    assert CAND_ID in preflight.frontier_doc["frontier"]
    assert holdout["verdict"] == "pass"
    assert set(holdout["unverified_impacts"])
    assert set(holdout["impacted_targets"]) == {
        str(item["suite_id"]) for item in holdout["regression_results"]
    } | set(holdout["unverified_impacts"])


def test_suite_bearing_resolution_failure_is_not_downgraded_to_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    scenario_path = suite_dir / "scenario.yaml"
    scenario_path.write_text("placeholder\n", encoding="utf-8")
    monkeypatch.setattr(ev, "scenario_suite_dir", lambda *_args: suite_dir)
    monkeypatch.setattr(
        ev,
        "validate_target_suite",
        lambda *_args: (_ for _ in ()).throw(ValueError("invalid suite")),
    )

    with pytest.raises(ValueError, match="invalid suite"):
        ev._resolve_regression_suites(
            PACKAGE_DIR,
            SCHEMA_DIR,
            (REGRESSION_TARGET,),
            holdout=False,
        )


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


def test_promote_rejects_budget_latched_regression_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_hash = "a" * 64
    evaluator_hash = "d" * 64
    own = _holdout_own_event(
        run_id="run-own-holdout",
        suite_hash=suite_hash,
        evaluator_hash=evaluator_hash,
    )
    latched = _holdout_regression_event(
        run_id="run-issue-latched",
        suite_id=REGRESSION_TARGET,
        verdict="error",
        suite_hash="e" * 64,
        evaluator_hash=evaluator_hash,
    )
    summary = {
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
                "run_ids": [latched["run_id"]],
                "verdict": "error",
                "critical_pass": False,
            }
        ],
        "budget_latched_suites": [REGRESSION_TARGET],
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
    events = [own, latched, train_summary, summary]
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
    monkeypatch.setattr(prm, "_evaluation_covers_current_holdouts", lambda *_args: True)
    monkeypatch.setattr(prm, "_current_unverified_impacts", lambda _evaluation: set())

    with pytest.raises(
        prm.PromotionValidationError,
        match=rf"budget-latched.*{CAND_ID}.*{REGRESSION_TARGET}",
    ):
        prm._has_current_hash_pair(
            events,
            CAND_ID,
            TARGET,
            frontier,
            mh.DEFAULTS,
            holdout_evaluation=summary,
        )


def test_promote_rejects_train_budget_latched_zero_holdout_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    # This fixture keeps `validate_target_suite`/`compute_suite_hash` fully mocked below, so it
    # is independent of claude-harness's real suite content; it deliberately only fabricates a
    # train-phase latch (ADR-20260817-052 gave claude-harness real holdout scenarios too, but
    # that does not affect this synthetic scenario).
    train_summary = {
        "event": "evaluation_completed",
        "evaluation_id": EVALUATION_ID,
        "cand_id": CAND_ID,
        "target": TARGET,
        "holdout": False,
        "own_suite_hash": suite_hash,
        "evaluator_hash": evaluator_hash,
        "regression_results": [
            {
                "suite_id": "claude-harness",
                "suite_hash": "e" * 64,
                "run_ids": ["run-claude-harness-train"],
                "verdict": "error",
                "critical_pass": False,
            }
        ],
        "budget_latched_suites": ["claude-harness"],
        "verdict": "pass",
    }
    events = [own, train_summary, holdout_summary]
    frontier = {"suite_hash": suite_hash, "evaluator_hash": evaluator_hash}
    monkeypatch.setattr(
        prm.ev,
        "validate_target_suite",
        lambda _package, _schema, suite_id: [Path(f"{suite_id}.yaml")],
    )
    monkeypatch.setattr(prm.ev, "compute_suite_hash", lambda _paths: suite_hash)
    monkeypatch.setattr(prm.ev, "compute_configured_evaluator_hash", lambda _config: evaluator_hash)
    monkeypatch.setattr(prm, "_evaluation_covers_current_holdouts", lambda *_args: True)
    monkeypatch.setattr(prm, "_current_unverified_impacts", lambda _evaluation: set())

    with pytest.raises(
        prm.PromotionValidationError,
        match=rf"budget-latched.*{CAND_ID}.*claude-harness",
    ):
        prm._has_current_hash_pair(
            events,
            CAND_ID,
            TARGET,
            frontier,
            mh.DEFAULTS,
            holdout_evaluation=holdout_summary,
        )


def test_promote_rejects_holdout_budget_latch_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_hash = "a" * 64
    evaluator_hash = "d" * 64
    holdout_summary = {
        "event": "evaluation_completed",
        "evaluation_id": EVALUATION_ID,
        "cand_id": CAND_ID,
        "target": TARGET,
        "holdout": True,
        "own_suite_hash": suite_hash,
        "evaluator_hash": evaluator_hash,
        "budget_latched_suites": [REGRESSION_TARGET],
        "verdict": "pass",
    }
    frontier = {"suite_hash": suite_hash, "evaluator_hash": evaluator_hash}
    monkeypatch.setattr(
        prm.ev,
        "validate_target_suite",
        lambda _package, _schema, suite_id: [Path(f"{suite_id}.yaml")],
    )
    monkeypatch.setattr(prm.ev, "compute_suite_hash", lambda _paths: suite_hash)
    monkeypatch.setattr(prm.ev, "compute_configured_evaluator_hash", lambda _config: evaluator_hash)
    monkeypatch.setattr(prm, "_evaluation_runs_are_consistent", lambda *_args: True)

    with pytest.raises(
        prm.PromotionValidationError,
        match=rf"budget-latched.*{CAND_ID}.*{REGRESSION_TARGET}",
    ):
        prm._has_current_hash_pair(
            [holdout_summary],
            CAND_ID,
            TARGET,
            frontier,
            mh.DEFAULTS,
            holdout_evaluation=holdout_summary,
        )


def test_promote_accepts_events_without_budget_latched_suites(
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
        "budget_latched_suites": [],
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
    monkeypatch.setattr(prm, "_evaluation_covers_current_holdouts", lambda *_args: True)
    monkeypatch.setattr(prm, "_current_unverified_impacts", lambda _evaluation: set())

    assert prm._has_current_hash_pair(
        events,
        CAND_ID,
        TARGET,
        frontier,
        mh.DEFAULTS,
        holdout_evaluation=holdout_summary,
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


def test_routing_config_promote_pr_body_lists_every_unverified_skill() -> None:
    impacted_targets = tuple(
        sorted(
            [
                "claude-harness",
                *[
                    f"skill:{path.stem}"
                    for path in (REPO_ROOT / "facets" / "compositions" / "skills").glob("*.yaml")
                ],
            ]
        )
    )
    _, unverified = ev._resolve_regression_suites(
        PACKAGE_DIR,
        SCHEMA_DIR,
        impacted_targets,
        holdout=True,
    )

    body = prm._build_pr_body(
        CAND_ID,
        {"target": ROUTING_CONFIG_TARGET, "description": "routing update"},
        {"points": []},
        [],
        holdout_evaluation={"unverified_impacts": unverified},
        routing_config_changes=[],
    )
    listed = {
        line[3:-1]
        for line in body.splitlines()
        if line.startswith("- `skill:") and line.endswith("`")
    }

    assert unverified
    assert listed == set(unverified)
