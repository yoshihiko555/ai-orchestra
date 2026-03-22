"""FacetBuilder の knowledge/scripts リソース整合性テスト。"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager", "scripts/orchestra-manager.py")
FacetBuilder = manager_mod.FacetBuilder

REPO_ROOT = Path(__file__).resolve().parents[2]
FACETS_DIR = REPO_ROOT / "facets"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_minimal_sources(
    orchestra_dir: Path,
    *,
    knowledge: list[str] | None = None,
    scripts: list[str] | None = None,
) -> None:
    """テスト用の最小 facet 構成を作成する。"""
    compositions_dir = orchestra_dir / "facets" / "compositions"
    policies_dir = orchestra_dir / "facets" / "policies"
    compositions_dir.mkdir(parents=True, exist_ok=True)
    policies_dir.mkdir(parents=True, exist_ok=True)

    (policies_dir / "base-policy.md").write_text(
        "# Base Policy\n\nbase-policy-body\n", encoding="utf-8"
    )

    composition: dict[str, object] = {
        "name": "sample-skill",
        "frontmatter": {
            "name": "sample-skill",
            "description": "Sample description",
            "disable-model-invocation": True,
        },
        "policies": ["base-policy"],
        "instruction": "# Sample\n\nsample-body\n",
    }

    if knowledge:
        composition["knowledge"] = knowledge
        knowledge_dir = orchestra_dir / "facets" / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        for kname in knowledge:
            (knowledge_dir / f"{kname}.md").write_text(
                f"# {kname}\n\n{kname}-content\n", encoding="utf-8"
            )

    if scripts:
        composition["scripts"] = scripts
        scripts_dir = orchestra_dir / "facets" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for sname in scripts:
            (scripts_dir / sname).write_text(
                f"# {sname} script\nprint('hello')\n", encoding="utf-8"
            )

    (compositions_dir / "sample-skill.yaml").write_text(
        yaml.safe_dump(composition, allow_unicode=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Real-repo integrity tests
# ---------------------------------------------------------------------------


class TestRealCompositionIntegrity:
    def test_all_composition_knowledge_files_exist(self) -> None:
        """全 composition.yaml の knowledge エントリに対応するファイルが存在する。"""
        missing: list[str] = []

        for comp_path in sorted(FACETS_DIR.glob("compositions/*.yaml")):
            try:
                comp = yaml.safe_load(comp_path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            if not isinstance(comp, dict):
                continue

            for kname in comp.get("knowledge", []):
                expected = FACETS_DIR / "knowledge" / f"{kname}.md"
                if not expected.exists():
                    missing.append(f"{comp_path.name}: knowledge '{kname}' -> {expected} not found")

        assert missing == [], "Missing knowledge files:\n" + "\n".join(missing)

    def test_all_composition_script_files_exist(self) -> None:
        """全 composition.yaml の scripts エントリに対応するファイルが存在する。"""
        missing: list[str] = []

        for comp_path in sorted(FACETS_DIR.glob("compositions/*.yaml")):
            try:
                comp = yaml.safe_load(comp_path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            if not isinstance(comp, dict):
                continue

            for sname in comp.get("scripts", []):
                expected = FACETS_DIR / "scripts" / sname
                if not expected.exists():
                    missing.append(f"{comp_path.name}: scripts '{sname}' -> {expected} not found")

        assert missing == [], "Missing script files:\n" + "\n".join(missing)


# ---------------------------------------------------------------------------
# FacetBuilder unit tests
# ---------------------------------------------------------------------------


class TestKnowledgeDeployment:
    def test_knowledge_deployed_to_references(self, tmp_path: Path) -> None:
        """knowledge エントリのファイルが skill_dir/references/ にデプロイされる。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["sample-kb"])

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)

        # Assert
        assert output_path is not None
        skill_dir = output_path.parent
        deployed = skill_dir / "references" / "sample-kb.md"
        assert deployed.is_file(), f"Expected {deployed} to exist"
        content = deployed.read_text(encoding="utf-8")
        assert "sample-kb-content" in content

    def test_scripts_deployed_to_scripts_dir(self, tmp_path: Path) -> None:
        """scripts エントリのファイルが skill_dir/scripts/ にデプロイされる。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, scripts=["run.py"])

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)

        # Assert
        assert output_path is not None
        skill_dir = output_path.parent
        deployed = skill_dir / "scripts" / "run.py"
        assert deployed.is_file(), f"Expected {deployed} to exist"
        content = deployed.read_text(encoding="utf-8")
        assert "print('hello')" in content

    def test_multiple_knowledge_files_all_deployed(self, tmp_path: Path) -> None:
        """複数の knowledge エントリが全て references/ にデプロイされる。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["kb-alpha", "kb-beta"])

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)

        # Assert
        assert output_path is not None
        skill_dir = output_path.parent
        assert (skill_dir / "references" / "kb-alpha.md").is_file()
        assert (skill_dir / "references" / "kb-beta.md").is_file()


