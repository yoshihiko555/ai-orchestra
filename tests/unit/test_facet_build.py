"""orchestra-manager.py の facet build 機能テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager", "scripts/orchestra-manager.py")
FacetBuilder = manager_mod.FacetBuilder


def _setup_facet_sources(
    orchestra_dir: Path,
    *,
    include_output_contracts: bool = True,
) -> None:
    """facet の最小構成を作成する。"""
    compositions_dir = orchestra_dir / "facets" / "compositions"
    policies_dir = orchestra_dir / "facets" / "policies"
    contracts_dir = orchestra_dir / "facets" / "output-contracts"

    compositions_dir.mkdir(parents=True, exist_ok=True)
    policies_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    (policies_dir / "code-quality.md").write_text(
        "# Code Quality\n\npolicy-body\n",
        encoding="utf-8",
    )
    (policies_dir / "dialog-rules.md").write_text(
        "# Dialog Rules\n\ndialog-policy-body\n",
        encoding="utf-8",
    )
    (contracts_dir / "tiered-review.md").write_text(
        "# Tiered Contract\n\ncontract-body\n",
        encoding="utf-8",
    )

    if include_output_contracts:
        composition = """\
name: simplify
description: sample skill
frontmatter:
  name: simplify
  description: Sample description
  disable-model-invocation: true
policies:
  - code-quality
output_contracts:
  - tiered-review
instruction: |
  # Simplify Code

  simplify-body
"""
    else:
        composition = """\
name: simplify
description: sample skill
frontmatter:
  name: simplify
  description: Sample description
  disable-model-invocation: true
policies:
  - code-quality
instruction: |
  # Simplify Code

  simplify-body
"""

    (compositions_dir / "simplify.yaml").write_text(composition, encoding="utf-8")


def _setup_second_composition(orchestra_dir: Path) -> None:
    """build_all 用に2つ目の composition を作成する。"""
    compositions_dir = orchestra_dir / "facets" / "compositions"
    composition = """\
name: review
description: review skill
frontmatter:
  name: review
  description: Review description
  disable-model-invocation: true
policies:
  - dialog-rules
instruction: |
  # Review

  review-body
"""
    (compositions_dir / "review.yaml").write_text(composition, encoding="utf-8")


class TestFacetBuilder:
    def test_build_single_composition_produces_skill_md(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("simplify", "claude", project_dir)

        assert output_path == project_dir / ".claude" / "skills" / "simplify" / "SKILL.md"
        content = output_path.read_text(encoding="utf-8")
        assert "---\nname: simplify\n" in content
        assert "# Simplify Code" in content
        assert "simplify-body" in content

    def test_resolve_policy_facets_content_appears(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("simplify", "claude", project_dir)

        content = output_path.read_text(encoding="utf-8")
        assert "policy-body" in content
        assert "# Code Quality" in content

    def test_resolve_output_contract_facets_content_appears(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("simplify", "claude", project_dir)

        content = output_path.read_text(encoding="utf-8")
        assert "contract-body" in content
        assert "# Tiered Contract" in content

    def test_missing_facet_file_raises_system_exit(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        (orchestra_dir / "facets" / "policies" / "code-quality.md").unlink()

        builder = FacetBuilder(orchestra_dir)
        with pytest.raises(SystemExit) as exc_info:
            builder.build_one("simplify", "claude", project_dir)

        assert exc_info.value.code == 1

    def test_both_targets_produce_expected_paths(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        claude_path = builder.build_one("simplify", "claude", project_dir)
        codex_path = builder.build_one("simplify", "codex", project_dir)

        assert claude_path == project_dir / ".claude" / "skills" / "simplify" / "SKILL.md"
        assert codex_path == project_dir / ".codex" / "skills" / "simplify" / "SKILL.md"

    def test_frontmatter_is_correctly_formatted(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        composition = builder.load_composition(
            orchestra_dir / "facets" / "compositions" / "simplify.yaml"
        )
        content = builder.build_skill_md(composition)

        assert content.startswith("---\n")
        assert "disable-model-invocation: true" in content
        assert "\n---\n\n# Code Quality" in content

    def test_build_all_builds_all_compositions(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        _setup_second_composition(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        output_paths = builder.build_all("claude", project_dir)

        assert len(output_paths) == 2
        assert (project_dir / ".claude" / "skills" / "simplify" / "SKILL.md").is_file()
        assert (project_dir / ".claude" / "skills" / "review" / "SKILL.md").is_file()

    def test_manifest_installed_package_builds(self, tmp_path: Path) -> None:
        """manifest に含まれ、パッケージがインストール済みならビルドされる。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(
            orchestra_dir,
            manifest_compositions={"simplify": "optional-pkg"},
            installed_packages=["core", "optional-pkg"],
        )
        result = builder.build_one("simplify", "claude", project_dir)

        assert result is not None
        assert result == project_dir / ".claude" / "skills" / "simplify" / "SKILL.md"

    def test_manifest_uninstalled_package_skips(self, tmp_path: Path) -> None:
        """manifest に含まれるが、パッケージが未インストールならスキップされる。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(
            orchestra_dir,
            manifest_compositions={"simplify": "optional-pkg"},
            installed_packages=["core"],
        )
        result = builder.build_one("simplify", "claude", project_dir)

        assert result is None

    def test_global_composition_always_builds(self, tmp_path: Path) -> None:
        """manifest_compositions に含まれない composition はグローバルとして常にビルドされる。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(
            orchestra_dir,
            manifest_compositions={"other-skill": "core"},
            installed_packages=["core"],
        )
        result = builder.build_one("simplify", "claude", project_dir)

        assert result is not None
        assert result == project_dir / ".claude" / "skills" / "simplify" / "SKILL.md"

    def test_no_manifest_compositions_builds_all(self, tmp_path: Path) -> None:
        """manifest_compositions が None の場合は全 composition をビルドする。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir, manifest_compositions=None)
        result = builder.build_one("simplify", "claude", project_dir)

        assert result is not None
        assert result == project_dir / ".claude" / "skills" / "simplify" / "SKILL.md"

    def test_composition_without_output_contracts_works(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir, include_output_contracts=False)

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("simplify", "claude", project_dir)

        content = output_path.read_text(encoding="utf-8")
        assert "# Code Quality" in content
        assert "# Simplify Code" in content
        assert "# Tiered Contract" not in content

    def test_instruction_file_reference(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        instructions_dir = orchestra_dir / "facets" / "instructions"
        instructions_dir.mkdir(parents=True, exist_ok=True)
        (instructions_dir / "my-instruction.md").write_text(
            "# Referenced Instruction\n\nreferenced-body\n",
            encoding="utf-8",
        )
        composition = """\
