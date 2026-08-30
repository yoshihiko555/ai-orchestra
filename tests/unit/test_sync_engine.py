"""sync_engine.py のユニットテスト。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# sync_engine は scripts/ からの相対 import を使うため sys.path にスクリプトルートを追加
_repo_root = Path(__file__).resolve().parents[2]
_scripts_dir = str(_repo_root / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from tests.module_loader import load_module

sync_engine = load_module("sync_engine", "scripts/lib/sync_engine.py")
hook_utils = load_module("hook_utils_for_sync_engine_test", "scripts/lib/hook_utils.py")


class TestNeedsSync:
    """needs_sync のテスト。"""

    def test_dst_not_exists(self, tmp_path):
        """dst が存在しない場合、True を返す。"""
        src = tmp_path / "src.txt"
        src.write_text("content")
        dst = tmp_path / "dst.txt"
        assert sync_engine.needs_sync(src, dst) is True

    def test_src_newer(self, tmp_path):
        """src が dst より新しい場合、True を返す。"""

        dst = tmp_path / "dst.txt"
        dst.write_text("old")
        # mtime を古くする
        old_time = time.time() - 10
        os.utime(dst, (old_time, old_time))

        src = tmp_path / "src.txt"
        src.write_text("new")

        assert sync_engine.needs_sync(src, dst) is True

    def test_dst_newer(self, tmp_path):
        """dst が src より新しい場合、False を返す。"""

        src = tmp_path / "src.txt"
        src.write_text("old")
        old_time = time.time() - 10
        os.utime(src, (old_time, old_time))

        dst = tmp_path / "dst.txt"
        dst.write_text("new")

        assert sync_engine.needs_sync(src, dst) is False


class TestIsLocalOverride:
    """is_local_override のテスト。"""

    def test_config_local_yaml(self):
        """config カテゴリの .local.yaml は True。"""
        assert sync_engine.is_local_override("config", Path("cli-tools.local.yaml")) is True

    def test_config_local_json(self):
        """config カテゴリの .local.json は True。"""
        assert sync_engine.is_local_override("config", Path("settings.local.json")) is True

    def test_config_non_local(self):
        """config カテゴリの通常ファイルは False。"""
        assert sync_engine.is_local_override("config", Path("cli-tools.yaml")) is False

    def test_non_config_category(self):
        """config 以外のカテゴリは常に False。"""
        assert sync_engine.is_local_override("agents", Path("test.local.yaml")) is False
        assert sync_engine.is_local_override("hooks", Path("hook.local.json")) is False


class TestRemoveStaleFiles:
    """remove_stale_files のテスト。"""

    def test_removes_stale_file(self, tmp_path):
        """前回同期したが今回は対象外のファイルを削除する。"""
        claude_dir = tmp_path / ".claude"
        stale = claude_dir / "agents" / "old-agent.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("old")

        prev = ["agents/old-agent.md", "agents/keep.md"]
        current = {"agents/keep.md"}

        removed = sync_engine.remove_stale_files(claude_dir, prev, current)
        assert removed == 1
        assert not stale.exists()

    def test_skips_current_files(self, tmp_path):
        """current_synced に含まれるファイルは削除しない。"""
        claude_dir = tmp_path / ".claude"
        keep = claude_dir / "agents" / "keep.md"
        keep.parent.mkdir(parents=True)
        keep.write_text("keep")

        prev = ["agents/keep.md"]
        current = {"agents/keep.md"}

        removed = sync_engine.remove_stale_files(claude_dir, prev, current)
        assert removed == 0
        assert keep.exists()

    def test_skips_facet_managed(self, tmp_path):
        """facet_managed に含まれるファイルは削除しない。"""
        claude_dir = tmp_path / ".claude"
        managed = claude_dir / "skills" / "review" / "SKILL.md"
        managed.parent.mkdir(parents=True)
        managed.write_text("managed")

        prev = ["skills/review/SKILL.md"]
        current: set[str] = set()
        facet_managed = {"skills/review/SKILL.md"}

        removed = sync_engine.remove_stale_files(claude_dir, prev, current, facet_managed)
        assert removed == 0
        assert managed.exists()

    def test_skips_local_override(self, tmp_path):
        """config の .local.yaml は削除しない。"""
        claude_dir = tmp_path / ".claude"
        local = claude_dir / "config" / "cli-tools.local.yaml"
        local.parent.mkdir(parents=True)
        local.write_text("local override")

        prev = ["config/cli-tools.local.yaml"]
        current: set[str] = set()

        removed = sync_engine.remove_stale_files(claude_dir, prev, current)
        assert removed == 0
        assert local.exists()

    def test_removes_empty_parent_dirs(self, tmp_path):
        """削除後に空になったディレクトリも削除する。"""
        claude_dir = tmp_path / ".claude"
        stale = claude_dir / "agents" / "sub" / "file.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("content")

        prev = ["agents/sub/file.md"]
        current: set[str] = set()

        sync_engine.remove_stale_files(claude_dir, prev, current)
        assert not stale.exists()
        assert not (claude_dir / "agents" / "sub").exists()

    def test_keeps_nonempty_parent_dirs(self, tmp_path):
        """他のファイルがある親ディレクトリは残す。"""
        claude_dir = tmp_path / ".claude"
        stale = claude_dir / "agents" / "old.md"
        keep = claude_dir / "agents" / "keep.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("old")
        keep.write_text("keep")

        prev = ["agents/old.md"]
        current: set[str] = set()

        sync_engine.remove_stale_files(claude_dir, prev, current)
        assert not stale.exists()
        assert keep.exists()
        assert (claude_dir / "agents").exists()

    def test_nonexistent_file_ignored(self, tmp_path):
        """ファイルが既に存在しない場合は無視する。"""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        prev = ["agents/nonexistent.md"]
        current: set[str] = set()

        removed = sync_engine.remove_stale_files(claude_dir, prev, current)
        assert removed == 0


class TestCollectFacetManagedPaths:
    """collect_facet_managed_paths のテスト。"""

    def test_skill_composition(self, tmp_path):
        """skill タイプの composition パスを収集する。"""
        orchestra_path = tmp_path / "orchestra"
        comp_dir = orchestra_path / "facets" / "compositions"
        comp_dir.mkdir(parents=True)
        comp = comp_dir / "review.yaml"
        comp.write_text("name: review\ntype: skill\n", encoding="utf-8")

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        result = sync_engine.collect_facet_managed_paths(orchestra_path, project_dir)
        assert "skills/review/SKILL.md" in result

    def test_rule_composition(self, tmp_path):
        """rule タイプの composition パスを収集する。"""
        orchestra_path = tmp_path / "orchestra"
        comp_dir = orchestra_path / "facets" / "compositions"
        comp_dir.mkdir(parents=True)
        comp = comp_dir / "coding.yaml"
        comp.write_text("name: coding-principles\ntype: rule\n", encoding="utf-8")

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        result = sync_engine.collect_facet_managed_paths(orchestra_path, project_dir)
        assert "rules/coding-principles.md" in result

    def test_knowledge_and_scripts(self, tmp_path):
        """knowledge と scripts エントリも収集する。"""
        orchestra_path = tmp_path / "orchestra"
        comp_dir = orchestra_path / "facets" / "compositions"
        comp_dir.mkdir(parents=True)
        comp = comp_dir / "review.yaml"
        comp.write_text(
            "name: review\ntype: skill\nknowledge:\n  - best-practices\nscripts:\n  - lint.py\n",
            encoding="utf-8",
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        result = sync_engine.collect_facet_managed_paths(orchestra_path, project_dir)
        assert "skills/review/SKILL.md" in result
        assert "skills/review/references/best-practices.md" in result
        assert "skills/review/scripts/lint.py" in result

    def test_no_compositions_dir(self, tmp_path):
        """compositions ディレクトリが存在しない場合、空セットを返す。"""
        orchestra_path = tmp_path / "orchestra"
        orchestra_path.mkdir()
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        result = sync_engine.collect_facet_managed_paths(orchestra_path, project_dir)
        assert result == set()

    def test_invalid_yaml_skipped(self, tmp_path):
        """不正な YAML ファイルをスキップする。"""
        orchestra_path = tmp_path / "orchestra"
        comp_dir = orchestra_path / "facets" / "compositions"
        comp_dir.mkdir(parents=True)
        bad = comp_dir / "bad.yaml"
        bad.write_text("not: a\ncomposition: without name\n", encoding="utf-8")

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        result = sync_engine.collect_facet_managed_paths(orchestra_path, project_dir)
        assert result == set()

    def test_local_compositions_merged(self, tmp_path):
        """プロジェクトローカルの compositions も収集する。"""
        orchestra_path = tmp_path / "orchestra"
        orchestra_path.mkdir()

        project_dir = tmp_path / "project"
        local_comp = project_dir / ".claude" / "facets" / "compositions"
        local_comp.mkdir(parents=True)
        comp = local_comp / "custom.yaml"
        comp.write_text("name: custom-skill\ntype: skill\n", encoding="utf-8")

        result = sync_engine.collect_facet_managed_paths(orchestra_path, project_dir)
        assert "skills/custom-skill/SKILL.md" in result


class TestCollectManifestCompositions:
    """collect_manifest_compositions のテスト。"""

    def test_collects_from_manifests(self, tmp_path):
        """パッケージ manifest から compositions を収集する。"""
        packages_dir = tmp_path / "packages"
        pkg_dir = packages_dir / "core"
        pkg_dir.mkdir(parents=True)
        manifest = {
            "name": "core",
            "skills": ["review", "commit"],
            "rules": ["coding-principles"],
        }
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        result = sync_engine.collect_manifest_compositions(tmp_path)
        assert result["review"] == "core"
        assert result["commit"] == "core"
        assert result["coding-principles"] == "core"

    def test_no_packages_dir(self, tmp_path):
        """packages ディレクトリがない場合、空辞書を返す。"""
        result = sync_engine.collect_manifest_compositions(tmp_path)
        assert result == {}

    def test_invalid_manifest_skipped(self, tmp_path):
        """壊れた manifest.json をスキップする。"""
        packages_dir = tmp_path / "packages"
        pkg_dir = packages_dir / "broken"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "manifest.json").write_text("invalid json", encoding="utf-8")

        result = sync_engine.collect_manifest_compositions(tmp_path)
        assert result == {}

    def test_duplicate_composition_warning(self, tmp_path, capsys):
        """同じ composition が複数パッケージで宣言された場合に警告する。"""
        packages_dir = tmp_path / "packages"
        for pkg_name in ("pkg-a", "pkg-b"):
            pkg_dir = packages_dir / pkg_name
            pkg_dir.mkdir(parents=True)
            manifest = {"name": pkg_name, "skills": ["shared-skill"]}
            (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        sync_engine.collect_manifest_compositions(tmp_path)
        captured = capsys.readouterr()
        assert "shared-skill" in captured.err
        assert "warn" in captured.err


class TestSyncPackages:
    """sync_packages のテスト。"""

    def test_syncs_agent_file(self, tmp_path):
        """agents ファイルを同期する。"""
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "core"
        pkg_dir.mkdir(parents=True)

        agent_file = pkg_dir / "agents" / "testing-reality-checker.md"
        agent_file.parent.mkdir()
        agent_file.write_text("# Reality Checker")

        manifest = {"name": "core", "agents": ["agents/testing-reality-checker.md"], "config": []}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)

        count, files = sync_engine.sync_packages(claude_dir, orchestra_path, ["core"], set())
        assert count == 1
        assert "agents/testing-reality-checker.md" in files
        assert (claude_dir / "agents" / "testing-reality-checker.md").exists()

    def test_syncs_config_file(self, tmp_path):
        """config ファイルを config/{pkg_name}/ 以下に同期する。"""
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "agent-routing"
        pkg_dir.mkdir(parents=True)

        config_file = pkg_dir / "cli-tools.yaml"
        config_file.write_text("codex:\n  model: gpt-5\n")

        manifest = {"name": "agent-routing", "agents": [], "config": ["cli-tools.yaml"]}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)

        count, files = sync_engine.sync_packages(
            claude_dir, orchestra_path, ["agent-routing"], set()
        )
        assert count == 1
        assert "config/agent-routing/cli-tools.yaml" in files

    def test_syncs_nested_config_file_without_flattening(self, tmp_path):
        """config 配下の相対ディレクトリを package namespace 内で保持する。"""
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "loop-harness"
        config_file = pkg_dir / "config" / "loops" / "issue-loop.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text("id: issue-loop\n", encoding="utf-8")
        manifest = {
            "name": "loop-harness",
            "agents": [],
            "config": ["config/loops/issue-loop.yaml"],
        }
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)

        count, files = sync_engine.sync_packages(
            claude_dir, orchestra_path, ["loop-harness"], set()
        )

        expected = "config/loop-harness/loops/issue-loop.yaml"
        assert count == 1
        assert expected in files
        assert (claude_dir / expected).read_text(encoding="utf-8") == "id: issue-loop\n"
        assert not (claude_dir / "config" / "loop-harness" / "issue-loop.yaml").exists()

    def test_skips_facet_managed(self, tmp_path):
        """facet_managed に含まれるファイルをスキップする。"""
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "core"
        pkg_dir.mkdir(parents=True)

        agent_file = pkg_dir / "agents" / "test.md"
        agent_file.parent.mkdir()
        agent_file.write_text("test")

        manifest = {"name": "core", "agents": ["agents/test.md"], "config": []}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)

        count, files = sync_engine.sync_packages(
            claude_dir, orchestra_path, ["core"], {"agents/test.md"}
        )
        assert count == 0
        assert "agents/test.md" not in files

    def test_missing_manifest_skipped(self, tmp_path):
        """manifest.json がないパッケージをスキップする。"""
        orchestra_path = tmp_path / "orchestra"
        (orchestra_path / "packages" / "no-manifest").mkdir(parents=True)

        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)

        count, files = sync_engine.sync_packages(claude_dir, orchestra_path, ["no-manifest"], set())
        assert count == 0
        assert files == set()

    def test_syncs_directory(self, tmp_path):
        """ディレクトリ内のファイルを再帰的に同期する。"""
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "core"
        config_dir = pkg_dir / "config" / "sub"
        config_dir.mkdir(parents=True)
        (config_dir / "a.yaml").write_text("a: 1")
        (config_dir / "b.yaml").write_text("b: 2")

        manifest = {"name": "core", "agents": [], "config": ["config"]}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)

        count, files = sync_engine.sync_packages(claude_dir, orchestra_path, ["core"], set())
        assert count == 2


class TestSyncHooks:
    """sync_hooks のテスト（Issue #95 T3: 配布 hook の timeout 同期）。"""

    def _write_manifest(self, orchestra_path: Path, pkg_name: str, hooks: dict) -> None:
        pkg_dir = orchestra_path / "packages" / pkg_name
        pkg_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"name": pkg_name, "hooks": hooks}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _write_settings(self, project_dir: Path, hooks: dict) -> Path:
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path = claude_dir / "settings.local.json"
        settings_path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        return settings_path

    def test_new_hook_registered_with_manifest_timeout(self, tmp_path):
        """manifest に timeout 指定がある新規 hook は、その値で settings に登録される。"""
        orchestra_path = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        self._write_manifest(
            orchestra_path,
            "codd",
            {
                "PostToolUse": [
                    {"file": "codd-scan-postedit.py", "matcher": "Edit|Write", "timeout": 90}
                ]
            },
        )
        settings_path = self._write_settings(project_dir, {})

        changes = sync_engine.sync_hooks(project_dir, orchestra_path, ["codd"])

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        hook = settings["hooks"]["PostToolUse"][0]["hooks"][0]
        assert changes == 1
        assert hook["timeout"] == 90

    def test_new_hook_without_manifest_timeout_defaults_to_five(self, tmp_path):
        """timeout 未指定の既存パッケージは従来通り既定値 5 で登録される（後方互換）。"""
        orchestra_path = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        self._write_manifest(
            orchestra_path,
            "core",
            {"SessionStart": [{"file": "check-plan-gate.py"}]},
        )
        settings_path = self._write_settings(project_dir, {})

        changes = sync_engine.sync_hooks(project_dir, orchestra_path, ["core"])

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        hook = settings["hooks"]["SessionStart"][0]["hooks"][0]
        assert changes == 1
        assert hook["timeout"] == 5

    def test_existing_hook_with_stale_timeout_is_updated(self, tmp_path):
        """既に登録済みの hook の timeout が manifest 側と乖離している場合は更新する。

        導入済みプロジェクトが古い timeout（既定 5 秒）で settings.local.json に
        登録済みのまま、ai-orchestra 側の manifest だけが timeout 90 に更新された
        ケースを再現する。
        """
        orchestra_path = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        self._write_manifest(
            orchestra_path,
            "codd",
            {
                "PostToolUse": [
                    {"file": "codd-scan-postedit.py", "matcher": "Edit|Write", "timeout": 90}
                ]
            },
        )
        command = hook_utils.get_hook_command("codd", "codd-scan-postedit.py")
        settings_path = self._write_settings(
            project_dir,
            {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": command, "timeout": 5}],
                    }
                ]
            },
        )

        changes = sync_engine.sync_hooks(project_dir, orchestra_path, ["codd"])

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        hook = settings["hooks"]["PostToolUse"][0]["hooks"][0]
        assert changes == 1
        assert hook["timeout"] == 90
        assert hook["command"] == command

    def test_existing_hook_with_matching_timeout_is_noop(self, tmp_path):
        """既に manifest と同じ timeout で登録済みの場合は変更なしとして扱う。"""
        orchestra_path = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        self._write_manifest(
            orchestra_path,
            "codd",
            {
                "PostToolUse": [
                    {"file": "codd-scan-postedit.py", "matcher": "Edit|Write", "timeout": 90}
                ]
            },
        )
        command = hook_utils.get_hook_command("codd", "codd-scan-postedit.py")
        self._write_settings(
            project_dir,
            {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": command, "timeout": 90}],
                    }
                ]
            },
        )

        changes = sync_engine.sync_hooks(project_dir, orchestra_path, ["codd"])

        assert changes == 0

    def test_legacy_interpreter_hook_is_migrated_to_current_command(self, tmp_path):
        """旧表記（リテラル python3）の hook は現行表記へ差し替えられる（Issue #343）。

        導入済みプロジェクトの settings.local.json が PATH 依存の python3 で登録済みの
        まま残ると、hook 起動シェルの PATH 次第で全 hook が失敗する。SessionStart 同期で
        現行表記へ移行し、旧エントリを残さない（重複起動させない）ことを確認する。
        """
        orchestra_path = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        self._write_manifest(
            orchestra_path,
            "codd",
            {
                "PostToolUse": [
                    {"file": "codd-scan-postedit.py", "matcher": "Edit|Write", "timeout": 90}
                ]
            },
        )
        legacy_command = 'python3 "$AI_ORCHESTRA_DIR/packages/codd/hooks/codd-scan-postedit.py"'
        settings_path = self._write_settings(
            project_dir,
            {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": legacy_command, "timeout": 90}],
                    }
                ]
            },
        )

        sync_engine.sync_hooks(project_dir, orchestra_path, ["codd"])

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        commands = [hook["command"] for hook in settings["hooks"]["PostToolUse"][0]["hooks"]]
        assert commands == [hook_utils.get_hook_command("codd", "codd-scan-postedit.py")]

    def test_legacy_sync_hook_is_migrated_and_not_pruned(self, tmp_path):
        """旧表記の sync-orchestra hook は削除されず現行表記へ書き換えられる（Issue #343）。"""
        orchestra_path = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        self._write_manifest(orchestra_path, "core", {})
        legacy_command = 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"'
        settings_path = self._write_settings(
            project_dir,
            {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": legacy_command, "timeout": 15}]}
                ]
            },
        )

        changes = sync_engine.sync_hooks(project_dir, orchestra_path, ["core"])

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = settings["hooks"]["SessionStart"][0]["hooks"]
        assert changes == 1
        assert [h["command"] for h in hooks] == [hook_utils.SYNC_HOOK_COMMAND]
        assert hooks[0]["timeout"] == 15

    def test_duplicated_sync_hook_entries_are_collapsed(self, tmp_path):
        """新旧表記の sync hook が併存する場合は 1 件に畳む（二重起動の防止）。"""
        orchestra_path = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        self._write_manifest(orchestra_path, "core", {})
        legacy_command = 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"'
        settings_path = self._write_settings(
            project_dir,
            {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": legacy_command, "timeout": 15},
                            {
                                "type": "command",
                                "command": hook_utils.SYNC_HOOK_COMMAND,
                                "timeout": 15,
                            },
                        ]
                    }
                ]
            },
        )

        sync_engine.sync_hooks(project_dir, orchestra_path, ["core"])

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = settings["hooks"]["SessionStart"][0]["hooks"]
        assert [h["command"] for h in hooks] == [hook_utils.SYNC_HOOK_COMMAND]

    def test_sync_does_not_write_global_interpreter_env(self, tmp_path, monkeypatch):
        """EV-37（Issue #343）: SessionStart 同期は env.AI_ORCHESTRA_PYTHON を書かない。

        sync hook 自身が `PATH` の `python3` で起動されうるため、そこで観測した
        インタプリタを固定すると、本 Issue が想定する劣化したインタプリタを恒久化して
        しまう。表記の移行だけを行い、インタプリタ値の決定は init/setup 側に残す。
        """
        orchestra_path = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        self._write_manifest(orchestra_path, "core", {})
        legacy_command = 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"'
        self._write_settings(
            project_dir,
            {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": legacy_command, "timeout": 15}]}
                ]
            },
        )

        sync_engine.sync_hooks(project_dir, orchestra_path, ["core"])

        assert not (home / ".claude" / "settings.json").exists()


