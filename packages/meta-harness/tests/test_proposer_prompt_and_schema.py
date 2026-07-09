"""Phase 2 M2: proposal schema と proposer prompt template のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

proposer = load_module(
    "meta_harness_proposer_test",
    "packages/meta-harness/lib/proposer.py",
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


class TestProposalSchema:
    def test_valid_proposal_round_trips_through_json_and_schema(self) -> None:
        encoded = json.dumps(_VALID_PROPOSAL, ensure_ascii=False)
        decoded = json.loads(encoded)

        assert proposer.validate_proposal(decoded, SCHEMA_DIR) == []

    def test_rejects_path_outside_facets(self) -> None:
        proposal = json.loads(json.dumps(_VALID_PROPOSAL))
        proposal["changes"][0]["path"] = "docs/evaluation/meta-harness.md"

        errors = proposer.validate_proposal(proposal, SCHEMA_DIR)

        assert any("does not match pattern" in e for e in errors)

    def test_rejects_parent_directory_escape(self) -> None:
        proposal = json.loads(json.dumps(_VALID_PROPOSAL))
        proposal["changes"][0]["path"] = "facets/../secrets.txt"

        errors = proposer.validate_proposal(proposal, SCHEMA_DIR)

        assert any("does not match pattern" in e for e in errors)

    def test_rejects_missing_based_on_runs(self) -> None:
        proposal = {k: v for k, v in _VALID_PROPOSAL.items() if k != "based_on_runs"}

        errors = proposer.validate_proposal(proposal, SCHEMA_DIR)

        assert any("based_on_runs" in e for e in errors)


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
            focus_candidate_id="cand-focus",
        )

        assert str(view_dir.resolve()) in prompt
        assert "target: claude-harness" in prompt
        assert "focus runs: run-focus-a, run-focus-b" in prompt
        assert "focus candidate: cand-focus" in prompt
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
            target="skill:example",
        )

        assert "focus runs: (none)" in prompt
        assert "focus candidate: (none)" in prompt
        assert "- frontier: (none)" in prompt
        assert "変更合計は 200000 バイト以内" in prompt
