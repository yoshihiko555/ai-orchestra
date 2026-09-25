"""config 読み込みユーティリティのテスト。

テスト対象:
- hook_common: _read_config_file の一部分岐（deep_merge/find_package_config/
  load_package_config の基本分岐は tests/unit/test_hook_common.py 側で検証）
- route_config: load_config (load_package_config への委譲)
- 実ファイル: ai-orchestra 内の config が正しく読めるか
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")

# route_config は hook_common を import するため sys.path を設定
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
route_config = load_module("route_config", "packages/agent-routing/hooks/route_config.py")


def _read_cli_tools_yaml_raw() -> dict:
    """ローダとは独立に、解決済み cli-tools.yaml を直接 PyYAML で読む。

    期待値を yaml 由来で導出することで、テストを literal（モデル名・ルーティング先）に
    依存させない。cli-tools.yaml の codex.model / antigravity.model / agents.*.tool を
    変更してもテストが壊れない。

    cli-tools.local.yaml が実在する環境では base に deep_merge して返す。
    ローカル上書きは正式にサポートされ永続化されるため、これを無視すると
    上書きのある環境で load_package_config の結果と乖離してテストが誤って失敗する。
    """
    path = hook_common.find_package_config("agent-routing", "cli-tools.yaml", str(REPO_ROOT))
    assert path, "cli-tools.yaml が解決できること"
    with open(path, encoding="utf-8") as f:
        base = yaml.safe_load(f)
    local_path = hook_common._find_local_config_path(
        "agent-routing", "cli-tools.yaml", str(REPO_ROOT), path
    )
    if not os.path.isfile(local_path):
        return base
    with open(local_path, encoding="utf-8") as f:
        local = yaml.safe_load(f)
    return hook_common.deep_merge(base, local) if local else base


# agents.<name>.tool が取りうる実行先の語彙（config-loading / codex-delegation ルール準拠）
VALID_AGENT_TOOLS = frozenset({"codex", "antigravity", "claude-direct", "auto"})


# =========================================================================
# _read_config_file
# =========================================================================


class TestReadConfigFile:
    def test_reads_yml(self, tmp_path: Path) -> None:
        f = tmp_path / "test.yml"
        f.write_text("x: 1\n")
        assert hook_common._read_config_file(str(f)) == {"x": 1}

    def test_returns_empty_for_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("{invalid")
        assert hook_common._read_config_file(str(f)) == {}


# =========================================================================
# route_config.load_config（load_package_config への委譲）
# =========================================================================


class TestRouteConfigLoadConfig:
    def test_loads_via_orchestra_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        config = route_config.load_config({"cwd": str(REPO_ROOT)})
        expected = _read_cli_tools_yaml_raw()
        assert config.get("codex", {}).get("model") == expected["codex"]["model"]
        assert config.get("antigravity", {}).get("model") == expected["antigravity"]["model"]

    def test_loads_agents_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """agents セクションが tool 値まで含めて読み込まれることを検証する。

        期待値は cli-tools.yaml から導出する。ルーティングの実 config 値（例:
        `agents.debugger.tool`）を literal でピンすると、設定変更のたびに無関係な
        テストが落ちる（Issue #341）。ここで担保したいのは「読み込み結果が
        cli-tools.yaml と一致すること」であり、個々の割り当て先ではない。
        """
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
        config = route_config.load_config({"cwd": str(REPO_ROOT)})
        agents = config.get("agents", {})
        expected_agents = _read_cli_tools_yaml_raw()["agents"]
        assert len(agents) >= 20
        # ローダが agent を取りこぼしたり増やしたりしないこと（yaml との集合一致）
        assert set(agents) == set(expected_agents)
        # tool 値は yaml 由来で導出（literal 比較を廃止し、ルーティング変更で壊れない）
        for name in ("planner", "debugger", "researcher"):
            assert agents.get(name, {}).get("tool") == expected_agents[name]["tool"]
        # 構造契約: 全 agent の tool が既知の実行先語彙に含まれる（ファイル健全性の保証）
        for name, spec in agents.items():
            assert spec.get("tool") in VALID_AGENT_TOOLS, f"unknown tool for agent: {name}"


# =========================================================================
# 実ファイル統合テスト（ai-orchestra 内の config が読めるか）
# =========================================================================


class TestRealConfigFiles:
    """ai-orchestra リポジトリ内の実 config ファイルが正しく読めるか検証する。"""

    @pytest.fixture(autouse=True)
    def _set_orchestra_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))

    def test_audit_flags(self) -> None:
        # sync 前の配布先ではなく、今回更新する package 同梱 base を検証する。
        project_without_distributed_config = REPO_ROOT / "packages" / "audit"
        flags = hook_common.load_package_config(
            "audit", "audit-flags.json", str(project_without_distributed_config)
        )
        assert flags["version"] == 3
        assert "features" in flags
        assert "route_audit" in flags["features"]
        assert isinstance(flags["features"]["route_audit"].get("enabled"), bool)
        # Contract: 具体的なデフォルト値の検証
        assert flags["features"]["route_audit"]["max_excerpt_chars"] == 160
        assert flags["paths"]["logs_dir"] == ".claude/logs/audit"

    def test_quality_gates_json(self) -> None:
        config = hook_common.load_package_config(
            "quality-gates", "quality-gates.json", str(REPO_ROOT)
        )
        assert config["features"]["quality_gate"]["block_on_failed_test"] is True
        assert config["paths"]["state_dir"] == ".claude/state"
        assert config["features"]["context_optimization"]["read_line_threshold"] == 200

    def test_delegation_policy(self) -> None:
        policy = hook_common.load_package_config("audit", "delegation-policy.json", str(REPO_ROOT))
        assert "default_route" in policy
        assert "rules" in policy
        assert isinstance(policy["rules"], list)
        # Contract: デフォルトルートとエイリアス定義の検証
        assert policy["default_route"] == "claude-direct"
        assert "aliases" in policy
        assert "claude-direct" in policy["aliases"]
        claude_direct_aliases = policy["aliases"]["claude-direct"]
        assert isinstance(claude_direct_aliases, list)
        assert "skill:commit" in claude_direct_aliases

    def test_cli_tools_yaml(self) -> None:
        config = hook_common.load_package_config("agent-routing", "cli-tools.yaml", str(REPO_ROOT))
        expected = _read_cli_tools_yaml_raw()
        assert "codex" in config
        assert "antigravity" in config
        assert "agents" in config
        # モデル値・sandbox 値は yaml 由来で導出（literal 比較を廃止し、モデル変更で壊れない）
        assert config["codex"]["model"] == expected["codex"]["model"]
        assert config["codex"]["sandbox"]["analysis"] == expected["codex"]["sandbox"]["analysis"]
        assert config["antigravity"]["model"] == expected["antigravity"]["model"]
        # 構造契約: model は非空かつ allowlist に含まれる（ファイル健全性の保証）
        assert isinstance(config["codex"]["model"], str) and config["codex"]["model"]
        assert config["antigravity"]["model"] in config["antigravity"]["model_allowlist"]
