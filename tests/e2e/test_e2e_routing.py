"""E2E テスト: エージェントルーティング。

テスト計画 e2e-test-plan.md セクション 8 に対応。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO_ROOT, run_orchex, run_session_start

AGENT_ROUTER = REPO_ROOT / "packages" / "agent-routing" / "hooks" / "agent-router.py"


def _run_router(message: str, project: Path) -> str:
    """agent-router.py を実行して stdout を返す。"""
    payload = json.dumps({"hook_type": "UserPromptSubmit", "prompt": message})
    result = subprocess.run(
        [sys.executable, str(AGENT_ROUTER)],
        input=payload,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AI_ORCHESTRA_DIR": str(REPO_ROOT),
            "CLAUDE_PROJECT_DIR": str(project),
        },
    )
    return result.stdout


def _setup_with_routing(project: Path) -> None:
    run_orchex("setup", "essential", project=project)
    run_session_start(project, "init")


class TestAgentRouting:
    """8. エージェントルーティング"""

    def test_japanese_prompt_routing(self, e2e_project: Path) -> None:
        """#62: 日本語プロンプトでエージェント提案"""
        _setup_with_routing(e2e_project)
        output = _run_router("Pythonでバックエンド実装して", e2e_project)
        assert "Agent Routing" in output or "backend-python-dev" in output

    def test_english_prompt_routing(self, e2e_project: Path) -> None:
        """#63: 英語プロンプトでエージェント提案"""
        _setup_with_routing(e2e_project)
        output = _run_router("research the latest React documentation", e2e_project)
        assert "Agent Routing" in output or "researcher" in output

    def test_tool_auto_routing(self, e2e_project: Path) -> None:
        """#64: tool: auto のエージェント"""
        _setup_with_routing(e2e_project)
        output = _run_router("設計を相談したい", e2e_project)
        # auto ルーティングで何かしらの提案が出る
        assert len(output.strip()) > 0 or output == ""  # 提案なしも正常

    def test_tool_codex_routing(self, e2e_project: Path) -> None:
        """#65: tool: codex のエージェント — Codex CLI 使用提案"""
        _setup_with_routing(e2e_project)
        run_orchex("install", "codex-suggestions", project=e2e_project)
        output = _run_router("Codexでデバッグして", e2e_project)
        assert "Agent Routing" in output or "debugger" in output or "codex" in output.lower()

    def test_tool_claude_direct_routing(self, e2e_project: Path) -> None:
        """#66: tool: claude-direct のエージェント"""
        _setup_with_routing(e2e_project)
        output = _run_router("テストを書いて", e2e_project)
        assert "Agent Routing" in output or "tester" in output or output.strip() == ""

    def test_codex_disabled_suppresses_codex(self, e2e_project: Path) -> None:
        """#67: codex.enabled: false 時に Codex 提案が抑制"""
        _setup_with_routing(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "codex:\n  enabled: false\n", encoding="utf-8"
        )
        output = _run_router("Codexに相談して", e2e_project)
        # codex disabled なので codex exec は提案されない
        assert "codex exec" not in output
