"""E2E テスト: setup フロー（一括導入）。

テスト計画 e2e-test-plan.md セクション 2 に対応。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import run_orchex, run_session_start


class TestSetup:
    """2. setup フロー"""

    def test_setup_essential_fresh(self, e2e_project: Path) -> None:
        """#18: setup essential で未初期化プロジェクトに一括導入"""
        result = run_orchex("setup", "essential", project=e2e_project)
        assert result.returncode == 0

    def test_setup_essential_packages(self, e2e_project: Path) -> None:
        """#19: setup 後の orchestra.json に 3 パッケージ"""
        run_orchex("setup", "essential", project=e2e_project)
        orch = json.loads((e2e_project / ".claude" / "orchestra.json").read_text(encoding="utf-8"))
        pkgs = orch["installed_packages"]
        assert "core" in pkgs
        assert "agent-routing" in pkgs
        assert "quality-gates" in pkgs

    def test_setup_essential_hooks(self, e2e_project: Path) -> None:
        """#20: setup 後に全パッケージの hooks が登録"""
        run_orchex("setup", "essential", project=e2e_project)
        settings = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        hooks_str = json.dumps(settings.get("hooks", {}))
        assert "sync-orchestra" in hooks_str
        assert "agent-router" in hooks_str

    def test_setup_essential_idempotent(self, e2e_project: Path) -> None:
        """#21: setup essential を2回実行しても冪等"""
        run_orchex("setup", "essential", project=e2e_project)
        result = run_orchex("setup", "essential", project=e2e_project)
        assert "スキップ" in result.stdout

    def test_setup_all(self, e2e_project: Path) -> None:
        """#22: setup all で全パッケージ導入"""
        result = run_orchex("setup", "all", project=e2e_project)
        assert result.returncode == 0
        orch = json.loads((e2e_project / ".claude" / "orchestra.json").read_text(encoding="utf-8"))
        assert len(orch["installed_packages"]) >= 8

    def test_setup_then_session_start(self, e2e_project: Path) -> None:
        """#23: setup 後に SessionStart が正常動作"""
        run_orchex("setup", "essential", project=e2e_project)
        result = run_session_start(e2e_project, "s1")
        assert result.returncode == 0
        assert "facets built" in result.stdout
