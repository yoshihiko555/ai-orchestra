"""EV-25、EV-38〜46: hook の記録と非干渉。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.module_loader import load_module

se = load_module(
    "skill_evolution_common_hooks", "packages/skill-evolution/lib/skill_evolution_common.py"
)
HOOK_CASES = [
    ("inject-lessons.py", {"tool_name": "Skill", "tool_input": {"skill": "review"}}),
    ("capture-skill-telemetry.py", {"tool_name": "Skill"}),
    ("record-skill-scope.py", {"hook_event_name": "UserPromptSubmit"}),
    (
        "record-skill-scope.py",
        {"hook_event_name": "SubagentStart", "agent_id": "agent-1", "agent_type": "Explore"},
    ),
    (
        "record-skill-scope.py",
        {
            "hook_event_name": "UserPromptExpansion",
            "expansion_type": "slash_command",
            "command_name": "review",
        },
    ),
    ("capture-subagent-skill.py", {"agent_id": "agent-1"}),
    ("capture-skill-stop.py", {}),
]


def _input(**fields):
    return {"session_id": "session-1", "prompt_id": "prompt-1", "tool_use_id": "toolu_1", **fields}


def _start(run_hook, **fields):
    run_hook(
        "inject-lessons.py", _input(tool_name="Skill", tool_input={"skill": "review"}, **fields)
    )


def test_ev38_skill_tool_records_exact_run_started(project, skill_dir, run_hook, read_events):
    _start(run_hook)
    [event] = read_events()
    assert set(event) == {
        "v",
        "ts",
        "type",
        "run_id",
        "sid",
        "prompt_id",
        "skill",
        "trigger",
        "tool_use_id",
        "agent_id",
        "agent_type",
        "model",
        "skill_hash",
        "project",
        "src",
    }
    assert event["v"] == 2 and event["type"] == "run.started"
    assert re.fullmatch(r"R-[0-9a-f]{12}", event["run_id"])
    assert datetime.fromisoformat(event["ts"]).utcoffset() == timedelta(0)
    assert (
        event["sid"],
        event["prompt_id"],
        event["skill"],
        event["trigger"],
        event["tool_use_id"],
    ) == (
        "session-1",
        "prompt-1",
        "review",
        "skill_tool",
        "toolu_1",
    )
    assert event["skill_hash"] == se.hash_skill_directory(skill_dir)
    assert event["project"] == project.name
    assert event["src"] == {
        "tool_use_id": "hook_input",
        "agent_id": "absent",
        "model": "absent",
        "skill_hash": "project_skill_dir",
    }


def test_ev38_fallback_name_and_subagent_fields(run_hook, read_events):
    run_hook(
        "inject-lessons.py",
        _input(
            tool_name="Skill",
            tool_input={"skill_name": "review"},
            agent_id="agent-1",
            agent_type="Explore",
        ),
    )
    [event] = read_events()
    assert event["skill"] == "review"
    assert event["agent_id"] == "agent-1" and event["agent_type"] == "Explore"
    assert event["src"]["agent_id"] == "hook_input"


@pytest.mark.parametrize("scope", ["project", "user", "symlink"])
def test_ev39_direct_skill_records_without_tool_id(project, tmp_path, run_hook, read_events, scope):
    base = project if scope != "user" else Path.home()
    directory = base / ".claude" / "skills" / "review"
    directory.parent.mkdir(parents=True, exist_ok=True)
    if scope == "symlink":
        target = tmp_path / "real-skill"
        target.mkdir()
        directory.symlink_to(target, target_is_directory=True)
    else:
        directory.mkdir()
    (directory / "SKILL.md").write_text("手順", encoding="utf-8")
    run_hook(
        "record-skill-scope.py",
        _input(
            hook_event_name="UserPromptExpansion",
            expansion_type="slash_command",
            command_name="review",
        ),
    )
    [event] = read_events()
    assert event["trigger"] == "direct"
    assert event["tool_use_id"] is None and event["src"]["tool_use_id"] == "absent"
    assert event["skill_hash"] == se.hash_skill_directory(directory)
    assert event["src"]["skill_hash"] == (
        "user_skill_dir" if scope == "user" else "project_skill_dir"
    )


@pytest.mark.parametrize(
    "kind,name",
    [
        ("slash_command", "legacy"),
        ("slash_command", "missing"),
        ("mcp_prompt", "review"),
        ("other", "review"),
        ("slash_command", "plugin:review"),
        ("slash_command", "."),
        ("slash_command", ".."),
    ],
)
def test_ev40_non_skill_commands_are_ignored(project, skill_dir, run_hook, read_events, kind, name):
    directory = project / ".claude" / "commands"
    directory.mkdir()
    (directory / "legacy.md").write_text("旧コマンド")
    run_hook(
        "record-skill-scope.py",
        _input(
            hook_event_name="UserPromptExpansion",
            expansion_type=kind,
            command_name=name,
        ),
    )
    assert read_events() == []


def test_ev41_confirmation_requires_session_and_tool_match(run_hook, read_events):
    _start(run_hook)
    for changes in (
        {"tool_use_id": "different"},
        {"session_id": "different"},
        {"tool_use_id": None},
    ):
        run_hook("capture-skill-telemetry.py", _input(tool_name="Skill", **changes))
    assert len(read_events()) == 1
    run_hook("capture-skill-telemetry.py", _input(tool_name="Skill"))
    assert read_events()[-1] == {
        "v": 2,
        "ts": read_events()[-1]["ts"],
        "type": "run.confirmed",
        "sid": "session-1",
        "tool_use_id": "toolu_1",
    }


def test_ev41_previous_month_and_bounded_tail(project, run_hook, read_events):
    directory = project / ".claude" / "logs" / "skill-evolution"
    directory.mkdir(parents=True)
    previous = datetime.now(UTC).replace(day=1) - timedelta(days=1)
    path = directory / f"runs-{previous:%Y%m}.jsonl"
    start = {"v": 2, "type": "run.started", "sid": "session-1", "tool_use_id": "toolu_1"}
    path.write_bytes(
        b"x" * (se.RUNS_TAIL_BYTES + 1024)
        + b"\nnot-json\n[]\n"
        + json.dumps(start).encode()
        + b"\n"
    )
    run_hook("capture-skill-telemetry.py", _input(tool_name="Skill"))
    current = directory / f"runs-{datetime.now(UTC):%Y%m}.jsonl"
    assert json.loads(current.read_text())["type"] == "run.confirmed"
    current.unlink()
    path.write_bytes(json.dumps(start).encode() + b"\n" + b"x" * (se.RUNS_TAIL_BYTES + 1024))
    run_hook("capture-skill-telemetry.py", _input(tool_name="Skill"))
    assert not current.exists()


@pytest.mark.parametrize(
    "filename,payload,expected",
    [
        (
            "record-skill-scope.py",
            {"hook_event_name": "UserPromptSubmit"},
            {"type": "turn.started", "sid": "session-1", "prompt_id": "prompt-1"},
        ),
        (
            "capture-skill-stop.py",
            {},
            {"type": "turn.stopped", "sid": "session-1", "prompt_id": "prompt-1"},
        ),
        (
            "record-skill-scope.py",
            {"hook_event_name": "SubagentStart", "agent_id": "agent-1", "agent_type": "Explore"},
            {
                "type": "agent.started",
                "sid": "session-1",
                "agent_id": "agent-1",
                "agent_type": "Explore",
            },
        ),
        (
            "capture-subagent-skill.py",
            {"agent_id": "agent-1"},
            {"type": "agent.stopped", "sid": "session-1", "agent_id": "agent-1"},
        ),
    ],
)
def test_ev42_scope_boundaries(run_hook, read_events, filename, payload, expected):
    run_hook(filename, _input(**payload))
    [event] = read_events()
    assert event == {"v": 2, "ts": event["ts"], **expected}


def test_ev43_missing_optional_values_are_null_and_model_is_never_inferred(run_hook, read_events):
    run_hook(
        "inject-lessons.py",
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "review"},
            "model": "ignore-me",
        },
    )
    [event] = read_events()
    for field in ("tool_use_id", "agent_id", "agent_type", "model", "sid", "prompt_id"):
        assert event[field] is None
    assert event["src"] == {
        "tool_use_id": "absent",
        "agent_id": "absent",
        "model": "absent",
        "skill_hash": "unresolved",
    }


@pytest.mark.parametrize(
    "field", ["session_id", "prompt_id", "agent_id", "agent_type", "tool_use_id", "skill"]
)
@pytest.mark.parametrize("invalid", ["x\ny", "x\x00y", "\x1b[31mx", "a/b", "", 123, "x" * 129])
def test_ev44_invalid_fields_drop_entire_event(project, run_hook, read_events, field, invalid):
    payload = _input(tool_name="Skill", tool_input={"skill": "review"})
    if field == "skill":
        payload["tool_input"]["skill"] = invalid
    else:
        payload[field] = invalid
    run_hook("inject-lessons.py", payload)
    assert read_events() == []
    assert not (project / ".claude" / "logs").exists()


def test_ev44_invalid_project_name_drops_event(project, monkeypatch, run_hook, read_events):
    invalid = project.parent / "invalid project"
    invalid.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(invalid))
    _start(run_hook)
    assert not (invalid / ".claude" / "logs").exists()


@pytest.mark.parametrize("filename,payload", HOOK_CASES)
@pytest.mark.parametrize(
    "raw",
    [b"not-json", b"[]", b"null", b"1", b"\xff", b" " * (1024 * 1024 + 1)],
    ids=["broken", "array", "null", "number", "invalid-utf8", "over-1mib"],
)
def test_ev45_invalid_json_and_oversized_input_are_safe(
    run_hook, read_events, filename, payload, raw
):
    run_hook(filename, payload, raw=raw)
    assert read_events() == []


@pytest.mark.parametrize("filename,payload", HOOK_CASES)
def test_ev45_write_failure_is_safe(project, skill_dir, run_hook, filename, payload):
    _start(run_hook)
    directory = project / ".claude" / "logs" / "skill-evolution"
    before = {path: path.read_bytes() for path in directory.iterdir()}
    directory.chmod(0o500)
    try:
        if os.access(directory, os.W_OK) and os.geteuid() == 0:
            pytest.skip("root では権限による失敗を再現できない")
        for path in before:
            path.chmod(0o400)
        run_hook(filename, _input(**payload))
        assert {path: path.read_bytes() for path in directory.iterdir()} == before
    finally:
        directory.chmod(0o700)
        for path in before:
            path.chmod(0o600)


@pytest.mark.parametrize("filename,payload", HOOK_CASES)
def test_ev45_lock_timeout_is_safe_and_bounded(
    project, skill_dir, write_config, run_hook, filename, payload
):
    write_config("ledger:\n  lock_wait_seconds: 0.03\n")
    _start(run_hook)
    path = next((project / ".claude" / "logs" / "skill-evolution").glob("runs-*.jsonl"))
    before = path.read_bytes()
    with path.open("ab") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        run_hook(filename, _input(**payload))
        assert time.monotonic() - start < 3
    assert path.read_bytes() == before


@pytest.mark.parametrize("filename,payload", HOOK_CASES)
def test_ev46_no_injection_or_v1_data_or_transcript_access(
    project, skill_dir, run_hook, read_events, filename, payload
):
    _start(run_hook)
    legacy = project / ".claude" / "skill-evolution"
    legacy.mkdir()
    for name in ("lessons", "metrics", "pending", "locks"):
        (legacy / name).mkdir()
        (legacy / name / "review.md").write_text("PRIVATE-V1")
        os.mkfifo(legacy / name / "review.jsonl")
    transcript = project / "transcript.jsonl"
    os.mkfifo(transcript)
    before = {str(path): path.stat().st_mtime_ns for path in legacy.rglob("*")}
    payload = dict(payload)
    if "tool_input" in payload:
        payload["tool_input"] = {**payload["tool_input"], "args": "PRIVATE-TOOL-ARGS"}
    run_hook(
        filename,
        _input(
            **payload,
            prompt="PRIVATE-PROMPT",
            command_args="PRIVATE-ARGS",
            tool_response="PRIVATE-RESPONSE",
            last_assistant_message="PRIVATE-ASSISTANT",
            transcript_path=str(transcript),
        ),
    )
    assert {str(path): path.stat().st_mtime_ns for path in legacy.rglob("*")} == before
    assert "PRIVATE-" not in json.dumps(read_events())
    assert not (project / ".claude" / "logs" / "skill-evolution" / "pending").exists()


@pytest.mark.parametrize("filename,payload", HOOK_CASES)
def test_ev25_disabled_config_or_local_override_writes_nothing(
    project, skill_dir, write_config, run_hook, read_events, filename, payload
):
    _start(run_hook)
    before = read_events()
    for local in (False, True):
        write_config("enabled: true\n")
        write_config("enabled: false\nlessons:\n  max_lines: 1\n", local=local)
        run_hook(filename, _input(**payload))
        assert read_events() == before


def test_ev25_local_reenables_recording_and_env_project_precedes_cwd(
    project, tmp_path, write_config, run_hook, read_events
):
    write_config("enabled: false\n")
    write_config("enabled: true\n", local=True)
    _start(run_hook, cwd=str(tmp_path / "wrong"))
    assert len(read_events()) == 1


def test_ev38_hook_cwd_is_used_when_env_is_absent(project, monkeypatch, run_hook, read_events):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    _start(run_hook, cwd=str(project))
    assert len(read_events()) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": {"skill": "review"}},
        {"tool_name": "Skill", "tool_input": []},
        {"tool_name": "Skill", "tool_input": "review"},
        {"tool_name": "Skill"},
    ],
)
def test_ev45_unrelated_or_malformed_tool_input_is_ignored(run_hook, read_events, payload):
    run_hook("inject-lessons.py", _input(**payload))
    assert read_events() == []


@pytest.mark.parametrize("filename,payload", HOOK_CASES)
def test_ev45_unexpected_configuration_exception_is_safe(
    skill_dir, write_config, run_hook, read_events, filename, payload
):
    # storage の型違いは既定の保存先に戻すので（EV-49）、型を検査しない ledger で書き込み中の例外を起こす
    write_config("ledger: not-a-mapping\n")
    completed = run_hook(filename, _input(**payload))
    assert b"Traceback" not in completed.stderr
    assert read_events() == []
