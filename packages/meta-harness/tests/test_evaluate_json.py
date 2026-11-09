"""evaluate --json の評価イベントと既存出力の互換性（EV-119）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

cli = load_module(
    "meta_harness_cli_evaluate_json",
    "packages/meta-harness/scripts/meta_harness.py",
)

CAND_ID = "cand-20261109-120000-json-ab12"
TRAIN_ID = "summarize-readme"
HOLDOUT_ID = "implement-username-validator-holdout"
EVALUATION_KEYS = {
    "evaluation_id",
    "cand_id",
    "holdout",
    "own_suite_hash",
    "evaluator_hash",
    "own_run_ids",
    "verdict",
    "evaluation_base_commit",
}


def _run_result(scenario_id: str, *, verdict: str = "pass") -> dict:
    return {
        "run_id": f"run-json-{scenario_id}",
        "cand_id": CAND_ID,
        "scenario_id": scenario_id,
        "verdict": verdict,
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
        "attempt": 1,
        "attempts_total": 1,
    }


@pytest.fixture()
def evaluate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    config = copy.deepcopy(cli.mh.DEFAULTS)
    config["evaluate"]["repeat_default"] = 1
    config["evaluate"]["repeat_frontier"] = 1
    cli.mh.init_store(tmp_path, config)
    manifest = {
        "cand_id": CAND_ID,
        "parent_id": None,
        "created_by": "human",
        "target": "claude-harness",
        "source_commit": "a" * 40,
    }
    manifest_path = cli.mh.candidates_dir(tmp_path, config) / CAND_ID / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    cli.mh.append_ledger_event(tmp_path, config, {"event": "candidate_registered", **manifest})
    suite_dir = cli._PACKAGE_DIR / "scenarios" / "claude-harness"
    suite_paths = [suite_dir / f"{scenario_id}.yaml" for scenario_id in (TRAIN_ID, HOLDOUT_ID)]
    monkeypatch.setattr(cli, "_resolve_context", lambda _project: (tmp_path, config))
    monkeypatch.setattr(cli.ev, "validate_target_suite", lambda *_args: suite_paths)
    monkeypatch.setattr(cli.ev.siso, "execution_boundary_available", lambda _config: True)
    monkeypatch.setattr(
        cli.ev,
        "check_cli_capabilities",
        lambda *_args, **_kwargs: cli.ev.CliCapabilities(
            claude_version="2.1.202",
            version_pin=None,
            version_pin_match=None,
            checks={},
            judge_tool="codex",
            ok=True,
            reason=None,
        ),
    )
    monkeypatch.setattr(
        cli.ev,
        "candidate_impact_context",
        lambda **_kwargs: cli.ev.skill_targets.SkillImpactContext((), "c" * 64),
    )
    monkeypatch.setattr(
        cli.ev,
        "run_single_attempt",
        lambda **kwargs: _run_result(kwargs["scenario"]["id"]),
    )
    return config


def _evaluate(project: Path, *scenario_ids: str) -> int:
    args = ["evaluate", "--candidate", CAND_ID, "--json", "--project", str(project)]
    for scenario_id in scenario_ids:
        args.extend(["--scenario", scenario_id])
    return cli.main(args)


def _summaries(project: Path, config: dict) -> list[dict]:
    return [
        event
        for event in cli.mh.read_ledger_events_strict(project, config)
        if event["event"] == "evaluation_completed"
    ]


def test_evaluate_json_evaluations_match_current_train_and_holdout_ledger_events(
    tmp_path: Path, evaluate_config: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _evaluate(tmp_path) == cli.EXIT_OK
    capsys.readouterr()
    previous = _summaries(tmp_path, evaluate_config)

    assert _evaluate(tmp_path) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    summaries = _summaries(tmp_path, evaluate_config)[len(previous) :]

    assert len(payload["evaluations"]) == len(summaries) == 2
    assert payload["evaluations"] == [
        {key: event[key] for key in EVALUATION_KEYS} for event in summaries
    ]
    assert all(set(event) == EVALUATION_KEYS for event in payload["evaluations"])
    train, holdout = payload["evaluations"]
    assert train["holdout"] is False
    assert holdout["holdout"] is True
    assert train["evaluation_id"] == holdout["evaluation_id"]
    assert train["evaluation_id"] != previous[0]["evaluation_id"]
    assert train["own_suite_hash"] == holdout["own_suite_hash"]
    assert train["evaluator_hash"] == holdout["evaluator_hash"]
    assert train["cand_id"] == holdout["cand_id"] == CAND_ID
    assert train["evaluation_base_commit"] == holdout["evaluation_base_commit"] == "a" * 40
    assert train["own_run_ids"] == [f"run-json-{TRAIN_ID}"]
    assert holdout["own_run_ids"] == [f"run-json-{HOLDOUT_ID}"]


def test_evaluate_json_preserves_existing_success_payload_golden(
    tmp_path: Path, evaluate_config: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _evaluate(tmp_path) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) == {"status", "runs", "evaluations"}
    assert {key: value for key, value in payload.items() if key != "evaluations"} == {
        "status": "ok",
        "runs": [_run_result(TRAIN_ID), _run_result(HOLDOUT_ID)],
    }


@pytest.mark.parametrize("scenario_id", [TRAIN_ID, HOLDOUT_ID], ids=["train", "holdout"])
def test_evaluate_json_includes_only_the_executed_batch(
    tmp_path: Path,
    evaluate_config: dict,
    capsys: pytest.CaptureFixture[str],
    scenario_id: str,
) -> None:
    assert _evaluate(tmp_path, scenario_id) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    summaries = _summaries(tmp_path, evaluate_config)

    assert len(payload["evaluations"]) == len(summaries) == 1
    assert payload["evaluations"] == [
        {key: event[key] for key in EVALUATION_KEYS} for event in summaries
    ]
    assert payload["evaluations"][0]["holdout"] is (scenario_id == HOLDOUT_ID)
    assert payload["evaluations"][0]["own_run_ids"] == [f"run-json-{scenario_id}"]


def test_evaluate_json_runtime_error_preserves_existing_payload(
    tmp_path: Path,
    evaluate_config: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.ev,
        "run_single_attempt",
        lambda **kwargs: _run_result(kwargs["scenario"]["id"], verdict="error"),
    )

    assert _evaluate(tmp_path) == cli.EXIT_RUNTIME_ERROR
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok",
        "runs": [_run_result(TRAIN_ID, verdict="error"), _run_result(HOLDOUT_ID, verdict="error")],
    }


def test_evaluate_json_batch_error_preserves_empty_stdout(
    tmp_path: Path,
    evaluate_config: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_evaluation(**_kwargs) -> list[dict]:
        raise cli.ev.EvaluationBatchError("forced evaluation failure")

    monkeypatch.setattr(cli.ev, "evaluate_candidate", fail_evaluation)

    assert _evaluate(tmp_path) == cli.EXIT_RUNTIME_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: forced evaluation failure\n"
