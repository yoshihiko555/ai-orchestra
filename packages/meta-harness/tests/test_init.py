"""`init` サブコマンドのテスト（EV-01, EV-02）。"""

from __future__ import annotations

import json
from pathlib import Path


def _store_dir(project: Path) -> Path:
    return project / ".claude" / "meta-harness"


class TestInitCreatesLayout:
    # EV-01
    def test_init_creates_all_required_directories(self, git_project: Path, run_meta) -> None:
        result = run_meta("init", project=git_project, check=True)
        assert result.returncode == 0

        store = _store_dir(git_project)
        for name in ("candidates", "runs", "locks", "tmp", "rejected", "reports"):
            assert (store / name).is_dir(), f"missing dir: {name}"
        assert (store / "holdout" / "runs").is_dir()

    # EV-01
    def test_init_creates_ledger_and_frontier_files(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        store = _store_dir(git_project)

        ledger = store / "ledger.jsonl"
        assert ledger.is_file()
        assert ledger.read_text(encoding="utf-8") == ""

        frontier = store / "frontier-claude-harness.json"
        assert frontier.is_file()
        doc = json.loads(frontier.read_text(encoding="utf-8"))
        assert doc["schema_version"] == "1.0"
        assert doc["target"] == "claude-harness"
        assert doc["ledger_line_count"] == 0
        assert doc["points"] == []
        assert doc["frontier"] == []
        assert doc["dominated"] == []
        assert doc["suite_hash"] == "0" * 64
        assert doc["evaluator_hash"] == "0" * 64


class TestInitIdempotent:
    # EV-02
    def test_init_twice_exits_0_both_times(self, git_project: Path, run_meta) -> None:
        first = run_meta("init", project=git_project, check=True)
        second = run_meta("init", project=git_project, check=True)
        assert first.returncode == 0
        assert second.returncode == 0

    # EV-02
    def test_init_twice_does_not_destroy_existing_data(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        store = _store_dir(git_project)

        marker = store / "candidates" / "marker-file.txt"
        marker.write_text("do-not-delete", encoding="utf-8")
        (store / "ledger.jsonl").write_text('{"event": "candidate_registered"}\n', encoding="utf-8")

        run_meta("init", project=git_project, check=True)

        assert marker.is_file()
        assert marker.read_text(encoding="utf-8") == "do-not-delete"
        assert (store / "ledger.jsonl").read_text(encoding="utf-8") == (
            '{"event": "candidate_registered"}\n'
        )
