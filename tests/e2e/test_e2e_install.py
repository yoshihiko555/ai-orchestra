"""E2E テスト: パッケージ導入フロー（install / uninstall / enable / disable）。

テスト計画 e2e-test-plan.md セクション 1 に対応。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import run_orchex, run_session_start


class TestInstallBasic:
    """1.1 install 基本"""

    def test_install_auto_init(self, e2e_project: Path) -> None:
        """#1: 未初期化プロジェクトへの install で自動 init"""
        result = run_orchex("install", "core", project=e2e_project)
        assert result.returncode == 0
        assert (e2e_project / ".claude" / "orchestra.json").is_file()

    def test_orchestra_json_records_package(self, e2e_project: Path) -> None:
        """#2: install 後の orchestra.json にパッケージ記録"""
        run_orchex("install", "core", project=e2e_project)
        orch = json.loads((e2e_project / ".claude" / "orchestra.json").read_text(encoding="utf-8"))
        assert "core" in orch["installed_packages"]

    def test_hooks_registered(self, e2e_project: Path) -> None:
        """#3: install 後に hooks が settings.local.json に登録"""
        run_orchex("install", "core", project=e2e_project)
        settings = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        hooks = settings.get("hooks", {})
        assert len(hooks) > 0
        assert any("sync-orchestra" in json.dumps(entries) for entries in hooks.values())

    def test_config_files_copied(self, e2e_project: Path) -> None:
        """#4: install 後に config ファイルがコピー"""
        run_orchex("install", "core", project=e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "core"
        assert config_dir.is_dir()
        assert (config_dir / "task-memory.yaml").is_file()

    def test_skills_generated_after_session_start(self, e2e_project: Path) -> None:
        """#5: install + SessionStart 後に facet build で skills が生成される"""
        run_orchex("install", "core", project=e2e_project)
        run_session_start(e2e_project, "s1")
        assert (e2e_project / ".claude" / "skills").is_dir()

    def test_orchestra_dir_env_set(self, e2e_project: Path) -> None:
        """#6: AI_ORCHESTRA_DIR が設定"""
        run_orchex("install", "core", project=e2e_project)
        global_settings = Path.home() / ".claude" / "settings.json"
        if global_settings.is_file():
            settings = json.loads(global_settings.read_text(encoding="utf-8"))
            env = settings.get("env", {})
            assert "AI_ORCHESTRA_DIR" in env


class TestInstallDependency:
    """1.2 install 依存解決"""

    def test_missing_dependency_warning(self, e2e_project: Path) -> None:
        """#7: 依存パッケージ未インストールで警告"""
        result = run_orchex("install", "quality-gates", project=e2e_project)
        output = result.stdout + result.stderr
        assert "依存" in output or "depend" in output.lower()

    def test_no_warning_with_dependency(self, e2e_project: Path) -> None:
        """#8: 依存パッケージインストール済みなら警告なし"""
        run_orchex("install", "core", project=e2e_project)
        result = run_orchex("install", "quality-gates", project=e2e_project)
        assert "依存" not in result.stdout

    def test_idempotent_install(self, e2e_project: Path) -> None:
        """#9: 同じパッケージを2回 install しても冪等"""
        run_orchex("install", "core", project=e2e_project)
        run_orchex("install", "core", project=e2e_project)
        orch = json.loads((e2e_project / ".claude" / "orchestra.json").read_text(encoding="utf-8"))
        pkgs = orch["installed_packages"]
        assert pkgs.count("core") == 1


class TestUninstall:
    """1.3 uninstall"""

    def test_uninstall_removes_from_orchestra_json(self, e2e_project: Path) -> None:
        """#10: uninstall で orchestra.json から除外"""
        run_orchex("install", "core", project=e2e_project)
        run_orchex("install", "quality-gates", project=e2e_project)
        run_orchex("uninstall", "quality-gates", project=e2e_project)
        orch = json.loads((e2e_project / ".claude" / "orchestra.json").read_text(encoding="utf-8"))
        assert "quality-gates" not in orch["installed_packages"]

    def test_uninstall_removes_hooks(self, e2e_project: Path) -> None:
        """#11: uninstall 後に該当 hooks が除去"""
        run_orchex("install", "core", project=e2e_project)
        run_orchex("install", "agent-routing", project=e2e_project)
        run_orchex("uninstall", "agent-routing", project=e2e_project)
        settings = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        assert "agent-router" not in json.dumps(settings.get("hooks", {}))

    def test_uninstall_preserves_other_hooks(self, e2e_project: Path) -> None:
        """#12: uninstall 後に他パッケージの hooks は残存"""
        run_orchex("install", "core", project=e2e_project)
        run_orchex("install", "agent-routing", project=e2e_project)
        run_orchex("uninstall", "agent-routing", project=e2e_project)
        settings = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        hooks_str = json.dumps(settings.get("hooks", {}))
        assert "sync-orchestra" in hooks_str or "load-task-state" in hooks_str

    def test_uninstall_nonexistent_package(self, e2e_project: Path) -> None:
        """#13: 未インストールパッケージの uninstall はエラー"""
        run_orchex("install", "core", project=e2e_project)
        result = run_orchex("uninstall", "nonexistent-pkg", project=e2e_project, check=False)
        assert result.returncode != 0


class TestEnableDisable:
    """1.4 enable / disable"""

    def test_disable_removes_hooks(self, e2e_project: Path) -> None:
        """#14: disable で hooks が無効化"""
        run_orchex("install", "core", project=e2e_project)
        run_orchex("install", "agent-routing", project=e2e_project)
        run_orchex("disable", "agent-routing", project=e2e_project)
        settings = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        assert "agent-router" not in json.dumps(settings.get("hooks", {}))

    def test_disable_keeps_installed_packages(self, e2e_project: Path) -> None:
        """#15: disable 後も installed_packages に残る"""
        run_orchex("install", "core", project=e2e_project)
        run_orchex("install", "agent-routing", project=e2e_project)
        run_orchex("disable", "agent-routing", project=e2e_project)
        orch = json.loads((e2e_project / ".claude" / "orchestra.json").read_text(encoding="utf-8"))
        assert "agent-routing" in orch["installed_packages"]

    def test_enable_restores_hooks(self, e2e_project: Path) -> None:
        """#16: enable で hooks が再登録"""
        run_orchex("install", "core", project=e2e_project)
        run_orchex("install", "agent-routing", project=e2e_project)
        run_orchex("disable", "agent-routing", project=e2e_project)
        run_orchex("enable", "agent-routing", project=e2e_project)
        settings = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        assert "agent-router" in json.dumps(settings.get("hooks", {}))

    def test_enable_nonexistent_package(self, e2e_project: Path) -> None:
        """#17: 未インストールパッケージの enable はエラー"""
        run_orchex("install", "core", project=e2e_project)
        result = run_orchex("enable", "nonexistent-pkg", project=e2e_project, check=False)
        assert result.returncode != 0
