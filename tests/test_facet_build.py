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

    def test_rule_type_generates_without_frontmatter(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        comp = "name: my-rule\ntype: rule\npolicies:\n  - code-quality\n"
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").write_text(comp, encoding="utf-8")
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
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").write_text(comp, encoding="utf-8")
        builder = FacetBuilder(orchestra_dir)
        output_path = builder.build_one("my-rule", "claude", project_dir)
        assert output_path == project_dir / ".claude" / "rules" / "my-rule.md"

    def test_rule_type_instruction_optional(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_facet_sources(orchestra_dir)
        comp = "name: my-rule\ntype: rule\npolicies:\n  - code-quality\n"
        (orchestra_dir / "facets" / "compositions" / "my-rule.yaml").write_text(comp, encoding="utf-8")
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
