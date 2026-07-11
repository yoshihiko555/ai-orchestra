"""Shared fixtures for Phase 3 loop CLI tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_loop_tests", "packages/meta-harness/lib/meta_harness_common.py"
)
loop_cli = load_module("meta_harness_loop_cli_tests", "packages/meta-harness/lib/loop_cli.py")

_HASH = "a" * 64


def _config(**overrides) -> dict:
    config = copy.deepcopy(mh.DEFAULTS)
    config["loop"]["convergence"]["enabled"] = False
    for section, values in overrides.items():
        config[section].update(values)
    return config


def _events(project: Path, config: dict) -> list[dict]:
    return mh.read_ledger_events(project, config)


def _write_manifest(project: Path, config: dict, cand_id: str, target: str) -> None:
    cand_dir = mh.candidates_dir(project, config) / cand_id
    cand_dir.mkdir(parents=True, exist_ok=True)
    overlay_dir = cand_dir / "overlay"
    overlay_dir.mkdir()
    manifest = mh.build_candidate_manifest(
        cand_id=cand_id,
        parent_id=None,
        generation=0,
        target=target,
        source_commit="a" * 40,
        config_hash=mh.compute_config_hash(overlay_dir, config),
        overlay_files=[],
        description="loop fixture",
        created_by="proposer",
    )
    (cand_dir / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")


def _register_loop_candidate(project: Path, config: dict, spec, iteration: int) -> str:
    cand_id = f"cand-20260711-1200{iteration:02d}-loop-{iteration}-abcd"
    _write_manifest(project, config, cand_id, spec.target)
    mh.append_ledger_event(
        project,
        config,
        {
            "event": "candidate_registered",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": cand_id,
            "parent_id": None,
            "generation": iteration,
            "target": spec.target,
            "created_by": "proposer",
            "proposal": {
                "theme": f"iteration {iteration}",
                "based_on_runs": ["run-seed"],
                "cost_usd": 0.0,
                "loop_id": spec.loop_id,
                "iteration": iteration,
            },
        },
    )
    return cand_id


def _append_run(
    project: Path,
    config: dict,
    cand_id: str,
    quality: float,
    *,
    cost: float = 1.0,
    holdout: bool = False,
    scenario_id: str = "create-version-file",
    suite_hash: str | None = None,
    evaluator_hash: str | None = None,
    scenario_hash: str | None = None,
    attempt: int = 1,
    attempts_total: int = 1,
) -> None:
    paths = loop_cli.ev.discover_scenario_paths(
        loop_cli.ev.scenario_suite_dir(loop_cli._PACKAGE_DIR, "claude-harness")
    )
    suite_hash = suite_hash or loop_cli.ev.compute_suite_hash(paths)
    evaluator_hash = evaluator_hash or loop_cli.ev.compute_evaluator_hash(
        config.get("scoring") or {}
    )
    scenario_path = next(
        path
        for path in paths
        if loop_cli.ev.load_scenario(path, loop_cli._SCHEMA_DIR)["id"] == scenario_id
    )
    scenario_hash = scenario_hash or loop_cli.ev.compute_scenario_hash(scenario_path)
    mh.append_ledger_event(
        project,
        config,
        {
            "event": "run_completed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "run_id": f"run-{'holdout-' if holdout else ''}{cand_id}-{scenario_id}-{attempt}",
            "cand_id": cand_id,
            "scenario_id": scenario_id,
            "target": "claude-harness",
            "suite_id": "claude-harness",
            "suite_hash": suite_hash,
            "scenario_hash": scenario_hash,
            "evaluator_hash": evaluator_hash,
            "verdict": "pass",
            "quality_score": quality,
            "critical_pass_rate": 1.0,
            "cost": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "tool_uses": 0,
                "duration_ms": 1,
                "total_cost_usd": cost,
                "num_turns": 1,
            },
            "attempt": attempt,
            "attempts_total": attempts_total,
            "holdout": holdout,
        },
    )


def _install_pipeline(monkeypatch, project: Path, config: dict, qualities: list[float]) -> None:
    def propose(_main_root, _config, _project_dir, spec, iteration):
        return _register_loop_candidate(project, config, spec, iteration)

    def evaluate(_main_root, _config, _project_dir, cand_id, *, holdout):
        if holdout:
            return []
        iteration = next(
            int((event.get("proposal") or {})["iteration"])
            for event in _events(project, config)
            if event.get("cand_id") == cand_id and event.get("event") == "candidate_registered"
        )
        for scenario_id in ("create-version-file", "summarize-readme"):
            _append_run(
                project,
                config,
                cand_id,
                qualities[iteration - 1],
                scenario_id=scenario_id,
            )
        return [{"verdict": "pass"}]

    monkeypatch.setattr(loop_cli, "_propose_candidate", propose)
    monkeypatch.setattr(loop_cli, "_evaluate_candidate", evaluate)
    monkeypatch.setattr(loop_cli, "_evaluation_complete", _stub_evaluation_complete)


def _stub_evaluation_complete(events, _config, _target, cand_id, *, holdout):
    if holdout:
        return True
    return any(
        event.get("event") == "run_completed"
        and event.get("cand_id") == cand_id
        and not event.get("holdout")
        for event in events
    )
