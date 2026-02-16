"""config 読み込みユーティリティのテスト。

テスト対象:
- hook_common: deep_merge, find_package_config, _read_config_file, load_package_config
- route_config: load_config (load_package_config への委譲)
- 実ファイル: ai-orchestra 内の config が正しく読めるか
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")

# route_config は hook_common を import するため sys.path を設定
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
route_config = load_module("route_config", "packages/agent-routing/hooks/route_config.py")


# =========================================================================
# deep_merge
# =========================================================================


class TestDeepMerge:
    def test_flat_override(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = hook_common.deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_override(self) -> None:
        base = {"codex": {"model": "gpt-5.3", "sandbox": {"analysis": "read-only"}}}
        override = {"codex": {"model": "o3-pro"}}
        result = hook_common.deep_merge(base, override)
        assert result["codex"]["model"] == "o3-pro"
        assert result["codex"]["sandbox"]["analysis"] == "read-only"

    def test_empty_override(self) -> None:
        base = {"a": 1}
        assert hook_common.deep_merge(base, {}) == {"a": 1}

    def test_empty_base(self) -> None:
        override = {"a": 1}
        assert hook_common.deep_merge({}, override) == {"a": 1}

    def test_does_not_mutate_original(self) -> None:
        base = {"a": {"x": 1}}
        override = {"a": {"y": 2}}
        hook_common.deep_merge(base, override)
        assert base == {"a": {"x": 1}}


# =========================================================================
# find_package_config
# =========================================================================


class TestFindPackageConfig:
    def test_finds_from_orchestra_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        path = hook_common.find_package_config(
            "route-audit", "orchestration-flags.json", "/nonexistent"
        )
        assert path.endswith("packages/route-audit/config/orchestration-flags.json")
        assert os.path.isfile(path)

    def test_finds_from_project_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        config_dir = tmp_path / ".claude" / "config" / "test-pkg"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "test.json"
        config_file.write_text('{"key": "value"}')
        path = hook_common.find_package_config("test-pkg", "test.json", str(tmp_path))
        assert path == str(config_file)

    def test_project_dir_takes_priority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        config_dir = tmp_path / ".claude" / "config" / "route-audit"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "orchestration-flags.json"
        config_file.write_text('{"source": "project"}')
        path = hook_common.find_package_config(
            "route-audit", "orchestration-flags.json", str(tmp_path)
        )
        assert path == str(config_file)

    def test_returns_empty_when_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        path = hook_common.find_package_config("nonexistent", "nope.json", "/nonexistent")
        assert path == ""


# =========================================================================
# _read_config_file
# =========================================================================


class TestReadConfigFile:
    def test_reads_json(self, tmp_path: Path) -> None:
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}')
        assert hook_common._read_config_file(str(f)) == {"key": "value"}

    def test_reads_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "test.yaml"
        f.write_text("key: value\nnested:\n  a: 1\n")
        result = hook_common._read_config_file(str(f))
        assert result == {"key": "value", "nested": {"a": 1}}

    def test_reads_yml(self, tmp_path: Path) -> None:
        f = tmp_path / "test.yml"
        f.write_text("x: 1\n")
        assert hook_common._read_config_file(str(f)) == {"x": 1}

    def test_returns_empty_for_nonexistent(self) -> None:
        assert hook_common._read_config_file("/nonexistent/file.json") == {}

    def test_returns_empty_for_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("{invalid")
        assert hook_common._read_config_file(str(f)) == {}

    def test_returns_empty_for_empty_path(self) -> None:
        assert hook_common._read_config_file("") == {}


# =========================================================================
# load_package_config
# =========================================================================


class TestLoadPackageConfig:
    def test_loads_json_with_local_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        config_dir = tmp_path / ".claude" / "config" / "mypkg"
        config_dir.mkdir(parents=True)
        (config_dir / "settings.json").write_text(json.dumps({"a": 1, "b": {"x": 10, "y": 20}}))
        (config_dir / "settings.local.json").write_text(json.dumps({"b": {"x": 99}}))
        result = hook_common.load_package_config("mypkg", "settings.json", str(tmp_path))
        assert result == {"a": 1, "b": {"x": 99, "y": 20}}

    def test_loads_yaml_with_local_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        config_dir = tmp_path / ".claude" / "config" / "mypkg"
        config_dir.mkdir(parents=True)
        (config_dir / "tools.yaml").write_text("codex:\n  model: gpt-5\n  flags: --auto\n")
        (config_dir / "tools.local.yaml").write_text("codex:\n  model: o3-pro\n")
        result = hook_common.load_package_config("mypkg", "tools.yaml", str(tmp_path))
        assert result["codex"]["model"] == "o3-pro"
        assert result["codex"]["flags"] == "--auto"

    def test_loads_without_local(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        config_dir = tmp_path / ".claude" / "config" / "mypkg"
        config_dir.mkdir(parents=True)
        (config_dir / "conf.json").write_text('{"only": "base"}')
        result = hook_common.load_package_config("mypkg", "conf.json", str(tmp_path))
        assert result == {"only": "base"}

    def test_returns_empty_when_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        result = hook_common.load_package_config("nope", "nope.json", "/nonexistent")
        assert result == {}


# =========================================================================
# route_config.load_config（load_package_config への委譲）
# =========================================================================


class TestRouteConfigLoadConfig:
    def test_loads_via_orchestra_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        config = route_config.load_config({"cwd": str(REPO_ROOT)})
        assert config.get("codex", {}).get("model") == "gpt-5.3-codex"
        assert config.get("gemini", {}).get("model") == "gemini-2.5-pro"

    def test_loads_agents_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        config = route_config.load_config({"cwd": str(REPO_ROOT)})
        agents = config.get("agents", {})
        assert len(agents) >= 20
        assert agents.get("planner", {}).get("tool") == "claude-direct"
        assert agents.get("debugger", {}).get("tool") == "codex"
        assert agents.get("researcher", {}).get("tool") == "gemini"


# =========================================================================
# 実ファイル統合テスト（ai-orchestra 内の config が読めるか）
# =========================================================================


class TestRealConfigFiles:
    """ai-orchestra リポジトリ内の実 config ファイルが正しく読めるか検証する。"""

    @pytest.fixture(autouse=True)
    def _set_orchestra_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))

    def test_orchestration_flags(self) -> None:
        flags = hook_common.load_package_config(
            "route-audit", "orchestration-flags.json", str(REPO_ROOT)
        )
        assert "features" in flags
        assert "route_audit" in flags["features"]
        assert isinstance(flags["features"]["route_audit"].get("enabled"), bool)

    def test_delegation_policy(self) -> None:
        policy = hook_common.load_package_config(
            "route-audit", "delegation-policy.json", str(REPO_ROOT)
        )
        assert "default_route" in policy
        assert "rules" in policy
        assert isinstance(policy["rules"], list)

    def test_cli_tools_yaml(self) -> None:
        config = hook_common.load_package_config("agent-routing", "cli-tools.yaml", str(REPO_ROOT))
        assert "codex" in config
        assert "gemini" in config
        assert "agents" in config
        assert config["codex"]["model"] == "gpt-5.3-codex"
        assert config["codex"]["sandbox"]["analysis"] == "read-only"
        assert config["gemini"]["model"] == "gemini-2.5-pro"
