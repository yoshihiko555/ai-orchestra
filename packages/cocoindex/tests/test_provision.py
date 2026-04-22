"""provision-mcp-servers.py のテスト。

テスト対象:
- Claude Code (.mcp.json) へのプロビジョニング・クリーンアップ
- Codex CLI (.codex/config.toml) へのプロビジョニング・クリーンアップ
- Gemini CLI (.gemini/settings.json) へのプロビジョニング・クリーンアップ
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
        "gemini": {"enabled": True},
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
# Gemini CLI (.gemini/settings.json)
# =========================================================================


class TestProvisionGemini:
    def test_adds_entry(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))

        result = provision.provision_gemini(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result == "gemini"

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

        provision.provision_gemini(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)

        data = json.loads(settings_path.read_text())
        assert data["model"]["name"] == "gemini-2.5-pro"
        assert "other" in data["mcpServers"]

    def test_idempotent(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))

        provision.provision_gemini(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        result = provision.provision_gemini(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None

    def test_skips_when_file_missing(self, tmp_path: Path) -> None:
        result = provision.provision_gemini(str(tmp_path), SAMPLE_CONFIG, SERVER_NAME)
        assert result is None


class TestCleanupGemini:
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

        result = provision.cleanup_gemini(str(tmp_path), SERVER_NAME)
        assert result == "gemini"

        data = json.loads(settings_path.read_text())
        assert "mcpServers" not in data
        assert data["model"]["name"] == "gemini-2.5-pro"

    def test_noop_when_not_present(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {}}))

        result = provision.cleanup_gemini(str(tmp_path), SERVER_NAME)
        assert result is None


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
        "gemini": {"enabled": True, "force_stdio": False},
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

    def test_gemini_proxy_entry(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))

        provision.provision_gemini(str(tmp_path), SAMPLE_CONFIG_V2, SERVER_NAME, proxy_enabled=True)

        data = json.loads(settings_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["url"] == "http://127.0.0.1:8792/sse"
        assert "command" not in entry

    def test_gemini_force_stdio(self, tmp_path: Path) -> None:
        config = {
            **SAMPLE_CONFIG_V2,
            "targets": {
                **SAMPLE_CONFIG_V2["targets"],
                "gemini": {"enabled": True, "force_stdio": True},
            },
        }
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))

        provision.provision_gemini(str(tmp_path), config, SERVER_NAME, proxy_enabled=True)

        data = json.loads(settings_path.read_text())
        entry = data["mcpServers"]["cocoindex-code"]
        assert entry["command"] == "uvx"
        assert "url" not in entry

    # --- proxy_enabled=False → 従来の stdio ---

    def test_v2_config_with_proxy_inactive(self, tmp_path: Path) -> None:
        """proxy_enabled=False なら v2 config でも stdio エントリを生成する。"""
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("{}")

        provision.provision_claude(str(tmp_path), SAMPLE_CONFIG_V2, SERVER_NAME, proxy_enabled=False)

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
