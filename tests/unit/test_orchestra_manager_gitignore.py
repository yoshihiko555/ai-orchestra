"""gitignore 管理と plan ディレクトリ初期化のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.module_loader import load_module

gitignore_mod = load_module("gitignore_sync", "scripts/lib/gitignore_sync.py")
BLOCK_START = gitignore_mod.BLOCK_START
BLOCK_END = gitignore_mod.BLOCK_END
ENTRIES = gitignore_mod.ENTRIES
merge_content = gitignore_mod.merge_content
scaffold_mod = load_module("scaffold_plans", "scripts/lib/scaffold.py")
OrchestraManager = load_module(
    "orchestra_manager_plans", "scripts/orchestra-manager.py"
).OrchestraManager


@pytest.fixture()
def plan_templates(tmp_path: Path) -> Path:
    """旧テンプレートが残る配布元でもコピーを止められるようにする。"""
    orchestra_dir = tmp_path / "orchestra"
    template_dir = orchestra_dir / "templates" / "project"
    template_dir.mkdir(parents=True)
    (orchestra_dir / "packages").mkdir()
    (template_dir / "Plans.md").write_text("# 旧テンプレート\n", encoding="utf-8")
    (template_dir / "plan.md").write_text("## Project: {project-name}\n", encoding="utf-8")
    return orchestra_dir


@pytest.fixture()
def init_manager(plan_templates: Path, monkeypatch: pytest.MonkeyPatch) -> OrchestraManager:
    manager = OrchestraManager(plan_templates)
    monkeypatch.setattr(manager, "setup_env_var", lambda dry_run: None)
    monkeypatch.setattr(manager, "run_initial_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "context_sync", lambda *args, **kwargs: None)
    return manager


class TestMergeGitignoreContent:
    def test_empty_creates_managed_block(self) -> None:
        merged = merge_content("")
        assert BLOCK_START in merged
        assert BLOCK_END in merged
        for entry in ENTRIES:
            assert entry in merged

    def test_appends_block_to_existing_content(self) -> None:
        existing = "node_modules/\n.env\n"
        merged = merge_content(existing)
        assert merged.startswith("node_modules/\n.env\n")
        assert merged.count(BLOCK_START) == 1
        assert ".claude/plans/" in merged
        assert ".claude/Plans.md" in merged
        assert ".claude/Plans.archive.md" in merged

    def test_replaces_existing_managed_block(self) -> None:
        existing = "\n".join(
            [
                "node_modules/",
                BLOCK_START,
                ".claude/old/",
                BLOCK_END,
                "coverage/",
                "",
            ]
        )
        merged = merge_content(existing)
        assert ".claude/old/" not in merged
        assert ".claude/state/" in merged
        assert "node_modules/" in merged
        assert "coverage/" in merged
        assert merged.count(BLOCK_START) == 1

    def test_keeps_existing_when_all_entries_already_present(self) -> None:
        entries = "\n".join(ENTRIES) + "\n"
        merged = merge_content(entries)
        assert merged == entries


class TestClaudeScaffold:
    def test_creates_empty_plans_directory_without_legacy_plan(
        self, tmp_path: Path, plan_templates: Path
    ) -> None:
        project_dir = tmp_path / "project"

        scaffold_mod.ensure_claude_scaffold(project_dir, plan_templates)

        plans_dir = project_dir / ".claude" / "plans"
        assert plans_dir.is_dir()
        assert list(plans_dir.iterdir()) == []
        assert not (project_dir / ".claude" / "Plans.md").exists()

    def test_preserves_existing_legacy_plan(self, tmp_path: Path, plan_templates: Path) -> None:
        project_dir = tmp_path / "project"
        legacy_plan = project_dir / ".claude" / "Plans.md"
        legacy_plan.parent.mkdir(parents=True)
        original = "# 既存の計画\r\n- `cc:WIP` 作業中\r\n".encode()
        legacy_plan.write_bytes(original)

        scaffold_mod.ensure_claude_scaffold(project_dir, plan_templates)

        assert legacy_plan.read_bytes() == original
        assert (project_dir / ".claude" / "plans").is_dir()

    def test_preserves_existing_plan_files_on_repeated_sync(
        self, tmp_path: Path, plan_templates: Path
    ) -> None:
        project_dir = tmp_path / "project"
        scaffold_mod.ensure_claude_scaffold(project_dir, plan_templates)
        plan = project_dir / ".claude" / "plans" / "active-project.md"
        original = "## Project: 作業中\n- `cc:WIP` 続行する\n".encode()
        plan.write_bytes(original)

        created = scaffold_mod.ensure_claude_scaffold(project_dir, plan_templates)

        assert created == 0
        assert plan.read_bytes() == original
        assert list(plan.parent.iterdir()) == [plan]
        assert not (project_dir / ".claude" / "Plans.md").exists()


class TestInitPlanDirectory:
    def test_init_creates_empty_plans_directory_without_legacy_plan(
        self, tmp_path: Path, init_manager: OrchestraManager
    ) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        init_manager.init(str(project_dir))

        plans_dir = project_dir / ".claude" / "plans"
        assert plans_dir.is_dir()
        assert list(plans_dir.iterdir()) == []
        assert not (project_dir / ".claude" / "Plans.md").exists()

    def test_init_preserves_existing_legacy_plan(
        self, tmp_path: Path, init_manager: OrchestraManager
    ) -> None:
        project_dir = tmp_path / "project"
        legacy_plan = project_dir / ".claude" / "Plans.md"
        legacy_plan.parent.mkdir(parents=True)
        original = "# 既存の計画\r\n- `cc:TODO` 続行する\r\n".encode()
        legacy_plan.write_bytes(original)

        init_manager.init(str(project_dir))

        assert legacy_plan.read_bytes() == original
        assert (project_dir / ".claude" / "plans").is_dir()

    def test_init_dry_run_does_not_create_plan_directory(
        self,
        tmp_path: Path,
        init_manager: OrchestraManager,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        init_manager.init(str(project_dir), dry_run=True)

        assert not (project_dir / ".claude").exists()
        output = capsys.readouterr().out
        assert "[DRY-RUN] ディレクトリ作成: .claude/plans" in output
        assert "Plans.md" not in output
