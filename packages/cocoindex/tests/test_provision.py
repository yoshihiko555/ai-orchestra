"""provision-mcp-servers.py のテスト。

テスト対象:
- Claude Code (.mcp.json) へのプロビジョニング・クリーンアップ
- Codex CLI (.codex/config.toml) へのプロビジョニング・クリーンアップ
- Antigravity CLI (.gemini/settings.json) へのプロビジョニング・クリーンアップ
- 冪等性（同一入力で再実行しても変更なし）
- TOML セクション検出（行走査方式）
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

from tests.module_loader import REPO_ROOT, load_module

# hook_common を先に読み込む（provision が import するため）
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))

provision = load_module(
    "provision_mcp_servers",
    "packages/cocoindex/hooks/provision-mcp-servers.py",
)

# テスト用の共通 config
SAMPLE_CONFIG: dict = {
    "enabled": True,
    "server_name": "cocoindex-code",
    "command": "uvx",
    "args": ["--prerelease=explicit", "--with", "cocoindex>=1.0.0a16", "cocoindex-code@latest"],
    "targets": {
        "claude": {"enabled": True, "type": "stdio"},
        "codex": {"enabled": True},
        "antigravity": {"enabled": True},
    },
}

SERVER_NAME = "cocoindex-code"


# =========================================================================
# Claude Code (.mcp.json)
# =========================================================================


class TestProvisionClaude:
    def test_creates_entry_in_empty_mcp_json(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("{}")

        result = provision.provision_claude(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result == "claude"

        data = json.loads(mcp_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["command"] == "uvx"
        assert entry["args"] == SAMPLE_CONFIG["args"]
        assert entry["type"] == "stdio"

    def test_preserves_existing_servers(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(
            json.dumps({"mcpServers": {"other-server": {"command": "node", "args": ["server.js"]}}})
        )

        provision.provision_claude(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)

        data = json.loads(mcp_path.read_text())
        assert "other-server" in data["mcpServers"]
        assert "cocoindex-code" in data["mcpServers"]

    def test_idempotent(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("{}")

        provision.provision_claude(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)

        result = provision.provision_claude(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None  # 変更なし

    def test_creates_file_if_not_exists(self, tmp_path: Path) -> None:
        """Claude Code の .mcp.json はファイルが存在しなくても作成する。"""
        result = provision.provision_claude(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result == "claude"
        mcp_path = tmp_path / ".mcp.json"
        assert mcp_path.exists()

    def test_skips_corrupted_json_without_overwriting(self, tmp_path: Path, capsys) -> None:
        """構文エラーのある .mcp.json は上書きせず警告を出してスキップする。"""
        mcp_path = tmp_path / ".mcp.json"
        broken_content = '{"mcpServers": {"broken": '
        mcp_path.write_text(broken_content)

        result = provision.provision_claude(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None
        assert mcp_path.read_text() == broken_content

        stderr = capsys.readouterr().err
        assert "not valid JSON" in stderr

    def test_treats_empty_file_as_fresh(self, tmp_path: Path) -> None:
        """0 バイトのファイルは破損ではなく空の状態として扱う。"""
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("")

        result = provision.provision_claude(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result == "claude"

        data = json.loads(mcp_path.read_text())
        assert "cocoindex-code" in data["mcpServers"]


class TestCleanupClaude:
    def test_removes_entry(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "cocoindex-code": {"command": "uvx", "args": []},
                        "other": {"command": "node", "args": []},
                    }
                }
            )
        )

        result = provision.cleanup_claude(str(tmp_path), SERVER_NAME)
        assert result == "claude"

        data = json.loads(mcp_path.read_text())
        assert "cocoindex-code" not in data["mcpServers"]
        assert "other" in data["mcpServers"]

    def test_deletes_file_when_empty(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(
            json.dumps({"mcpServers": {"cocoindex-code": {"command": "uvx", "args": []}}})
        )

        provision.cleanup_claude(str(tmp_path), SERVER_NAME)
        assert not mcp_path.exists()

    def test_noop_when_not_present(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(json.dumps({"mcpServers": {"other": {}}}))

        result = provision.cleanup_claude(str(tmp_path), SERVER_NAME)
        assert result is None

    def test_noop_when_file_missing(self, tmp_path: Path) -> None:
        result = provision.cleanup_claude(str(tmp_path), SERVER_NAME)
        assert result is None

    def test_skips_corrupted_json_without_overwriting(self, tmp_path: Path, capsys) -> None:
        """構文エラーのある .mcp.json は上書きせず警告を出してスキップする。"""
        mcp_path = tmp_path / ".mcp.json"
        broken_content = '{"mcpServers": {"cocoindex-code": '
        mcp_path.write_text(broken_content)

        result = provision.cleanup_claude(str(tmp_path), SERVER_NAME)
        assert result is None
        assert mcp_path.read_text() == broken_content

        stderr = capsys.readouterr().err
        assert "not valid JSON" in stderr


# =========================================================================
# Codex CLI (.codex/config.toml)
# =========================================================================


CODEX_BASE_TOML = """\
model = "gpt-5.3-codex"
approval_policy = "on-request"