name: simplify
description: sample skill
frontmatter:
  name: simplify
  description: Sample description
  disable-model-invocation: true
policies:
  - code-quality
output_contracts:
  - tiered-review
instruction: my-instruction
"""
        (orchestra_dir / "facets" / "compositions" / "simplify.yaml").write_text(
            composition,
            encoding="utf-8",
        )

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("simplify", "claude", project_dir)

        content = output_path.read_text(encoding="utf-8")
        assert "# Referenced Instruction" in content
        assert "referenced-body" in content

    def test_inline_instruction_still_works(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("simplify", "claude", project_dir)

        content = output_path.read_text(encoding="utf-8")
        assert "# Simplify Code" in content
        assert "simplify-body" in content

    def test_rule_type_generates_without_frontmatter(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        comp = "name: my-rule\ntype: rule\npolicies:\n  - code-quality\n"
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").write_text(
            comp, encoding="utf-8"
        )
        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("my-rule", "claude", project_dir)
        content = output_path.read_text(encoding="utf-8")
        assert "---" not in content
        assert "policy-body" in content

    def test_rule_type_output_path_is_rules_directory(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        comp = "name: my-rule\ntype: rule\npolicies:\n  - code-quality\n"
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").write_text(
            comp, encoding="utf-8"
        )
        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("my-rule", "claude", project_dir)
        assert output_path == project_dir / ".claude" / "rules" / "my-rule.md"

    def test_rule_type_instruction_optional(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        comp = "name: my-rule\ntype: rule\npolicies:\n  - code-quality\n"
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").write_text(
            comp, encoding="utf-8"
        )
        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("my-rule", "claude", project_dir)
        content = output_path.read_text(encoding="utf-8")
        assert "policy-body" in content

    def test_default_type_is_skill(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("simplify", "claude", project_dir)
        assert output_path == project_dir / ".claude" / "skills" / "simplify" / "SKILL.md"
        content = output_path.read_text(encoding="utf-8")
        assert content.startswith("---")

    def test_project_local_facet_overrides_orchestra(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        # Create a project-local facet that overrides the orchestra policy
        local_facets_dir = project_dir / ".claude" / "facets"
        local_policies_dir = local_facets_dir / "policies"
        local_policies_dir.mkdir(parents=True, exist_ok=True)
        (local_policies_dir / "code-quality.md").write_text(
            "# Local Code Quality\n\nlocal-policy-body\n",
            encoding="utf-8",
        )

        builder = FacetBuilder(orchestra_dir=orchestra_dir, project_facets_dir=local_facets_dir)
        output_path = builder.build_one("simplify", "claude", project_dir)

        content = output_path.read_text(encoding="utf-8")
        assert "local-policy-body" in content
        assert "\npolicy-body\n" not in content

    def test_project_local_composition_is_found(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        # Create a composition only in project-local (not in orchestra)
        local_facets_dir = project_dir / ".claude" / "facets"
        local_compositions_dir = local_facets_dir / "compositions"
        local_compositions_dir.mkdir(parents=True, exist_ok=True)
        local_composition = """name: local-only