class TestSyncPackagesAgentsHashGuard:
    """sync_packages() の agents ハッシュガードのテスト（Issue #241）。"""

    def _sha256(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _setup_pkg(self, tmp_path: Path, distributed_content: str) -> tuple[Path, Path, Path]:
        orchestra_path = tmp_path / "orchestra"
        pkg_dir = orchestra_path / "packages" / "core"
        pkg_dir.mkdir(parents=True)
        agent_file = pkg_dir / "agents" / "foo.md"
        agent_file.parent.mkdir()
        agent_file.write_text(distributed_content, encoding="utf-8")
        manifest = {"name": "core", "agents": ["agents/foo.md"], "config": []}
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)
        return orchestra_path, claude_dir, agent_file

    def test_user_modified_agent_file_is_not_overwritten(self, tmp_path: Path, capsys) -> None:
        """記録ハッシュと現在の内容が異なる（ユーザー編集済み）場合は上書きしない。"""
        orchestra_path, claude_dir, agent_file = self._setup_pkg(tmp_path, "v2 distributed")

        dst = claude_dir / "agents" / "foo.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("user edited", encoding="utf-8")
        # source を dst より新しくする（ガード無しなら needs_sync=True で上書きされる）
        future = time.time() + 10
        os.utime(agent_file, (future, future))

        orch = {"file_hashes": {"core": {"agents/foo.md": self._sha256("v1 distributed")}}}

        count, files = sync_engine.sync_packages(claude_dir, orchestra_path, ["core"], set(), orch)

        assert count == 0
        assert "agents/foo.md" in files
        assert dst.read_text(encoding="utf-8") == "user edited"
        assert "warn" in capsys.readouterr().err

    def test_unmodified_agent_file_still_syncs_and_updates_hash(self, tmp_path: Path) -> None:
        """記録ハッシュと現在の内容が一致（未編集）なら同期し、ハッシュ台帳を更新する。"""
        orchestra_path, claude_dir, agent_file = self._setup_pkg(tmp_path, "v2 distributed")

        dst = claude_dir / "agents" / "foo.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("v1 distributed", encoding="utf-8")
        future = time.time() + 10
        os.utime(agent_file, (future, future))

        orch = {"file_hashes": {"core": {"agents/foo.md": self._sha256("v1 distributed")}}}

        count, files = sync_engine.sync_packages(claude_dir, orchestra_path, ["core"], set(), orch)

        assert count == 1
        assert dst.read_text(encoding="utf-8") == "v2 distributed"
        assert orch["file_hashes"]["core"]["agents/foo.md"] == self._sha256("v2 distributed")

    def test_no_ledger_entry_syncs_and_starts_recording(self, tmp_path: Path) -> None:
        """未記録（旧 orchestra.json 由来）の場合は従来どおり同期し、以後の記録を開始する。"""
        orchestra_path, claude_dir, agent_file = self._setup_pkg(tmp_path, "v2 distributed")

        dst = claude_dir / "agents" / "foo.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("v1 distributed", encoding="utf-8")
        future = time.time() + 10
        os.utime(agent_file, (future, future))

        orch: dict = {"file_hashes": {}}

        count, files = sync_engine.sync_packages(claude_dir, orchestra_path, ["core"], set(), orch)

        assert count == 1
        assert dst.read_text(encoding="utf-8") == "v2 distributed"
        assert orch["file_hashes"]["core"]["agents/foo.md"] == self._sha256("v2 distributed")

    def test_orch_none_preserves_legacy_mtime_only_behavior(self, tmp_path: Path) -> None:
        """orch を渡さない場合は従来どおり mtime のみで判定する（呼び出し側の後方互換）。"""
        orchestra_path, claude_dir, agent_file = self._setup_pkg(tmp_path, "v2 distributed")

        dst = claude_dir / "agents" / "foo.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("user edited", encoding="utf-8")
        future = time.time() + 10
        os.utime(agent_file, (future, future))

        count, files = sync_engine.sync_packages(claude_dir, orchestra_path, ["core"], set())

        assert count == 1
        assert dst.read_text(encoding="utf-8") == "v2 distributed"
