"""Phase 2 M2: proposal schema と proposer prompt template のテスト。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.module_loader import load_module

proposer = load_module(
    "meta_harness_proposer_test",
    "packages/meta-harness/lib/proposer.py",
)
evaluator = load_module(
    "meta_harness_evaluator_proposal_schema_test",
    "packages/meta-harness/lib/evaluator.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "packages" / "meta-harness"
SCHEMA_DIR = PACKAGE_DIR / "schemas"


_VALID_PROPOSAL = {
    "schema_version": "1.0",
    "hypothesis": "Adding a stricter instruction will reduce missing artifacts.",
    "theme": "tighten artifact creation guidance",
    "changes": [
        {
            "path": "facets/example/SKILL.md",
            "new_content": "# Example\n\nAlways create the requested artifact.\n",
        }
    ],
    "based_on_runs": ["run-20260101-000000-cand-scn-a1-abcd"],
    "expected_effect": "artifact_exists checks should pass more consistently.",
    "risk_notes": "May overfit to artifact-oriented scenarios.",
}

_VALID_CONFIG_PATCH_PROPOSAL = {
    "schema_version": "1.0",
    "hypothesis": "Route debugger work directly through Claude.",
    "theme": "route debugger directly",
    "config_patch": [
        {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": "agents.debugger.tool",
            "value": "claude-direct",
        }
    ],
    "based_on_runs": ["run-20260101-000000-cand-scn-a1-abcd"],
    "expected_effect": "The routing-sensitive scenario should improve.",
    "risk_notes": "May reduce deep debugging quality.",
}


def _append_candidate_registered(
    project: Path,
    config: dict,
    manifest: dict,
    *,
    created_by: str | None = None,
    target: str | None = None,
) -> None:
    proposer.mh.append_ledger_event(
        project,
        config,
        {
            "event": "candidate_registered",
            "ts": proposer.mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": manifest["cand_id"],
            "parent_id": manifest["parent_id"],
            "generation": manifest["generation"],
            "target": target or manifest["target"],
            "created_by": created_by or manifest["created_by"],
        },
    )


class TestProposalSchema:
    def test_valid_proposal_round_trips_through_json_and_schema(self) -> None:
        encoded = json.dumps(_VALID_PROPOSAL, ensure_ascii=False)
        decoded = json.loads(encoded)

        assert proposer.validate_proposal(decoded, SCHEMA_DIR) == []

    def test_valid_config_patch_proposal_passes_the_simple_schema(self) -> None:
        assert proposer.validate_proposal(_VALID_CONFIG_PATCH_PROPOSAL, SCHEMA_DIR) == []

    def test_schema_leaves_payload_exclusivity_to_registration(self) -> None:
        schema = json.loads((SCHEMA_DIR / "proposal.schema.json").read_text(encoding="utf-8"))
        serialized = json.dumps(schema, sort_keys=True)

        assert "oneOf" not in serialized
        assert "changes" not in schema["required"]
        assert "config_patch" not in schema["required"]

    def test_rejects_path_outside_facets(self) -> None:
        proposal = json.loads(json.dumps(_VALID_PROPOSAL))
        proposal["changes"][0]["path"] = "docs/evaluation/meta-harness.md"

        errors = proposer.validate_proposal(proposal, SCHEMA_DIR)

        assert any("does not match pattern" in e for e in errors)

    def test_materialize_rejects_parent_directory_escape(self, tmp_path: Path) -> None:
        proposal = json.loads(json.dumps(_VALID_PROPOSAL))
        proposal["changes"][0]["path"] = "facets/../secrets.txt"

        try:
            proposer.materialize_overlay_from_proposal(
                proposal,
                tmp_path / "overlay",
                max_overlay_bytes=200000,
            )
        except proposer.ProposalValidationError as exc:
            assert "unsafe proposal change path" in str(exc)
        else:
            raise AssertionError("expected parent directory escape to be rejected")

    def test_rejects_missing_based_on_runs(self) -> None:
        proposal = {k: v for k, v in _VALID_PROPOSAL.items() if k != "based_on_runs"}

        errors = proposer.validate_proposal(proposal, SCHEMA_DIR)

        assert any("based_on_runs" in e for e in errors)

    def test_rejects_candidate_id_in_based_on_runs(self) -> None:
        proposal = json.loads(json.dumps(_VALID_PROPOSAL))
        proposal["based_on_runs"] = ["cand-20260707-231339-phase1b-e2e-baseline-6e67"]

        errors = proposer.validate_proposal(proposal, SCHEMA_DIR)

        assert any("does not match pattern" in e for e in errors)

    def test_accepts_run_prefixed_id_until_membership_validation(self) -> None:
        proposal = json.loads(json.dumps(_VALID_PROPOSAL))
        proposal["based_on_runs"] = ["run-20260707-231339-phase1b-e2e-baseline-6e67"]

        errors = proposer.validate_proposal(proposal, SCHEMA_DIR)

        assert errors == []

    def test_evaluator_minted_run_id_passes_proposal_schema(self) -> None:
        proposal = json.loads(json.dumps(_VALID_PROPOSAL))
        proposal["based_on_runs"] = [
            evaluator.generate_run_id(
                "cand-20260710-010000-schema-compat-abcd",
                "scenario-with-dashes",
                1,
            )
        ]

        assert proposer.validate_proposal(proposal, SCHEMA_DIR) == []


class TestProposerPrompt:
    def test_prompt_renders_view_path_and_frontier_summary(self, tmp_path: Path) -> None:
        view_dir = tmp_path / "view"
        frontier_doc = {
            "frontier": ["cand-frontier"],
            "dominated": ["cand-old"],
            "points": [
                {
                    "cand_id": "cand-frontier",
                    "quality_mean": 87.5,
                    "cost_mean": 1234,
                    "runs": 3,
                }
            ],
        }

        prompt = proposer.render_proposer_prompt(
            view_dir=view_dir,
            frontier_doc=frontier_doc,
            config={"proposer": {"max_overlay_bytes": 12345}},
            package_dir=PACKAGE_DIR,
            target="claude-harness",
            focus_run_ids=("run-focus-a", "run-focus-b"),
            valid_based_on_run_ids=("run-valid-a", "run-valid-b", "run-valid-c"),
            focus_candidate_id="cand-focus",
        )

        assert str(view_dir.resolve()) in prompt
        assert "target: claude-harness" in prompt
        assert "focus runs（優先分析対象）: run-focus-a, run-focus-b" in prompt
        assert "valid based_on_runs candidates: run-valid-a, run-valid-b, run-valid-c" in prompt
        assert "focus candidate: cand-focus" in prompt
        assert "cand_id（`cand-` で始まる ID）は based_on_runs に絶対に入れない" in prompt
        assert "run_id を推測・合成・変形しない" in prompt
        assert "cand-frontier" in prompt
        assert "quality_mean=87.500" in prompt
        assert "cost_mean=1234.000" in prompt
        assert "変更合計は 12345 バイト以内" in prompt
        assert "events.jsonl を選択的に検査" in prompt
        assert "events.jsonl.gz" not in prompt
        assert "untrusted input" in prompt
        assert "$view_dir" not in prompt
        assert "$frontier_summary" not in prompt

    def test_prompt_uses_safe_defaults_when_focus_is_absent(self, tmp_path: Path) -> None:
        prompt = proposer.render_proposer_prompt(
            view_dir=tmp_path / "view",
            frontier_doc=None,
            config={},
            package_dir=PACKAGE_DIR,
            target="claude-harness",
        )

        assert "focus runs（優先分析対象）: (none)" in prompt
        assert "valid based_on_runs candidates: (none)" in prompt
        assert "focus candidate: (none)" in prompt
        assert "- frontier: (none)" in prompt
        assert "変更合計は 200000 バイト以内" in prompt

    def test_prompt_reflects_register_time_allowlist_for_skill_target(self, tmp_path: Path) -> None:
        baseline = tmp_path / "view" / "baseline"
        composition_dir = baseline / "facets" / "compositions" / "skills"
        composition_dir.mkdir(parents=True)
        (composition_dir / "alpha.yaml").write_text(
            "name: alpha\nfrontmatter: {}\ninstruction: alpha\npolicies:\n  - shared-policy\n",
            encoding="utf-8",
        )
        instructions_dir = baseline / "facets" / "instructions"
        instructions_dir.mkdir(parents=True)
        (instructions_dir / "alpha.md").write_text("alpha baseline\n", encoding="utf-8")
        policies_dir = baseline / "facets" / "policies"
        policies_dir.mkdir(parents=True)
        (policies_dir / "shared-policy.md").write_text("shared\n", encoding="utf-8")

        enabled_prompt = proposer.render_proposer_prompt(
            view_dir=tmp_path / "view",
            frontier_doc=None,
            config={"regression": {"enabled": True}},
            package_dir=PACKAGE_DIR,
            target="skill:alpha",
        )
        disabled_prompt = proposer.render_proposer_prompt(
            view_dir=tmp_path / "view",
            frontier_doc=None,
            config={"regression": {"enabled": False}},
            package_dir=PACKAGE_DIR,
            target="skill:alpha",
        )

        assert "facets/policies/shared-policy.md" in enabled_prompt
        assert "facets/policies/shared-policy.md" not in disabled_prompt
        assert "facets/instructions/alpha.md" in enabled_prompt
        assert "facets/instructions/alpha.md" in disabled_prompt

    def test_routing_config_prompt_lists_only_phase_a_menu(self, tmp_path: Path) -> None:
        source_commit = proposer.mh.git_head(REPO_ROOT)
        assert source_commit is not None
        prompt = proposer.render_proposer_prompt(
            view_dir=tmp_path / "view",
            frontier_doc=None,
            config={},
            package_dir=PACKAGE_DIR,
            target="routing-config",
            valid_based_on_run_ids=("run-routing-baseline",),
            main_root=REPO_ROOT,
            source_commit=source_commit,
        )

        assert "agents.*.tool" in prompt
        assert "agents.debugger.tool = codex" in prompt
        assert "allowed values: antigravity | auto | claude-direct | codex" in prompt
        assert "antigravity.model" in prompt
        assert "allowed values from model_allowlist: gemini-3.1-pro" in prompt
        assert "current value: gemini-3.1-pro-high" in prompt
        assert "codex.model" not in prompt
        assert "config_patch のみ" in prompt
        assert "proposal schema（schema_version, hypothesis, theme, config_patch" in prompt

    def test_routing_config_prompt_uses_source_commit_not_working_tree(
        self,
        git_project: Path,
        git_run: Callable,
        tmp_path: Path,
    ) -> None:
        config_path = git_project / "packages/agent-routing/config/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "agents:\n"
            "  source-agent:\n"
            "    tool: codex\n"
            "antigravity:\n"
            "  model: source-model\n"
            "  model_allowlist:\n"
            "    - source-model\n",
            encoding="utf-8",
        )
        git_run("add", config_path.relative_to(git_project).as_posix(), cwd=git_project)
        git_run("commit", "-m", "source routing config", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        config_path.write_text(
            "agents:\n"
            "  working-agent:\n"
            "    tool: claude-direct\n"
            "antigravity:\n"
            "  model: working-model\n"
            "  model_allowlist:\n"
            "    - working-model\n",
            encoding="utf-8",
        )

        prompt = proposer.render_proposer_prompt(
            view_dir=tmp_path / "view",
            frontier_doc=None,
            config={},
            package_dir=PACKAGE_DIR,
            target="routing-config",
            main_root=git_project,
            source_commit=source_commit,
        )

        assert "agents.source-agent.tool = codex" in prompt
        assert "source-model" in prompt
        assert "working-agent" not in prompt
        assert "working-model" not in prompt

    def test_routing_config_prompt_fails_closed_when_source_config_is_unreadable(
        self, git_project: Path, tmp_path: Path
    ) -> None:
        source_commit = proposer.mh.git_head(git_project)
        assert source_commit is not None

        with pytest.raises(
            ValueError, match="could not read agent-routing config from source_commit"
        ):
            proposer.render_proposer_prompt(
                view_dir=tmp_path / "view",
                frontier_doc=None,
                config={},
                package_dir=PACKAGE_DIR,
                target="routing-config",
                main_root=git_project,
                source_commit=source_commit,
            )

    def test_routing_config_prompt_intersects_effective_allowlist(
        self,
        git_project: Path,
        git_run: Callable,
        tmp_path: Path,
    ) -> None:
        config_path = git_project / "packages/agent-routing/config/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "agents:\n"
            "  debugger:\n"
            "    tool: codex\n"
            "antigravity:\n"
            "  model: source-model\n"
            "  model_allowlist:\n"
            "    - source-model\n",
            encoding="utf-8",
        )
        git_run("add", config_path.relative_to(git_project).as_posix(), cwd=git_project)
        git_run("commit", "-m", "source routing config", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        effective_config = {
            "config_patch": {"allowlist": ["agent-routing/cli-tools.yaml#antigravity.model"]}
        }

        prompt = proposer.render_proposer_prompt(
            view_dir=tmp_path / "view",
            frontier_doc=None,
            config=effective_config,
            package_dir=PACKAGE_DIR,
            target="routing-config",
            main_root=git_project,
            source_commit=source_commit,
        )

        assert "antigravity.model" in prompt
        assert "source-model" in prompt
        assert "agents.*.tool" not in prompt
        assert "agents.debugger.tool" not in prompt
        assert "codex.model" not in prompt

    def test_routing_config_prompt_applies_parent_config_patch_lineage(
        self,
        git_project: Path,
        git_run: Callable,
        tmp_path: Path,
    ) -> None:
        config_path = git_project / "packages/agent-routing/config/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "agents:\n"
            "  debugger:\n"
            "    tool: codex\n"
            "antigravity:\n"
            "  model: source-model\n"
            "  model_allowlist:\n"
            "    - source-model\n",
            encoding="utf-8",
        )
        git_run("add", config_path.relative_to(git_project).as_posix(), cwd=git_project)
        git_run("commit", "-m", "source routing config", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        config = proposer.mh.DEFAULTS
        proposer.mh.init_store(git_project, config)
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.debugger.tool",
                "value": "antigravity",
            }
        ]
        overlay_dir = tmp_path / "parent-overlay"
        overlay_dir.mkdir()
        (overlay_dir / proposer.mh.CONFIG_PATCH_FILENAME).write_text(
            json.dumps(patch), encoding="utf-8"
        )
        parent_id = "cand-20260718-120000-routing-parent-abcd"
        manifest = proposer.mh.build_candidate_manifest(
            cand_id=parent_id,
            parent_id=None,
            generation=0,
            target="routing-config",
            source_commit=source_commit,
            config_hash=proposer.mh.compute_config_hash(overlay_dir, config),
            overlay_files=[],
            description="routing parent",
            created_by="proposer",
            config_patch_hash=proposer.mh.compute_config_patch_hash(patch),
        )
        proposer.mh.register_candidate(
            git_project,
            config,
            cand_id=parent_id,
            manifest=manifest,
            overlay_dir=overlay_dir,
            overlay_files=[],
            target="routing-config",
            created_by="proposer",
            baseline_root=git_project,
            schema_dir=SCHEMA_DIR,
        )
        _append_candidate_registered(git_project, config, manifest)

        prompt = proposer.render_proposer_prompt(
            view_dir=tmp_path / "view",
            frontier_doc=None,
            config=config,
            package_dir=PACKAGE_DIR,
            target="routing-config",
            focus_candidate_id=parent_id,
            main_root=git_project,
            source_commit=source_commit,
        )

        assert "agents.debugger.tool = antigravity" in prompt
        assert "agents.debugger.tool = codex" not in prompt

    def test_routing_config_prompt_rejects_manifest_provenance_drift(
        self,
        git_project: Path,
        git_run: Callable,
    ) -> None:
        config_path = git_project / "packages/agent-routing/config/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "agents:\n  debugger:\n    tool: codex\n",
            encoding="utf-8",
        )
        git_run("add", config_path.relative_to(git_project).as_posix(), cwd=git_project)
        git_run("commit", "-m", "source routing config", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        config = proposer.mh.DEFAULTS
        proposer.mh.init_store(git_project, config)
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.debugger.tool",
                "value": "antigravity",
            }
        ]
        parent_id = "cand-20260718-120001-routing-provenance-abcd"
        cand_dir = proposer.mh.candidates_dir(git_project, config) / parent_id
        overlay_dir = cand_dir / "overlay"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / proposer.mh.CONFIG_PATCH_FILENAME).write_text(
            json.dumps(patch), encoding="utf-8"
        )
        manifest = proposer.mh.build_candidate_manifest(
            cand_id=parent_id,
            parent_id=None,
            generation=0,
            target="routing-config",
            source_commit=source_commit,
            config_hash=proposer.mh.compute_config_hash(overlay_dir, config),
            overlay_files=[],
            description="routing parent with mutable provenance",
            created_by="human",
            config_patch_hash=proposer.mh.compute_config_patch_hash(patch),
        )
        (cand_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        _append_candidate_registered(git_project, config, manifest, created_by="proposer")

        with pytest.raises(ValueError, match="created_by does not match ledger provenance"):
            proposer.render_proposer_prompt(
                view_dir=git_project / "view",
                frontier_doc=None,
                config=config,
                package_dir=PACKAGE_DIR,
                target="routing-config",
                focus_candidate_id=parent_id,
                main_root=git_project,
                source_commit=source_commit,
            )

    def test_routing_config_prompt_rejects_manifest_missing_created_by(
        self,
        git_project: Path,
        git_run: Callable,
    ) -> None:
        config_path = git_project / "packages/agent-routing/config/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "agents:\n  debugger:\n    tool: codex\n",
            encoding="utf-8",
        )
        git_run("add", config_path.relative_to(git_project).as_posix(), cwd=git_project)
        git_run("commit", "-m", "source routing config", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        config = proposer.mh.DEFAULTS
        proposer.mh.init_store(git_project, config)
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.debugger.tool",
                "value": "antigravity",
            }
        ]
        parent_id = "cand-20260718-120002-routing-missing-created-by-abcd"
        cand_dir = proposer.mh.candidates_dir(git_project, config) / parent_id
        overlay_dir = cand_dir / "overlay"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / proposer.mh.CONFIG_PATCH_FILENAME).write_text(
            json.dumps(patch), encoding="utf-8"
        )
        manifest = proposer.mh.build_candidate_manifest(
            cand_id=parent_id,
            parent_id=None,
            generation=0,
            target="routing-config",
            source_commit=source_commit,
            config_hash=proposer.mh.compute_config_hash(overlay_dir, config),
            overlay_files=[],
            description="routing parent missing mutable provenance",
            created_by="human",
            config_patch_hash=proposer.mh.compute_config_patch_hash(patch),
        )
        manifest.pop("created_by")
        (cand_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        _append_candidate_registered(git_project, config, manifest, created_by="human")

        with pytest.raises(ValueError, match="created_by does not match ledger provenance"):
            proposer.render_proposer_prompt(
                view_dir=git_project / "view",
                frontier_doc=None,
                config=config,
                package_dir=PACKAGE_DIR,
                target="routing-config",
                focus_candidate_id=parent_id,
                main_root=git_project,
                source_commit=source_commit,
            )

    def test_routing_config_prompt_rejects_manifest_target_drift(
        self,
        git_project: Path,
        git_run: Callable,
    ) -> None:
        config_path = git_project / "packages/agent-routing/config/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "agents:\n  debugger:\n    tool: codex\n",
            encoding="utf-8",
        )
        git_run("add", config_path.relative_to(git_project).as_posix(), cwd=git_project)
        git_run("commit", "-m", "source routing config", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        config = proposer.mh.DEFAULTS
        proposer.mh.init_store(git_project, config)
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.debugger.tool",
                "value": "antigravity",
            }
        ]
        parent_id = "cand-20260718-120003-routing-target-drift-abcd"
        cand_dir = proposer.mh.candidates_dir(git_project, config) / parent_id
        overlay_dir = cand_dir / "overlay"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / proposer.mh.CONFIG_PATCH_FILENAME).write_text(
            json.dumps(patch), encoding="utf-8"
        )
        manifest = proposer.mh.build_candidate_manifest(
            cand_id=parent_id,
            parent_id=None,
            generation=0,
            target="routing-config",
            source_commit=source_commit,
            config_hash=proposer.mh.compute_config_hash(overlay_dir, config),
            overlay_files=[],
            description="routing parent with mutable target",
            created_by="human",
            config_patch_hash=proposer.mh.compute_config_patch_hash(patch),
        )
        (cand_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        _append_candidate_registered(git_project, config, manifest, target="skill:reverse")

        with pytest.raises(ValueError, match="target does not match ledger provenance"):
            proposer.render_proposer_prompt(
                view_dir=git_project / "view",
                frontier_doc=None,
                config=config,
                package_dir=PACKAGE_DIR,
                target="routing-config",
                focus_candidate_id=parent_id,
                main_root=git_project,
                source_commit=source_commit,
            )

    def test_routing_config_prompt_rejects_parent_patch_for_unknown_source_key(
        self,
        git_project: Path,
        git_run: Callable,
        tmp_path: Path,
    ) -> None:
        config_path = git_project / "packages/agent-routing/config/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "agents:\n"
            "  source-agent:\n"
            "    tool: codex\n"
            "antigravity:\n"
            "  model: source-model\n"
            "  model_allowlist:\n"
            "    - source-model\n",
            encoding="utf-8",
        )
        git_run("add", config_path.relative_to(git_project).as_posix(), cwd=git_project)
        git_run("commit", "-m", "source routing config", cwd=git_project)
        source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        config = proposer.mh.DEFAULTS
        proposer.mh.init_store(git_project, config)
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.debugger.tool",
                "value": "antigravity",
            }
        ]
        parent_id = "cand-20260718-120001-routing-parent-abcd"
        cand_dir = proposer.mh.candidates_dir(git_project, config) / parent_id
        cand_dir.mkdir(parents=True)
        stored_overlay = cand_dir / "overlay"
        stored_overlay.mkdir()
        (stored_overlay / proposer.mh.CONFIG_PATCH_FILENAME).write_text(
            json.dumps(patch), encoding="utf-8"
        )
        manifest = proposer.mh.build_candidate_manifest(
            cand_id=parent_id,
            parent_id=None,
            generation=0,
            target="routing-config",
            source_commit=source_commit,
            config_hash=proposer.mh.compute_config_hash(stored_overlay, config),
            overlay_files=[],
            description="invalid routing parent",
            created_by="proposer",
            config_patch_hash=proposer.mh.compute_config_patch_hash(patch),
        )
        (cand_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        _append_candidate_registered(git_project, config, manifest)

        with pytest.raises(ValueError, match="unknown agent name|unknown key"):
            proposer.render_proposer_prompt(
                view_dir=tmp_path / "view",
                frontier_doc=None,
                config=config,
                package_dir=PACKAGE_DIR,
                target="routing-config",
                focus_candidate_id=parent_id,
                main_root=git_project,
                source_commit=source_commit,
            )
