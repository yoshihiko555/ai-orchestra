"""Phase A routing-config reward-hacking countermeasure dry-runs (EV-92)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_adversarial_routing_config",
    "packages/meta-harness/lib/meta_harness_common.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO_ROOT / "packages" / "meta-harness" / "schemas"
TARGET = "routing-config"
SUITE_HASH = "a" * 64
EVALUATOR_HASH = "e" * 64
BASELINE_ID = "cand-20260718-120000-routing-baseline-abcd"
CHEAP_ID = "cand-20260718-120001-cheap-everywhere-abcd"


def _agent_routing_config() -> dict:
    return mh._load_agent_routing_config(SCHEMA_DIR)


def _all_agent_tool_patch(*, force_tool: str | None = None) -> list[dict]:
    agents = _agent_routing_config()["agents"]
    return [
        {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": f"agents.{agent_name}.tool",
            "value": force_tool or str(agent_config["tool"]),
        }
        for agent_name, agent_config in sorted(agents.items())
    ]


def _register_patch(
    main_root: Path,
    config: dict,
    overlay_root: Path,
    *,
    cand_id: str,
    patch: list[dict],
    created_by: str,
) -> dict:
    overlay_dir = overlay_root / cand_id
    overlay_dir.mkdir(parents=True)
    (overlay_dir / mh.CONFIG_PATCH_FILENAME).write_text(json.dumps(patch), encoding="utf-8")
    manifest = mh.build_candidate_manifest(
        cand_id=cand_id,
        parent_id=None,
        generation=0,
        target=TARGET,
        source_commit="a" * 40,
        config_hash=mh.compute_config_hash(overlay_dir, config),
        overlay_files=[],
        description="adversarial routing-config dry-run",
        created_by=created_by,
        config_patch_hash=mh.compute_config_patch_hash(patch),
    )
    mh.register_candidate(
        main_root,
        config,
        cand_id=cand_id,
        manifest=manifest,
        overlay_dir=overlay_dir,
        overlay_files=[],
        target=TARGET,
        created_by=created_by,
        schema_dir=SCHEMA_DIR,
    )
    return {
        "event": "candidate_registered",
        "cand_id": cand_id,
        "target": TARGET,
        "created_by": created_by,
    }


def _run(
    cand_id: str,
    scenario_id: str,
    *,
    quality: float,
    cost_usd: float,
    verdict: str = "pass",
) -> dict:
    return {
        "event": "run_completed",
        "run_id": f"run-{cand_id}-{scenario_id}",
        "cand_id": cand_id,
        "scenario_id": scenario_id,
        "target": TARGET,
        "suite_id": TARGET,
        "suite_hash": SUITE_HASH,
        "scenario_hash": "b" * 64,
        "evaluator_hash": EVALUATOR_HASH,
        "verdict": verdict,
        "quality_score": quality,
        "critical_pass_rate": 1.0 if verdict == "pass" else 0.0,
        "cost": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "tool_uses": 0,
            "duration_ms": 1,
            "total_cost_usd": cost_usd,
            "num_turns": 1,
        },
        "attempt": 1,
        "attempts_total": 1,
        "holdout": False,
    }


def _evaluation(cand_id: str, runs: list[dict], *, verdict: str) -> dict:
    return {
        "event": "evaluation_completed",
        "evaluation_id": f"eval-{cand_id}",
        "cand_id": cand_id,
        "target": TARGET,
        "holdout": False,
        "own_run_ids": [str(run["run_id"]) for run in runs],
        "own_suite_hash": SUITE_HASH,
        "evaluator_hash": EVALUATOR_HASH,
        "own_critical_pass": verdict == "pass",
        "regression_results": [],
        "verdict": verdict,
        "unverified_impacts": [],
        "evaluation_base_commit": "a" * 40,
        "impacted_targets": [],
        "impact_input_hash": "c" * 64,
        "regression_cost_usd": 0.0,
    }


def test_cheap_everywhere_cannot_win_on_cost_and_behavioral_failure_excludes_it(
    tmp_path: Path,
) -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    mh.init_store(tmp_path, config)
    baseline_registered = _register_patch(
        tmp_path,
        config,
        tmp_path / "overlays",
        cand_id=BASELINE_ID,
        patch=_all_agent_tool_patch(),
        created_by="human",
    )
    cheap_patch = _all_agent_tool_patch(force_tool="claude-direct")
    cheap_registered = _register_patch(
        tmp_path,
        config,
        tmp_path / "overlays",
        cand_id=CHEAP_ID,
        patch=cheap_patch,
        created_by="proposer",
    )

    assert len(cheap_patch) == len(_agent_routing_config()["agents"])
    assert all(item["value"] == "claude-direct" for item in cheap_patch)

    baseline_runs = [
        _run(BASELINE_ID, "mechanical", quality=100.0, cost_usd=0.20),
        _run(BASELINE_ID, "behavioral", quality=100.0, cost_usd=0.20),
    ]
    equal_quality_cheap_runs = [
        _run(CHEAP_ID, "mechanical", quality=100.0, cost_usd=0.01),
        _run(CHEAP_ID, "behavioral", quality=100.0, cost_usd=0.01),
    ]
    equal_quality_events = [
        baseline_registered,
        cheap_registered,
        *baseline_runs,
        *equal_quality_cheap_runs,
        _evaluation(BASELINE_ID, baseline_runs, verdict="pass"),
        _evaluation(CHEAP_ID, equal_quality_cheap_runs, verdict="pass"),
    ]

    points = mh.aggregate_run_points(equal_quality_events, config, TARGET)
    by_id = {point["cand_id"]: point for point in points}
    assert by_id[CHEAP_ID]["quality_mean"] == by_id[BASELINE_ID]["quality_mean"]
    assert by_id[CHEAP_ID]["cost_mean"] < by_id[BASELINE_ID]["cost_mean"]
    frontier, dominated = mh.compute_pareto_frontier(points, TARGET)
    assert set(frontier) == {BASELINE_ID, CHEAP_ID}
    assert dominated == []

    degraded_cheap_runs = [
        _run(CHEAP_ID, "mechanical", quality=100.0, cost_usd=0.01),
        _run(
            CHEAP_ID,
            "behavioral",
            quality=50.0,
            cost_usd=0.01,
            verdict="fail",
        ),
    ]
    degraded_events = [
        baseline_registered,
        cheap_registered,
        *baseline_runs,
        *degraded_cheap_runs,
        _evaluation(BASELINE_ID, baseline_runs, verdict="pass"),
        _evaluation(CHEAP_ID, degraded_cheap_runs, verdict="fail"),
    ]

    degraded_points = mh.aggregate_run_points(degraded_events, config, TARGET)
    degraded_by_id = {point["cand_id"]: point for point in degraded_points}
    assert degraded_by_id[CHEAP_ID]["quality_mean"] < degraded_by_id[BASELINE_ID]["quality_mean"]
    assert degraded_by_id[CHEAP_ID]["eligible"] is False
    eligible = [point for point in degraded_points if point["eligible"]]
    frontier, dominated = mh.compute_pareto_frontier(eligible, TARGET)
    assert frontier == [BASELINE_ID]
    assert CHEAP_ID not in frontier
    assert dominated == []


def test_register_rejects_mixed_kind_and_codex_model_proposer_patches(tmp_path: Path) -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    mh.init_store(tmp_path, config)
    invalid_cases = [
        (
            "cand-20260718-120002-mixed-kind-abcd",
            [
                *_all_agent_tool_patch(force_tool="claude-direct"),
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "antigravity.model",
                    "value": _agent_routing_config()["antigravity"]["model"],
                },
            ],
            "mixed kinds",
        ),
        (
            "cand-20260718-120003-codex-model-abcd",
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": _agent_routing_config()["codex"]["model"],
                }
            ],
            "created_by='proposer' is not allowed",
        ),
    ]

    for cand_id, patch, expected in invalid_cases:
        with pytest.raises(ValueError, match=expected):
            _register_patch(
                tmp_path,
                config,
                tmp_path / "invalid-overlays",
                cand_id=cand_id,
                patch=patch,
                created_by="proposer",
            )
        assert mh.read_candidate_manifest(tmp_path, config, cand_id) is None
