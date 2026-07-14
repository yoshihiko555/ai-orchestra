"""skill target の facet closure と private overlay 制約のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

skill_targets = load_module(
    "skill_targets",
    "packages/meta-harness/lib/skill_targets.py",
)
mh = load_module(
    "meta_harness_common",
    "packages/meta-harness/lib/meta_harness_common.py",
)
ev = load_module(
    "meta_harness_evaluator_skill_target_lineage",
    "packages/meta-harness/lib/evaluator.py",
)
facet_builder = load_module(
    "facet_builder_skill_target_equivalence",
    "scripts/lib/facet_builder.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestRepositorySkillClosures:
    def test_handoff_closure_matches_facet_builder_references(self) -> None:
        resolution = skill_targets.resolve_skill_target(REPO_ROOT, "skill:handoff")
        builder = facet_builder.FacetBuilder(REPO_ROOT)
        composition_path = builder._find_composition("handoff")
        composition = builder.load_composition(composition_path)
        expected = {composition_path.relative_to(REPO_ROOT).as_posix()}

        for policy in composition["policies"]:
            path = REPO_ROOT / "facets" / "policies" / f"{policy}.md"
            assert builder.resolve_facet("policies", policy) == path.read_text().strip()
            expected.add(path.relative_to(REPO_ROOT).as_posix())
        instruction = composition["instruction"].strip()
        instruction_path = REPO_ROOT / "facets" / "instructions" / f"{instruction}.md"
        assert builder.resolve_instruction(composition["instruction"]) == (
            instruction_path.read_text().strip()
        )
        expected.add(instruction_path.relative_to(REPO_ROOT).as_posix())
        for script in composition["scripts"]:
            path = builder.resolve_script(script)
            assert path == REPO_ROOT / "facets" / "scripts" / script
            expected.add(path.relative_to(REPO_ROOT).as_posix())

        assert resolution.closure_paths == frozenset(expected)
        assert resolution.closure_paths == frozenset(
            {
                "facets/compositions/skills/handoff.yaml",
                "facets/instructions/handoff.md",
                "facets/policies/cli-language.md",
                "facets/scripts/handoff.py",
            }
        )
        assert resolution.private_paths == frozenset(
            {
                "facets/compositions/skills/handoff.yaml",
                "facets/instructions/handoff.md",
                "facets/scripts/handoff.py",
            }
        )
        assert len(resolution.closure_hash) == 64

    def test_issue_create_shared_policy_is_not_private(self) -> None:
        resolution = skill_targets.resolve_skill_target(REPO_ROOT, "skill:issue-create")

        assert "facets/policies/dialog-rules.md" in resolution.closure_paths
        assert "facets/policies/dialog-rules.md" not in resolution.private_paths
        assert "facets/instructions/issue-create.md" in resolution.private_paths

    def test_regression_enabled_fails_closed_until_evaluator_exists(self) -> None:
        with pytest.raises(skill_targets.SkillTargetError, match="not available"):
            skill_targets.allowed_overlay_paths(
                REPO_ROOT,
                "skill:handoff",
                {"regression": {"enabled": True}},
            )


class TestSkillOverlayValidation:
    def test_private_instruction_is_accepted(self, tmp_path: Path) -> None:
        overlay = tmp_path / "overlay"
        path = overlay / "facets" / "instructions" / "handoff.md"
        path.parent.mkdir(parents=True)
        path.write_text("updated\n", encoding="utf-8")

        assert (
            mh.validate_overlay(
                overlay,
                mh.DEFAULTS,
                target="skill:handoff",
                baseline_root=REPO_ROOT,
            )
            == []
        )

    def test_shared_policy_is_rejected(self, tmp_path: Path) -> None:
        overlay = tmp_path / "overlay"
        path = overlay / "facets" / "policies" / "cli-language.md"
        path.parent.mkdir(parents=True)
        path.write_text("updated\n", encoding="utf-8")

        errors = mh.validate_overlay(
            overlay,
            mh.DEFAULTS,
            target="skill:handoff",
            baseline_root=REPO_ROOT,
        )

        assert any("outside private facet closure" in error for error in errors)

    def test_skill_target_requires_baseline_root(self, tmp_path: Path) -> None:
        overlay = tmp_path / "overlay"
        overlay.mkdir()

        assert mh.validate_overlay(overlay, mh.DEFAULTS, target="skill:handoff") == [
            "baseline_root is required for skill target overlay validation"
        ]

    def test_uniquely_referenced_policy_is_still_rejected(self, tmp_path: Path) -> None:
        composition = tmp_path / "facets" / "compositions" / "skills" / "alpha.yaml"
        instruction = tmp_path / "facets" / "instructions" / "alpha.md"
        policy = tmp_path / "facets" / "policies" / "alpha-only.md"
        for path in (composition, instruction, policy):
            path.parent.mkdir(parents=True, exist_ok=True)
        composition.write_text(
            "name: alpha\nfrontmatter: {}\ninstruction: alpha\npolicies:\n  - alpha-only\n",
            encoding="utf-8",
        )
        instruction.write_text("alpha\n", encoding="utf-8")
        policy.write_text("unique policy\n", encoding="utf-8")
        overlay = tmp_path / "overlay"
        changed_policy = overlay / "facets" / "policies" / "alpha-only.md"
        changed_policy.parent.mkdir(parents=True)
        changed_policy.write_text("changed\n", encoding="utf-8")

        resolution = skill_targets.resolve_skill_target(tmp_path, "skill:alpha")
        errors = mh.validate_overlay(
            overlay,
            mh.DEFAULTS,
            target="skill:alpha",
            baseline_root=tmp_path,
        )

        assert "facets/policies/alpha-only.md" in resolution.closure_paths
        assert "facets/policies/alpha-only.md" not in resolution.private_paths
        assert any("outside private facet closure" in error for error in errors)


class TestSkillTargetSafety:
    def test_inline_instruction_does_not_become_a_path(self, tmp_path: Path) -> None:
        composition = tmp_path / "facets" / "compositions" / "skills" / "inline.yaml"
        composition.parent.mkdir(parents=True)
        composition.write_text(
            "name: inline\nfrontmatter: {}\ninstruction: |\n  Inline text.\n",
            encoding="utf-8",
        )

        resolution = skill_targets.resolve_skill_target(tmp_path, "skill:inline")

        assert resolution.closure_paths == frozenset({"facets/compositions/skills/inline.yaml"})

    @pytest.mark.parametrize(
        ("length", "expects_instruction_file"),
        [(100, True), (101, False)],
    )
    def test_instruction_length_boundary_matches_facet_builder(
        self, tmp_path: Path, length: int, expects_instruction_file: bool
    ) -> None:
        instruction = "a" * length
        slug = instruction if expects_instruction_file else "inline"
        composition = tmp_path / "facets" / "compositions" / "skills" / f"{slug}.yaml"
        composition.parent.mkdir(parents=True)
        composition.write_text(
            "\n".join(
                [
                    f"name: {slug}",
                    "frontmatter:",
                    "  description: boundary",
                    f"instruction: {instruction}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        if expects_instruction_file:
            instruction_path = tmp_path / "facets" / "instructions" / f"{instruction}.md"
            instruction_path.parent.mkdir(parents=True)
            instruction_path.write_text("boundary file\n", encoding="utf-8")

        builder = facet_builder.FacetBuilder(tmp_path)
        builder_composition = builder.load_composition(composition)
        builder_result = builder.resolve_instruction(builder_composition["instruction"])
        resolution = skill_targets.resolve_skill_target(tmp_path, f"skill:{slug}")

        instruction_rel = f"facets/instructions/{instruction}.md"
        if expects_instruction_file:
            assert builder_result == "boundary file"
            assert instruction_rel in resolution.closure_paths
        else:
            assert builder_result == instruction
            assert resolution.closure_paths == frozenset(
                {f"facets/compositions/skills/{slug}.yaml"}
            )

    def test_script_parent_traversal_is_rejected(self, tmp_path: Path) -> None:
        composition = tmp_path / "facets" / "compositions" / "skills" / "unsafe.yaml"
        composition.parent.mkdir(parents=True)
        composition.write_text(
            "name: unsafe\nfrontmatter: {}\ninstruction: ''\nscripts:\n  - ../secret.py\n",
            encoding="utf-8",
        )

        with pytest.raises(skill_targets.SkillTargetError, match="unsafe.*script"):
            skill_targets.resolve_skill_target(tmp_path, "skill:unsafe")

    def test_symlinked_source_is_rejected(self, tmp_path: Path) -> None:
        composition = tmp_path / "facets" / "compositions" / "skills" / "linked.yaml"
        instruction = tmp_path / "facets" / "instructions" / "linked.md"
        composition.parent.mkdir(parents=True)
        instruction.parent.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text("secret\n", encoding="utf-8")
        instruction.symlink_to(outside)
        composition.write_text(
            "name: linked\nfrontmatter: {}\ninstruction: linked\n",
            encoding="utf-8",
        )

        with pytest.raises(skill_targets.SkillTargetError, match="symlink"):
            skill_targets.resolve_skill_target(tmp_path, "skill:linked")


def _write_alpha_facets(project: Path) -> None:
    composition = project / "facets" / "compositions" / "skills" / "alpha.yaml"
    instruction_dir = project / "facets" / "instructions"
    composition.parent.mkdir(parents=True)
    instruction_dir.mkdir(parents=True)
    script_dir = project / "facets" / "scripts"
    script_dir.mkdir(parents=True)
    composition.write_text(
        "name: alpha\nfrontmatter: {}\ninstruction: alpha\n",
        encoding="utf-8",
    )
    (instruction_dir / "alpha.md").write_text("alpha baseline\n", encoding="utf-8")
    (instruction_dir / "beta.md").write_text("beta baseline\n", encoding="utf-8")
    (script_dir / "beta.py").write_text("print('baseline')\n", encoding="utf-8")


def _store_candidate(
    project: Path,
    cand_id: str,
    *,
    parent_id: str | None,
    source_commit: str,
    closure_hash: str,
    files: dict[str, str],
) -> dict:
    candidate = mh.candidates_dir(project, mh.DEFAULTS) / cand_id
    overlay = candidate / "overlay"
    for relative, content in files.items():
        path = overlay / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest = mh.build_candidate_manifest(
        cand_id=cand_id,
        parent_id=parent_id,
        generation=0 if parent_id is None else 1,
        target="skill:alpha",
        source_commit=source_commit,
        config_hash=mh.compute_config_hash(overlay, mh.DEFAULTS),
        overlay_files=mh.list_overlay_files(overlay),
        description=cand_id,
        target_closure_hash=closure_hash,
    )
    (candidate / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return manifest


class TestBaselineAuthority:
    def test_materialized_baseline_uses_source_commit_not_working_tree(
        self, git_project: Path, git_run
    ) -> None:
        _write_alpha_facets(git_project)
        git_run("add", "facets", cwd=git_project)
        git_run("commit", "-m", "add alpha facets", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        composition = git_project / "facets" / "compositions" / "skills" / "alpha.yaml"
        composition.write_text(
            "name: alpha\nfrontmatter: {}\ninstruction: beta\n",
            encoding="utf-8",
        )

        with skill_targets.materialized_baseline(git_project, source_commit) as baseline:
            resolution = skill_targets.resolve_skill_target(baseline, "skill:alpha")

        assert "facets/instructions/alpha.md" in resolution.closure_paths
        assert "facets/instructions/beta.md" not in resolution.closure_paths

    def test_composition_change_cannot_expand_same_candidate_authority(
        self, tmp_path: Path
    ) -> None:
        _write_alpha_facets(tmp_path)
        overlay = tmp_path / "overlay"
        changed_composition = (
            "name: alpha\nfrontmatter: {}\ninstruction: alpha\nscripts:\n  - beta.py\n"
        )
        files = {
            "facets/compositions/skills/alpha.yaml": changed_composition,
            "facets/scripts/beta.py": "print('changed too early')\n",
        }
        for relative, content in files.items():
            path = overlay / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        errors = mh.validate_overlay(
            overlay,
            mh.DEFAULTS,
            target="skill:alpha",
            baseline_root=tmp_path,
        )

        assert any("facets/scripts/beta.py" in error for error in errors)

    def test_parent_composition_change_expands_only_child_authority(
        self, git_project: Path, git_run
    ) -> None:
        _write_alpha_facets(git_project)
        git_run("add", "facets", cwd=git_project)
        git_run("commit", "-m", "add alpha facets", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        mh.init_store(git_project, mh.DEFAULTS)
        original = skill_targets.resolve_skill_target(git_project, "skill:alpha")
        changed_composition = (
            "name: alpha\nfrontmatter: {}\ninstruction: alpha\nscripts:\n  - beta.py\n"
        )
        parent = _store_candidate(
            git_project,
            "parent",
            parent_id=None,
            source_commit=source_commit,
            closure_hash=original.closure_hash,
            files={"facets/compositions/skills/alpha.yaml": changed_composition},
        )
        with skill_targets.materialized_baseline(git_project, source_commit) as baseline:
            ev.apply_registered_candidate_overlay(
                main_root=git_project,
                config=mh.DEFAULTS,
                manifest=parent,
                worktree_dir=baseline,
                schema_dir=REPO_ROOT / "packages" / "meta-harness" / "schemas",
            )
            child_authority = skill_targets.resolve_skill_target(baseline, "skill:alpha")
        child = _store_candidate(
            git_project,
            "child",
            parent_id="parent",
            source_commit=source_commit,
            closure_hash=child_authority.closure_hash,
            files={
                "facets/compositions/skills/alpha.yaml": changed_composition,
                "facets/scripts/beta.py": "print('child update')\n",
            },
        )

        with skill_targets.materialized_baseline(git_project, source_commit) as evaluation:
            ev.apply_registered_candidate_overlay(
                main_root=git_project,
                config=mh.DEFAULTS,
                manifest=child,
                worktree_dir=evaluation,
                schema_dir=REPO_ROOT / "packages" / "meta-harness" / "schemas",
            )
            result = (evaluation / "facets" / "scripts" / "beta.py").read_text()

        assert result == "print('child update')\n"
