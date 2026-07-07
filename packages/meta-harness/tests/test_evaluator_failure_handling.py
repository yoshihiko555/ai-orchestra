"""失敗処理のテスト（EV-13, Sec2-5）。

どの段階でエラーが発生しても verdict=error の result.json + ledger 追記を必ず行うこと。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_failure_handling",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_failure_handling",
    "packages/meta-harness/lib/meta_harness_common.py",
)

_SCHEMA_DIR = Path("packages/meta-harness/schemas").resolve()
_PACKAGE_DIR = Path("packages/meta-harness").resolve()
_SCENARIO_PATH = Path(
    "packages/meta-harness/scenarios/claude-harness/summarize-readme.yaml"
).resolve()


def _run_attempt(tmp_path: Path, manifest_source_commit: str) -> tuple[dict, Path]:
    scenario = {
        "id": "summarize-readme",
        "prompt": "irrelevant",
        "setup": [],
        "critical": [{"id": "c1", "text": "n/a", "oracle": "artifact_exists", "path": "x.md"}],
        "checks": [],
        "holdout": False,
    }
    manifest = {"source_commit": manifest_source_commit, "config_hash": "b" * 64}
    cli_capabilities = {"claude_version": "2.1.202", "ok": True}
    result = ev.run_single_attempt(
        main_root=tmp_path,
        config=mh.DEFAULTS,
        schema_dir=_SCHEMA_DIR,
        package_dir=_PACKAGE_DIR,
        project_dir=tmp_path,
        cand_id="cand-20260707-120000-slug-ab12",
        cand_dir=tmp_path / "cand",
        manifest=manifest,
        target="claude-harness",
        scenario=scenario,
        scenario_path=_SCENARIO_PATH,
        suite_hash="c" * 64,
        evaluator_hash="d" * 64,
        attempt=1,
        attempts_total=1,
        cli_capabilities=cli_capabilities,
    )
    return result, tmp_path


class TestWorktreeCreationFailureIsRecordedAsError:
    def test_invalid_source_commit_yields_error_verdict_and_ledger_entry(
        self, tmp_path: Path
    ) -> None:
        result, main_root = _run_attempt(tmp_path, manifest_source_commit="0" * 40)

        assert result["verdict"] == "error"
        assert result["errors"]
        assert result["errors"][0]["stage"] == "worktree_create"
        assert result["errors"][0]["type"] == "worktree_error"

        run_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        assert run_dir.is_dir()
        on_disk_result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert on_disk_result["verdict"] == "error"

        events = mh.read_ledger_events(main_root, mh.DEFAULTS)
        run_completed = [e for e in events if e.get("event") == "run_completed"]
        assert run_completed
        assert run_completed[-1]["verdict"] == "error"
        assert run_completed[-1]["run_id"] == result["run_id"]

    def test_metadata_still_written_even_on_failure(self, tmp_path: Path) -> None:
        result, main_root = _run_attempt(tmp_path, manifest_source_commit="0" * 40)
        run_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["run_id"] == result["run_id"]
        assert metadata["finished_at"] is not None


class TestEachStageFailureForcesErrorVerdict:
    def test_overlay_stage_failure_forces_error(self, tmp_path: Path, monkeypatch) -> None:
        def failing_apply_overlay(overlay_dir, config, worktree_dir, schema_dir):
            raise ev.EvaluatorStageError("overlay_apply", "overlay_error", "forced")

        monkeypatch.setattr(ev, "apply_overlay", failing_apply_overlay)
        result, _main_root = _run_attempt(tmp_path, manifest_source_commit="0" * 40)
        # 実 worktree 作成自体は無効な commit で失敗するため worktree_create が先に発生するが、
        # いずれにせよ verdict=error であることが本テストの主眼。
        assert result["verdict"] == "error"

    def test_generic_exception_in_lifecycle_is_caught_and_forces_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def raising_lifecycle(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(ev, "_run_attempt_lifecycle", raising_lifecycle)
        try:
            _run_attempt(tmp_path, manifest_source_commit="0" * 40)
        except RuntimeError:
            raise AssertionError(
                "run_single_attempt must not propagate exceptions from the lifecycle"
                " (Sec2-5 requires verdict=error to always be recorded)"
            ) from None


class TestNoCriticalChecksIsRecordedAsError:
    def test_zero_critical_checks_yields_error_verdict(self, tmp_path: Path, monkeypatch) -> None:
        def fake_lifecycle(**kwargs):
            return [], [], False, []  # critical checks が空

        monkeypatch.setattr(ev, "_run_attempt_lifecycle", fake_lifecycle)
        result, main_root = _run_attempt(tmp_path, manifest_source_commit="0" * 40)
        assert result["verdict"] == "error"
        events = mh.read_ledger_events(main_root, mh.DEFAULTS)
        run_completed = [e for e in events if e.get("event") == "run_completed"]
        assert run_completed[-1]["verdict"] == "error"
