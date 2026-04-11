"""event_logger.py のユニットテスト。"""

from __future__ import annotations

import json
import os
import sys

import pytest

from tests.module_loader import REPO_ROOT, load_module

# event_logger を読み込み
sys.path.insert(0, str(REPO_ROOT / "packages" / "audit" / "hooks"))
event_logger = load_module("event_logger", "packages/audit/hooks/event_logger.py")


# ---------------------------------------------------------------------------
# generate_id
# ---------------------------------------------------------------------------


class TestGenerateId:
    def test_returns_12_char_hex(self) -> None:
        id_ = event_logger.generate_id()
        assert len(id_) == 12
        assert all(c in "0123456789abcdef" for c in id_)

    def test_unique(self) -> None:
        ids = {event_logger.generate_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# emit_event
# ---------------------------------------------------------------------------


class TestEmitEvent:
    def test_schema_v1_fields(self, tmp_path: object) -> None:
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        record = event_logger.emit_event(
            "session_start",
            {"packages": ["core", "audit"]},
            session_id="test-session-123",
            project_dir=project_dir,
        )

        assert record["v"] == 1
        assert record["type"] == "session_start"
        assert record["sid"] == "test-session-123"
        assert record["data"]["packages"] == ["core", "audit"]
        assert record["eid"]
        assert record["tid"]
        assert record["ts"]
        assert record["ctx"] == {"skill": None, "phase": None}
        assert record["ptid"] is None
        assert record["aid"] is None

    def test_writes_to_session_file(self, tmp_path: object) -> None:
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        event_logger.emit_event(
            "prompt",
            {"user_input_excerpt": "test", "expected_route": "claude-direct", "matched_rule": None},
            session_id="sess-abc",
            project_dir=project_dir,
        )

        log_path = event_logger.get_session_log_path("sess-abc", project_dir)
        assert os.path.exists(log_path)

        with open(log_path) as f:
            line = f.readline()
        record = json.loads(line)
        assert record["type"] == "prompt"
        assert record["sid"] == "sess-abc"

    def test_invalid_event_type_raises(self, tmp_path: object) -> None:
        with pytest.raises(ValueError, match="Unknown event_type"):
            event_logger.emit_event("invalid_type", {}, session_id="s1")

    def test_no_session_id_returns_record_without_write(self, tmp_path: object) -> None:
        record = event_logger.emit_event("session_start", {"packages": []})
        assert record["v"] == 1
        assert record["sid"] == ""

    def test_custom_trace_and_context(self, tmp_path: object) -> None:
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        record = event_logger.emit_event(
            "cli_call",
            {"tool": "codex", "success": True},
            session_id="s1",
            tid="my-trace",
            ptid="parent-trace",
            aid="agent-123",
            ctx={"skill": "issue-fix", "phase": "implementation"},
            project_dir=project_dir,
        )

        assert record["tid"] == "my-trace"
        assert record["ptid"] == "parent-trace"
        assert record["aid"] == "agent-123"
        assert record["ctx"]["skill"] == "issue-fix"

    def test_multiple_events_append(self, tmp_path: object) -> None:
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        for i in range(3):
            event_logger.emit_event(
                "route_decision",
                {
                    "expected": "claude-direct",
                    "actual": {"tool": "Bash", "detail": "codex"},
                    "matched": False,
                },
                session_id="s1",
                project_dir=project_dir,
            )

        log_path = event_logger.get_session_log_path("s1", project_dir)
        with open(log_path) as f:
            lines = [l for l in f if l.strip()]
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# Trace State
# ---------------------------------------------------------------------------


class TestTraceState:
    def test_save_and_load(self, tmp_path: object) -> None:
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude", "state"), exist_ok=True)

        event_logger.save_trace_state(
            "tid-123",
            session_id="s1",
            expected_route="codex",
            project_dir=project_dir,
        )

        state = event_logger.load_trace_state(project_dir)
        assert state["tid"] == "tid-123"
        assert state["session_id"] == "s1"
        assert state["expected_route"] == "codex"

    def test_load_missing_returns_empty(self, tmp_path: object) -> None:
        state = event_logger.load_trace_state(str(tmp_path))
        assert state == {}


# ---------------------------------------------------------------------------
# Session Lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    def test_init_creates_sessions_dir(self, tmp_path: object) -> None:
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        path = event_logger.init_session_dir("test-sess", project_dir)
        assert os.path.isdir(os.path.dirname(path))
        assert "test-sess.jsonl" in path