class TestSkillMdReferencesSection:
    def test_skill_md_contains_additional_resources_section(self, tmp_path: Path) -> None:
        """knowledge を持つスキルの SKILL.md に '## Additional resources' セクションが含まれる。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["my-kb"])

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)

        # Assert
        assert output_path is not None
        content = output_path.read_text(encoding="utf-8")
        assert "## Additional resources" in content

    def test_skill_md_contains_correct_markdown_link(self, tmp_path: Path) -> None:
        """knowledge エントリの markdown リンクが正しい形式で SKILL.md に含まれる。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["my-kb"])

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)

        # Assert
        assert output_path is not None
        content = output_path.read_text(encoding="utf-8")
        assert "[references/my-kb.md](references/my-kb.md)" in content

    def test_skill_md_contains_links_for_all_knowledge_entries(self, tmp_path: Path) -> None:
        """複数の knowledge エントリが全て SKILL.md のリンクとして含まれる。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["kb-one", "kb-two"])

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)

        # Assert
        assert output_path is not None
        content = output_path.read_text(encoding="utf-8")
        assert "[references/kb-one.md](references/kb-one.md)" in content
        assert "[references/kb-two.md](references/kb-two.md)" in content


class TestBackwardCompatibility:
    def test_composition_without_knowledge_scripts_works(self, tmp_path: Path) -> None:
        """knowledge/scripts キーを持たない composition が正常にビルドされる。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir)  # no knowledge or scripts

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)

        # Assert
        assert output_path is not None
        content = output_path.read_text(encoding="utf-8")
        assert "## Additional resources" not in content

        skill_dir = output_path.parent
        assert not (skill_dir / "references").exists()
        assert not (skill_dir / "scripts").exists()

    def test_composition_without_knowledge_scripts_skill_md_intact(self, tmp_path: Path) -> None:
        """knowledge/scripts なし構成でも SKILL.md の基本コンテンツは正しく生成される。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)

        # Assert
        assert output_path is not None
        content = output_path.read_text(encoding="utf-8")
        assert "name: sample-skill" in content
        assert content.startswith("---\n")
        assert "base-policy-body" in content
        assert "# Sample" in content
        assert "sample-body" in content


class TestOrphanCleanupWithResources:
    def test_cleanup_removes_references_and_scripts_dirs(self, tmp_path: Path) -> None:
        """孤児スキルの削除時に references/ と scripts/ サブディレクトリも削除される。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["kb-x"], scripts=["tool.py"])

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_all("claude", project_dir)
        assert len(output_path) == 1

        skill_dir = output_path[0].parent
        assert (skill_dir / "references" / "kb-x.md").is_file()
        assert (skill_dir / "scripts" / "tool.py").is_file()

        # Act — remove the composition and rebuild
        (orchestra_dir / "facets" / "compositions" / "sample-skill.yaml").unlink()
        builder2 = FacetBuilder(orchestra_dir)

        # Need at least one composition for build_all to succeed; add a dummy
        dummy_comp = {
            "name": "dummy-skill",
            "frontmatter": {
                "name": "dummy-skill",
                "description": "Dummy",
                "disable-model-invocation": True,
            },
            "policies": ["base-policy"],
            "instruction": "# Dummy\n\ndummy-body\n",
        }
        (orchestra_dir / "facets" / "compositions" / "dummy-skill.yaml").write_text(
            yaml.safe_dump(dummy_comp, allow_unicode=True), encoding="utf-8"
        )
        builder2.build_all("claude", project_dir)

        # Assert — both resource subdirs and the skill dir itself are gone
        assert not (skill_dir / "references").exists()
        assert not (skill_dir / "scripts").exists()
        assert not skill_dir.exists()

    def test_cleanup_leaves_surviving_skill_references_intact(self, tmp_path: Path) -> None:
        """孤児でないスキルの references/ は削除されない。"""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["keep-kb"])

        # Add a second composition (orphan candidate)
        orphan_comp = {
            "name": "orphan-skill",
            "frontmatter": {
                "name": "orphan-skill",
                "description": "Orphan",
                "disable-model-invocation": True,
            },
            "policies": ["base-policy"],
            "instruction": "# Orphan\n\norphan-body\n",
        }
        (orchestra_dir / "facets" / "compositions" / "orphan-skill.yaml").write_text(
            yaml.safe_dump(orphan_comp, allow_unicode=True), encoding="utf-8"
        )

        builder = FacetBuilder(orchestra_dir)
        paths = builder.build_all("claude", project_dir)
        assert len(paths) == 2

        survivor_skill_dir = project_dir / ".claude" / "skills" / "sample-skill"
        assert (survivor_skill_dir / "references" / "keep-kb.md").is_file()

        # Act — remove only the orphan composition
        (orchestra_dir / "facets" / "compositions" / "orphan-skill.yaml").unlink()
        builder2 = FacetBuilder(orchestra_dir)
        builder2.build_all("claude", project_dir)

        # Assert — survivor's references remain untouched
        assert (survivor_skill_dir / "references" / "keep-kb.md").is_file()


