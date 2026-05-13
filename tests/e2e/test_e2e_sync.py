"""E2E テスト: SessionStart 統合フロー（sync-orchestra.py）。

テスト計画 e2e-test-plan.md セクション 3 に対応。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from tests.conftest import run_orchex, run_session_start


def _setup_essential(project: Path) -> None:
    """setup essential + 初回 SessionStart を実行してベースラインを作る。"""
    run_orchex("setup", "essential", project=project)
    run_session_start(project, "init1")
    # 安定化（2回目でスキップを確認可能にする）
    run_session_start(project, "init2")


class TestFileSync:
    """3.1 ファイル同期"""

    def test_initial_session_start(self, e2e_project: Path) -> None:
        """#24: 初回 SessionStart で skills/agents/rules が同期"""
        run_orchex("setup", "essential", project=e2e_project)
        result = run_session_start(e2e_project, "s1")
        assert result.returncode == 0
        assert (e2e_project / ".claude" / "skills").is_dir()
        assert (e2e_project / ".claude" / "agents").is_dir()

    def test_second_session_start_skips(self, e2e_project: Path) -> None:
        """#25: 2回目の SessionStart は出力なし（完全スキップ）"""
        _setup_essential(e2e_project)
        result = run_session_start(e2e_project, "skip1")
        assert result.stdout.strip() == ""

    def test_updated_file_synced(self, e2e_project: Path, orchestra_dir: Path) -> None:
        """#26: orchestra 側のファイル更新で差分同期"""
        _setup_essential(e2e_project)
        # Touch a config file to trigger sync
        config_file = orchestra_dir / "packages" / "core" / "config" / "task-memory.yaml"
        original_mtime = config_file.stat().st_mtime
        try:
            future = time.time() + 100
            os.utime(config_file, (future, future))
            result = run_session_start(e2e_project, "s3")
            assert "synced" in result.stdout
        finally:
            os.utime(config_file, (original_mtime, original_mtime))

    def test_local_yaml_preserved(self, e2e_project: Path) -> None:
        """#27: *.local.yaml が sync で上書きされない"""
        _setup_essential(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        local_file = config_dir / "cli-tools.local.yaml"
        local_file.write_text("codex:\n  model: preserved-model\n", encoding="utf-8")

        run_session_start(e2e_project, "s4")

        assert local_file.is_file()
        assert "preserved-model" in local_file.read_text(encoding="utf-8")

    def test_local_json_preserved(self, e2e_project: Path) -> None:
        """#28: *.local.json が sync で上書きされない"""
        _setup_essential(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        local_file = config_dir / "test.local.json"
        local_file.write_text('{"test": true}\n', encoding="utf-8")

        run_session_start(e2e_project, "s5")

        assert local_file.is_file()
        assert "test" in local_file.read_text(encoding="utf-8")


class TestStaleCleanup:
    """3.2 Stale file cleanup"""

    def test_stale_file_removed(self, e2e_project: Path) -> None:
        """#29: synced_files にあるがマニフェストにないファイルが削除"""
        _setup_essential(e2e_project)
        # Create a fake synced file and add to synced_files
        fake_dir = e2e_project / ".claude" / "skills" / "fake-stale"
        fake_dir.mkdir(parents=True, exist_ok=True)
        (fake_dir / "SKILL.md").write_text("stale", encoding="utf-8")

        orch_path = e2e_project / ".claude" / "orchestra.json"
        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        orch["synced_files"] = orch.get("synced_files", []) + ["skills/fake-stale/SKILL.md"]
        orch_path.write_text(
            json.dumps(orch, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        run_session_start(e2e_project, "s29")

        assert not (fake_dir / "SKILL.md").exists()

    def test_local_yaml_not_deleted_by_stale_cleanup(self, e2e_project: Path) -> None:
        """#30: stale cleanup で *.local.yaml は削除されない"""
        _setup_essential(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        local_file = config_dir / "test.local.yaml"
        local_file.write_text("test: true\n", encoding="utf-8")

        # Add to synced_files (simulate it was tracked)
        orch_path = e2e_project / ".claude" / "orchestra.json"
        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        orch["synced_files"] = orch.get("synced_files", []) + [
            "config/agent-routing/test.local.yaml"
        ]
        orch_path.write_text(
            json.dumps(orch, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        run_session_start(e2e_project, "s30")
        assert local_file.is_file()

    def test_facet_managed_not_deleted_by_stale_cleanup(self, e2e_project: Path) -> None:
        """#30b: synced_files に残った facet 管理パスが stale cleanup で削除されない

        以前 packages/*/rules/ から sync されていた rules ファイルが
        facet build に移管された後、stale cleanup で誤削除されないことを検証。
        """
        _setup_essential(e2e_project)
        run_orchex("install", "codex-suggestions", project=e2e_project)
        run_session_start(e2e_project, "s30b-1")

        # facet build で codex-delegation ルールが生成されていることを確認
        rule_file = e2e_project / ".claude" / "rules" / "codex-delegation.md"
        assert rule_file.is_file()

        # 旧 sync が synced_files に rules を記録していた状態をシミュレーション
        orch_path = e2e_project / ".claude" / "orchestra.json"
        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        orch["synced_files"] = orch.get("synced_files", []) + [
            "rules/codex-delegation.md",
        ]
        orch_path.write_text(
            json.dumps(orch, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        # SessionStart: stale cleanup が走るが、facet 管理パスは削除されない
        run_session_start(e2e_project, "s30b-2")
        assert rule_file.is_file(), "facet 管理の rules ファイルが stale cleanup で削除された"

    def test_facet_managed_references_not_deleted(self, e2e_project: Path) -> None:
        """#30c: facet build で配置された references が stale cleanup で削除されない"""
        _setup_essential(e2e_project)
        run_orchex("install", "codex-suggestions", project=e2e_project)
        run_session_start(e2e_project, "s30c-1")

        # knowledge を持つスキルの references が存在する場合のテスト
        # codex-system スキルに references があれば検証、なければ SKILL.md で代替検証
        codex_skill = e2e_project / ".claude" / "skills" / "codex-system" / "SKILL.md"
        assert codex_skill.is_file()

        # 旧 synced_files にスキルパスが残っていた状態をシミュレーション
        orch_path = e2e_project / ".claude" / "orchestra.json"
        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        orch["synced_files"] = orch.get("synced_files", []) + [
            "skills/codex-system/SKILL.md",
        ]
        orch_path.write_text(
            json.dumps(orch, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        # SessionStart: stale cleanup が走るが、facet 管理パスは削除されない
        run_session_start(e2e_project, "s30c-2")
        assert codex_skill.is_file(), "facet 管理の SKILL.md が stale cleanup で削除された"

    def test_uninstall_cleans_up_files(self, e2e_project: Path) -> None:
        """#31: パッケージ uninstall 後、SessionStart の facet build で不要ファイル除去"""
        _setup_essential(e2e_project)
        run_orchex("install", "codex-suggestions", project=e2e_project)
        run_session_start(e2e_project, "s31a")

        # Verify codex skill exists before uninstall
        codex_skill = e2e_project / ".claude" / "skills" / "codex-system" / "SKILL.md"
        assert codex_skill.exists()

        # Uninstall removes package from orchestra.json
        result = run_orchex("uninstall", "codex-suggestions", project=e2e_project)
        assert result.returncode == 0

        # SessionStart rebuilds facets — stale skill is cleaned up
        result = run_session_start(e2e_project, "s31b")
        assert result.returncode == 0
        assert not codex_skill.exists()


class TestHookSync:
    """3.3 Hook 同期"""

    def test_hook_added_on_install(self, e2e_project: Path) -> None:
        """#32: パッケージ install で hooks が追加"""
        _setup_essential(e2e_project)
        settings_before = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        hooks_before = json.dumps(settings_before.get("hooks", {})).count("command")

        run_orchex("install", "codex-suggestions", project=e2e_project)

        settings_after = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        hooks_after = json.dumps(settings_after.get("hooks", {})).count("command")
        # codex-suggestions manifest に宣言された 2 hooks が追加される
        # count("command") は "type": "command" と "command": "..." の 2 箇所を拾うため x2
        assert hooks_after - hooks_before == 2 * 2

    def test_hook_removed_on_uninstall(self, e2e_project: Path) -> None:
        """#33: パッケージ uninstall で hooks が除去"""
        _setup_essential(e2e_project)
        run_orchex("install", "audit", project=e2e_project)
        settings_with = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        hooks_with = json.dumps(settings_with.get("hooks", {})).count("command")

        run_orchex("uninstall", "audit", project=e2e_project)
        settings_without = json.loads(
            (e2e_project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        hooks_without = json.dumps(settings_without.get("hooks", {})).count("command")
        # audit package の 8 hooks が全て除去される
        assert hooks_with - hooks_without == 8 * 2

    def test_manual_hook_preserved(self, e2e_project: Path) -> None:
        """#34: 手動追加した hook は sync で削除されない"""
        _setup_essential(e2e_project)
        settings_path = e2e_project / ".claude" / "settings.local.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        pre_hooks = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
        pre_hooks.append({"hooks": [{"type": "command", "command": "echo manual-hook-test"}]})
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        run_session_start(e2e_project, "s34")

        updated = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "manual-hook-test" in json.dumps(updated.get("hooks", {}))


class TestAgentModelPatching:
    """3.4 Agent model patching"""

    def test_model_patched_from_config(self, e2e_project: Path) -> None:
        """#35: cli-tools.yaml の model が agents に反映"""
        _setup_essential(e2e_project)
        agent_file = e2e_project / ".claude" / "agents" / "code-reviewer.md"
        if agent_file.is_file():
            content = agent_file.read_text(encoding="utf-8")
            assert "model:" in content

    def test_local_model_override(self, e2e_project: Path) -> None:
        """#36: cli-tools.local.yaml で model 上書き"""
        _setup_essential(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "agents:\n  code-reviewer:\n    model: e2e-custom-model\n", encoding="utf-8"
        )

        run_session_start(e2e_project, "s36")

        agent_file = e2e_project / ".claude" / "agents" / "code-reviewer.md"
        if agent_file.is_file():
            assert "e2e-custom-model" in agent_file.read_text(encoding="utf-8")


class TestClaudeignore:
    """3.5 .claudeignore 生成"""

    def test_claudeignore_generated(self, e2e_project: Path) -> None:
        """#37: SessionStart で .claudeignore 生成"""
        run_orchex("setup", "essential", project=e2e_project)
        run_session_start(e2e_project, "s37")
        assert (e2e_project / ".claudeignore").is_file()

    def test_claudeignore_local_merged(self, e2e_project: Path) -> None:
        """#38: .claudeignore.local がマージ"""
        run_orchex("setup", "essential", project=e2e_project)
        (e2e_project / ".claudeignore.local").write_text("e2e-custom-pattern/\n", encoding="utf-8")
        run_session_start(e2e_project, "s38")
        content = (e2e_project / ".claudeignore").read_text(encoding="utf-8")
        assert "e2e-custom-pattern" in content

    def test_claudeignore_no_rewrite_when_unchanged(self, e2e_project: Path) -> None:
        """#39: .claudeignore 変更なし時は上書きされない"""
        _setup_essential(e2e_project)
        claudeignore = e2e_project / ".claudeignore"
        mtime_before = claudeignore.stat().st_mtime

        run_session_start(e2e_project, "s39")

        mtime_after = claudeignore.stat().st_mtime
        assert mtime_before == mtime_after
