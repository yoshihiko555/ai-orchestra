"""sync-orchestra.py の build_facets mtime スキップのテスト。

テスト観点:
- 生成物がソースより新しい場合はビルドをスキップする
- 生成物が存在しない場合はビルドする
- ソースが更新された場合はビルドする
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from tests.module_loader import REPO_ROOT, load_module

sync_mod = load_module("sync_orchestra", "scripts/sync-orchestra.py")
build_facets = sync_mod.build_facets


def _setup_minimal_facets(orchestra_dir: Path, project_dir: Path) -> None:
    """build_facets が動作する最小構成を作成する。"""
    # composition
    compositions_dir = orchestra_dir / "facets" / "compositions"
    compositions_dir.mkdir(parents=True, exist_ok=True)
    (compositions_dir / "test-skill.yaml").write_text(
        """\
name: test-skill
description: test
frontmatter:
  name: test-skill
  description: test
policies:
  - test-policy
instruction: |
  # Test
  original-body
""",
        encoding="utf-8",
    )

    # policy
    policies_dir = orchestra_dir / "facets" / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    (policies_dir / "test-policy.md").write_text(
        "# Test Policy\n\npolicy-body\n",
        encoding="utf-8",
    )

    # orchestra-manager.py と依存モジュールが必要（build_facets がサブプロセスで呼ぶ）
    scripts_dir = orchestra_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(
        REPO_ROOT / "scripts" / "orchestra-manager.py", scripts_dir / "orchestra-manager.py"
    )

    lib_dst = scripts_dir / "lib"
    lib_dst.mkdir(exist_ok=True)
    for lib_name in [
        "__init__.py",
        "orchestra_models.py",
        "orchestra_context.py",
        "orchestra_hooks.py",
        "facet_builder.py",
        "gitignore_sync.py",
        "scaffold.py",
        "agent_model_patch.py",
        "hook_utils.py",
        "sync_engine.py",
        "settings_io.py",
        "toml_merge.py",
    ]:
        src_script = REPO_ROOT / "scripts" / "lib" / lib_name
        dst_script = lib_dst / lib_name
        if src_script.exists() and not dst_script.exists():
            shutil.copy2(src_script, dst_script)


def _create_stale_generated(orchestra_dir: Path, project_dir: Path) -> None:
    """生成物をソースより新しいタイムスタンプで作成する。"""
    # 過去にビルド済みの状態として packages-hash も生成する
    build_facets(orchestra_dir, project_dir)
    skills_dir = project_dir / ".claude" / "skills" / "test-skill"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skills_dir / "SKILL.md"
    skill_path.write_text("old-content", encoding="utf-8")
    # 生成物のタイムスタンプを未来に設定
    future_time = time.time() + 3600
    os.utime(skill_path, (future_time, future_time))


class TestBuildFacetsMtime:
    def test_skip_when_generated_newer(self, tmp_path: Path) -> None:
        """生成物がソースより新しい場合はスキップ（return 0）。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)
        _create_stale_generated(orchestra_dir, project_dir)

        result = build_facets(orchestra_dir, project_dir)
        assert result == 0

    def test_build_when_source_newer(self, tmp_path: Path) -> None:
        """ソースが生成物より新しい場合はビルドする。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)
        _create_stale_generated(orchestra_dir, project_dir)

        # ソースのタイムスタンプを生成物より新しくする
        skill_path = project_dir / ".claude" / "skills" / "test-skill" / "SKILL.md"
        composition_path = orchestra_dir / "facets" / "compositions" / "test-skill.yaml"
        far_future = time.time() + 7200
        os.utime(composition_path, (far_future, far_future))

        result = build_facets(orchestra_dir, project_dir)
        assert result > 0

        content = skill_path.read_text(encoding="utf-8")
        assert "original-body" in content
        assert "policy-body" in content

    def test_build_when_no_generated_exists(self, tmp_path: Path) -> None:
        """生成物が存在しない場合はビルドする。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)

        result = build_facets(orchestra_dir, project_dir)
        assert result > 0

    def test_packages_hash_written_to_cache_dir(self, tmp_path: Path) -> None:
        """packages-hash が .cache 未作成状態から .claude/.cache/ 配下に生成される。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)

        # hash 書き込み経路の前提となる orchestra.json を用意する
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "orchestra.json").write_text(
            json.dumps({"installed_packages": ["core"]}), encoding="utf-8"
        )

        # .cache は未作成の状態から開始する
        assert not (claude_dir / ".cache").exists()

        build_facets(orchestra_dir, project_dir)

        hash_file = claude_dir / ".cache" / "packages-hash"
        assert hash_file.is_file()
        assert hash_file.read_text(encoding="utf-8").strip() != ""

    def test_cleanup_orphan_when_composition_deleted(self, tmp_path: Path) -> None:
        """composition の削除だけでも再ビルドし、孤立した生成物を削除する。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)

        compositions_dir = orchestra_dir / "facets" / "compositions"
        second_composition = compositions_dir / "test-skill-2.yaml"
        second_composition.write_text(
            """\
name: test-skill-2
description: test 2
frontmatter:
  name: test-skill-2
  description: test 2
policies:
  - test-policy
instruction: |
  # Test 2
  original-body-2
""",
            encoding="utf-8",
        )

        result = build_facets(orchestra_dir, project_dir)
        assert result > 0

        first_skill = project_dir / ".claude" / "skills" / "test-skill" / "SKILL.md"
        second_skill = project_dir / ".claude" / "skills" / "test-skill-2" / "SKILL.md"
        assert first_skill.is_file()
        assert second_skill.is_file()

        time.sleep(0.01)
        os.utime(first_skill, None)
        os.utime(second_skill, None)
        second_composition.unlink()

        build_facets(orchestra_dir, project_dir)

        assert first_skill.is_file()
        assert not second_skill.exists()

    def test_generated_survives_when_all_compositions_disappear(self, tmp_path: Path) -> None:
        """composition が 0 件になった場合は掃除せず生成物を温存する（fail-closed）。

        0 件は「最後の 1 件を意図的に削除した」よりも orchestra_dir の設定ミス・導入破損
        である可能性が高い。この状態で cleanup を走らせると前回マニフェストの全スキルが
        削除されるため、孤立生成物が 1 件残ることを許容して全削除を防ぐ。
        """
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)

        assert build_facets(orchestra_dir, project_dir) > 0
        skill_path = project_dir / ".claude" / "skills" / "test-skill" / "SKILL.md"
        assert skill_path.is_file()

        time.sleep(0.01)
        os.utime(skill_path, None)
        (orchestra_dir / "facets" / "compositions" / "test-skill.yaml").unlink()

        assert build_facets(orchestra_dir, project_dir) == 0
        assert skill_path.is_file()

    def test_no_build_when_no_compositions_exist(self, tmp_path: Path) -> None:
        """composition が存在しなければ何もせず 0 を返す。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)

        result = build_facets(orchestra_dir, project_dir)

        assert result == 0
        skills_dir = project_dir / ".claude" / "skills"
        assert not skills_dir.exists() or not any(skills_dir.iterdir())
