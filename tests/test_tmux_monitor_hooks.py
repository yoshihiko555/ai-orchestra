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


def test_run_tmux_invokes_subprocess_with_expected_args(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tmux_common.subprocess, "run", fake_run)

    tmux_common.run_tmux("has-session", "-t", "abc")

    assert captured["cmd"] == ["tmux", "has-session", "-t", "abc"]


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

    def fake_run(cmd, capture_output, text):
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

