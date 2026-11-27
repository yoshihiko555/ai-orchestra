"""公開 CLI から設定・ループ・出力ファイルまでを API 接続なしで検証する。"""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from lib.llm.types import LLMError

from tests.unit.test_llm_agent_loop import _read, _response
from tests.unit.test_llm_cli import _run
from tests.unit.test_llm_cli import isolated_environment as isolated_environment


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "project"
    config_dir = path / ".claude/config/agent-routing"
    config_dir.mkdir(parents=True)
    config = {
        "api": {
            "providers": {"gemini": {"enabled": True, "model": "test-model"}},
            "profiles": {
                "p": {
                    "provider": "gemini",
                    "tools": {"repo_read": True},
                    "limits": {"rate_limit_initial_delay_sec": 0},
                }
            },
        }
    }
    (config_dir / "cli-tools.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-private-credentialmarker")
    profiles = importlib.import_module("api_profiles")
    monkeypatch.setattr(profiles, "provider_dependency_available", lambda provider: True)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for name in tuple(os.environ):
        if name.startswith("GIT_") and name not in {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"}:
            monkeypatch.delenv(name)
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(path)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    (path / "a.py").write_text("public\n", encoding="utf-8")
    return path


@pytest.fixture
def turn(monkeypatch):
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("実ネットワーク接続は禁止です。"))
    )
    boundary = Mock(side_effect=AssertionError("テストにない API 呼び出しです。"))
    llm_cli = importlib.import_module("lib.llm_cli")
    monkeypatch.setattr(llm_cli, "call_tool_turn", boundary, raising=False)
    return boundary


def _agent(monkeypatch, project, *extra, profile="p"):
    return _run(
        monkeypatch,
        "llm",
        "agent",
        "--project",
        project,
        "--profile",
        profile,
        *extra,
        "質問",
    )


def _local(project, data):
    (project / ".claude/config/agent-routing/cli-tools.local.yaml").write_text(
        yaml.safe_dump({"api": {"profiles": data}}), encoding="utf-8"
    )


def test_agent_stdout_has_public_result_schema(monkeypatch, project, turn, capsys):
    citation = {"path": "a.py", "start_line": 1, "end_line": 1, "claim": "主張"}
    turn.side_effect = [
        _response(calls=(_read(),)),
        _response(json.dumps({"answer": "回答", "citations": [citation]})),
    ]

    code = _agent(monkeypatch, project)

    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(result) == {
        "status",
        "stop_reason",
        "turns",
        "tokens",
        "answer",
        "citations",
        "files_read",
        "unverified_count",
    }
    assert result == {
        "status": "complete",
        "stop_reason": None,
        "turns": 2,
        "tokens": {"input": 4, "output": 2, "total": 6},
        "answer": "回答",
        "citations": [{**citation, "verified": True}],
        "files_read": ["a.py"],
        "unverified_count": 0,
    }


