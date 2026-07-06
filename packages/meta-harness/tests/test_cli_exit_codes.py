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


class TestPhase1bStubs:
    def test_evaluate_stub_exits_2_with_phase1a_message(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("evaluate", project=git_project, check=False)
        assert result.returncode == 2
        assert "not implemented in Phase 1a" in result.stderr

    def test_propose_stub_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("propose", project=git_project, check=False)
        assert result.returncode == 2
        assert "not implemented in Phase 1a" in result.stderr

    def test_promote_stub_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("promote", project=git_project, check=False)
        assert result.returncode == 2
        assert "not implemented in Phase 1a" in result.stderr

    def test_loop_stub_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta("loop", project=git_project, check=False)
        assert result.returncode == 2
        assert "not implemented in Phase 1a" in result.stderr
