"""tmux-monitor hooks のユニットテスト。"""

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

tmux_format = load_module(
    "tmux_format_output_test", "packages/tmux-monitor/hooks/tmux-format-output.py"
)
tmux_pre_task = load_module("tmux_pre_task_test", "packages/tmux-monitor/hooks/tmux-pre-task.py")
tmux_session_end = load_module(
    "tmux_session_end_test", "packages/tmux-monitor/hooks/tmux-session-end.py"
)
tmux_session_start = load_module(
    "tmux_session_start_test", "packages/tmux-monitor/hooks/tmux-session-start.py"
)
tmux_subagent_start = load_module(
    "tmux_subagent_start_test", "packages/tmux-monitor/hooks/tmux-subagent-start.py"
)
tmux_subagent_stop = load_module(
    "tmux_subagent_stop_test", "packages/tmux-monitor/hooks/tmux-subagent-stop.py"
)


class TestTmuxFormatOutput:
    """tmux-format-output.py のテスト。"""

    def test_format_tool_input_prefers_known_keys(self) -> None:
        """command / pattern / file_path を優先して表示する。"""
        assert tmux_format.format_tool_input({"command": "pytest -q"}) == "pytest -q"
        assert tmux_format.format_tool_input({"pattern": "TODO"}) == "TODO"
        assert tmux_format.format_tool_input({"file_path": "src/app.py"}) == "src/app.py"

    def test_main_formats_supported_record_types(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """assistant / user / progress を順に整形表示する。"""
        lines = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "hello"},
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": "pytest -q"},
                                },
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {"content": [{"type": "tool_result", "content": "done"}]},
                    }
                ),
                json.dumps(
                    {"type": "progress", "data": {"type": "bash_progress", "content": "running"}}
                ),
            ]
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(lines))

        tmux_format.main()

        captured = capsys.readouterr()
        assert "hello" in captured.out
        assert "[Bash]" in captured.out
        assert "done" in captured.out
        assert "running" in captured.out


class TestTmuxPreTask:
    """tmux-pre-task.py のテスト。"""

    def test_main_appends_description_to_queue(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """有効時は description をキューに追記する。"""
        queue_dir = tmp_path / "session-info"
        monkeypatch.setattr(tmux_pre_task, "SESSION_INFO_DIR", str(queue_dir))
        monkeypatch.setattr(tmux_pre_task, "is_tmux_monitoring_enabled", lambda _: True)
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "cwd": str(tmp_path),
                        "session_id": "sess-1",
                        "tool_input": {"description": "Fix flaky tests"},
                    }
                )
            ),
        )

        with pytest.raises(SystemExit, match="0"):
            tmux_pre_task.main()

        queue_file = queue_dir / "sess-1.task-queue"
        lines = queue_file.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0]) == {"description": "Fix flaky tests"}


class TestTmuxSessionEnd:
    """tmux-session-end.py のテスト。"""

    def test_main_removes_related_files_for_same_pid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """同一 PID に紐づく session info と共有ストアを削除する。"""
        info_dir = tmp_path / "session-info"
        shared_a = tmp_path / "shared-a"
        shared_b = tmp_path / "shared-b"
        info_dir.mkdir()
        shared_a.mkdir()
        shared_b.mkdir()
        lock_a = tmp_path / "lock-a"
        lock_b = tmp_path / "lock-b"
        lock_a.mkdir()
        lock_b.mkdir()

        def write(name: str, content: str) -> None:
            (info_dir / name).write_text(content, encoding="utf-8")

        write("sess-1.pid", "123")
        write("sess-1.lock-path", str(lock_a))
        write("sess-1.shared-dir", str(shared_a))
        write("sess-1.tmux-session", "tmux-a")
        write("sess-1.task-queue", '{"description": "x"}\n')
        write("sess-2.pid", "123")
        write("sess-2.lock-path", str(lock_b))
        write("sess-2.shared-dir", str(shared_b))
        write("sess-2.tmux-session", "tmux-b")
        write("sess-2.task-queue", '{"description": "y"}\n')

        monkeypatch.setattr(tmux_session_end, "SESSION_INFO_DIR", str(info_dir))
        monkeypatch.setattr(tmux_session_end, "is_tmux_monitoring_enabled", lambda _: True)
        monkeypatch.setattr(
            tmux_session_end,
            "read_hook_input",
            lambda: {"cwd": str(tmp_path), "session_id": "sess-1"},
        )

        tmux_session_end.main()

        for suffix in (".pid", ".lock-path", ".shared-dir", ".tmux-session", ".task-queue"):
            assert not (info_dir / f"sess-1{suffix}").exists()
            assert not (info_dir / f"sess-2{suffix}").exists()
        assert not lock_a.exists()
        assert not lock_b.exists()
        assert not shared_a.exists()
        assert not shared_b.exists()


