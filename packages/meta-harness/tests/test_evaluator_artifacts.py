"""成果物移送のテスト（EV-09, EV-10, EV-11, EV-12, EV-19, EV-24, Sec2-1 手順8, Sec2-6, Sec3-6）。"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

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


def _minimal_result(run_id: str = "run-20260707-120000-slug-scn-a1-abcd") -> dict:
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

    def test_result_json_records_claude_version(self, tmp_path: Path, monkeypatch) -> None:
        """EV-24: result.json に claude_version フィールドが必須として記録される。"""
        result, main_root = self._run(tmp_path, monkeypatch, holdout=False)
        run_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        on_disk = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert on_disk["claude_version"] == "2.1.202"


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
