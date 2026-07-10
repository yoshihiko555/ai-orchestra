"""CLI exit code / `--json` 共通契約のテスト（EV-25, EV-26, Sec6）。"""

from __future__ import annotations

import json
from pathlib import Path


class TestMissingRequiredArgsExit2:
    # EV-25
    def test_register_missing_overlay_and_target_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("register", project=git_project, check=False)
        assert result.returncode == 2

    def test_unknown_subcommand_exits_2(self, git_project: Path, run_meta) -> None:
        result = run_meta("not-a-real-subcommand", project=git_project, check=False)
        assert result.returncode == 2

    def test_no_subcommand_exits_2(self, git_project: Path, run_meta) -> None:
        result = run_meta(project=git_project, check=False)
        assert result.returncode == 2


class TestJsonFlagAllSubcommands:
    # EV-26
    def test_init_json_output_is_parseable(self, git_project: Path, run_meta) -> None:
        result = run_meta("init", "--json", project=git_project, check=True)
        json.loads(result.stdout)

    def test_register_json_output_is_parseable(
        self, git_project: Path, run_meta, default_overlay, tmp_path: Path
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)
        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--json",
            project=git_project,
            check=True,
        )
        payload = json.loads(result.stdout)
        assert "cand_id" in payload

    def test_frontier_json_output_is_parseable(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("frontier", "--json", project=git_project, check=True)
        json.loads(result.stdout)

    def test_purge_json_output_is_parseable(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("purge", "--json", project=git_project, check=True)
        json.loads(result.stdout)

    def test_status_json_output_is_parseable_in_both_flag_orderings(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)

        before = run_meta("--json", "status", project=git_project, check=True)
        after = run_meta("status", "--json", project=git_project, check=True)

        payload_before = json.loads(before.stdout)
        payload_after = json.loads(after.stdout)
        assert payload_before == payload_after


class TestEvaluateArgparseContract:
    # evaluate は Phase 1b で実装済み（`--candidate` 必須）。CLI capability gate 到達前の
    # argparse レベルの契約のみをここで検証する（実 claude/codex は決して呼ばない）。
    def test_evaluate_missing_candidate_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("evaluate", project=git_project, check=False)
        assert result.returncode == 2
        assert "--candidate" in result.stderr

    def test_evaluate_invalid_candidate_id_exits_2_before_capability_gate(
        self, git_project: Path, run_meta
    ) -> None:
        """Codex 指摘（meta_harness.py:505）: `candidate` はパス結合前に検証すること
        （`../` トラバーサル対策）。この検証は CLI capability gate（実 `claude --version`
        呼び出し）より前に行われるため、実 CLI が無い環境でも subprocess のまま検証できる。"""
        run_meta("init", project=git_project, check=True)
        result = run_meta(
            "evaluate", "--candidate", "../../../etc/passwd", project=git_project, check=False
        )
        assert result.returncode == 2
        assert "invalid candidate id" in result.stderr


class TestProposeArgparseContract:
    def test_propose_missing_target_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("propose", project=git_project, check=False)
        assert result.returncode == 2
        assert "--target" in result.stderr


class TestPromoteArgparseContract:
    def test_promote_missing_candidate_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("promote", project=git_project, check=False)
        assert result.returncode == 2
        assert "candidate" in result.stderr


class TestPhase23Stubs:
    def test_loop_stub_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("loop", project=git_project, check=False)
        assert result.returncode == 2
        assert "not implemented yet" in result.stderr


class TestValidationErrorExitCodes:
    def test_register_malformed_config_patch_json_exits_2_not_1(
        self, git_project: Path, run_meta, tmp_path: Path, make_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = make_overlay(
            tmp_path, {"facets/example-facet/SKILL.md": "# example facet\n\ncontent\n"}
        )
        (overlay_dir / "config-patch.json").write_text("{not valid json", encoding="utf-8")

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )

        assert result.returncode == 2
        assert "not valid JSON" in result.stderr

    def test_purge_negative_keep_generations_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)

        result = run_meta("purge", "--keep-generations", "-1", project=git_project, check=False)

        assert result.returncode == 2
        assert "keep-generations" in result.stderr
