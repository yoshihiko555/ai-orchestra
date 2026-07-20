"""成果物移送のテスト（EV-09, EV-10, EV-11, EV-12, EV-19, EV-24, Sec2-1 手順8, Sec2-6, Sec3-6）。"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_artifacts",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_artifacts",
    "packages/meta-harness/lib/meta_harness_common.py",
)

_SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"

_CONFIG = {
    "scoring": {
        "critical_weight": 70,
        "penalty_base": 30,
        "penalty_per_item": 5,
        "penalty_missing_report": 6,
    }
}


def test_routing_config_base_hash_tracks_promotion_ssot_bytes(git_project: Path, git_run) -> None:
    initial_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
    with pytest.raises(ValueError, match="could not be read from source_commit"):
        ev.compute_routing_config_base_hash(git_project, initial_commit)

    ssot = git_project / ev.ROUTING_CONFIG_SSOT_RELATIVE
    ssot.parent.mkdir(parents=True)
    known_content = b"codex:\n  model: before\n"
    ssot.write_bytes(known_content)
    git_run("add", ssot.relative_to(git_project).as_posix(), cwd=git_project)
    git_run("commit", "-m", "add routing config", cwd=git_project)
    source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()

    assert (
        ev.compute_routing_config_base_hash(git_project, source_commit)
        == hashlib.sha256(known_content).hexdigest()
    )
    with pytest.raises(ValueError, match="could not be read from source_commit"):
        ev.compute_routing_config_base_hash(git_project, "0" * 40)


def test_routing_config_batch_threads_one_base_hash_to_all_attempt_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = copy.deepcopy(mh.DEFAULTS)
    scenario_paths = ev.validate_target_suite(
        Path("packages/meta-harness").resolve(),
        Path("packages/meta-harness/schemas").resolve(),
        "routing-config",
    )
    scenario_path, scenario = next(
        (path, ev.load_scenario(path, Path("packages/meta-harness/schemas").resolve()))
        for path in scenario_paths
        if not ev.load_scenario(path, Path("packages/meta-harness/schemas").resolve()).get(
            "holdout"
        )
    )
    sentinel_hash = "e" * 64
    hash_calls: list[tuple[Path, str]] = []
    emitted: list[dict] = []

    def fake_base_hash(project_dir: Path, source_commit: str) -> str:
        hash_calls.append((project_dir, source_commit))
        return sentinel_hash

    def fake_lifecycle(**kwargs):
        isolation = {
            "backend": "srt",
            "srt_version": "1.0.0",
            "settings_sha256": "f" * 64,
            "platform_profile_input_sha256": "a" * 64,
        }
        staging = kwargs["staging_dir"]
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "isolation.json").write_text(
            json.dumps(isolation) + "\n",
            encoding="utf-8",
        )
        checks = [{"id": "c1", "passed": True, "oracle": "command_exit", "detail": "exit=0"}]
        return checks, [], False, []

    monkeypatch.setattr(ev, "compute_routing_config_base_hash", fake_base_hash)
    monkeypatch.setattr(ev, "_run_attempt_lifecycle", fake_lifecycle)
    monkeypatch.setattr(
        ev,
        "candidate_impact_context",
        lambda **_kwargs: ev.skill_targets.SkillImpactContext((), "b" * 64),
    )
    monkeypatch.setattr(
        ev,
        "_append_evaluation_events",
        lambda _root, _config, _schema, events: emitted.extend(events),
    )
    source_commit = "c" * 40

    results = ev._evaluate_scenario_batch(
        main_root=tmp_path,
        config=config,
        schema_dir=Path("packages/meta-harness/schemas").resolve(),
        package_dir=Path("packages/meta-harness").resolve(),
        project_dir=tmp_path,
        cand_id="cand-20260716-120000-routing-abcd",
        cand_dir=tmp_path / "candidate",
        manifest={"source_commit": source_commit, "config_hash": "d" * 64},
        target="routing-config",
        own_suite_paths=[scenario_path],
        own_scenarios=[(scenario_path, scenario)],
        holdout=False,
        repeat_override=2,
        cli_capabilities={"claude_version": "2.1.207", "ok": True},
        runner=lambda *_args, **_kwargs: None,
    )

    summary = next(event for event in emitted if event["event"] == "evaluation_completed")
    assert hash_calls == [(tmp_path, source_commit)]
    assert summary["routing_config_base_hash"] == sentinel_hash
    assert len(results) == 2
    for result in results:
        metadata_path = mh.runs_dir(tmp_path, config) / result["run_id"] / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["routing_config_base_hash"] == summary["routing_config_base_hash"]


def _minimal_result(run_id: str = "run-20260707-120000-slug-scn-a1-abcd1234") -> dict:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "cand_id": "cand-20260707-120000-slug-ab12",
        "scenario_id": "scn",
        "verdict": "pass",
        "critical": [{"id": "c1", "passed": True, "oracle": "command_exit", "detail": "exit=0"}],
        "critical_pass_rate": 1.0,
        "checks": [],
        "self_report": None,
        "penalty": 6.0,
        "quality_score": 70.0,
        "cost": dict(ev.ZERO_COST),
        "attempt": 1,
        "attempts_total": 1,
        "claude_version": "2.1.202",
        "errors": [],
    }


class TestFinalizeArtifactsRedactionAndCompression:
    def test_events_jsonl_is_redacted_then_gzipped(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run_dir"
        run_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "events.jsonl").write_text(
            json.dumps({"type": "result", "secret": _SECRET}) + "\n", encoding="utf-8"
        )

        ev._finalize_artifacts(run_dir, staging_dir, _minimal_result())

        gz_path = run_dir / "events.jsonl.gz"
        assert gz_path.is_file()
        with gzip.open(gz_path, "rt", encoding="utf-8") as f:
            content = f.read()
        assert _SECRET not in content
        assert "[REDACTED:" in content

    def test_progress_log_is_redacted_but_not_gzipped(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run_dir"
        run_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "progress.log").write_text(f"warning: leaked {_SECRET}\n", encoding="utf-8")

        ev._finalize_artifacts(run_dir, staging_dir, _minimal_result())

        progress_path = run_dir / "progress.log"
        assert progress_path.is_file()
        assert not (run_dir / "progress.log.gz").exists()
        content = progress_path.read_text(encoding="utf-8")
        assert _SECRET not in content
        assert "[REDACTED:" in content

    def test_result_json_and_report_md_are_written(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run_dir"
        run_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        result = _minimal_result()
        ev._finalize_artifacts(run_dir, staging_dir, result)

        result_path = run_dir / "result.json"
        assert result_path.is_file()
        assert json.loads(result_path.read_text(encoding="utf-8"))["run_id"] == result["run_id"]

        report_path = run_dir / "report.md"
        assert report_path.is_file()
        assert result["run_id"] in report_path.read_text(encoding="utf-8")

    def test_result_json_and_report_md_are_redacted(self, tmp_path: Path) -> None:
        """Codex 指摘（evaluator.py:1211）: result["errors"] の detail に紛れ込んだ
        シークレット文字列が result.json/report.md へ平文で残らないこと。"""
        run_dir = tmp_path / "run_dir"
        run_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        result = _minimal_result()
        result["verdict"] = "error"
        result["errors"] = [
            {"stage": "setup", "type": "setup_error", "message": f"leaked token: {_SECRET}"}
        ]

        ev._finalize_artifacts(run_dir, staging_dir, result)

        result_content = (run_dir / "result.json").read_text(encoding="utf-8")
        assert _SECRET not in result_content
        assert "[REDACTED:" in result_content
        # 依然として valid JSON であること（redaction が構造を壊していない）
        assert json.loads(result_content)["run_id"] == result["run_id"]

        report_content = (run_dir / "report.md").read_text(encoding="utf-8")
        assert _SECRET not in report_content
        assert "[REDACTED:" in report_content

    def test_missing_staging_files_do_not_crash(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run_dir"
        run_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        # events.jsonl / progress.log は存在しない（worktree 作成前に失敗したケース）。
        ev._finalize_artifacts(run_dir, staging_dir, _minimal_result())
        assert not (run_dir / "events.jsonl.gz").exists()
        assert not (run_dir / "progress.log").exists()
        assert (run_dir / "result.json").is_file()


class TestHoldoutPhysicalSeparation:
    """EV-19: holdout シナリオの run 成果物は holdout/runs/ に分離保存される。"""

    def _run(self, tmp_path: Path, monkeypatch, *, holdout: bool) -> tuple[dict, Path]:
        mh_config = {**mh.DEFAULTS, "scoring": _CONFIG["scoring"]}
        main_root = tmp_path

        def fake_lifecycle(**kwargs):
            isolation = {
                "backend": "srt",
                "srt_version": "1.0.0",
                "settings_sha256": "e" * 64,
                "platform_profile_input_sha256": "f" * 64,
            }
            staging = kwargs["staging_dir"]
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "isolation.json").write_text(json.dumps(isolation) + "\n", encoding="utf-8")
            checks = [{"id": "c1", "passed": True, "oracle": "command_exit", "detail": "exit=0"}]
            return checks, [], False, []

        monkeypatch.setattr(ev, "_run_attempt_lifecycle", fake_lifecycle)

        scenario = {
            "id": "scn",
            "prompt": "irrelevant prompt",
            "setup": [],
            "critical": [],
            "checks": [],
            "holdout": holdout,
        }
        cli_capabilities = {"claude_version": "2.1.202", "ok": True}
        manifest = {"source_commit": "a" * 40, "config_hash": "b" * 64}

        result = ev.run_single_attempt(
            main_root=main_root,
            config=mh_config,
            schema_dir=Path("packages/meta-harness/schemas").resolve(),
            package_dir=Path("packages/meta-harness").resolve(),
            project_dir=tmp_path,
            cand_id="cand-20260707-120000-slug-ab12",
            cand_dir=tmp_path / "cand",
            manifest=manifest,
            target="claude-harness",
            routing_config_base_hash=None,
            scenario=scenario,
            scenario_path=Path(
                "packages/meta-harness/scenarios/claude-harness/summarize-readme.yaml"
            ).resolve(),
            suite_hash="c" * 64,
            evaluator_hash="d" * 64,
            attempt=1,
            attempts_total=1,
            cli_capabilities=cli_capabilities,
        )
        return result, main_root

    def test_holdout_scenario_goes_to_holdout_runs_dir(self, tmp_path: Path, monkeypatch) -> None:
        result, main_root = self._run(tmp_path, monkeypatch, holdout=True)
        run_dir = mh.holdout_runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        assert run_dir.is_dir()
        non_holdout_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        assert not non_holdout_dir.exists()

    def test_non_holdout_scenario_goes_to_runs_dir(self, tmp_path: Path, monkeypatch) -> None:
        result, main_root = self._run(tmp_path, monkeypatch, holdout=False)
        run_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        assert run_dir.is_dir()
        holdout_dir = mh.holdout_runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        assert not holdout_dir.exists()

    def test_ledger_event_records_holdout_flag(self, tmp_path: Path, monkeypatch) -> None:
        _result, main_root = self._run(tmp_path, monkeypatch, holdout=True)
        events = mh.read_ledger_events(main_root, mh.DEFAULTS)
        run_completed = [e for e in events if e.get("event") == "run_completed"]
        assert run_completed
        assert run_completed[-1]["holdout"] is True

    def test_metadata_records_finished_at_after_completion(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        result, main_root = self._run(tmp_path, monkeypatch, holdout=False)
        run_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["finished_at"] is not None
        assert metadata["holdout"] is False
        assert metadata["isolation"]["backend"] == "srt"
        assert metadata["isolation"]["srt_version"] == "1.0.0"
        assert metadata["allowed_tools"] == mh.DEFAULTS["evaluate"]["allowed_tools"]
        assert metadata["allowed_tools_source"] == "global"
        assert metadata["model_tools"] == ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
        assert metadata["max_output_tokens"] == 4096
        assert metadata["max_output_tokens_source"] == "global"
        assert metadata["path_prepend"] == []

    def test_result_json_records_claude_version(self, tmp_path: Path, monkeypatch) -> None:
        """EV-24: result.json に claude_version フィールドが必須として記録される。"""
        result, main_root = self._run(tmp_path, monkeypatch, holdout=False)
        run_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        on_disk = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert on_disk["claude_version"] == "2.1.202"


def _run_budget_latch_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    checks_non_critical: list[dict] | None = None,
) -> tuple[dict, dict]:
    def fake_lifecycle(**kwargs):
        isolation = {
            "broker": {
                "metrics": {
                    "budget_rejected_count": 1,
                    "budget_exceeded": True,
                    "anomaly_reasons": [
                        "request cost upper bound exceeds the remaining run budget"
                    ],
                }
            }
        }
        staging = kwargs["staging_dir"]
        (staging / "isolation.json").write_text(json.dumps(isolation) + "\n", encoding="utf-8")
        errors = [
            {
                "stage": "run",
                "type": "run_error",
                "message": "claude -p reported is_error=True (subtype=success)",
            },
        ]
        return [], checks_non_critical or [], True, errors

    monkeypatch.setattr(ev, "_run_attempt_lifecycle", fake_lifecycle)
    result = ev.run_single_attempt(
        main_root=tmp_path,
        config=mh.DEFAULTS,
        schema_dir=Path("packages/meta-harness/schemas").resolve(),
        package_dir=Path("packages/meta-harness").resolve(),
        project_dir=tmp_path,
        cand_id="cand-20260720-120000-latch-ab12",
        cand_dir=tmp_path / "cand",
        manifest={"source_commit": "a" * 40, "config_hash": "b" * 64},
        target="claude-harness",
        routing_config_base_hash=None,
        scenario={
            "id": "scn",
            "prompt": "irrelevant prompt",
            "critical": [],
            "holdout": False,
        },
        scenario_path=Path(
            "packages/meta-harness/scenarios/claude-harness/summarize-readme.yaml"
        ).resolve(),
        suite_hash="c" * 64,
        evaluator_hash="d" * 64,
        attempt=1,
        attempts_total=1,
        cli_capabilities={"claude_version": "2.1.207", "ok": True},
    )

    run_dir = mh.runs_dir(tmp_path, mh.DEFAULTS) / result["run_id"]
    on_disk = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    return result, on_disk


def test_error_result_records_budget_latch_from_broker_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, on_disk = _run_budget_latch_attempt(tmp_path, monkeypatch)

    assert result["verdict"] == "error"
    assert result["budget_latched"] is True
    assert on_disk["budget_latched"] is True


def test_schema_error_prevents_budget_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_check = {
        "id": "invalid-check",
        "passed": "not-a-boolean",
        "oracle": "command_exit",
        "detail": "invalid check result",
    }

    result, on_disk = _run_budget_latch_attempt(
        tmp_path,
        monkeypatch,
        checks_non_critical=[invalid_check],
    )

    assert result["verdict"] == "error"
    assert "budget_latched" not in result
    assert "budget_latched" not in on_disk
    assert any(error["type"] == "schema_error" for error in result["errors"])


@pytest.mark.parametrize(("verdict", "count"), [("pass", 1), ("error", 0)])
def test_budget_latch_requires_error_and_positive_rejection_count(verdict: str, count: int) -> None:
    isolation = {
        "broker": {
            "metrics": {
                "budget_rejected_count": count,
                "budget_exceeded": True,
                "anomaly_reasons": ["request cost upper bound exceeds the remaining run budget"],
            }
        }
    }
    errors = [{"stage": "run", "type": "budget_exceeded", "message": "budget rejected"}]

    assert ev._is_budget_latched_run(isolation, verdict, [], errors) is False


@pytest.mark.parametrize(
    ("critical", "extra_error"),
    [
        ([{"passed": False}], None),
        ([], {"stage": "isolation_cleanup", "type": "cleanup_error", "message": "failed"}),
        ([], {"stage": "run", "type": "run_error", "message": "scenario metadata missing"}),
    ],
)
def test_budget_latch_rejects_independent_hard_failure(
    critical: list[dict], extra_error: dict | None
) -> None:
    isolation = {
        "broker": {
            "metrics": {
                "budget_rejected_count": 1,
                "budget_exceeded": True,
                "anomaly_reasons": ["request token upper bound exceeds the remaining run budget"],
            }
        }
    }
    errors = [{"stage": "run", "type": "budget_exceeded", "message": "budget rejected"}]
    if extra_error is not None:
        errors.append(extra_error)

    assert ev._is_budget_latched_run(isolation, "error", critical, errors) is False


class TestEnforceResultSchema:
    """EV-12: result.json が schema を満たさない場合、書き込み前に verdict=error へ強制する。"""

    _SCHEMA_DIR = Path("packages/meta-harness/schemas").resolve()

    def test_valid_result_is_returned_unchanged(self) -> None:
        result = _minimal_result()
        enforced = ev._enforce_result_schema(result, self._SCHEMA_DIR)
        assert enforced == result

    def test_invalid_result_is_forced_to_error_verdict(self) -> None:
        result = _minimal_result()
        result["quality_score"] = "not-a-number"  # schema 違反（number 必須）

        enforced = ev._enforce_result_schema(result, self._SCHEMA_DIR)

        assert enforced["verdict"] == "error"
        assert any(e["type"] == "schema_error" for e in enforced["errors"])

    def test_missing_required_field_is_forced_to_error_verdict(self) -> None:
        result = _minimal_result()
        del result["self_report"]

        enforced = ev._enforce_result_schema(result, self._SCHEMA_DIR)

        assert enforced["verdict"] == "error"
        assert any(e["type"] == "schema_error" for e in enforced["errors"])
