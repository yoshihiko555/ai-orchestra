"""quality-gates hooks のユニットテスト。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

core_hooks_dir = str(REPO_ROOT / "packages" / "core" / "hooks")
if core_hooks_dir not in sys.path:
    sys.path.insert(0, core_hooks_dir)

post_impl_review = load_module(
    "post_impl_review_test", "packages/quality-gates/hooks/post-implementation-review.py"
)
post_test_analysis = load_module(
    "post_test_analysis_test", "packages/quality-gates/hooks/post-test-analysis.py"
)
test_gate_checker = load_module(
    "test_gate_checker_test", "packages/quality-gates/hooks/test-gate-checker.py"
)


def _make_stdin(data: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin を JSON 入力で置き換える。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))


class TestPostImplementationReview:
    """post-implementation-review.py のテスト。"""

    def test_main_skips_non_code_extension(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """コード拡張子以外は state を作らない。"""
        _make_stdin(
            {
                "tool_name": "Write",
                "cwd": str(tmp_path),
                "tool_input": {"file_path": "notes.md", "content": "memo"},
            },
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            post_impl_review.main()

        state_file = tmp_path / ".claude" / "state" / post_impl_review.STATE_FILENAME
        assert not state_file.exists()


class TestPostTestAnalysis:
    """post-test-analysis.py のテスト。"""

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
        assert "< /dev/null" in command

    def test_main_successful_exit_with_error_text_does_not_block_quality_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """exit code 0 なら quality gate は block しない。"""
        monkeypatch.setattr(
            post_test_analysis,
            "load_quality_gate_config",
            lambda _project_dir, config=None: {"enabled": True, "block_on_failed_test": True},
        )
        monkeypatch.setattr(
            post_test_analysis, "resolve_project_root_from_hook_data", lambda data: data["cwd"]
        )
        monkeypatch.setattr(
            post_test_analysis, "load_trace_state", lambda **_kwargs: {"tid": "tid-1"}
        )
        monkeypatch.setattr(post_test_analysis, "emit_event", lambda *_args, **_kwargs: None)
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
                "tool_response": {
                    "exit_code": 0,
                    "stdout": "ValueError: expected output marker",
                },
            },
            monkeypatch,
        )

        with pytest.raises(SystemExit, match="0"):
            post_test_analysis.main()

        captured = capsys.readouterr()
        assert "[quality-gates] quality gate blocked" not in captured.err

    def test_main_blocks_when_quality_gate_requires_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """block_on_failed_test=true のときは exit 2 で止める。"""
        monkeypatch.setattr(
            post_test_analysis, "emit_quality_gate_event", lambda *_args, **_kwargs: True
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

        with pytest.raises(SystemExit, match="2"):
            post_test_analysis.main()

        captured = capsys.readouterr()
        assert "[quality-gates] quality gate blocked" in captured.err


class TestTestGateChecker:
    """test-gate-checker.py のテスト。"""

    def test_load_thresholds_reads_quality_gate_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """設定ファイルから閾値を読む。"""
        monkeypatch.setattr(
            test_gate_checker,
            "load_quality_gates_config",
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
        """閾値到達時にテスト実行を促す。

        main() は quality-gates.json を一度だけ読んで enabled 判定と閾値取得を
        兼ねる（Issue #154: 重複読み込み解消）ため、load_quality_gates_config を
        monkeypatch して両方を一括で差し替える。
        """
        monkeypatch.setattr(
            test_gate_checker,
            "load_quality_gates_config",
            lambda *args: {
                "features": {
                    "quality_gate": {
                        "enabled": True,
                        "test_file_threshold": 1,
                        "test_line_threshold": 100,
                    }
                }
            },
        )
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
        state = test_gate_checker.load_test_gate_state(str(tmp_path))
        assert state["warned"] is True
        assert state["files_modified_since_test"] == ["src/main.py"]
