"""E2E テスト: ライフサイクル横断シナリオ。

テスト計画 e2e-test-plan.md セクション 9 に対応。
新規導入 → 運用 → パッケージ追加/削除の一気通貫テスト。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import run_orchex, run_session_start


class TestLifecycleNewProject:
    """9.1 新規プロジェクト導入 → 運用 → パッケージ追加"""

    def test_setup_session_start_add_package(self, e2e_project: Path) -> None:
        """#68: setup → SessionStart → facet build → パッケージ追加 → SessionStart"""
        # Step 1: setup essential
        result = run_orchex("setup", "essential", project=e2e_project)
        assert result.returncode == 0

        orch = json.loads((e2e_project / ".claude" / "orchestra.json").read_text(encoding="utf-8"))
        assert "core" in orch["installed_packages"]
        assert "agent-routing" in orch["installed_packages"]
        assert "quality-gates" in orch["installed_packages"]

        # Step 2: SessionStart → facet build
        result = run_session_start(e2e_project, "s1")
        assert result.returncode == 0
        assert "facets built" in result.stdout

        # Verify skills generated
        skills_dir = e2e_project / ".claude" / "skills"
        assert (skills_dir / "review" / "SKILL.md").is_file()
        assert (skills_dir / "simplify" / "SKILL.md").is_file()

        # Step 3: Add package
        result = run_orchex("install", "codex-suggestions", project=e2e_project)
        assert result.returncode == 0

        # Step 4: SessionStart after add
        result = run_session_start(e2e_project, "s2")
        assert result.returncode == 0
        assert (skills_dir / "codex-system" / "SKILL.md").is_file()

    def test_consecutive_session_start_skips(self, e2e_project: Path) -> None:
        """#69: 連続 SessionStart でスキップ"""
        run_orchex("setup", "essential", project=e2e_project)
        run_session_start(e2e_project, "s1")

        # 2nd: should produce no output (complete skip)
        result = run_session_start(e2e_project, "s2")
        assert result.stdout.strip() == ""

        # 3rd: same
        result = run_session_start(e2e_project, "s3")
        assert result.stdout.strip() == ""


class TestLifecycleOrchestraUpdate:
    """9.2 orchestra 側の更新 → プロジェクト反映"""

    def test_policy_change_propagation(self, e2e_project: Path, orchestra_dir: Path) -> None:
        """#70: facet policy 変更 → SessionStart → 全参照 skill に伝播"""
        run_orchex("setup", "essential", project=e2e_project)
        run_session_start(e2e_project, "s1")
        # Stabilize
        run_session_start(e2e_project, "s2")

        # Modify policy
        policy_path = orchestra_dir / "facets" / "policies" / "code-quality.md"
        original = policy_path.read_text(encoding="utf-8")
        try:
            policy_path.write_text(original + "\n<!-- E2E_POLICY_TEST -->\n", encoding="utf-8")

            result = run_session_start(e2e_project, "s3")
            assert "facets built" in result.stdout

            simplify = (e2e_project / ".claude" / "skills" / "simplify" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            assert "E2E_POLICY_TEST" in simplify

            principles = (e2e_project / ".claude" / "rules" / "coding-principles.md").read_text(
                encoding="utf-8"
            )
            assert "E2E_POLICY_TEST" in principles
        finally:
            policy_path.write_text(original, encoding="utf-8")

    def test_config_update_preserves_local(self, e2e_project: Path, orchestra_dir: Path) -> None:
        """#72: config 値変更 → ベース更新、local 保持"""
        run_orchex("setup", "essential", project=e2e_project)
        run_session_start(e2e_project, "s1")

        # Create local override
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        local_config = config_dir / "cli-tools.local.yaml"
        local_config.write_text("codex:\n  model: e2e-local-model\n", encoding="utf-8")

        # Modify base config
        base_config = orchestra_dir / "packages" / "agent-routing" / "config" / "cli-tools.yaml"
        original = base_config.read_text(encoding="utf-8")
        try:
            base_config.write_text(original + "\n# E2E_CONFIG_TEST\n", encoding="utf-8")

            run_session_start(e2e_project, "s2")

            # Base updated
            synced_base = (config_dir / "cli-tools.yaml").read_text(encoding="utf-8")
            assert "E2E_CONFIG_TEST" in synced_base

            # Local preserved
            assert local_config.is_file()
            assert "e2e-local-model" in local_config.read_text(encoding="utf-8")
        finally:
            base_config.write_text(original, encoding="utf-8")


class TestLifecycleUninstall:
    """9.3 パッケージ削除 → クリーンアップ"""

    def test_uninstall_removes_facet_artifacts(self, e2e_project: Path) -> None:
        """#73: uninstall → SessionStart → facet 生成物が削除される"""
        run_orchex("setup", "essential", project=e2e_project)
        run_orchex("install", "codex-suggestions", project=e2e_project)
        run_session_start(e2e_project, "s1")

        # Verify codex skills exist
        assert (e2e_project / ".claude" / "skills" / "codex-system" / "SKILL.md").is_file()

        # Uninstall
        run_orchex("uninstall", "codex-suggestions", project=e2e_project)
        run_session_start(e2e_project, "s2")

        # Facet artifacts removed
        assert not (e2e_project / ".claude" / "skills" / "codex-system" / "SKILL.md").exists()
        assert not (e2e_project / ".claude" / "rules" / "codex-delegation.md").exists()

    def test_uninstall_preserves_other_packages(self, e2e_project: Path) -> None:
        """#74: uninstall 後に残パッケージが正常動作"""
        run_orchex("setup", "essential", project=e2e_project)
        run_orchex("install", "codex-suggestions", project=e2e_project)
        run_session_start(e2e_project, "s1")

        run_orchex("uninstall", "codex-suggestions", project=e2e_project)
        run_session_start(e2e_project, "s2")

        # quality-gates skills still exist
        assert (e2e_project / ".claude" / "skills" / "review" / "SKILL.md").is_file()
        assert (e2e_project / ".claude" / "skills" / "simplify" / "SKILL.md").is_file()

        # agent-routing hooks still present
        settings = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        assert "agent-router" in json.dumps(settings.get("hooks", {}))