def test_output_files_include_live_progress_and_completion_result(
    monkeypatch, project, turn, tmp_path, capsys
):
    output = tmp_path / "out"
    output.mkdir()
    observed = []
    replace = os.replace

    def publish(source, destination):
        if Path(destination).name == "exit":
            assert (
                json.loads((output / "result.json").read_text(encoding="utf-8"))["status"]
                == "complete"
            )
        replace(source, destination)

    monkeypatch.setattr(os, "replace", publish)

    def call(provider, request, key):
        assert provider == "gemini"
        assert not (output / "exit").exists()
        assert not (output / "result.json").exists()
        if observed:
            progress = json.loads((output / "progress.json").read_text(encoding="utf-8"))
            assert progress["turns"] == len(observed)
            assert progress["tokens"] == {
                "input": 2 * len(observed),
                "output": len(observed),
                "total": 3 * len(observed),
            }
        observed.append(request)
        return (
            _response(calls=(_read(str(len(observed))),))
            if len(observed) < 3
            else _response("完了")
        )

    turn.side_effect = call
    code = _agent(monkeypatch, project, "--output", output)

    assert code == 0
    assert not capsys.readouterr().out
    assert {path.name for path in output.iterdir()} == {"progress.json", "result.json", "exit"}
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    progress = json.loads((output / "progress.json").read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert result["turns"] == progress["turns"] == 3
    assert result["tokens"] == progress["tokens"] == {"input": 6, "output": 3, "total": 9}
    assert (output / "exit").read_text(encoding="utf-8") == "0\n"


@pytest.mark.parametrize("existing", ["exit", "other.txt"])
def test_nonempty_output_is_rejected_before_call_without_changing_files(
    monkeypatch, project, turn, tmp_path, existing
):
    output = tmp_path / "out"
    output.mkdir()
    (output / existing).write_bytes(b"previous\n")

    code = _agent(monkeypatch, project, "--output", output)

    assert code == 13
    turn.assert_not_called()
    assert {path.name: path.read_bytes() for path in output.iterdir()} == {existing: b"previous\n"}


@pytest.mark.parametrize(
    "category,code",
    [
        ("quota_exhausted", 9),
        ("rate_limited", 10),
        ("auth_error", 11),
        ("timeout", 6),
        ("api_error", 7),
    ],
)
def test_agent_error_reason_exit_code_and_output_file_agree(
    monkeypatch, project, turn, tmp_path, capsys, category, code
):
    reason = "api_timeout" if category == "timeout" else category
    turn.side_effect = LLMError(category, "安全なエラー")
    output = tmp_path / "out"

    actual = _agent(monkeypatch, project, "--output", output)

    assert actual == code
    assert (output / "exit").read_text(encoding="utf-8") == f"{code}\n"
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "partial"
    assert result["stop_reason"] == reason
    assert turn.call_count == (3 if category == "rate_limited" else 1)
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.strip()


@pytest.mark.parametrize("local_only", [False, True])
def test_local_profile_turn_limit_reaches_execution(monkeypatch, project, turn, capsys, local_only):
    profile = "local-agent" if local_only else "p"
    fields = {"limits": {"max_turns": 1}}
    if local_only:
        fields.update(provider="gemini", tools={"repo_read": True})
    _local(project, {profile: fields})
    turn.side_effect = [_response(calls=(_read(),)), _response("要約")]

    code = _agent(monkeypatch, project, profile=profile)

    result = json.loads(capsys.readouterr().out)
    assert code == 12
    assert (result["status"], result["stop_reason"]) == ("partial", "max_turns")
    assert turn.call_count == 2
    assert turn.call_args_list[0].args[1].tools
    assert turn.call_args_list[1].args[1].tools == ()


def test_profile_deny_reaches_read_tool_and_excludes_file_from_files_read(
    monkeypatch, project, turn, capsys
):
    _local(project, {"p": {"deny": ["a.py"]}})

    def call(provider, request, key):
        if len(turn.call_args_list) == 1:
            return _response(calls=(_read(),))
        result = request.messages[-1].results[0]
        assert result.call_id == "read"
        assert result.is_error is True
        assert "public" not in result.content
        return _response("拒否を確認")

    turn.side_effect = call
    assert _agent(monkeypatch, project) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["files_read"] == []


@pytest.mark.parametrize("allowance", [None, {"repo_read": False}])
def test_tools_are_disabled_when_profile_does_not_allow_repo_read(
    monkeypatch, project, turn, capsys, allowance
):
    fields = {"provider": "gemini"}
    if allowance is not None:
        fields["tools"] = allowance
    _local(project, {"no-tools": fields})
    turn.side_effect = [_response("回答")]

    assert _agent(monkeypatch, project, profile="no-tools") == 0

    assert turn.call_args.args[1].tools == ()
    assert json.loads(capsys.readouterr().out)["files_read"] == []


@pytest.mark.parametrize("output_files", [False, True])
def test_key_echo_and_provider_state_are_not_exposed(
    monkeypatch, project, turn, tmp_path, capsys, caplog, output_files
):
    from lib.llm.tool_types import AssistantMessage, TokenUsage, ToolTurnResponse

    turn.side_effect = [
        ToolTurnResponse(
            AssistantMessage(
                "回答 dummy-private-credentialmarker",
                provider_state=("gemini", '{"raw":"body-private-responsemarker"}'),
            ),
            usage=TokenUsage(2, 1),
        )
    ]
    output = tmp_path / "out"
    extra = ("--output", output) if output_files else ()

    assert _agent(monkeypatch, project, *extra) == 0

    captured = capsys.readouterr()
    serialized = (
        (output / "result.json").read_text(encoding="utf-8") if output_files else captured.out
    )
    result = json.loads(serialized)
    assert "回答" in result["answer"]
    assert "[REDACTED]" in result["answer"]
    for text in (serialized, captured.out, captured.err, caplog.text):
        for marker in ("dummy-private", "credentialmarker", "body-private", "responsemarker"):
            assert marker not in text.casefold()


@pytest.mark.parametrize(
    "fields,message",
    [
        (
            {"limits": {"max_turns": 0}},
            "API プロファイルの max_turns は正の整数が必要です。",
        ),
        (
            {"tools": {"repo_read": "true"}},
            "API プロファイルの tools は repo_read の真偽値だけを指定してください。",
        ),
        ({"deny": "*.db"}, "API プロファイルの deny は文字列の配列が必要です。"),
    ],
)
def test_invalid_agent_config_writes_failure_exit_without_starting(
    monkeypatch, project, turn, tmp_path, capsys, fields, message
):
    _local(project, {"p": fields})
    output = tmp_path / "out"

    assert _agent(monkeypatch, project, "--output", output) == 8

    turn.assert_not_called()
    assert (output / "exit").read_text(encoding="utf-8") == "8\n"
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == message + "\n"


def test_surrogate_answer_reports_io_error_and_writes_failure_exit(
    monkeypatch, project, turn, tmp_path, capsys
):
    turn.side_effect = [_response('{"answer": "\\ud800", "citations": []}')]
    output = tmp_path / "out"

    assert _agent(monkeypatch, project, "--output", output) == 8

    turn.assert_called_once()
    assert (output / "exit").read_text(encoding="utf-8") == "8\n"
    assert {path.name for path in output.iterdir()} == {"progress.json", "exit"}
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == (
        "LLM 入出力を完了できませんでした。ファイルの権限と文字コードを確認してください。\n"
    )


@pytest.mark.parametrize("pattern", ["", "a\x00b", "../x"])
def test_invalid_deny_content_reports_deny_error_without_starting(
    monkeypatch, project, turn, tmp_path, capsys, pattern
):
    _local(project, {"p": {"deny": [pattern]}})
    output = tmp_path / "out"

    assert _agent(monkeypatch, project, "--output", output) == 8

    turn.assert_not_called()
    assert (output / "exit").read_text(encoding="utf-8") == "8\n"
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == "道具の拒否リストが不正です。設定を確認してください。\n"


@pytest.mark.parametrize(
    "condition,code", [("key_missing", 3), ("provider_disabled", 4), ("dependency_missing", 5)]
)
def test_unavailable_agent_writes_exit_without_api_call(
    monkeypatch, project, turn, tmp_path, condition, code
):
    if condition == "key_missing":
        monkeypatch.delenv("GEMINI_API_KEY")
    elif condition == "provider_disabled":
        config_path = project / ".claude/config/agent-routing/cli-tools.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["api"]["providers"]["gemini"]["enabled"] = False
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    else:
        monkeypatch.setattr(
            importlib.import_module("api_profiles"),
            "provider_dependency_available",
            lambda provider: False,
        )
    output = tmp_path / "out"

    assert _agent(monkeypatch, project, "--output", output) == code

    turn.assert_not_called()
    assert (output / "exit").read_text(encoding="utf-8") == f"{code}\n"


def test_failed_result_write_returns_io_error_and_leaves_failure_exit(
    monkeypatch, project, turn, tmp_path, capsys
):
    from lib.llm.run_output import RunOutput

    turn.side_effect = [_response("回答")]
    monkeypatch.setattr(RunOutput, "write_result", Mock(side_effect=OSError("書込失敗")))
    output = tmp_path / "out"

    assert _agent(monkeypatch, project, "--output", output) == 8

    assert (output / "exit").read_text(encoding="utf-8") == "8\n"
    assert not (output / "result.json").exists()
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.strip()


def test_summary_api_error_controls_json_and_process_exit(monkeypatch, project, turn, capsys):
    _local(project, {"p": {"limits": {"max_turns": 1}}})
    turn.side_effect = [
        _response("最後の回答", calls=(_read(),)),
        LLMError.for_category("quota_exhausted"),
    ]

    assert _agent(monkeypatch, project) == 9

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "partial"
    assert result["stop_reason"] == "quota_exhausted"
    assert result["answer"] == "最後の回答"
    assert result["turns"] == 1
    assert "枠" in captured.err or "残高" in captured.err
