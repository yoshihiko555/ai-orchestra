"""context_store.py のユニットテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

context_store = load_module("context_store", "packages/core/hooks/context_store.py")

init_context_dir = context_store.init_context_dir
write_entry = context_store.write_entry
read_entries = context_store.read_entries
update_working_context = context_store.update_working_context
read_working_context = context_store.read_working_context
cleanup_session = context_store.cleanup_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_dir(project_dir: Path) -> Path:
    return project_dir / ".claude" / "context" / "session"


def _entries_dir(project_dir: Path) -> Path:
    return _session_dir(project_dir) / "entries"


def _shared_dir(project_dir: Path) -> Path:
    return project_dir / ".claude" / "context" / "shared"


def _working_context_path(project_dir: Path) -> Path:
    return _shared_dir(project_dir) / "working-context.json"


# ---------------------------------------------------------------------------
# init_context_dir
# ---------------------------------------------------------------------------


class TestInitContextDir:
    def test_creates_required_directories(self, tmp_path: Path) -> None:
        # Arrange / Act
        init_context_dir(str(tmp_path))

        # Assert
        assert _entries_dir(tmp_path).is_dir()
        assert _shared_dir(tmp_path).is_dir()

    def test_creates_meta_json(self, tmp_path: Path) -> None:
        # Arrange / Act
        init_context_dir(str(tmp_path))

        # Assert
        meta_path = _session_dir(tmp_path) / "meta.json"
        assert meta_path.is_file()
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "session_id" in data
        assert "started_at" in data

    def test_idempotent_does_not_overwrite_meta(self, tmp_path: Path) -> None:
        # Arrange
        init_context_dir(str(tmp_path))
        meta_path = _session_dir(tmp_path) / "meta.json"
        first_id = json.loads(meta_path.read_text(encoding="utf-8"))["session_id"]

        # Act – call again
        init_context_dir(str(tmp_path))

        # Assert – session_id unchanged
        second_id = json.loads(meta_path.read_text(encoding="utf-8"))["session_id"]
        assert first_id == second_id

    def test_idempotent_does_not_raise_when_dirs_exist(self, tmp_path: Path) -> None:
        # Arrange
        init_context_dir(str(tmp_path))

        # Act / Assert – no exception
        init_context_dir(str(tmp_path))


# ---------------------------------------------------------------------------
# write_entry
# ---------------------------------------------------------------------------


class TestWriteEntry:
    def test_writes_entry_file(self, tmp_path: Path) -> None:
        # Arrange
        agent_id = "tester"
        data = {"agent_id": agent_id, "task_name": "run tests", "summary": "all passed"}

        # Act
        write_entry(str(tmp_path), agent_id, data)

        # Assert – ファイル名は {agent_id}_{timestamp}.json 形式
        entries = list(_entries_dir(tmp_path).glob(f"{agent_id}_*.json"))
        assert len(entries) == 1
        stored = json.loads(entries[0].read_text(encoding="utf-8"))
        assert stored["agent_id"] == agent_id
        assert stored["task_name"] == "run tests"

    def test_creates_entries_dir_if_missing(self, tmp_path: Path) -> None:
        # Arrange – no prior init
        agent_id = "backend-python-dev"

        # Act
        write_entry(str(tmp_path), agent_id, {"key": "value"})

        # Assert
        entries = list(_entries_dir(tmp_path).glob(f"{agent_id}_*.json"))
        assert len(entries) == 1

    def test_does_not_overwrite_existing_entry(self, tmp_path: Path) -> None:
        # Arrange – 同一 agent_id で2回呼ぶと別ファイルが生成される
        agent_id = "debugger"
        write_entry(str(tmp_path), agent_id, {"summary": "first"})

        # Act
        write_entry(str(tmp_path), agent_id, {"summary": "second"})

        # Assert – 2件のエントリーが保持される
        entries = list(_entries_dir(tmp_path).glob(f"{agent_id}_*.json"))
        assert len(entries) == 2
        summaries = {json.loads(e.read_text(encoding="utf-8"))["summary"] for e in entries}
        assert summaries == {"first", "second"}

    def test_sanitizes_agent_id_for_filename(self, tmp_path: Path) -> None:
        # Arrange
        agent_id = "../unsafe agent id"

        # Act
        write_entry(str(tmp_path), agent_id, {"summary": "safe"})

        # Assert
        entries = list(_entries_dir(tmp_path).glob("*.json"))
        assert len(entries) == 1
        assert entries[0].name.startswith("unsafe-agent-id_")


# ---------------------------------------------------------------------------
# read_entries
# ---------------------------------------------------------------------------


class TestReadEntries:
    def test_returns_empty_list_when_no_entries_dir(self, tmp_path: Path) -> None:
        # Act
        result = read_entries(str(tmp_path))

        # Assert
        assert result == []

    def test_returns_all_entries(self, tmp_path: Path) -> None:
        # Arrange
        write_entry(str(tmp_path), "agent-a", {"summary": "a"})
        write_entry(str(tmp_path), "agent-b", {"summary": "b"})

        # Act
        result = read_entries(str(tmp_path))

        # Assert
        assert len(result) == 2
        summaries = {e["summary"] for e in result}
        assert summaries == {"a", "b"}

    def test_skips_non_json_files(self, tmp_path: Path) -> None:
        # Arrange
        init_context_dir(str(tmp_path))
        (_entries_dir(tmp_path) / "notes.txt").write_text("ignore me", encoding="utf-8")
        write_entry(str(tmp_path), "agent-c", {"summary": "c"})

        # Act
        result = read_entries(str(tmp_path))

        # Assert
        assert len(result) == 1

    def test_skips_invalid_json_files(self, tmp_path: Path) -> None:
        # Arrange
        init_context_dir(str(tmp_path))
        (_entries_dir(tmp_path) / "bad.json").write_text("not-json", encoding="utf-8")
        write_entry(str(tmp_path), "agent-d", {"summary": "d"})

        # Act
        result = read_entries(str(tmp_path))

        # Assert – invalid file is silently skipped
        assert len(result) == 1
        assert result[0]["summary"] == "d"

    def test_returns_empty_list_for_empty_entries_dir(self, tmp_path: Path) -> None:
        # Arrange
        init_context_dir(str(tmp_path))

        # Act
        result = read_entries(str(tmp_path))

        # Assert
        assert result == []


# ---------------------------------------------------------------------------
# update_working_context
# ---------------------------------------------------------------------------


class TestUpdateWorkingContext:
    def test_creates_working_context_file(self, tmp_path: Path) -> None:
        # Act
        update_working_context(str(tmp_path), {"current_phase": "phase-1"})

        # Assert
        assert _working_context_path(tmp_path).is_file()

    def test_adds_modified_files(self, tmp_path: Path) -> None:
        # Act
        update_working_context(str(tmp_path), {"modified_files": ["src/foo.py"]})

        # Assert
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert "src/foo.py" in ctx["modified_files"]

    def test_deduplicates_modified_files(self, tmp_path: Path) -> None:
        # Arrange
        update_working_context(str(tmp_path), {"modified_files": ["src/foo.py"]})

        # Act
        update_working_context(str(tmp_path), {"modified_files": ["src/foo.py", "src/bar.py"]})

        # Assert
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert ctx["modified_files"].count("src/foo.py") == 1
        assert "src/bar.py" in ctx["modified_files"]

    def test_updates_existing_scalar_field(self, tmp_path: Path) -> None:
        # Arrange
        update_working_context(str(tmp_path), {"current_phase": "phase-1"})

        # Act
        update_working_context(str(tmp_path), {"current_phase": "phase-2"})

        # Assert
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert ctx["current_phase"] == "phase-2"

    def test_updates_updated_at_timestamp(self, tmp_path: Path) -> None:
        # Act
        update_working_context(str(tmp_path), {"current_phase": "x"})

        # Assert
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert "updated_at" in ctx
        assert ctx["updated_at"]  # non-empty


# ---------------------------------------------------------------------------
# read_working_context
# ---------------------------------------------------------------------------


class TestReadWorkingContext:
    def test_returns_empty_dict_when_no_file(self, tmp_path: Path) -> None:
        # Act
        result = read_working_context(str(tmp_path))

        # Assert
        assert result == {}

    def test_returns_stored_context(self, tmp_path: Path) -> None:
        # Arrange
        update_working_context(str(tmp_path), {"current_phase": "phase-3"})

        # Act
        result = read_working_context(str(tmp_path))

        # Assert
        assert result["current_phase"] == "phase-3"

    def test_returns_modified_files(self, tmp_path: Path) -> None:
        # Arrange
        update_working_context(str(tmp_path), {"modified_files": ["a.py", "b.py"]})

        # Act
        result = read_working_context(str(tmp_path))

        # Assert
        assert set(result["modified_files"]) == {"a.py", "b.py"}


# ---------------------------------------------------------------------------
# cleanup_session
# ---------------------------------------------------------------------------


class TestCleanupSession:
    def test_removes_session_directory(self, tmp_path: Path) -> None:
        # Arrange
        init_context_dir(str(tmp_path))
        assert _session_dir(tmp_path).is_dir()

        # Act
        cleanup_session(str(tmp_path))

        # Assert
        assert not _session_dir(tmp_path).exists()

    def test_removes_working_context_file(self, tmp_path: Path) -> None:
        # Arrange
        update_working_context(str(tmp_path), {"current_phase": "x"})
        assert _working_context_path(tmp_path).is_file()

        # Act
        cleanup_session(str(tmp_path))

        # Assert
        assert not _working_context_path(tmp_path).exists()
        assert _shared_dir(tmp_path).is_dir()

    def test_does_not_raise_when_already_clean(self, tmp_path: Path) -> None:
        # Act / Assert – no exception
        cleanup_session(str(tmp_path))

    def test_shared_dir_remains_after_cleanup(self, tmp_path: Path) -> None:
        # Arrange
        init_context_dir(str(tmp_path))

        # Act
        cleanup_session(str(tmp_path))

        # Assert – shared/ directory itself is kept; only working-context.json is deleted
        assert _shared_dir(tmp_path).is_dir()
