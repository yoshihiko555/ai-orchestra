from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

from tests.module_loader import REPO_ROOT, load_module

os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)
core_hooks = REPO_ROOT / "packages" / "core" / "hooks"
if str(core_hooks) not in sys.path:
    sys.path.insert(0, str(core_hooks))

tmux_common = load_module("tmux_common", "packages/tmux-monitor/hooks/tmux_common.py")
tmux_format_output = load_module(
    "tmux_format_output", "packages/tmux-monitor/hooks/tmux-format-output.py"
)
tmux_session_start = load_module(
    "tmux_session_start", "packages/tmux-monitor/hooks/tmux-session-start.py"
)
tmux_subagent_start = load_module(
    "tmux_subagent_start", "packages/tmux-monitor/hooks/tmux-subagent-start.py"
)


def test_run_tmux_invokes_subprocess_with_expected_args(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, capture_output, text, timeout=None):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tmux_common.subprocess, "run", fake_run)

    tmux_common.run_tmux("has-session", "-t", "abc")

    assert captured["cmd"] == ["tmux", "has-session", "-t", "abc"]


def test_run_tmux_returns_failure_on_timeout(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(tmux_common.subprocess, "run", fake_run)

    result = tmux_common.run_tmux("has-session", "-t", "x")

    assert result.returncode != 0


def test_tmux_has_session_returns_true_on_success(monkeypatch) -> None:
    monkeypatch.setattr(
        tmux_common,
        "run_tmux",
        lambda *args: SimpleNamespace(returncode=0),
    )
    assert tmux_common.tmux_has_session("session-a")


def test_tmux_has_session_returns_false_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        tmux_common,
        "run_tmux",
        lambda *args: SimpleNamespace(returncode=1),
    )
    assert not tmux_common.tmux_has_session("session-a")


def test_is_tmux_monitoring_enabled_depends_on_tmux_binary(monkeypatch) -> None:
    monkeypatch.setattr(tmux_common.shutil, "which", lambda name: "/usr/bin/tmux")
    assert tmux_common.is_tmux_monitoring_enabled(".")

    monkeypatch.setattr(tmux_common.shutil, "which", lambda name: None)
    assert not tmux_common.is_tmux_monitoring_enabled(".")


