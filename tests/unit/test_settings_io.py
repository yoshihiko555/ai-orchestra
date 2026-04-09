"""settings_io.py のユニットテスト。"""

from __future__ import annotations

import json

from tests.module_loader import load_module

settings_io = load_module("settings_io", "scripts/lib/settings_io.py")


class TestLoadSettings:
    """load_settings のテスト。"""

    def test_file_not_found(self, tmp_path):
        """ファイルが存在しない場合、デフォルト値を返す。"""
        result = settings_io.load_settings(tmp_path)
        assert result == {"hooks": {}}

    def test_valid_json(self, tmp_path):
        """正常な JSON を読み込める。"""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.local.json"
        data = {"hooks": {"PreToolUse": []}, "custom": "value"}
        settings_path.write_text(json.dumps(data), encoding="utf-8")

        result = settings_io.load_settings(tmp_path)
        assert result == data

    def test_invalid_json(self, tmp_path):
        """壊れた JSON の場合、デフォルト値を返す。"""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.local.json"
        settings_path.write_text("{invalid json}", encoding="utf-8")

        result = settings_io.load_settings(tmp_path)
        assert result == {"hooks": {}}

    def test_empty_file(self, tmp_path):
        """空ファイルの場合、デフォルト値を返す（JSONDecodeError）。"""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.local.json"
        settings_path.write_text("", encoding="utf-8")

        result = settings_io.load_settings(tmp_path)
        assert result == {"hooks": {}}


class TestSaveSettings:
    """save_settings のテスト。"""

    def test_creates_parent_dirs(self, tmp_path):
        """親ディレクトリを自動作成する。"""
        data = {"hooks": {"PreToolUse": []}}
        settings_io.save_settings(tmp_path, data)

        settings_path = tmp_path / ".claude" / "settings.local.json"
        assert settings_path.exists()
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        assert loaded == data

    def test_overwrites_existing(self, tmp_path):
        """既存ファイルを上書きする。"""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.local.json"
        settings_path.write_text('{"old": true}', encoding="utf-8")

        new_data = {"hooks": {}, "new": True}
        settings_io.save_settings(tmp_path, new_data)

        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        assert loaded == new_data

    def test_trailing_newline(self, tmp_path):
        """ファイル末尾に改行がある。"""
        settings_io.save_settings(tmp_path, {"hooks": {}})
        settings_path = tmp_path / ".claude" / "settings.local.json"
        content = settings_path.read_text(encoding="utf-8")
        assert content.endswith("\n")


class TestLoadOrchestraJson:
    """load_orchestra_json のテスト。"""

    def test_file_not_found(self, tmp_path):
        """ファイルが存在しない場合、デフォルト値を返す。"""
        result = settings_io.load_orchestra_json(tmp_path)
        assert result == {"installed_packages": [], "orchestra_dir": "", "last_sync": ""}

    def test_valid_json(self, tmp_path):
        """正常な JSON を読み込める。"""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        path = claude_dir / "orchestra.json"
        data = {"installed_packages": ["core"], "orchestra_dir": "/tmp", "last_sync": "2026-01-01"}
        path.write_text(json.dumps(data), encoding="utf-8")

        result = settings_io.load_orchestra_json(tmp_path)
        assert result == data

    def test_invalid_json(self, tmp_path):
        """壊れた JSON の場合、デフォルト値を返す。"""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        path = claude_dir / "orchestra.json"
        path.write_text("not json", encoding="utf-8")

        result = settings_io.load_orchestra_json(tmp_path)
        assert result == {"installed_packages": [], "orchestra_dir": "", "last_sync": ""}


class TestSaveOrchestraJson:
    """save_orchestra_json のテスト。"""

    def test_creates_and_writes(self, tmp_path):
        """ファイルを作成して書き込む。"""
        data = {"installed_packages": ["core", "agent-routing"], "orchestra_dir": "/home/test"}
        settings_io.save_orchestra_json(tmp_path, data)

        path = tmp_path / ".claude" / "orchestra.json"
        assert path.exists()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == data

    def test_trailing_newline(self, tmp_path):
        """ファイル末尾に改行がある。"""
        settings_io.save_orchestra_json(tmp_path, {"test": True})
        path = tmp_path / ".claude" / "orchestra.json"
        content = path.read_text(encoding="utf-8")
        assert content.endswith("\n")
