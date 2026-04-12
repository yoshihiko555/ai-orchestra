"""quality-gates hooks のユニットテスト。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.module_loader import REPO_ROOT, load_module

core_hooks_dir = str(REPO_ROOT / "packages" / "core" / "hooks")
if core_hooks_dir not in sys.path:
    sys.path.insert(0, core_hooks_dir)

lint_on_save = load_module("lint_on_save_test", "packages/quality-gates/hooks/lint-on-save.py")
post_impl_review = load_module(
    "post_impl_review_test", "packages/quality-gates/hooks/post-implementation-review.py"
)
post_test_analysis = load_module(
    "post_test_analysis_test", "packages/quality-gates/hooks/post-test-analysis.py"
)
test_gate_checker = load_module(
    "test_gate_checker_test", "packages/quality-gates/hooks/test-gate-checker.py"
)
test_tampering_detector = load_module(
    "test_tampering_detector_test", "packages/quality-gates/hooks/test-tampering-detector.py"
)


def _make_stdin(data: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin を JSON 入力で置き換える。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))


class TestLintOnSave:
    """lint-on-save.py のテスト。"""

    def test_run_lint_commands_uses_fallback_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """先頭コマンドがない場合はフォールバックを試す。"""
        file_path = str(tmp_path / "pkg" / "sample.py")
        Path(file_path).parent.mkdir(parents=True)
        calls: list[tuple[list[str], str]] = []

        def fake_run(cmd: list[str], **kwargs) -> SimpleNamespace:
            calls.append((cmd, kwargs["cwd"]))
            if cmd[0] == "uv":
                raise FileNotFoundError
            return SimpleNamespace(returncode=0, stdout="fixed", stderr="")

        monkeypatch.setattr(lint_on_save.subprocess, "run", fake_run)

        results = lint_on_save.run_lint_commands(file_path)

        assert [call[0][0] for call in calls] == ["uv", "ruff", "uv", "ruff"]
        assert all(call[1] == str(Path(file_path).parent) for call in calls)
        assert results == [
            {"name": "ruff format", "success": True, "output": "fixed"},
            {"name": "ruff check", "success": True, "output": "fixed"},
        ]

    def test_main_outputs_lint_summary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """lint 結果があれば JSON 出力する。"""
        monkeypatch.setattr(
            lint_on_save,
            "run_lint_commands",
            lambda _: [
                {"name": "ruff format", "success": True, "output": "1 file reformatted"},
                {"name": "ruff check", "success": False, "output": "line too long"},
            ],
        )
        _make_stdin(
            {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/example.py"}},
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            lint_on_save.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "[Lint Issues found]" in output["hookSpecificOutput"]["additionalContext"]
        assert "ruff format" in output["hookSpecificOutput"]["additionalContext"]
        assert "ruff check" in output["hookSpecificOutput"]["additionalContext"]

    def test_main_ignores_non_python_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Python 以外のファイルは処理しない。"""
        _make_stdin(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/example.md"}},
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            lint_on_save.main()


class TestPostImplementationReview:
    """post-implementation-review.py のテスト。"""

    def test_should_suggest_review_false_when_already_suggested(self) -> None:
        """一度提案済みなら再提案しない。"""
        state = {"files": ["a.py", "b.py", "c.py"], "total_lines": 120, "review_suggested": True}
        assert post_impl_review.should_suggest_review(state) is False

    def test_main_suggests_review_when_file_threshold_reached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """3 ファイル目の変更でレビュー提案を出す。"""
        state_file = tmp_path / "impl-review.json"
        monkeypatch.setattr(post_impl_review, "STATE_FILE", state_file)
        post_impl_review.save_state(
            {"files": ["a.py", "b.py"], "total_lines": 20, "review_suggested": False}
        )
        _make_stdin(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "c.py", "content": "print(1)\nprint(2)\n"},
            },
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            post_impl_review.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "[Review Suggestion]" in output["hookSpecificOutput"]["additionalContext"]
        state = post_impl_review.load_state()
        assert state["review_suggested"] is True
        assert state["files"][-1] == "c.py"

    def test_main_skips_non_code_extension(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """コード拡張子以外は state を作らない。"""
        state_file = tmp_path / "impl-review.json"
        monkeypatch.setattr(post_impl_review, "STATE_FILE", state_file)
        _make_stdin(
            {"tool_name": "Write", "tool_input": {"file_path": "notes.md", "content": "memo"}},
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            post_impl_review.main()

        assert not state_file.exists()


class TestPostTestAnalysis:
    """post-test-analysis.py のテスト。"""

    def test_record_test_result_resets_counters_on_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """成功時は test gate カウンタをリセットする。"""
        state_file = tmp_path / "test-gate.json"
        monkeypatch.setattr(post_test_analysis, "TEST_GATE_STATE_FILE", state_file)
        post_test_analysis.save_test_gate_state(
            {
                "files_modified_since_test": ["a.py"],
                "lines_modified_since_test": 42,
                "last_test_result": None,
                "warned": True,
            }
        )

        post_test_analysis.record_test_result("pytest", passed=True)

        state = post_test_analysis.load_test_gate_state()
        assert state["files_modified_since_test"] == []
        assert state["lines_modified_since_test"] == 0
        assert state["warned"] is False
        assert state["last_test_result"]["passed"] is True

    def test_build_codex_command_uses_loaded_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cli-tools 設定から Codex コマンドを組み立てる。"""
        monkeypatch.setattr(
            post_test_analysis,
            "load_package_config",
            lambda *args: {
                "codex": {
                    "model": "gpt-test",
                    "sandbox": {"analysis": "workspace-write"},
                    "flags": "--dangerously-fast",
                }
            },
        )

        command = post_test_analysis._build_codex_command({"cwd": "/project"})

        assert "gpt-test" in command
        assert "workspace-write" in command
        assert "--dangerously-fast" in command

    def test_main_outputs_debug_suggestion_for_failed_test(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """失敗したテストコマンドでは Codex 提案を出す。"""
        state_file = tmp_path / "test-gate.json"
        monkeypatch.setattr(post_test_analysis, "TEST_GATE_STATE_FILE", state_file)
        monkeypatch.setattr(
            post_test_analysis,
            "load_package_config",
            lambda *args: {"codex": {"model": "gpt-test", "sandbox": {"analysis": "read-only"}}},
        )
        _make_stdin(
            {
                "tool_name": "Bash",
                "cwd": str(tmp_path),
                "tool_input": {"command": "pytest -q"},
                "tool_response": {"exit_code": 1, "stdout": "FAILED test_example.py::test_case"},
            },
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            post_test_analysis.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "[Codex Debug Suggestion]" in output["hookSpecificOutput"]["additionalContext"]
        state = post_test_analysis.load_test_gate_state()
        assert state["last_test_result"]["passed"] is False
        assert state["last_test_result"]["command"] == "pytest -q"

    def test_main_ignores_non_test_bash_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """テストコマンド以外の Bash は無視する。"""
        _make_stdin(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
                "tool_response": {"exit_code": 0, "stdout": "hello"},
            },
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            post_test_analysis.main()


class TestTestGateChecker:
    """test-gate-checker.py のテスト。"""

    def test_load_thresholds_reads_quality_gate_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """設定ファイルから閾値を読む。"""
        monkeypatch.setattr(
            test_gate_checker,
            "load_package_config",
            lambda *args: {
                "features": {
                    "quality_gate": {
                        "test_file_threshold": 5,
                        "test_line_threshold": 250,
                    }
                }
            },
        )

        assert test_gate_checker.load_thresholds("/project") == (5, 250)

    def test_main_warns_when_threshold_reached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """閾値到達時にテスト実行を促す。"""
        state_file = tmp_path / "test-gate.json"
        monkeypatch.setattr(test_gate_checker, "TEST_GATE_STATE_FILE", state_file)
        monkeypatch.setattr(test_gate_checker, "is_quality_gate_enabled", lambda _: True)
        monkeypatch.setattr(test_gate_checker, "load_thresholds", lambda _: (1, 100))
        _make_stdin(
            {
                "tool_name": "Edit",
                "cwd": str(tmp_path),
                "tool_input": {"file_path": "src/main.py", "content": "print(1)\nprint(2)\n"},
            },
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            test_gate_checker.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "[Test Gate]" in output["hookSpecificOutput"]["additionalContext"]
        state = test_gate_checker.load_test_gate_state()
        assert state["warned"] is True
        assert state["files_modified_since_test"] == ["src/main.py"]

    def test_main_skips_when_quality_gate_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """quality gate 無効時は state を更新しない。"""
        state_file = tmp_path / "test-gate.json"
        monkeypatch.setattr(test_gate_checker, "TEST_GATE_STATE_FILE", state_file)
        monkeypatch.setattr(test_gate_checker, "is_quality_gate_enabled", lambda _: False)
        _make_stdin(
            {
                "tool_name": "Write",
                "cwd": str(tmp_path),
                "tool_input": {"file_path": "src/main.py", "content": "print(1)\n"},
            },
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            test_gate_checker.main()

        assert not state_file.exists()


class TestTestTamperingDetector:
    """test-tampering-detector.py のテスト。"""

    def test_main_warns_when_skip_marker_is_added(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """skip マーカー追加時に警告を出す。"""
        monkeypatch.setattr(
            test_tampering_detector,
            "collect_tampering_findings",
            lambda _data: [
                {
                    "type": "pattern",
                    "file_path": "tests/test_auth.py",
                    "label": "@pytest.mark.skip / @unittest.skip",
                    "snippet": "@pytest.mark.skip",
                }
            ],
        )
        _make_stdin(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "tests/test_auth.py", "content": "@pytest.mark.skip"},
            },
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            test_tampering_detector.main()

        output = json.loads(capsys.readouterr().out)
        assert "[Warning]" in output["hookSpecificOutput"]["additionalContext"]
        assert "@pytest.mark.skip" in output["hookSpecificOutput"]["additionalContext"]

    def test_collect_findings_for_delete_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """削除コマンド時は deleted test file を報告する。"""
        monkeypatch.setattr(
            test_tampering_detector,
            "get_deleted_test_files",
            lambda _project_dir, _delete_targets: ["tests/x.py"],
        )

        findings = test_tampering_detector.collect_tampering_findings(
            {
                "tool_name": "Bash",
                "cwd": "/project",
                "tool_input": {"command": "rm tests/x.py"},
            }
        )

        assert findings == [
            {
                "type": "deleted_test_file",
                "file_path": "tests/x.py",
                "label": "deleted test file",
                "snippet": "",
            }
        ]

    def test_non_test_file_does_not_warn_for_type_ignore(self) -> None:
        """通常コードの suppression だけでは警告しない。"""
        findings = test_tampering_detector.scan_added_lines(
            "src/main.py",
            ["# type: ignore[attr-defined]"],
        )

        assert findings == []

    def test_extract_delete_targets_supports_bash_wrapper(self) -> None:
        """bash -lc 経由の git rm からも削除ターゲットを取れる。"""
        targets = test_tampering_detector.extract_delete_targets(
            "Bash",
            {"command": 'bash -lc "git rm tests/*.py"'},
            "/project",
        )

        assert targets == ["tests/*.py"]

    def test_extract_delete_targets_collects_multiple_delete_commands(self) -> None:
        """複数の rm もすべて拾う。"""
        targets = test_tampering_detector.extract_delete_targets(
            "Bash",
            {"command": "rm tests/a.py && rm tests/b.py"},
            "/project",
        )

        assert targets == ["tests/a.py", "tests/b.py"]

    def test_extract_delete_targets_collects_semicolon_separated_commands(self) -> None:
        """セミコロン区切りの rm も拾う。"""
        targets = test_tampering_detector.extract_delete_targets(
            "Bash",
            {"command": "rm tests/a.py; rm tests/b.py"},
            "/project",
        )

        assert targets == ["tests/a.py", "tests/b.py"]
