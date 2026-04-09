"""E2E テスト: Facet-prompt フロー。

テスト計画 e2e-test-plan.md セクション 5 に対応。
詳細は facet-prompt-test-plan.md 参照。
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import run_orchex, run_session_start


def _setup_essential(project: Path) -> None:
    run_orchex("setup", "essential", project=project)
    run_session_start(project, "init")


class TestFacetBuild:
    """5. Facet-prompt フロー"""

    def test_facet_build_all(self, e2e_project: Path) -> None:
        """#45: facet build 基本フロー — 全 composition が正常ビルド"""
        _setup_essential(e2e_project)
        skills = e2e_project / ".claude" / "skills"
        rules = e2e_project / ".claude" / "rules"
        assert (skills / "review" / "SKILL.md").is_file()
        assert (skills / "tdd" / "SKILL.md").is_file()
        assert (rules / "coding-principles.md").is_file()
        assert (rules / "config-loading.md").is_file()

    def test_session_start_mtime_skip(self, e2e_project: Path) -> None:
        """#46: SessionStart 自動ビルド → mtime スキップ"""
        _setup_essential(e2e_project)
        # 2nd: should skip
        result = run_session_start(e2e_project, "s2")
        assert result.stdout.strip() == ""

    def test_local_override(self, e2e_project: Path) -> None:
        """#47: ローカル上書き — .claude/facets/ 手動配置が優先"""
        _setup_essential(e2e_project)
        # Create local policy override
        local_dir = e2e_project / ".claude" / "facets" / "policies"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "code-quality.md").write_text("# E2E LOCAL POLICY\n", encoding="utf-8")
        # Rebuild
        run_orchex("facet", "build", "--name", "coding-principles", project=e2e_project)
        content = (e2e_project / ".claude" / "rules" / "coding-principles.md").read_text(
            encoding="utf-8"
        )
        assert "E2E LOCAL POLICY" in content

    def test_extract(self, e2e_project: Path, orchestra_dir: Path) -> None:
        """#48: extract — instruction の書き戻し"""
        _setup_essential(e2e_project)
        skill_path = e2e_project / ".claude" / "skills" / "tdd" / "SKILL.md"
        instruction_path = orchestra_dir / "facets" / "instructions" / "tdd.md"
        original_instruction = instruction_path.read_text(encoding="utf-8")
        original = skill_path.read_text(encoding="utf-8")
        # Edit instruction section
        edited = original.replace("Red-Green-Refactor", "E2E-EXTRACT-MARKER")
        assert edited != original
        skill_path.write_text(edited, encoding="utf-8")

        run_orchex("facet", "extract", "--name", "tdd", project=e2e_project)

        try:
            instruction = instruction_path.read_text(encoding="utf-8")
            assert "E2E-EXTRACT-MARKER" in instruction
        finally:
            instruction_path.write_text(original_instruction, encoding="utf-8")