description: local only skill
frontmatter:
  name: local-only
  description: Local only description
  disable-model-invocation: true
policies:
  - code-quality
instruction: |
  # Local Only

  local-only-body
"""
        (local_compositions_dir / "local-only.yaml").write_text(local_composition, encoding="utf-8")

        builder = FacetBuilder(orchestra_dir=orchestra_dir, project_facets_dir=local_facets_dir)
        output_path = builder.build_one("local-only", "claude", project_dir)

        assert output_path == project_dir / ".claude" / "skills" / "local-only" / "SKILL.md"
        content = output_path.read_text(encoding="utf-8")
        assert "local-only-body" in content

    def test_build_all_merges_both_locations(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        _setup_second_composition(orchestra_dir)

        # Create a project-local composition (different name to avoid duplicate)
        local_facets_dir = project_dir / ".claude" / "facets"
        local_compositions_dir = local_facets_dir / "compositions"
        local_compositions_dir.mkdir(parents=True, exist_ok=True)
        local_composition = """name: local-extra
description: local extra skill
frontmatter:
  name: local-extra
  description: Local extra description
  disable-model-invocation: true
policies:
  - code-quality
instruction: |
  # Local Extra

  local-extra-body
"""
        (local_compositions_dir / "local-extra.yaml").write_text(
            local_composition, encoding="utf-8"
        )

        builder = FacetBuilder(orchestra_dir=orchestra_dir, project_facets_dir=local_facets_dir)
        output_paths = builder.build_all("claude", project_dir)

        # Should have 3 total: 2 from orchestra + 1 from project-local
        assert len(output_paths) == 3
        names = {p.parent.name for p in output_paths}
        assert "simplify" in names
        assert "review" in names
        assert "local-extra" in names

    def test_extract_recovers_instruction(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        # Build first to generate SKILL.md
        output_path = builder.build_one("simplify", "claude", project_dir)

        # Modify the instruction section in the generated SKILL.md
        content = output_path.read_text(encoding="utf-8")
        modified = content.replace("simplify-body", "tuned-body")
        output_path.write_text(modified, encoding="utf-8")

        # Extract should write the tuned instruction back to source
        instruction_path = builder.extract_one("simplify", "claude", project_dir)

        assert instruction_path is not None
        extracted = instruction_path.read_text(encoding="utf-8")
        assert "tuned-body" in extracted
        assert "# Simplify Code" in extracted

    def test_extract_preserves_policy_content(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        builder.build_one("simplify", "claude", project_dir)

        instruction_path = builder.extract_one("simplify", "claude", project_dir)

        assert instruction_path is not None
        extracted = instruction_path.read_text(encoding="utf-8")
        # The instruction section should NOT contain the policy content
        assert "policy-body" not in extracted
        assert "# Code Quality" not in extracted
        # But it should contain the instruction
        assert "simplify-body" in extracted

    def test_extract_rule_type(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        comp = """\
name: my-rule
type: rule
policies:
  - code-quality
instruction: |
  # Rule Instruction

  rule-body