# ---------------------------------------------------------------------------
# Regression tests for Issue #20: data loss prevention
# ---------------------------------------------------------------------------


class TestExtractOneStripsResources:
    def test_extract_one_strips_additional_resources_section(self, tmp_path: Path) -> None:
        """extract_one should NOT include the auto-generated Additional resources section in the extracted instruction."""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["ref-doc"])

        builder = FacetBuilder(orchestra_dir)

        # Act
        output_path = builder.build_one("sample-skill", "claude", project_dir)
        assert output_path is not None

        # Verify SKILL.md contains the section (precondition)
        skill_md_content = output_path.read_text(encoding="utf-8")
        assert "## Additional resources" in skill_md_content

        extracted_path = builder.extract_one("sample-skill", "claude", project_dir)
        assert extracted_path is not None

        # Assert — extracted instruction must NOT contain the auto-generated section
        extracted_content = extracted_path.read_text(encoding="utf-8")
        assert "## Additional resources" not in extracted_content


class TestStaleResourcesCleanup:
    def test_stale_knowledge_removed_on_rebuild(self, tmp_path: Path) -> None:
        """When a knowledge entry is removed from composition, the old file in references/ should be cleaned up on rebuild."""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["kb-a", "kb-b"])

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("sample-skill", "claude", project_dir)
        assert output_path is not None

        skill_dir = output_path.parent
        assert (skill_dir / "references" / "kb-a.md").is_file()
        assert (skill_dir / "references" / "kb-b.md").is_file()

        # Act — rewrite composition to only include kb-a, and remove kb-b source
        new_composition: dict[str, object] = {
            "name": "sample-skill",
            "frontmatter": {
                "name": "sample-skill",
                "description": "Sample description",
                "disable-model-invocation": True,
            },
            "policies": ["base-policy"],
            "instruction": "# Sample\n\nsample-body\n",
            "knowledge": ["kb-a"],
        }
        (orchestra_dir / "facets" / "compositions" / "sample-skill.yaml").write_text(
            yaml.safe_dump(new_composition, allow_unicode=True), encoding="utf-8"
        )
        (orchestra_dir / "facets" / "knowledge" / "kb-b.md").unlink()

        builder2 = FacetBuilder(orchestra_dir)
        builder2.build_one("sample-skill", "claude", project_dir)

        # Assert — kb-b.md removed, kb-a.md remains
        assert not (skill_dir / "references" / "kb-b.md").exists()
        assert (skill_dir / "references" / "kb-a.md").is_file()

    def test_stale_scripts_removed_on_rebuild(self, tmp_path: Path) -> None:
        """When a script entry is removed from composition, the old file in scripts/ should be cleaned up on rebuild."""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, scripts=["script-a.py", "script-b.py"])

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("sample-skill", "claude", project_dir)
        assert output_path is not None

        skill_dir = output_path.parent
        assert (skill_dir / "scripts" / "script-a.py").is_file()
        assert (skill_dir / "scripts" / "script-b.py").is_file()

        # Act — rewrite composition to only include script-a.py, and remove script-b.py source
        new_composition: dict[str, object] = {
            "name": "sample-skill",
            "frontmatter": {
                "name": "sample-skill",
                "description": "Sample description",
                "disable-model-invocation": True,
            },
            "policies": ["base-policy"],
            "instruction": "# Sample\n\nsample-body\n",
            "scripts": ["script-a.py"],
        }
        (orchestra_dir / "facets" / "compositions" / "sample-skill.yaml").write_text(
            yaml.safe_dump(new_composition, allow_unicode=True), encoding="utf-8"
        )
        (orchestra_dir / "facets" / "scripts" / "script-b.py").unlink()

        builder2 = FacetBuilder(orchestra_dir)
        builder2.build_one("sample-skill", "claude", project_dir)

        # Assert — script-b.py removed, script-a.py remains
        assert not (skill_dir / "scripts" / "script-b.py").exists()
        assert (skill_dir / "scripts" / "script-a.py").is_file()