def test_find_claude_pid_finds_parent_process(monkeypatch) -> None:
    monkeypatch.setattr(tmux_common.os, "getppid", lambda: 200)

    def fake_run(cmd, capture_output, text, timeout=None):
        if cmd == ["ps", "-o", "comm=", "-p", "200"]:
            return SimpleNamespace(stdout="zsh\n")
        if cmd == ["ps", "-o", "ppid=", "-p", "200"]:
            return SimpleNamespace(stdout="150\n")
        if cmd == ["ps", "-o", "comm=", "-p", "150"]:
            return SimpleNamespace(stdout="claude\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(tmux_common.subprocess, "run", fake_run)

    assert tmux_common.find_claude_pid() == 150


def test_find_claude_pid_returns_none_on_os_error(monkeypatch) -> None:
    monkeypatch.setattr(tmux_common.os, "getppid", lambda: 200)

    def fake_run(*args, **kwargs):
        raise OSError("ps unavailable")

    monkeypatch.setattr(tmux_common.subprocess, "run", fake_run)
    assert tmux_common.find_claude_pid() is None


def test_shell_quote_escapes_single_quote() -> None:
    assert tmux_common.shell_quote("a'b") == "'a'\\''b'"


def test_shell_quote_handles_empty_string() -> None:
    assert tmux_common.shell_quote("") == "''"


def test_shell_quote_wraps_normal_string() -> None:
    assert tmux_common.shell_quote("abc") == "'abc'"


def test_build_wait_cmd_escapes_project_name_and_keeps_date() -> None:
    wait_cmd = tmux_session_start.build_wait_cmd("project'; touch /tmp/x; echo '", "abc1234")

    assert "echo '(project'\\''; touch /tmp/x; echo '\\'' / PID:abc1234)'" in wait_cmd
    assert "$(date)" in wait_cmd


def test_cleanup_orphaned_sessions_keeps_alive_pid_session(monkeypatch, tmp_path) -> None:
    calls = _run_orphan_cleanup(monkeypatch, tmp_path, "123", pid_tracked=False, pid_alive=True)

    assert ("kill-session", "-t", "claude-project-123") not in calls


def test_cleanup_orphaned_sessions_kills_dead_pid_session(monkeypatch, tmp_path) -> None:
    calls = _run_orphan_cleanup(monkeypatch, tmp_path, "123", pid_tracked=False, pid_alive=False)

    assert ("kill-session", "-t", "claude-project-123") in calls


def test_cleanup_orphaned_sessions_keeps_tracked_fallback_session(monkeypatch, tmp_path) -> None:
    calls = _run_orphan_cleanup(monkeypatch, tmp_path, "abc123f", pid_tracked=True, pid_alive=False)

    assert ("kill-session", "-t", "claude-project-abc123f") not in calls


def test_cleanup_orphaned_sessions_kills_untracked_fallback_session(monkeypatch, tmp_path) -> None:
    calls = _run_orphan_cleanup(
        monkeypatch, tmp_path, "abc123f", pid_tracked=False, pid_alive=False
    )

    assert ("kill-session", "-t", "claude-project-abc123f") in calls


def test_cleanup_orphaned_sessions_removes_stale_fallback_files(monkeypatch, tmp_path) -> None:
    session_info_dir = tmp_path / "session-info"
    session_info_dir.mkdir()
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "entry.json").write_text("{}")

    session_id = "session"
    session_key = "abc123f"
    contents = {
        ".tmux-session": f"claude-project-{session_key}",
        ".lock-path": "/tmp/lock",
        ".pid": session_key,
        ".shared-dir": str(shared_dir),
        ".task-queue": "task",
    }
    for extension, content in contents.items():
        (session_info_dir / f"{session_id}{extension}").write_text(content)

    monkeypatch.setattr(tmux_session_start, "SESSION_INFO_DIR", str(session_info_dir))
    monkeypatch.setattr(tmux_common, "SESSION_INFO_DIR", str(session_info_dir))
    monkeypatch.setattr(
        tmux_session_start,
        "run_tmux",
        lambda *args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(tmux_session_start, "tmux_has_session", lambda name: False)

    tmux_session_start.cleanup_orphaned_sessions("project")

    assert not shared_dir.exists()
    for extension in tmux_common.SESSION_FILE_EXTENSIONS:
        assert not (session_info_dir / f"{session_id}{extension}").exists()


def test_cleanup_orphaned_sessions_does_not_delete_other_project_fallback_files(
    monkeypatch, tmp_path
) -> None:
    """他プロジェクト (A) のフォールバックセッションが、別プロジェクト (B) の

    起動時クリーンアップで誤って削除されないことを確認する回帰テスト。
    session info は /tmp/claude-session-info にプロジェクト横断で置かれるため、
    current project_name (B) でセッション名を再構成すると A の sid を誤って
    孤児判定してしまうバグの再発防止。
    """
    session_info_dir = tmp_path / "session-info"
    session_info_dir.mkdir()
    shared_dir_a = tmp_path / "shared-a"
    shared_dir_a.mkdir()
    (shared_dir_a / "entry.json").write_text("{}")

    session_id = "session-a"
    session_key = "aaa1111"
    contents = {
        ".tmux-session": f"claude-projectA-{session_key}",
        ".lock-path": "/tmp/lock-a",
        ".pid": session_key,
        ".shared-dir": str(shared_dir_a),
        ".task-queue": "task",
    }
    for extension, content in contents.items():
        (session_info_dir / f"{session_id}{extension}").write_text(content)

    monkeypatch.setattr(tmux_session_start, "SESSION_INFO_DIR", str(session_info_dir))
    monkeypatch.setattr(tmux_common, "SESSION_INFO_DIR", str(session_info_dir))
    monkeypatch.setattr(
        tmux_session_start,
        "run_tmux",
        lambda *args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    # A の tmux セッションは存在しない (かつ B の名前で問い合わせても当然存在しない)
    monkeypatch.setattr(tmux_session_start, "tmux_has_session", lambda name: False)

    # プロジェクト B の SessionStart から呼ばれた想定
    tmux_session_start.cleanup_orphaned_sessions("projectB")

    assert shared_dir_a.exists()
    for extension in tmux_common.SESSION_FILE_EXTENSIONS:
        assert (session_info_dir / f"{session_id}{extension}").exists()


def test_cleanup_orphaned_sessions_uses_recorded_session_name_for_own_project(
    monkeypatch, tmp_path
) -> None:
    """自プロジェクトのフォールバックセッションは、current project_name で

    再構成した名前ではなく .tmux-session に記録された実際のセッション名で
    生存判定されることを確認する (再構成すると常に一致してしまい検証にならない
    ため、記録名だけを False にする fake で判定ロジックの参照先を明示する)。
    """
    session_info_dir = tmp_path / "session-info"
    session_info_dir.mkdir()
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "entry.json").write_text("{}")

    session_id = "session"
    session_key = "bbb2222"
    recorded_session = f"claude-project-{session_key}"
    contents = {
        ".tmux-session": recorded_session,
        ".lock-path": "/tmp/lock",
        ".pid": session_key,
        ".shared-dir": str(shared_dir),
        ".task-queue": "task",
    }
    for extension, content in contents.items():
        (session_info_dir / f"{session_id}{extension}").write_text(content)

    monkeypatch.setattr(tmux_session_start, "SESSION_INFO_DIR", str(session_info_dir))
    monkeypatch.setattr(tmux_common, "SESSION_INFO_DIR", str(session_info_dir))
    monkeypatch.setattr(
        tmux_session_start,
        "run_tmux",
        lambda *args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    queried_names: list[str] = []

    def fake_tmux_has_session(name: str) -> bool:
        queried_names.append(name)
        return name != recorded_session

    monkeypatch.setattr(tmux_session_start, "tmux_has_session", fake_tmux_has_session)

    tmux_session_start.cleanup_orphaned_sessions("project")

    assert recorded_session in queried_names
    assert not shared_dir.exists()
    for extension in tmux_common.SESSION_FILE_EXTENSIONS:
        assert not (session_info_dir / f"{session_id}{extension}").exists()


def _run_orphan_cleanup(
    monkeypatch, tmp_path, session_key: str, pid_tracked: bool, pid_alive: bool
) -> list[tuple[str, ...]]:
    session_info_dir = tmp_path / "session-info"
    session_info_dir.mkdir()
    if pid_tracked:
        (session_info_dir / "session.pid").write_text(session_key)

    calls: list[tuple[str, ...]] = []

    def fake_run_tmux(*args: str):
        calls.append(args)
        if args[:2] == ("ls", "-F"):
            return SimpleNamespace(
                returncode=0,
                stdout=f"claude-project-{session_key}\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_kill(pid: int, signal: int) -> None:
        if not pid_alive:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(tmux_session_start, "SESSION_INFO_DIR", str(session_info_dir))
    monkeypatch.setattr(tmux_common, "SESSION_INFO_DIR", str(session_info_dir))
    monkeypatch.setattr(tmux_session_start, "run_tmux", fake_run_tmux)
    monkeypatch.setattr(tmux_session_start.os, "kill", fake_kill)
    monkeypatch.setattr(tmux_session_start, "tmux_has_session", lambda name: True)

    tmux_session_start.cleanup_orphaned_sessions("project")
    return calls


def test_snapshot_panes_lists_once_and_parses_snapshot(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_tmux(*args: str):
        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout="%1\tDONE:first\n%2\twaiting\n%3\tDONE:second\n%4\tother\n",
        )

    monkeypatch.setattr(tmux_subagent_start, "tmux_has_session", lambda name: True)
    monkeypatch.setattr(tmux_subagent_start, "run_tmux", fake_run_tmux)

    done_panes, waiting_pane_id = tmux_subagent_start.snapshot_panes("session")

    assert done_panes == ["%1", "%3"]
    assert waiting_pane_id == "%2"
    assert calls == [("list-panes", "-t", "session", "-F", "#{pane_id}\t#{pane_title}")]


def test_create_agent_pane_preserves_split_layout_call_order(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_tmux(*args: str):
        calls.append(args)
        if args[0] == "split-window":
            return SimpleNamespace(returncode=0, stdout="%2\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(tmux_subagent_start, "tmux_has_session", lambda name: True)
    monkeypatch.setattr(
        tmux_subagent_start.os, "mkdir", lambda path: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(tmux_subagent_start, "run_tmux", fake_run_tmux)

    pane_id = tmux_subagent_start.create_agent_pane("session", "/tmp/lock", "%1", "tail-command")

    assert pane_id == "%2"
    assert calls == [
        ("select-layout", "-t", "session", "tiled"),
        (
            "split-window",
            "-t",
            "session",
            "-P",
            "-F",
            "#{pane_id}",
            "tail-command",
        ),
        ("select-layout", "-t", "session", "tiled"),
    ]


def test_format_tool_input_prioritizes_known_keys() -> None:
    assert tmux_format_output.format_tool_input({"command": "pytest -q"}) == "pytest -q"
    assert tmux_format_output.format_tool_input({"pattern": "TODO"}) == "TODO"
    assert tmux_format_output.format_tool_input({"file_path": "src/a.py"}) == "src/a.py"


def test_format_tool_input_falls_back_to_json() -> None:
    result = tmux_format_output.format_tool_input({"foo": "bar"})
    assert result.startswith("{")
    assert '"foo": "bar"' in result


def test_handle_assistant_prints_text_and_tool_use(capsys) -> None:
    message = {
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
        ]
    }

    tmux_format_output.handle_assistant(message)
    captured = capsys.readouterr().out

    assert "hello" in captured
    assert "[Bash]" in captured
    assert "ls -la" in captured


def test_handle_user_prints_tool_result(capsys) -> None:
    message = {
        "content": [
            {"type": "tool_result", "content": "command output line"},
        ]
    }

    tmux_format_output.handle_user(message)
    captured = capsys.readouterr().out

    assert "→" in captured
    assert "command output line" in captured


def test_handle_progress_prints_only_bash_progress(capsys) -> None:
    tmux_format_output.handle_progress({"type": "bash_progress", "content": "running..."})
    printed = capsys.readouterr().out
    assert "running..." in printed

    tmux_format_output.handle_progress({"type": "other", "content": "skip"})
    not_printed = capsys.readouterr().out
    assert not_printed == ""


def test_handle_progress_skips_empty_content(capsys) -> None:
    tmux_format_output.handle_progress({"type": "bash_progress", "content": ""})
    assert capsys.readouterr().out == ""