"""
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").write_text(
            comp, encoding="utf-8"
        )

        builder = FacetBuilder(orchestra_dir)
        # Build rule first
        builder.build_one("my-rule", "claude", project_dir)

        # Modify instruction in generated rule
        rule_path = project_dir / ".claude" / "rules" / "my-rule.md"
        content = rule_path.read_text(encoding="utf-8")
        modified = content.replace("rule-body", "tuned-rule-body")
        rule_path.write_text(modified, encoding="utf-8")

        # Extract should recover the instruction without frontmatter stripping
        instruction_path = builder.extract_one("my-rule", "claude", project_dir)

        assert instruction_path is not None
        extracted = instruction_path.read_text(encoding="utf-8")
        assert "tuned-rule-body" in extracted
        assert "# Rule Instruction" in extracted
        # Policy content should not be in extracted instruction
        assert "policy-body" not in extracted

    def test_build_all_no_duplicate_when_same_name_in_both(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        # Create a project-local composition with the SAME name as orchestra composition
        local_facets_dir = project_dir / ".claude" / "facets"
        local_policies_dir = local_facets_dir / "policies"
        local_policies_dir.mkdir(parents=True, exist_ok=True)
        (local_policies_dir / "code-quality.md").write_text(
            "# Local Code Quality\n\nlocal-policy-body\n",
            encoding="utf-8",
        )
        local_compositions_dir = local_facets_dir / "compositions"
        local_compositions_dir.mkdir(parents=True, exist_ok=True)
        local_composition = """name: simplify
description: overridden simplify
frontmatter:
  name: simplify
  description: Overridden description
  disable-model-invocation: true
policies:
  - code-quality
instruction: |
  # Overridden Simplify

  overridden-body
"""
        (local_compositions_dir / "simplify.yaml").write_text(local_composition, encoding="utf-8")

        builder = FacetBuilder(orchestra_dir=orchestra_dir, project_facets_dir=local_facets_dir)
        output_paths = builder.build_all("claude", project_dir)

        # Should have only 1 (no duplicate): project-local overrides orchestra
        assert len(output_paths) == 1
        content = output_paths[0].read_text(encoding="utf-8")
        assert "overridden-body" in content
        assert "simplify-body" not in content


class TestOrphanCleanup:
    """孤児クリーンアップ機能のテスト。"""

    def test_orphan_skill_removed_on_second_build(self, tmp_path: Path) -> None:
        """composition を削除して再ビルドすると、前回の生成物が削除される。"""
        import json

        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        _setup_second_composition(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        builder.build_all("claude", project_dir)

        # simplify と review の両方がビルドされた
        assert (project_dir / ".claude" / "skills" / "simplify" / "SKILL.md").is_file()
        assert (project_dir / ".claude" / "skills" / "review" / "SKILL.md").is_file()

        # マニフェストが作成された
        manifest_path = project_dir / ".claude" / ".facet-manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "simplify" in manifest["skills"]
        assert "review" in manifest["skills"]

        # simplify の composition を削除して再ビルド
        (orchestra_dir / "facets" / "compositions" / "simplify.yaml").unlink()
        builder2 = FacetBuilder(orchestra_dir)
        builder2.build_all("claude", project_dir)

        # simplify は孤児として削除された
        assert not (project_dir / ".claude" / "skills" / "simplify" / "SKILL.md").exists()
        assert not (project_dir / ".claude" / "skills" / "simplify").exists()
        # review は残っている
        assert (project_dir / ".claude" / "skills" / "review" / "SKILL.md").is_file()

    def test_orphan_rule_removed_on_second_build(self, tmp_path: Path) -> None:
        """rule タイプの孤児も削除される。"""

        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        # rule タイプの composition を追加
        rule_comp = "name: my-rule\ntype: rule\npolicies:\n  - code-quality\n"
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").write_text(
            rule_comp, encoding="utf-8"
        )

        builder = FacetBuilder(orchestra_dir)
        builder.build_all("claude", project_dir)
        assert (project_dir / ".claude" / "rules" / "my-rule.md").is_file()

        # rule の composition を削除して再ビルド
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").unlink()
        builder2 = FacetBuilder(orchestra_dir)
        builder2.build_all("claude", project_dir)

        assert not (project_dir / ".claude" / "rules" / "my-rule.md").exists()

    def test_no_manifest_first_build_no_error(self, tmp_path: Path) -> None:
        """初回ビルド（マニフェストなし）でエラーにならない。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        builder = FacetBuilder(orchestra_dir)
        output_paths = builder.build_all("claude", project_dir)
        assert len(output_paths) == 1

    def test_non_facet_skill_not_deleted(self, tmp_path: Path) -> None:
        """facet 管理外のスキルは孤児クリーンアップで削除されない。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)

        # facet 管理外のスキルを手動配置
        manual_skill = project_dir / ".claude" / "skills" / "manual-skill" / "SKILL.md"
        manual_skill.parent.mkdir(parents=True, exist_ok=True)
        manual_skill.write_text("# Manual Skill\n", encoding="utf-8")

        builder = FacetBuilder(orchestra_dir)
        builder.build_all("claude", project_dir)

        # 手動配置のスキルは削除されない
        assert manual_skill.is_file()
