"""target 別 frontier / proposer store 隔離のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common",
    "packages/meta-harness/lib/meta_harness_common.py",
)
proposer = load_module(
    "meta_harness_proposer_target_isolation",
    "packages/meta-harness/lib/proposer.py",
)
mh_cli = load_module(
    "meta_harness_cli_target_isolation",
    "packages/meta-harness/scripts/meta_harness.py",
)


def _frontier(target: str, cand_id: str | None = None) -> dict:
    points = []
    frontier = []
    if cand_id is not None:
        points.append(
            {
                "cand_id": cand_id,
                "quality_mean": 100.0,
                "quality_var": 0.0,
                "quality_min": 100.0,
                "cost_mean": 10.0,
                "runs": 1,
                "eligible": True,
            }
        )
        frontier.append(cand_id)
    return {
        "schema_version": "1.0",
        "target": target,
        "generated_at": mh.now_iso(),
        "ledger_line_count": 0,
        "suite_hash": "a" * 64,
        "evaluator_hash": "b" * 64,
        "cost_axis": "total_tokens",
        "points": points,
        "frontier": frontier,
        "dominated": [],
    }


def _manifest(cand_id: str, target: str, generation: int = 0) -> dict:
    return {
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": None,
        "generation": generation,
        "created_at": mh.now_iso(),
        "created_by": "human",
        "target": target,
        "source_commit": "a" * 40,
        "config_hash": "b" * 64,
        "model_versions": {},
        "overlay_files": [],
        "description": cand_id,
    }


def _write_manifest(root: Path, config: dict, cand_id: str, target: str) -> None:
    candidate = mh.candidates_dir(root, config) / cand_id
    candidate.mkdir(parents=True)
    (candidate / "manifest.json").write_text(
        json.dumps(_manifest(cand_id, target)) + "\n",
        encoding="utf-8",
    )


def _registered(cand_id: str, target: str) -> dict:
    return {
        "event": "candidate_registered",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": None,
        "generation": 0,
        "target": target,
        "created_by": "human",
    }


def _run(cand_id: str, target: str) -> dict:
    return {
        "event": "run_completed",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "run_id": f"run-{cand_id}",
        "cand_id": cand_id,
        "scenario_id": "scenario-1",
        "target": target,
        "suite_id": target,
        "suite_hash": "a" * 64,
        "scenario_hash": "c" * 64,
        "evaluator_hash": "b" * 64,
        "verdict": "pass",
        "quality_score": 100.0,
        "critical_pass_rate": 1.0,
        "cost": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "tool_uses": 1,
            "duration_ms": 1,
            "total_cost_usd": 0.01,
            "num_turns": 1,
        },
        "attempt": 1,
        "attempts_total": 1,
        "holdout": False,
    }


class TestTargetFrontierCache:
    def test_targets_use_distinct_cache_files(self, tmp_path: Path) -> None:
        mh.init_store(tmp_path, mh.DEFAULTS)
        mh.write_frontier_cache(
            tmp_path,
            mh.DEFAULTS,
            _frontier("skill:handoff", "skill-candidate"),
            "skill:handoff",
        )

        assert mh.frontier_path(tmp_path, mh.DEFAULTS).name == "frontier-claude-harness.json"
        assert (
            mh.frontier_path(tmp_path, mh.DEFAULTS, "skill:handoff").name
            == "frontier-skill-handoff.json"
        )
        assert mh.read_frontier_cache(tmp_path, mh.DEFAULTS)["frontier"] == []
        assert mh.read_frontier_cache(tmp_path, mh.DEFAULTS, "skill:handoff")["frontier"] == [
            "skill-candidate"
        ]

    def test_init_migrates_legacy_cache_only_to_default_target(self, tmp_path: Path) -> None:
        store = mh.store_dir(tmp_path, mh.DEFAULTS)
        store.mkdir(parents=True)
        legacy = _frontier("claude-harness")
        legacy.pop("target")
        mh.legacy_frontier_path(tmp_path, mh.DEFAULTS).write_text(
            json.dumps(legacy) + "\n", encoding="utf-8"
        )

        mh.init_store(tmp_path, mh.DEFAULTS)

        assert mh.read_frontier_cache(tmp_path, mh.DEFAULTS)["target"] == "claude-harness"
        assert not mh.frontier_path(tmp_path, mh.DEFAULTS, "skill:handoff").exists()


class TestTargetAggregation:
    def test_cross_target_runs_are_excluded(self) -> None:
        events = [
            _registered("claude-candidate", "claude-harness"),
            _run("claude-candidate", "claude-harness"),
            _registered("skill-candidate", "skill:handoff"),
            _run("skill-candidate", "skill:handoff"),
        ]

        claude_points = mh.aggregate_run_points(events, mh.DEFAULTS, "claude-harness")
        skill_points = mh.aggregate_run_points(events, mh.DEFAULTS, "skill:handoff")

        assert [point["cand_id"] for point in claude_points] == ["claude-candidate"]
        assert [point["cand_id"] for point in skill_points] == ["skill-candidate"]


class TestTargetFilteredViewSnapshot:
    def test_snapshot_excludes_other_target_candidates_runs_and_events(
        self, tmp_path: Path
    ) -> None:
        mh.init_store(tmp_path, mh.DEFAULTS)
        mh.write_frontier_cache(
            tmp_path,
            mh.DEFAULTS,
            _frontier("skill:handoff", "skill-candidate"),
            "skill:handoff",
        )
        _write_manifest(tmp_path, mh.DEFAULTS, "claude-candidate", "claude-harness")
        _write_manifest(tmp_path, mh.DEFAULTS, "skill-candidate", "skill:handoff")
        for event in (
            _registered("claude-candidate", "claude-harness"),
            _run("claude-candidate", "claude-harness"),
            _registered("skill-candidate", "skill:handoff"),
            _run("skill-candidate", "skill:handoff"),
            {
                "event": "status_changed",
                "cand_id": "skill-candidate",
                "from": "candidate",
                "to": "evaluated",
            },
            {
                "event": "status_changed",
                "target": "skill:handoff",
                "cand_id": "claude-candidate",
                "from": "candidate",
                "to": "evaluated",
            },
        ):
            mh.append_ledger_event(tmp_path, mh.DEFAULTS, event)

        snapshot = proposer.snapshot_filtered_store(tmp_path, mh.DEFAULTS, "skill:handoff")

        assert snapshot.candidate_ids == ("skill-candidate",)
        assert snapshot.non_holdout_run_ids == ("run-skill-candidate",)
        serialized = "\n".join(json.dumps(event) for event in snapshot.ledger_events)
        assert "skill-candidate" in serialized
        assert "claude-candidate" not in serialized


class TestPurgeTargetUnion:
    def test_candidates_on_each_target_frontier_are_protected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        mh.init_store(tmp_path, mh.DEFAULTS)
        for cand_id, target in (
            ("claude-frontier", "claude-harness"),
            ("skill-frontier", "skill:handoff"),
            ("plain", "claude-harness"),
        ):
            _write_manifest(tmp_path, mh.DEFAULTS, cand_id, target)
            mh.append_ledger_event(tmp_path, mh.DEFAULTS, _registered(cand_id, target))

        seen_targets: list[str] = []

        def fake_frontier(_root: Path, _config: dict, target: str) -> dict:
            seen_targets.append(target)
            cand_id = "skill-frontier" if target == "skill:handoff" else "claude-frontier"
            return _frontier(target, cand_id)

        monkeypatch.setattr(mh_cli, "_compute_frontier", fake_frontier)

        deletable = mh_cli._purge_eligible_ids(tmp_path, mh.DEFAULTS, 0)

        assert seen_targets == ["claude-harness", "skill:handoff"]
        assert deletable == ["plain"]

    def test_frontier_recompute_failure_returns_validation_error_without_deletion(
        self, git_project: Path, monkeypatch, capsys
    ) -> None:
        mh.init_store(git_project, mh.DEFAULTS)
        for cand_id, target in (
            ("claude-candidate", "claude-harness"),
            ("skill-candidate", "skill:handoff"),
        ):
            _write_manifest(git_project, mh.DEFAULTS, cand_id, target)
            mh.append_ledger_event(git_project, mh.DEFAULTS, _registered(cand_id, target))

        def fail_on_skill(_root: Path, _config: dict, target: str) -> dict:
            if target == "skill:handoff":
                raise ValueError("skill frontier is invalid")
            return _frontier(target)

        monkeypatch.setattr(mh_cli, "_compute_frontier", fail_on_skill)

        result = mh_cli.cmd_purge(str(git_project), 0, False)

        assert result == mh_cli.EXIT_VALIDATION_ERROR
        assert "error: skill frontier is invalid" in capsys.readouterr().err
        candidates = mh.candidates_dir(git_project, mh.DEFAULTS)
        assert (candidates / "claude-candidate").is_dir()
        assert (candidates / "skill-candidate").is_dir()
