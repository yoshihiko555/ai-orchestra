"""sync-orchestra.py の build_facets force パラメータのテスト。

テスト観点:
- force=False かつ生成物がソースより新しい場合はビルドをスキップする
- force=True なら生成物がソースより新しくてもビルドを実行する
"""

from __future__ import annotations

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

    # orchestra-manager.py が必要（build_facets がサブプロセスで呼ぶ）
    scripts_dir = orchestra_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    # 実際の orchestra-manager.py をコピーではなくシンボリックリンクで参照
    src_script = REPO_ROOT / "scripts" / "orchestra-manager.py"
    dst_script = scripts_dir / "orchestra-manager.py"
    if not dst_script.exists():
        import shutil

        shutil.copy2(src_script, dst_script)


def _create_stale_generated(project_dir: Path) -> None:
    """生成物をソースより新しいタイムスタンプで作成する。"""
    skills_dir = project_dir / ".claude" / "skills" / "test-skill"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skills_dir / "SKILL.md"
    skill_path.write_text("old-content", encoding="utf-8")
    # 生成物のタイムスタンプを未来に設定
    future_time = time.time() + 3600
    os.utime(skill_path, (future_time, future_time))


class TestBuildFacetsForce:
    def test_skip_when_generated_newer_and_force_false(self, tmp_path: Path) -> None:
        """force=False で生成物がソースより新しい場合はスキップ（return 0）。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)
        _create_stale_generated(project_dir)

        result = build_facets(orchestra_dir, project_dir, force=False)
        assert result == 0

    def test_rebuild_when_force_true(self, tmp_path: Path) -> None:
        """force=True なら生成物がソースより新しくてもビルドを実行する。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)
        _create_stale_generated(project_dir)

        result = build_facets(orchestra_dir, project_dir, force=True)
        assert result > 0

    def test_rebuild_updates_content(self, tmp_path: Path) -> None:
        """force=True でビルドすると生成物の内容が更新される。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)
        _create_stale_generated(project_dir)

        skill_path = project_dir / ".claude" / "skills" / "test-skill" / "SKILL.md"
        assert skill_path.read_text(encoding="utf-8") == "old-content"

        build_facets(orchestra_dir, project_dir, force=True)

        content = skill_path.read_text(encoding="utf-8")
        assert "original-body" in content
        assert "policy-body" in content

    def test_build_when_no_generated_exists(self, tmp_path: Path) -> None:
        """生成物が存在しない場合は force に関わらずビルドする。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_facets(orchestra_dir, project_dir)

        result = build_facets(orchestra_dir, project_dir, force=False)
        assert result > 0
