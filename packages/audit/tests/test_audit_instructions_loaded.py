"""audit-instructions-loaded.py のユニットテスト。"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

# AI_ORCHESTRA_DIR は worktree 切り替え時に stale なコピーを指す可能性があるため、
# テスト中は必ず REPO_ROOT で上書きしてから hook を import する（sub-hook 内の
# sys.path 挿入がテスト対象の .py を参照するように強制する）。
os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)

_audit_hooks = str(REPO_ROOT / "packages" / "audit" / "hooks")
_core_hooks = str(REPO_ROOT / "packages" / "core" / "hooks")
for p in [_audit_hooks, _core_hooks]:
    if p not in sys.path:
        sys.path.insert(0, p)

# event_logger を先に load_module しておくと、hook が `from event_logger import`
# したときに同じインスタンスを再利用する（EVENT_TYPES の不一致を防ぐ）。
event_logger = load_module("event_logger", "packages/audit/hooks/event_logger.py")
audit_instructions = load_module(
    "audit_instructions_loaded",
    "packages/audit/hooks/audit-instructions-loaded.py",
)


class TestRelativize:
    """`_relativize` のテスト。"""

    def test_relativizes_path_under_project(self, tmp_path: Path) -> None:
        """project_dir 配下のパスが相対化されることを確認する。"""
        inside = str(tmp_path / "sub" / "rule.md")
        relative = audit_instructions._relativize(inside, str(tmp_path))
        assert relative == os.path.join("sub", "rule.md")

    def test_returns_original_for_empty_path(self) -> None:
        """空文字は空文字のまま返ることを確認する。"""
        assert audit_instructions._relativize("", "/tmp") == ""


class TestBuildPayload:
    """`build_payload` のテスト。"""

    def test_includes_core_fields(self, tmp_path: Path) -> None:
        """load_reason / memory_type が必ず含まれることを確認する。"""
        data = {"load_reason": "session_start", "memory_type": "user"}
        payload = audit_instructions.build_payload(data, str(tmp_path))
        assert payload["load_reason"] == "session_start"
        assert payload["memory_type"] == "user"

    def test_relativizes_all_path_keys(self, tmp_path: Path) -> None:
        """file_path / trigger_file_path / parent_file_path が相対化されることを確認する。"""
        data = {
            "load_reason": "path_glob_match",
            "file_path": str(tmp_path / "a" / "CLAUDE.md"),
            "trigger_file_path": str(tmp_path / "b" / "trigger.py"),
            "parent_file_path": str(tmp_path / "c" / "parent.md"),
        }
        payload = audit_instructions.build_payload(data, str(tmp_path))
        assert payload["file_path"] == os.path.join("a", "CLAUDE.md")
        assert payload["trigger_file_path"] == os.path.join("b", "trigger.py")
        assert payload["parent_file_path"] == os.path.join("c", "parent.md")

    def test_globs_truncated_to_10(self, tmp_path: Path) -> None:
        """globs は 10 件までに制限されることを確認する。"""
        data = {"load_reason": "x", "globs": [f"*.md{i}" for i in range(15)]}
        payload = audit_instructions.build_payload(data, str(tmp_path))
        assert len(payload["globs"]) == 10


class TestMain:
    """`main` のエンドツーエンド動作を確認する。"""

    def _invoke(self, payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        audit_instructions.main()

    def test_writes_event_to_session_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session_id ありの入力でセッションログに書き込まれることを確認する。"""
        project_dir = tmp_path
        (project_dir / ".claude").mkdir()

        payload = {
            "session_id": "sess-a",
            "cwd": str(project_dir),
            "load_reason": "session_start",
            "file_path": str(project_dir / "CLAUDE.md"),
            "memory_type": "project",
        }
        self._invoke(payload, monkeypatch)

        log_path = event_logger.get_session_log_path("sess-a", str(project_dir))
        assert os.path.exists(log_path)
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "instructions_loaded"
        assert record["data"]["load_reason"] == "session_start"
        assert record["data"]["file_path"] == "CLAUDE.md"

    def test_skips_without_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session_id が無い場合はログが出力されないことを確認する。"""
        (tmp_path / ".claude").mkdir()
        payload = {"cwd": str(tmp_path), "load_reason": "session_start"}
        self._invoke(payload, monkeypatch)

        sessions_dir = tmp_path / ".claude" / "logs" / "audit" / "sessions"
        assert not sessions_dir.exists() or not any(sessions_dir.iterdir())

    def test_disabled_flag_skips_logging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """audit-flags.json で features.instructions_loaded.enabled=false のとき書き込まれないことを確認する。"""
        project_dir = tmp_path
        config_dir = project_dir / ".claude" / "config" / "audit"
        config_dir.mkdir(parents=True)
        (config_dir / "audit-flags.json").write_text(
            json.dumps({"features": {"instructions_loaded": {"enabled": False}}})
        )

        payload = {
            "session_id": "sess-b",
            "cwd": str(project_dir),
            "load_reason": "session_start",
        }
        self._invoke(payload, monkeypatch)

        log_path = event_logger.get_session_log_path("sess-b", str(project_dir))
        assert not os.path.exists(log_path)