class TestTmuxSessionStart:
    """tmux-session-start.py のテスト。"""

    def test_main_creates_session_files_and_shared_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """新規 tmux セッションと共有ストアを初期化する。"""
        info_dir = tmp_path / "session-info"
        shared_prefix = str(tmp_path / "shared-")
        calls: list[tuple[str, ...]] = []

        def fake_run_tmux(*args: str) -> SimpleNamespace:
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(tmux_session_start, "SESSION_INFO_DIR", str(info_dir))
        monkeypatch.setattr(tmux_session_start, "SHARED_STORE_PREFIX", shared_prefix)
        monkeypatch.setattr(tmux_session_start, "is_tmux_monitoring_enabled", lambda _: True)
        monkeypatch.setattr(
            tmux_session_start,
            "read_hook_input",
            lambda: {"cwd": str(tmp_path / "proj"), "session_id": "abcdef123456"},
        )
        monkeypatch.setattr(tmux_session_start, "find_claude_pid", lambda: 4321)
        monkeypatch.setattr(tmux_session_start, "cleanup_orphaned_sessions", lambda _: None)
        monkeypatch.setattr(tmux_session_start, "tmux_has_session", lambda _: False)
        monkeypatch.setattr(tmux_session_start, "run_tmux", fake_run_tmux)

        tmux_session_start.main()

        assert (info_dir / "abcdef123456.tmux-session").read_text(
            encoding="utf-8"
        ) == "claude-proj-4321"
        assert (info_dir / "abcdef123456.pid").read_text(encoding="utf-8") == "4321"
        shared_dir = Path(f"{shared_prefix}4321")
        meta = json.loads((shared_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["session_key"] == "4321"
        assert meta["project"] == "proj"
        assert any(call[:3] == ("new-session", "-d", "-s") for call in calls)


class TestTmuxSubagentStart:
    """tmux-subagent-start.py のテスト。"""

    def test_pop_task_description_reads_fifo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """キューの先頭 description を返し、残りを保持する。"""
        info_dir = tmp_path / "session-info"
        info_dir.mkdir()
        queue_file = info_dir / "sess-1.task-queue"
        queue_file.write_text(
            "\n".join(
                [
                    json.dumps({"description": "first"}),
                    json.dumps({"description": "second"}),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(tmux_subagent_start, "SESSION_INFO_DIR", str(info_dir))

        description = tmux_subagent_start.pop_task_description("sess-1")

        assert description == "first"
        remaining = queue_file.read_text(encoding="utf-8").splitlines()
        assert json.loads(remaining[0]) == {"description": "second"}

    def test_main_fallback_creates_pane_info(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """SessionStart 情報がなくても fallback で pane 情報を保存する。"""
        info_dir = tmp_path / "session-info"
        info_dir.mkdir()
        calls: list[tuple[str, ...]] = []

        def fake_run_tmux(*args: str) -> SimpleNamespace:
            calls.append(args)
            if args[:4] == ("display-message", "-t", "claude-proj-sess123", "-p"):
                return SimpleNamespace(returncode=0, stdout="%1\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(tmux_subagent_start, "SESSION_INFO_DIR", str(info_dir))
        monkeypatch.setattr(tmux_subagent_start, "is_tmux_monitoring_enabled", lambda _: True)
        monkeypatch.setattr(
            tmux_subagent_start,
            "read_hook_input",
            lambda: {
                "cwd": str(tmp_path / "proj"),
                "agent_id": "agent1234567",
                "agent_type": "tester",
                "session_id": "sess123456",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
            },
        )
        monkeypatch.setattr(tmux_subagent_start, "find_claude_pid", lambda: None)
        monkeypatch.setattr(tmux_subagent_start, "tmux_has_session", lambda _: False)
        monkeypatch.setattr(tmux_subagent_start, "run_tmux", fake_run_tmux)
        monkeypatch.setattr(tmux_subagent_start, "pop_task_description", lambda _: "Run tests")
        monkeypatch.setattr(tmux_subagent_start.os.path, "isfile", lambda _: False)
        monkeypatch.setattr(tmux_subagent_start.os, "access", lambda *_: False)

        tmux_subagent_start.main()

        pane_info = (info_dir / "sess123456.pane-agent1234567").read_text(encoding="utf-8")
        assert pane_info.splitlines() == ["claude-proj-sess123", "%1"]
        assert any(call[:3] == ("new-session", "-d", "-s") for call in calls)


class TestTmuxSubagentStop:
    """tmux-subagent-stop.py のテスト。"""

    def test_read_pane_info_supports_both_formats(self, tmp_path: Path) -> None:
        """新旧の pane info 形式を読み分ける。"""
        new_format = tmp_path / "new"
        old_format = tmp_path / "old"
        new_format.write_text("tmux-session\n%3\n", encoding="utf-8")
        old_format.write_text("tmux-session\n", encoding="utf-8")

        assert tmux_subagent_stop.read_pane_info(str(new_format)) == ("tmux-session", "%3")
        assert tmux_subagent_stop.read_pane_info(str(old_format)) == ("tmux-session", "")

    def test_main_marks_pane_done_and_cleans_info_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """pane を DONE 表示に更新し、pane info を削除する。"""
        info_dir = tmp_path / "session-info"
        info_dir.mkdir()
        pane_info_file = info_dir / "sess-1.pane-agent1234567"
        pane_info_file.write_text("tmux-sess\n%3\n", encoding="utf-8")
        calls: list[tuple[str, ...]] = []

        def fake_run_tmux(*args: str) -> SimpleNamespace:
            calls.append(args)
            if args[:4] == ("list-panes", "-t", "tmux-sess", "-F"):
                return SimpleNamespace(returncode=0, stdout="%3\n", stderr="")
            if args[:4] == ("display-message", "-t", "%3", "-p"):
                return SimpleNamespace(returncode=0, stdout="tester:agent12\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(tmux_subagent_stop, "SESSION_INFO_DIR", str(info_dir))
        monkeypatch.setattr(tmux_subagent_stop, "is_tmux_monitoring_enabled", lambda _: True)
        monkeypatch.setattr(
            tmux_subagent_stop,
            "read_hook_input",
            lambda: {"cwd": str(tmp_path), "agent_id": "agent1234567", "session_id": "sess-1"},
        )
        monkeypatch.setattr(tmux_subagent_stop, "tmux_has_session", lambda _: True)
        monkeypatch.setattr(tmux_subagent_stop, "run_tmux", fake_run_tmux)

        tmux_subagent_stop.main()

        assert not pane_info_file.exists()
        assert ("select-pane", "-t", "%3", "-T", "DONE: tester:agent12") in calls
        assert ("set-option", "-t", "%3", "pane-border-style", "fg=green") in calls