class TestFullLifecycleKnowledgeCleanup:
    def test_full_lifecycle_knowledge_cleanup(self, tmp_path: Path) -> None:
        """Full lifecycle: knowledge files are deployed on build and cleaned up on orphan removal."""
        # Arrange
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_minimal_sources(orchestra_dir, knowledge=["lifecycle-kb"])

        builder = FacetBuilder(orchestra_dir)
        paths = builder.build_all("claude", project_dir)
        assert len(paths) == 1

        skill_dir = paths[0].parent
        references_dir = skill_dir / "references"
        assert references_dir.is_dir()
        assert (references_dir / "lifecycle-kb.md").is_file()

        # Act — simulate orphan removal by deleting composition and rebuilding with dummy
        (orchestra_dir / "facets" / "compositions" / "sample-skill.yaml").unlink()

        dummy_comp: dict[str, object] = {
            "name": "dummy-skill",
            "frontmatter": {
                "name": "dummy-skill",
                "description": "Dummy",
                "disable-model-invocation": True,
            },
            "policies": ["base-policy"],
            "instruction": "# Dummy\n\ndummy-body\n",
        }
        (orchestra_dir / "facets" / "compositions" / "dummy-skill.yaml").write_text(
            yaml.safe_dump(dummy_comp, allow_unicode=True), encoding="utf-8"
        )

        builder2 = FacetBuilder(orchestra_dir)
        builder2.build_all("claude", project_dir)

        # Assert — references/ dir and skill dir are fully removed
        assert not references_dir.exists()
        assert not skill_dir.exists()
