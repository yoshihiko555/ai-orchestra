"""ヘッドレス実行（`claude -p`）の結果判定テスト（Sec2-2, Sec2-5）。

PR #168 レビュー指摘（Codex P1 x2）に対応:
- `claude -p` が非ゼロ終了・is_error=true（budget 打ち切り含む）・result イベント欠落の
  場合、oracle 判定結果に関わらず run 段階のエラーとして扱われること（成果物が残っていても
  pass にしない）。
- シナリオ実行が候補ハーネス（worktree）を評価対象にするよう、`AI_ORCHESTRA_DIR` を
  worktree_dir に明示設定した env で `claude -p` を起動すること。

`claude` は一切呼ばない。runner は完全にフェイクに差し替える。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_headless_run",
    "packages/meta-harness/lib/evaluator.py",
)


def _completed(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _write_result_event(events_path: Path, event: dict) -> None:
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def _install_isolation_launch(monkeypatch, tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}\n", encoding="utf-8")
    launch = ev.siso.ScenarioIsolationLaunch(
        executable="/usr/bin/srt",
        settings_path=settings_path,
        settings={},
        env={"AI_ORCHESTRA_DIR": str(tmp_path / "worktree"), "PATH": "/usr/bin"},
        metadata={
            "backend": "srt",
            "srt_version": "1.0.0",
            "settings_sha256": "a" * 64,
            "platform_profile_input_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(ev.siso, "resolve_scenario_isolation", lambda **_kwargs: launch)
    monkeypatch.setattr(ev.siso, "cleanup_scenario_isolation", lambda _launch: None)
    monkeypatch.setattr(ev.siso, "execution_boundary_available", lambda _config: True)
    return launch


class TestCheckHeadlessRunOutcome:
    """`_check_headless_run_outcome` 単体のテスト（Codex P1: 非ゼロ終了が pass 化するバグ）。"""

    def test_success_case_does_not_raise(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_result_event(
            events_path, {"type": "result", "subtype": "success", "is_error": False}
        )
        ev._check_headless_run_outcome(_completed(0), events_path)  # 例外が出なければ OK

    def test_is_error_true_forces_error(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_result_event(
            events_path, {"type": "result", "subtype": "error_during_execution", "is_error": True}
        )
        try:
            ev._check_headless_run_outcome(_completed(1), events_path)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "run"
            assert exc.error_type == "run_error"
        else:
            raise AssertionError("is_error=true should raise EvaluatorStageError")

    def test_budget_exceeded_subtype_forces_error_with_budget_exceeded_type(
        self, tmp_path: Path
    ) -> None:
        """budget 打ち切りで成果物ファイルが残っていても pass にしない（Sec2-5）。"""
        events_path = tmp_path / "events.jsonl"
        _write_result_event(
            events_path, {"type": "result", "subtype": "error_max_budget_usd", "is_error": True}
        )
        try:
            ev._check_headless_run_outcome(_completed(1), events_path)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "run"
            assert exc.error_type == "budget_exceeded"
        else:
            raise AssertionError("error_max_budget_usd should raise EvaluatorStageError")

    def test_missing_result_event_forces_error(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        events_path.write_text("", encoding="utf-8")
        try:
            ev._check_headless_run_outcome(_completed(0), events_path)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "run"
            assert exc.error_type == "run_error"
        else:
            raise AssertionError("missing result event should raise EvaluatorStageError")

    def test_nonzero_exit_with_success_subtype_still_forces_error(self, tmp_path: Path) -> None:
        """result イベントは success を報告していても、プロセス自体が非ゼロ終了なら error。"""
        events_path = tmp_path / "events.jsonl"
        _write_result_event(
            events_path, {"type": "result", "subtype": "success", "is_error": False}
        )
        try:
            ev._check_headless_run_outcome(_completed(2), events_path)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "run"
            assert exc.error_type == "run_error"
        else:
            raise AssertionError("nonzero exit code should raise EvaluatorStageError")


class TestRunHeadlessScenarioEnvironment:
    """Codex P1: シナリオ実行が親環境の AI_ORCHESTRA_DIR を継承する問題。"""

    def test_ai_orchestra_dir_env_points_to_worktree_dir(self, tmp_path: Path, monkeypatch) -> None:
        launch = _install_isolation_launch(monkeypatch, tmp_path)
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        instruction_path = tmp_path / "self-report-instruction.md"
        instruction_path.write_text("irrelevant", encoding="utf-8")

        captured_env: dict[str, str] = {}
        captured_command: list[str] = []

        def fake_runner(cmd, **kwargs):
            captured_command.extend(cmd)
            captured_env.update(kwargs.get("env") or {})
            kwargs["stdout"].write(
                (
                    json.dumps({"type": "result", "subtype": "success", "is_error": False}) + "\n"
                ).encode()
            )
            return _completed(0)

        monkeypatch.setattr(ev.sproc, "run_bounded_process_tree", fake_runner)

        scenario = {"id": "s1", "prompt": "irrelevant"}
        ev.run_headless_scenario(
            scenario,
            {},
            worktree_dir,
            staging_dir,
            instruction_path,
            main_root=tmp_path,
            source_commit="a" * 40,
            runner=fake_runner,
        )

        assert captured_env.get("AI_ORCHESTRA_DIR") == str(worktree_dir)
        assert captured_command[:3] == [
            launch.executable,
            "--settings",
            str(launch.settings_path),
        ]
        assert "--setting-sources" in captured_command
        assert "project,local" in captured_command
        assert "--no-chrome" in captured_command
        metadata = json.loads((staging_dir / "isolation.json").read_text(encoding="utf-8"))
        assert metadata == launch.metadata

    def test_raises_when_result_event_indicates_budget_exceeded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _install_isolation_launch(monkeypatch, tmp_path)
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        instruction_path = tmp_path / "self-report-instruction.md"
        instruction_path.write_text("irrelevant", encoding="utf-8")

        def fake_runner(cmd, **kwargs):
            kwargs["stdout"].write(
                (
                    json.dumps(
                        {"type": "result", "subtype": "error_max_budget_usd", "is_error": True}
                    )
                    + "\n"
                ).encode()
            )
            return _completed(1)

        monkeypatch.setattr(ev.sproc, "run_bounded_process_tree", fake_runner)

        scenario = {"id": "s1", "prompt": "irrelevant"}
        try:
            ev.run_headless_scenario(
                scenario,
                {},
                worktree_dir,
                staging_dir,
                instruction_path,
                main_root=tmp_path,
                source_commit="a" * 40,
                runner=fake_runner,
            )
        except ev.EvaluatorStageError as exc:
            assert exc.error_type == "budget_exceeded"
        else:
            raise AssertionError(
                "budget-exceeded result event should raise EvaluatorStageError, not return"
                " a HeadlessRunResult that lets oracle checks decide pass/fail"
            )

    def test_isolation_failure_is_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        instruction_path = tmp_path / "instruction.md"
        instruction_path.write_text("irrelevant", encoding="utf-8")
        monkeypatch.setattr(
            ev.siso,
            "resolve_scenario_isolation",
            lambda **_kwargs: (_ for _ in ()).throw(
                ev.siso.ScenarioIsolationError("canary failed")
            ),
        )
        monkeypatch.setattr(ev.siso, "execution_boundary_available", lambda _config: True)

        with pytest.raises(ev.EvaluatorStageError, match="isolation unavailable") as exc_info:
            ev.run_headless_scenario(
                {"id": "s1", "prompt": "irrelevant"},
                {},
                worktree_dir,
                staging_dir,
                instruction_path,
                main_root=tmp_path,
                source_commit="a" * 40,
                runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            )

        assert exc_info.value.error_type == "run_error"

    def test_timeout_always_cleans_isolation(self, tmp_path: Path, monkeypatch) -> None:
        launch = _install_isolation_launch(monkeypatch, tmp_path)
        cleaned = []
        monkeypatch.setattr(ev.siso, "cleanup_scenario_isolation", cleaned.append)
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        instruction_path = tmp_path / "instruction.md"
        instruction_path.write_text("irrelevant", encoding="utf-8")

        def timeout_runner(cmd, **_kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(ev.sproc, "run_bounded_process_tree", timeout_runner)

        with pytest.raises(ev.EvaluatorStageError) as exc_info:
            ev.run_headless_scenario(
                {"id": "s1", "prompt": "irrelevant", "timeout_ms": 1000},
                {},
                worktree_dir,
                staging_dir,
                instruction_path,
                main_root=tmp_path,
                source_commit="a" * 40,
                runner=timeout_runner,
            )

        assert exc_info.value.error_type == "timeout"
        assert cleaned == [launch]