[features]
skills = true
"""


class TestProvisionCodex:
    def test_appends_section(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(CODEX_BASE_TOML)

        result = provision.provision_codex(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result == "codex"

        content = toml_path.read_text()
        assert "[mcp_servers.cocoindex-code]" in content
        assert 'command = "uvx"' in content
        assert "enabled = true" in content

    def test_updates_existing_section(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(
            CODEX_BASE_TOML
            + "\n[mcp_servers.cocoindex-code]\n"
            + 'command = "old-cmd"\n'
            + 'args = ["old"]\n'
            + "enabled = true\n"
        )

        result = provision.provision_codex(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result == "codex"

        content = toml_path.read_text()
        assert 'command = "uvx"' in content
        assert "old-cmd" not in content

    def test_idempotent(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(CODEX_BASE_TOML)

        provision.provision_codex(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        result = provision.provision_codex(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None

    def test_skips_when_file_missing(self, tmp_path: Path) -> None:
        result = provision.provision_codex(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None


class TestCleanupCodex:
    def test_removes_section(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(
            CODEX_BASE_TOML
            + "\n[mcp_servers.cocoindex-code]\n"
            + 'command = "uvx"\n'
            + "args = []\n"
            + "enabled = true\n"
        )

        result = provision.cleanup_codex(str(tmp_path), SERVER_NAME)
        assert result == "codex"

        content = toml_path.read_text()
        assert "cocoindex-code" not in content
        assert "model" in content  # 他の設定は残る

    def test_noop_when_not_present(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(CODEX_BASE_TOML)

        result = provision.cleanup_codex(str(tmp_path), SERVER_NAME)
        assert result is None


# =========================================================================
# Antigravity CLI (.gemini/settings.json)
# =========================================================================


class TestProvisionAntigravity:
    def test_adds_entry(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))

        result = provision.provision_antigravity(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result == "antigravity"

        data = json.loads(settings_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["command"] == "uvx"
        assert entry["args"] == SAMPLE_CONFIG["args"]

    def test_preserves_existing_settings(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "model": {"name": "gemini-2.5-pro"},
                    "mcpServers": {"other": {"command": "node"}},
                }
            )
        )

        provision.provision_antigravity(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)

        data = json.loads(settings_path.read_text())
        assert data["model"]["name"] == "gemini-2.5-pro"
        assert "other" in data["mcpServers"]

    def test_idempotent(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))

        provision.provision_antigravity(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        result = provision.provision_antigravity(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None

    def test_skips_when_file_missing(self, tmp_path: Path) -> None:
        result = provision.provision_antigravity(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None

    def test_skips_corrupted_json_without_overwriting(self, tmp_path: Path, capsys) -> None:
        """構文エラーのある settings.json は上書きせず警告を出してスキップする。"""
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        broken_content = '{"model": {'
        settings_path.write_text(broken_content)

        result = provision.provision_antigravity(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None
        assert settings_path.read_text() == broken_content

        stderr = capsys.readouterr().err
        assert "not valid JSON" in stderr


class TestCleanupAntigravity:
    def test_removes_entry(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "model": {"name": "gemini-2.5-pro"},
                    "mcpServers": {"cocoindex-code": {"command": "uvx", "args": []}},
                }
            )
        )

        result = provision.cleanup_antigravity(str(tmp_path), SERVER_NAME)
        assert result == "antigravity"

        data = json.loads(settings_path.read_text())
        assert "mcpServers" not in data
        assert data["model"]["name"] == "gemini-2.5-pro"

    def test_noop_when_not_present(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {}}))

        result = provision.cleanup_antigravity(str(tmp_path), SERVER_NAME)
        assert result is None

    def test_skips_corrupted_json_without_overwriting(self, tmp_path: Path, capsys) -> None:
        """構文エラーのある settings.json は上書きせず警告を出してスキップする。"""
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        broken_content = '{"mcpServers": {"cocoindex-code": '
        settings_path.write_text(broken_content)

        result = provision.cleanup_antigravity(str(tmp_path), SERVER_NAME)
        assert result is None
        assert settings_path.read_text() == broken_content

        stderr = capsys.readouterr().err
        assert "not valid JSON" in stderr


# =========================================================================
# _read_json_or_none
# =========================================================================


class TestReadJsonOrNone:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "does-not-exist.json"
        assert provision._read_json_or_none(str(path)) == {}

    def test_empty_file_returns_empty_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.json"
        path.write_text("")
        assert provision._read_json_or_none(str(path)) == {}

    def test_whitespace_only_file_returns_empty_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "whitespace.json"
        path.write_text("   \n")
        assert provision._read_json_or_none(str(path)) == {}

    def test_valid_empty_object_returns_empty_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "valid.json"
        path.write_text("{}")
        assert provision._read_json_or_none(str(path)) == {}

    def test_valid_object_returns_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "valid.json"
        path.write_text(json.dumps({"a": 1}))
        assert provision._read_json_or_none(str(path)) == {"a": 1}

    def test_broken_syntax_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text('{"a": ')
        assert provision._read_json_or_none(str(path)) is None

    def test_non_object_json_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "array.json"
        path.write_text(json.dumps([1, 2, 3]))
        assert provision._read_json_or_none(str(path)) is None


# =========================================================================
# TOML 文字列エスケープ
# =========================================================================


class TestTomlEscape:
    def test_escapes_quotes_and_backslashes(self) -> None:
        assert provision._toml_escape('say "hi"') == 'say \\"hi\\"'
        assert provision._toml_escape("C:\\path\\to\\bin") == "C:\\\\path\\\\to\\\\bin"

    def test_no_special_characters_unchanged(self) -> None:
        assert provision._toml_escape("uvx") == "uvx"

    def test_build_toml_section_escapes_command(self) -> None:
        config = {
            **SAMPLE_CONFIG,
            "command": 'uvx "weird" \\value',
        }
        section = provision._build_toml_section(SERVER_NAME, config, False, "/tmp/project")
        assert 'command = "uvx \\"weird\\" \\\\value"' in section

    def test_provision_codex_writes_valid_toml_with_special_chars(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(CODEX_BASE_TOML)

        config = {
            **SAMPLE_CONFIG,
            "command": 'uvx "weird" \\value',
        }
        result = provision.provision_codex(str(tmp_path), config, SERVER_NAME)
        assert result == "codex"

        content = toml_path.read_text()
        assert 'command = "uvx \\"weird\\" \\\\value"' in content

        # tomllib で妥当な TOML としてパースできることを確認する
        import tomllib

        parsed = tomllib.loads(content)
        assert parsed["mcp_servers"]["cocoindex-code"]["command"] == 'uvx "weird" \\value'


# =========================================================================
# TOML セクション検出
# =========================================================================


class TestFindTomlSection:
    def test_finds_section(self) -> None:
        content = '[top]\nkey = 1\n\n[mcp_servers.foo]\ncmd = "bar"\n\n[other]\nx = 1\n'
        span = provision._find_toml_section(content, "mcp_servers.foo")
        assert span is not None
        lines = content.splitlines()
        section = "\n".join(lines[span[0] : span[1]])
        assert "[mcp_servers.foo]" in section
        assert 'cmd = "bar"' in section
        assert "[other]" not in section

    def test_finds_last_section(self) -> None:
        content = '[top]\nkey = 1\n\n[mcp_servers.foo]\ncmd = "bar"\n'
        span = provision._find_toml_section(content, "mcp_servers.foo")
        assert span is not None
        assert span[1] == len(content.splitlines())

    def test_returns_none_when_not_found(self) -> None:
        content = "[top]\nkey = 1\n"
        result = provision._find_toml_section(content, "mcp_servers.foo")
        assert result is None

    def test_handles_empty_content(self) -> None:
        assert provision._find_toml_section("", "mcp_servers.foo") is None


# =========================================================================
# v2: proxy モードのエントリ形式
# =========================================================================

SAMPLE_CONFIG_V2: dict = {
    "enabled": True,
    "server_name": "cocoindex-code",
    "command": "uvx",
    "args": ["--prerelease=explicit", "--with", "cocoindex>=1.0.0a16", "cocoindex-code@latest"],
    "targets": {
        "claude": {"enabled": True, "type": "stdio", "force_stdio": False},
        "codex": {"enabled": True, "force_stdio": False},
        "antigravity": {"enabled": True, "force_stdio": False},
    },
    "proxy": {
        "enabled": True,
        "port": 8792,
        "port_range": 0,
        "host": "127.0.0.1",
        "pid_file": ".claude/.mcp-proxy.pid",
        "startup_timeout": 10,
    },
}

PROXY_CFG = SAMPLE_CONFIG_V2["proxy"]


class TestProxyModeEntries:
    """proxy_enabled=True 時の各 CLI エントリ形式テスト。"""

    # --- Claude Code: SSE ---

    def test_claude_proxy_entry(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("{}")

        provision.provision_claude(str(tmp_path), SAMPLE_CONFIG_V2, SERVER_NAME, proxy_enabled=True)

        data = json.loads(mcp_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["type"] == "sse"
        assert entry["url"] == "http://127.0.0.1:8792/sse"
        assert "command" not in entry

    def test_claude_force_stdio(self, tmp_path: Path) -> None:
        config = {
            **SAMPLE_CONFIG_V2,
            "targets": {
                **SAMPLE_CONFIG_V2["targets"],
                "claude": {"enabled": True, "type": "stdio", "force_stdio": True},
            },
        }
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("{}")

        provision.provision_claude(str(tmp_path), config, SERVER_NAME, proxy_enabled=True)

        data = json.loads(mcp_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["command"] == "uvx"
        assert entry["type"] == "stdio"
        assert "url" not in entry

    # --- Codex CLI: streamable-http ---

    def test_codex_proxy_entry(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(CODEX_BASE_TOML)

        provision.provision_codex(str(tmp_path), SAMPLE_CONFIG_V2, SERVER_NAME, proxy_enabled=True)

        content = toml_path.read_text()
        assert 'url = "http://127.0.0.1:8792/mcp"' in content
        assert "command" not in content.split("[mcp_servers.cocoindex-code]")[1]

    def test_codex_force_stdio(self, tmp_path: Path) -> None:
        config = {
            **SAMPLE_CONFIG_V2,
            "targets": {
                **SAMPLE_CONFIG_V2["targets"],
                "codex": {"enabled": True, "force_stdio": True},
            },
        }
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(CODEX_BASE_TOML)

        provision.provision_codex(str(tmp_path), config, SERVER_NAME, proxy_enabled=True)

        content = toml_path.read_text()
        assert 'command = "uvx"' in content
        assert "url" not in content.split("[mcp_servers.cocoindex-code]")[1]

    # --- Gemini CLI: SSE ---

    def test_antigravity_proxy_entry(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))

        provision.provision_antigravity(
            str(tmp_path), SAMPLE_CONFIG_V2, SERVER_NAME, proxy_enabled=True
        )

        data = json.loads(settings_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["url"] == "http://127.0.0.1:8792/sse"
        assert "command" not in entry

    def test_antigravity_force_stdio(self, tmp_path: Path) -> None:
        config = {
            **SAMPLE_CONFIG_V2,
            "targets": {
                **SAMPLE_CONFIG_V2["targets"],
                "antigravity": {"enabled": True, "force_stdio": True},
            },
        }
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))

        provision.provision_antigravity(str(tmp_path), config, SERVER_NAME, proxy_enabled=True)

        data = json.loads(settings_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["command"] == "uvx"
        assert "url" not in entry

    # --- proxy_enabled=False → 従来の stdio ---

    def test_v2_config_with_proxy_inactive(self, tmp_path: Path) -> None:
        """proxy_enabled=False なら v2 config でも stdio エントリを生成する。"""
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("{}")

        provision.provision_claude(
            str(tmp_path), SAMPLE_CONFIG_V2, SERVER_NAME, proxy_enabled=False
        )

        data = json.loads(mcp_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["command"] == "uvx"
        assert entry["type"] == "stdio"


class TestMain:
    def _invoke(self, payload: dict, monkeypatch) -> str:
        buffer = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "stdout", buffer)
        provision.main()
        return buffer.getvalue()

    def test_proxy_mode_creates_session_state_and_starts_warmup(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project_dir = tmp_path
        (project_dir / ".claude" / "config" / "cocoindex").mkdir(parents=True)
        (project_dir / ".claude" / "config" / "cocoindex" / "cocoindex.yaml").write_text(
            json.dumps(SAMPLE_CONFIG_V2)
        )

        monkeypatch.setattr(provision, "load_package_config", lambda *_: SAMPLE_CONFIG_V2)
        monkeypatch.setattr(
            provision,
            "get_proxy_state",
            lambda *_: {"proxy_state": "stopped"},
        )
        start_mock = MagicMock(return_value=True)
        monkeypatch.setattr(provision, "start_proxy_background", start_mock)

        output = self._invoke(
            {"cwd": str(project_dir), "session_id": "sess-1"},
            monkeypatch,
        )

        session_state = json.loads(
            (project_dir / ".claude" / "state" / "cocoindex-sessions" / "sess-1.json").read_text()
        )
        assert session_state["reconnect_required"] is True
        start_mock.assert_called_once_with(SAMPLE_CONFIG_V2, str(project_dir))
        assert "falling back to stdio" not in output
        assert "warmup started" in output

    def test_proxy_ready_session_does_not_start_warmup(self, tmp_path: Path, monkeypatch) -> None:
        project_dir = tmp_path
        monkeypatch.setattr(provision, "load_package_config", lambda *_: SAMPLE_CONFIG_V2)
        monkeypatch.setattr(
            provision,
            "get_proxy_state",
            lambda *_: {"proxy_state": "ready"},
        )
        start_mock = MagicMock(return_value=False)
        monkeypatch.setattr(provision, "start_proxy_background", start_mock)

        self._invoke({"cwd": str(project_dir), "session_id": "sess-2"}, monkeypatch)

        session_state = json.loads(
            (project_dir / ".claude" / "state" / "cocoindex-sessions" / "sess-2.json").read_text()
        )
        assert session_state["reconnect_required"] is False
        start_mock.assert_not_called()

    def test_proxy_idle_session_does_not_start_warmup(self, tmp_path: Path, monkeypatch) -> None:
        project_dir = tmp_path
        monkeypatch.setattr(provision, "load_package_config", lambda *_: SAMPLE_CONFIG_V2)
        monkeypatch.setattr(
            provision,
            "get_proxy_state",
            lambda *_: {"proxy_state": "idle"},
        )
        start_mock = MagicMock(return_value=False)
        monkeypatch.setattr(provision, "start_proxy_background", start_mock)

        self._invoke({"cwd": str(project_dir), "session_id": "sess-idle"}, monkeypatch)

        session_state = json.loads(
            (
                project_dir / ".claude" / "state" / "cocoindex-sessions" / "sess-idle.json"
            ).read_text()
        )
        assert session_state["reconnect_required"] is False
        start_mock.assert_not_called()

    def test_claude_force_stdio_session_skips_reconnect_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project_dir = tmp_path
        config = {
            **SAMPLE_CONFIG_V2,
            "targets": {
                **SAMPLE_CONFIG_V2["targets"],
                "claude": {"enabled": True, "type": "stdio", "force_stdio": True},
            },
        }
        monkeypatch.setattr(provision, "load_package_config", lambda *_: config)
        monkeypatch.setattr(
            provision,
            "get_proxy_state",
            lambda *_: {"proxy_state": "stopped"},
        )
        start_mock = MagicMock(return_value=True)
        monkeypatch.setattr(provision, "start_proxy_background", start_mock)

        self._invoke({"cwd": str(project_dir), "session_id": "sess-force-stdio"}, monkeypatch)

        session_state_path = (
            project_dir / ".claude" / "state" / "cocoindex-sessions" / "sess-force-stdio.json"
        )
        assert not session_state_path.exists()
        start_mock.assert_called_once_with(config, str(project_dir))

    def test_proxy_failed_state_with_occupied_port_falls_back_to_stdio(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """別プロセスがポートを占有する場合は乗っ取らず stdio へフォールバックする。"""
        project_dir = tmp_path
        mcp_path = project_dir / ".mcp.json"
        mcp_path.write_text("{}")

        monkeypatch.setattr(provision, "load_package_config", lambda *_: SAMPLE_CONFIG_V2)
        monkeypatch.setattr(
            provision,
            "get_proxy_state",
            lambda *_: {"proxy_state": "failed"},
        )
        start_mock = MagicMock(return_value=True)
        monkeypatch.setattr(provision, "start_proxy_background", start_mock)
        monkeypatch.setattr(provision, "is_proxy_port_free", lambda *_: False)

        output = self._invoke(
            {"cwd": str(project_dir), "session_id": "sess-failed"},
            monkeypatch,
        )

        assert "falling back to stdio" in output
        # 自動再起動は行わない
        start_mock.assert_not_called()
        # session state も proxy 未使用として扱われる（reconnect 不要）
        session_state_path = (
            project_dir / ".claude" / "state" / "cocoindex-sessions" / "sess-failed.json"
        )
        assert not session_state_path.exists()

        data = json.loads(mcp_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["command"] == "uvx"
        assert "url" not in entry
        assert entry.get("type") != "sse"

    def test_proxy_failed_state_with_free_port_retries_via_warmup(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project_dir = tmp_path
        mcp_path = project_dir / ".mcp.json"
        mcp_path.write_text("{}")

        monkeypatch.setattr(provision, "load_package_config", lambda *_: SAMPLE_CONFIG_V2)
        monkeypatch.setattr(
            provision,
            "get_proxy_state",
            lambda *_: {"proxy_state": "failed"},
        )
        monkeypatch.setattr(provision, "is_proxy_port_free", lambda *_: True)
        start_mock = MagicMock(return_value=True)
        monkeypatch.setattr(provision, "start_proxy_background", start_mock)

        output = self._invoke(
            {"cwd": str(project_dir), "session_id": "sess-retry"},
            monkeypatch,
        )

        assert "falling back to stdio" not in output
        assert "warmup started" in output
        start_mock.assert_called_once_with(SAMPLE_CONFIG_V2, str(project_dir))

        session_state = json.loads(
            (
                project_dir / ".claude" / "state" / "cocoindex-sessions" / "sess-retry.json"
            ).read_text()
        )
        assert session_state["reconnect_required"] is True

        data = json.loads(mcp_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["type"] == "sse"


class TestMainReconcileIntegration:
    """main() を通しての reconcile / クリーンアップ統合テスト。

    対象観点（docs/evaluation/cocoindex.md）:
    - EV-04（must）: `enabled: false` で 3 CLI 全エントリ削除（クリーンアップ）
    - EV-05（must）: `targets.<cli>.enabled: false` で該当 CLI のみ削除、他は不変
    - EV-06（must）: 旧 `targets.gemini`（`.local.yaml` 残存）読み替えの end-to-end 検証
    """

    def _invoke(self, payload: dict, monkeypatch) -> str:
        buffer = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "stdout", buffer)
        provision.main()
        return buffer.getvalue()

    def test_enabled_false_removes_all_three_cli_entries_and_preserves_unrelated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """EV-04: enabled=false は main() 経由で 3 CLI 全エントリを削除し、
        cocoindex 以外の既存エントリは削除しない。
        """
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(
            json.dumps(
                {"mcpServers": {SERVER_NAME: {"command": "uvx"}, "other-server": {"command": "x"}}}
            )
        )
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(
            '[mcp_servers.cocoindex-code]\ncommand = "uvx"\nargs = []\nenabled = true\n\n'
            '[mcp_servers.other]\ncommand = "y"\nargs = []\nenabled = true\n'
        )
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {"mcpServers": {SERVER_NAME: {"command": "uvx"}, "other-server": {"command": "z"}}}
            )
        )

        disabled_config = {**SAMPLE_CONFIG, "enabled": False}
        monkeypatch.setattr(provision, "load_package_config", lambda *_: disabled_config)

        output = self._invoke({"cwd": str(tmp_path), "session_id": "sess-disable-all"}, monkeypatch)

        mcp_data = json.loads(mcp_path.read_text())
        assert SERVER_NAME not in mcp_data.get("mcpServers", {})
        assert "other-server" in mcp_data["mcpServers"]

        toml_content = toml_path.read_text()
        assert "[mcp_servers.cocoindex-code]" not in toml_content
        assert "[mcp_servers.other]" in toml_content

        settings_data = json.loads(settings_path.read_text())
        assert SERVER_NAME not in settings_data.get("mcpServers", {})
        assert "other-server" in settings_data["mcpServers"]

        # cleanup 側でも changed 扱いになるため claude/codex/antigravity 全てが報告される
        assert "claude" in output
        assert "codex" in output
        assert "antigravity" in output

    def test_target_level_disable_only_removes_that_cli(self, tmp_path: Path, monkeypatch) -> None:
        """EV-05: targets.<cli>.enabled=false は該当 CLI のみ削除し、
        他の CLI はプロビジョニングされたまま変わらない。
        """
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(
            json.dumps(
                {"mcpServers": {SERVER_NAME: {"command": "uvx"}, "other-server": {"command": "x"}}}
            )
        )
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        toml_path = codex_dir / "config.toml"
        toml_path.write_text("")
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text("{}")

        config = {
            **SAMPLE_CONFIG,
            "targets": {
                "claude": {"enabled": False, "type": "stdio"},
                "codex": {"enabled": True},
                "antigravity": {"enabled": True},
            },
        }
        monkeypatch.setattr(provision, "load_package_config", lambda *_: config)

        self._invoke({"cwd": str(tmp_path), "session_id": "sess-target-disable"}, monkeypatch)

        # claude だけエントリが消え、無関係な既存エントリは残る（他は不変）
        mcp_data = json.loads(mcp_path.read_text())
        assert SERVER_NAME not in mcp_data.get("mcpServers", {})
        assert "other-server" in mcp_data["mcpServers"]

        # codex / antigravity は provision される（他は不変どころか正しく反映される）
        assert "[mcp_servers.cocoindex-code]" in toml_path.read_text()
        settings_data = json.loads(settings_path.read_text())
        assert SERVER_NAME in settings_data["mcpServers"]

    def test_legacy_gemini_local_yaml_disables_antigravity_end_to_end(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """EV-06: `.local.yaml` に残存する旧 targets.gemini.enabled=false が
        実際の load_package_config によるマージを経て antigravity を無効化する。
        """
        config_dir = tmp_path / ".claude" / "config" / "cocoindex"
        config_dir.mkdir(parents=True)
        (config_dir / "cocoindex.yaml").write_text(
            "enabled: true\n"
            "server_name: cocoindex-code\n"
            "command: uvx\n"
            "args: []\n"
            "targets:\n"
            "  claude:\n"
            "    enabled: true\n"
            "    type: stdio\n"
            "  codex:\n"
            "    enabled: true\n"
            "  antigravity:\n"
            "    enabled: true\n"
        )
        (config_dir / "cocoindex.local.yaml").write_text(
            "targets:\n  gemini:\n    enabled: false\n"
        )

        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {"mcpServers": {SERVER_NAME: {"command": "uvx"}, "other-server": {"command": "z"}}}
            )
        )

        self._invoke({"cwd": str(tmp_path), "session_id": "sess-legacy-gemini"}, monkeypatch)

        # antigravity は旧 targets.gemini 読み替えにより無効化されエントリが消える
        settings_data = json.loads(settings_path.read_text())
        assert SERVER_NAME not in settings_data.get("mcpServers", {})
        assert "other-server" in settings_data["mcpServers"]

        # claude は legacy 読み替えの影響を受けず、通常どおり provision される
        mcp_data = json.loads((tmp_path / ".mcp.json").read_text())
        assert SERVER_NAME in mcp_data["mcpServers"]


class TestNormalizeTargets:
    """旧 targets.gemini（.local.yaml 残存分）の読み替え。"""

    def test_legacy_gemini_disabled_propagates(self) -> None:
        config = {
            "targets": {
                "antigravity": {"enabled": True},
                "gemini": {"enabled": False},
            }
        }
        normalized = provision.normalize_targets(config)
        assert normalized["targets"]["antigravity"]["enabled"] is False

    def test_legacy_gemini_enabled_true_is_ignored(self) -> None:
        config = {
            "targets": {
                "antigravity": {"enabled": True},
                "gemini": {"enabled": True},
            }
        }
        normalized = provision.normalize_targets(config)
        assert normalized["targets"]["antigravity"]["enabled"] is True

    def test_no_legacy_key_is_noop(self) -> None:
        config = {"targets": {"antigravity": {"enabled": True}}}
        assert provision.normalize_targets(config) == config

    def test_does_not_mutate_input(self) -> None:
        config = {
            "targets": {
                "antigravity": {"enabled": True},
                "gemini": {"enabled": False},
            }
        }
        provision.normalize_targets(config)
        assert config["targets"]["antigravity"]["enabled"] is True
