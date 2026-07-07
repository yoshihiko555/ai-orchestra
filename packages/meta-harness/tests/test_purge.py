"""`purge` サブコマンドのテスト（EV-21, Sec12-3）。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_purge",
    "packages/meta-harness/lib/meta_harness_common.py",
)


def _manifest(cand_id: str, generation: int) -> dict:
    return {
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": None,
        "generation": generation,
        "created_at": mh.now_iso(),
        "created_by": "human",
        "target": "claude-harness",
        "source_commit": "a" * 40,
        "config_hash": "b" * 64,
        "model_versions": {},
        "overlay_files": ["facets/foo/SKILL.md"],
        "description": f"candidate {cand_id}",
    }


def _register_candidate(
    main_root: Path, config: dict, cand_id: str, tmp_root: Path, generation: int = 0
) -> None:
    overlay_dir = tmp_root / f"overlay-{cand_id}"
    (overlay_dir / "facets" / "foo").mkdir(parents=True)
    (overlay_dir / "facets" / "foo" / "SKILL.md").write_text("x", encoding="utf-8")
    mh.register_candidate(
        main_root,
        config,
        cand_id=cand_id,
        manifest=_manifest(cand_id, generation=generation),
        overlay_dir=overlay_dir,
        overlay_files=["facets/foo/SKILL.md"],
    )
    mh.append_ledger_event(
        main_root,
        config,
        {
            "event": "candidate_registered",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": cand_id,
            "parent_id": None,
            "generation": generation,
            "target": "claude-harness",
            "created_by": "human",
        },
    )


def _run_completed(cand_id: str, quality_score: float, verdict: str = "pass") -> dict:
    return {
        "event": "run_completed",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "run_id": f"run-{cand_id}",
        "cand_id": cand_id,
        "scenario_id": "scenario-1",
        "target": "claude-harness",
        "suite_id": "claude-harness",
        "suite_hash": "a" * 64,
        "scenario_hash": "b" * 64,
        "evaluator_hash": "e" * 64,
        "verdict": verdict,
        "quality_score": quality_score,
        "critical_pass_rate": 1.0 if verdict == "pass" else 0.0,
        "cost": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 100,
            "tool_uses": 0,
            "duration_ms": 1,
            "total_cost_usd": 0.01,
            "num_turns": 1,
        },
        "attempt": 1,
        "attempts_total": 1,
        "holdout": False,
    }


class TestPurgeProtection:
    # EV-21
    def test_frontier_candidate_promoted_candidate_and_reserved_candidate_are_protected(
        self, git_project: Path, run_meta, tmp_path: Path
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)

        _register_candidate(git_project, config, "cand-20260101-000001-frontier-cand", tmp_path)
        _register_candidate(git_project, config, "cand-20260101-000002-promoted-cand", tmp_path)
        _register_candidate(git_project, config, "cand-20260101-000003-reserved-cand", tmp_path)
        _register_candidate(git_project, config, "cand-20260101-000004-plain-cand", tmp_path)

        # frontier candidate: high quality run + frontier --rebuild
        mh.append_ledger_event(
            git_project, config, _run_completed("cand-20260101-000001-frontier-cand", 95)
        )
        run_meta("frontier", "--rebuild", project=git_project, check=True)

        # promoted candidate
        mh.append_ledger_event(
            git_project, config, _run_completed("cand-20260101-000002-promoted-cand", 10)
        )
        mh.append_ledger_event(
            git_project,
            config,
            {
                "event": "status_changed",
                "ts": mh.now_iso(),
                "schema_version": "1.0",
                "cand_id": "cand-20260101-000002-promoted-cand",
                "from": "evaluated",
                "to": "promoted",
                "reason": "confirmed",
            },
        )

        # reserved (unreleased) candidate
        mh.append_ledger_event(
            git_project, config, _run_completed("cand-20260101-000003-reserved-cand", 10)
        )
        mh.append_ledger_event(
            git_project,
            config,
            {
                "event": "promotion_reserved",
                "ts": mh.now_iso(),
                "schema_version": "1.0",
                "cand_id": "cand-20260101-000003-reserved-cand",
            },
        )

        # plain candidate: no protection, should be purged when keep_generations=0
        mh.append_ledger_event(
            git_project,
            config,
            _run_completed("cand-20260101-000004-plain-cand", 10, verdict="fail"),
        )

        result = run_meta(
            "purge", "--keep-generations", "0", "--json", project=git_project, check=True
        )
        payload = json.loads(result.stdout)

        candidates_dir = git_project / ".claude" / "meta-harness" / "candidates"
        assert (candidates_dir / "cand-20260101-000001-frontier-cand").is_dir()
        assert (candidates_dir / "cand-20260101-000002-promoted-cand").is_dir()
        assert (candidates_dir / "cand-20260101-000003-reserved-cand").is_dir()
        assert not (candidates_dir / "cand-20260101-000004-plain-cand").is_dir()
        assert payload["purged"] == ["cand-20260101-000004-plain-cand"]

    def test_keep_generations_keeps_newest_generation_unprotected_candidates(
        self, git_project: Path, run_meta, tmp_path: Path
    ) -> None:
        # keep-generations は「候補件数」ではなく「distinct generation の件数」を保持する
        # 単位のため、それぞれ異なる generation を持つ候補で検証する。
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)

        ids = [f"cand-20260101-00000{i}-plain" for i in range(1, 4)]
        for i, cand_id in enumerate(ids):
            _register_candidate(git_project, config, cand_id, tmp_path, generation=i)

        result = run_meta(
            "purge", "--keep-generations", "1", "--json", project=git_project, check=True
        )
        payload = json.loads(result.stdout)

        candidates_dir = git_project / ".claude" / "meta-harness" / "candidates"
        # generation の最大値（=最新世代）を持つ ids[-1] のみが保護される
        assert (candidates_dir / ids[-1]).is_dir()
        assert not (candidates_dir / ids[0]).is_dir()
        assert not (candidates_dir / ids[1]).is_dir()
        assert set(payload["purged"]) == {ids[0], ids[1]}


class TestPurgeKeepGenerationsIsGenerationBased:
    def test_keep_generations_1_protects_all_candidates_in_the_latest_generation(
        self, git_project: Path, run_meta, tmp_path: Path
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)

        gen0_ids = ["cand-20260101-000001-gen0-a", "cand-20260101-000002-gen0-b"]
        gen1_ids = ["cand-20260101-000003-gen1-a", "cand-20260101-000004-gen1-b"]
        for cand_id in gen0_ids:
            _register_candidate(git_project, config, cand_id, tmp_path, generation=0)
        for cand_id in gen1_ids:
            _register_candidate(git_project, config, cand_id, tmp_path, generation=1)

        result = run_meta(
            "purge", "--keep-generations", "1", "--json", project=git_project, check=True
        )
        payload = json.loads(result.stdout)

        candidates_dir = git_project / ".claude" / "meta-harness" / "candidates"
        for cand_id in gen1_ids:
            assert (candidates_dir / cand_id).is_dir()
        for cand_id in gen0_ids:
            assert not (candidates_dir / cand_id).is_dir()
        assert set(payload["purged"]) == set(gen0_ids)

    def test_keep_generations_0_purges_all_unprotected_regardless_of_generation(
        self, git_project: Path, run_meta, tmp_path: Path
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)

        gen0_ids = ["cand-20260101-000001-g0"]
        gen1_ids = ["cand-20260101-000002-g1"]
        for cand_id in gen0_ids:
            _register_candidate(git_project, config, cand_id, tmp_path, generation=0)
        for cand_id in gen1_ids:
            _register_candidate(git_project, config, cand_id, tmp_path, generation=1)

        result = run_meta(
            "purge", "--keep-generations", "0", "--json", project=git_project, check=True
        )
        payload = json.loads(result.stdout)

        candidates_dir = git_project / ".claude" / "meta-harness" / "candidates"
        assert not (candidates_dir / gen0_ids[0]).is_dir()
        assert not (candidates_dir / gen1_ids[0]).is_dir()
        assert set(payload["purged"]) == {*gen0_ids, *gen1_ids}

    def test_candidate_with_unreadable_manifest_is_excluded_from_purge_with_warning(
        self, git_project: Path, run_meta, tmp_path: Path
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)

        cand_id = "cand-20260101-000001-broken-manifest"
        _register_candidate(git_project, config, cand_id, tmp_path, generation=0)
        manifest_path = (
            git_project / ".claude" / "meta-harness" / "candidates" / cand_id / "manifest.json"
        )
        manifest_path.unlink()

        result = run_meta(
            "purge", "--keep-generations", "0", "--json", project=git_project, check=True
        )
        payload = json.loads(result.stdout)

        candidates_dir = git_project / ".claude" / "meta-harness" / "candidates"
        assert (candidates_dir / cand_id).is_dir()
        assert cand_id not in payload["purged"]
        assert "warning" in result.stderr.lower()
        assert cand_id in result.stderr
