"""context_store.py の追加テスト — カバレッジ不足箇所を補完。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

context_store = load_module("context_store", "packages/core/hooks/context_store.py")

init_context_dir = context_store.init_context_dir
write_entry = context_store.write_entry
update_working_context = context_store.update_working_context
cleanup_session = context_store.cleanup_session
get_project_dir = context_store.get_project_dir
_sanitize_agent_id = context_store._sanitize_agent_id
MAX_MODIFIED_FILES = context_store.MAX_MODIFIED_FILES


# ---------------------------------------------------------------------------
# _sanitize_agent_id
# ---------------------------------------------------------------------------


class TestSanitizeAgentId:
    def test_returns_unknown_for_empty_string(self) -> None:
        assert _sanitize_agent_id("") == "unknown"

    def test_returns_unknown_for_none(self) -> None:
        assert _sanitize_agent_id(None) == "unknown"

    def test_returns_unknown_for_only_special_chars(self) -> None:
        assert _sanitize_agent_id("../../../") == "unknown"

    def test_collapses_multiple_dashes(self) -> None:
        result = _sanitize_agent_id("a---b")
        assert result == "a-b"

    def test_strips_leading_trailing_dashes(self) -> None:
        result = _sanitize_agent_id("-agent-")
        assert result == "agent"

    def test_truncates_long_agent_id(self) -> None:
        long_id = "a" * 100
        result = _sanitize_agent_id(long_id)
        assert len(result) == 64

    def test_preserves_valid_characters(self) -> None:
        result = _sanitize_agent_id("backend-python-dev_v2")
        assert result == "backend-python-dev_v2"


# ---------------------------------------------------------------------------
# get_project_dir
# ---------------------------------------------------------------------------


class TestGetProjectDir:
    def test_returns_cwd_from_data(self) -> None:
        result = get_project_dir({"cwd": "/some/project"})
        assert result == "/some/project"

    def test_falls_back_to_env_var(self, monkeypatch: object) -> None:

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/env/project")  # type: ignore[attr-defined]
        result = get_project_dir({})
        assert result == "/env/project"

    def test_falls_back_to_getcwd(self, monkeypatch: object) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)  # type: ignore[attr-defined]
        result = get_project_dir({})
        # os.getcwd() を返すことを確認
        import os

        assert result == os.getcwd()

    def test_empty_cwd_falls_back(self, monkeypatch: object) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/fallback")  # type: ignore[attr-defined]
        result = get_project_dir({"cwd": ""})
        assert result == "/fallback"


# ---------------------------------------------------------------------------
# MAX_MODIFIED_FILES 上限
# ---------------------------------------------------------------------------


class TestModifiedFilesLimit:
    def test_limits_to_max_modified_files(self, tmp_path: Path) -> None:
        # MAX_MODIFIED_FILES を超えるファイルを追加
        files = [f"src/file_{i}.py" for i in range(MAX_MODIFIED_FILES + 20)]
        update_working_context(str(tmp_path), {"modified_files": files})

        ctx_path = tmp_path / ".claude" / "context" / "shared" / "working-context.json"
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        assert len(ctx["modified_files"]) == MAX_MODIFIED_FILES
        # 最新のファイルが保持される（末尾を切り取る）
        assert ctx["modified_files"][-1] == f"src/file_{MAX_MODIFIED_FILES + 19}.py"

    def test_incremental_additions_respect_limit(self, tmp_path: Path) -> None:
        # まず MAX_MODIFIED_FILES - 5 件追加
        initial = [f"src/old_{i}.py" for i in range(MAX_MODIFIED_FILES - 5)]
        update_working_context(str(tmp_path), {"modified_files": initial})

        # さらに 10 件追加（合計 MAX_MODIFIED_FILES + 5 → 上限で切り捨て）
        additional = [f"src/new_{i}.py" for i in range(10)]
        update_working_context(str(tmp_path), {"modified_files": additional})

        ctx_path = tmp_path / ".claude" / "context" / "shared" / "working-context.json"
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        assert len(ctx["modified_files"]) == MAX_MODIFIED_FILES


# ---------------------------------------------------------------------------
# cleanup_session — ロックファイル削除
# ---------------------------------------------------------------------------


class TestCleanupSessionLockFile:
    def test_removes_lock_file(self, tmp_path: Path) -> None:
        # working-context を更新するとロックファイルが作られる
        update_working_context(str(tmp_path), {"current_phase": "test"})
        lock_path = tmp_path / ".claude" / "context" / "shared" / "working-context.json.lock"
        assert lock_path.is_file()

        cleanup_session(str(tmp_path))
        assert not lock_path.exists()

    def test_handles_missing_lock_file(self, tmp_path: Path) -> None:
        # ロックファイルがなくても例外にならない
        init_context_dir(str(tmp_path))
        cleanup_session(str(tmp_path))
        # 例外なく完了すればOK


# ---------------------------------------------------------------------------
# update_working_context — 非リスト既存値の上書き
# ---------------------------------------------------------------------------


class TestUpdateWorkingContextEdgeCases:
    def test_overwrites_non_list_modified_files(self, tmp_path: Path) -> None:
        """modified_files が文字列等で壊れていてもリストとして復旧する。"""
        shared_dir = tmp_path / ".claude" / "context" / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        ctx_path = shared_dir / "working-context.json"
        ctx_path.write_text(
            json.dumps({"modified_files": "not-a-list", "updated_at": "old"}),
            encoding="utf-8",
        )

        update_working_context(str(tmp_path), {"modified_files": ["new.py"]})

        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        assert ctx["modified_files"] == ["new.py"]

    def test_preserves_other_keys_when_adding_files(self, tmp_path: Path) -> None:
        update_working_context(str(tmp_path), {"current_phase": "design"})
        update_working_context(str(tmp_path), {"modified_files": ["a.py"]})

        ctx_path = tmp_path / ".claude" / "context" / "shared" / "working-context.json"
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        assert ctx["current_phase"] == "design"
        assert "a.py" in ctx["modified_files"]
