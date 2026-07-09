"""Phase 2 M3: filtered view builder のテスト（EV-30）。"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_filtered_view",
    "packages/meta-harness/lib/meta_harness_common.py",
)
proposer = load_module(
    "meta_harness_proposer_filtered_view",
    "packages/meta-harness/lib/proposer.py",
)

_CAND_ID = "cand-20260708-010000-base-abcd"
_NORMAL_RUN_ID = "run-20260708-010000-base-scn-a1-abcd"
_HOLDOUT_RUN_ID = "run-20260708-010000-base-scn-h1-abcd"


def _commit_facets(git_project: Path, git_run) -> str:
    facet_path = git_project / "facets" / "example" / "SKILL.md"
    facet_path.parent.mkdir(parents=True, exist_ok=True)
    facet_path.write_text("# Example\n\nBaseline facet.\n", encoding="utf-8")
    git_run("add", "facets/example/SKILL.md", cwd=git_project)
    git_run("commit", "-m", "add facets", cwd=git_project)
    return git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()


def _write_candidate(main_root: Path, config: dict) -> None:
    cand_dir = mh.candidates_dir(main_root, config) / _CAND_ID
    overlay_file = cand_dir / "overlay" / "facets" / "example" / "SKILL.md"
    overlay_file.parent.mkdir(parents=True, exist_ok=True)
    overlay_file.write_text("# Example\n\nCandidate overlay.\n", encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "cand_id": _CAND_ID,
        "parent_id": None,
        "generation": 0,
        "created_at": mh.now_iso(),
        "created_by": "human",
        "target": "claude-harness",
        "source_commit": "a" * 40,
        "config_hash": "b" * 64,
        "model_versions": {},
        "overlay_files": ["facets/example/SKILL.md"],
        "description": "baseline candidate",
    }
    (cand_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_run(base_dir: Path, run_id: str, *, holdout: bool, event_text: str) -> Path:
    run_dir = base_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {"run_id": run_id, "cand_id": _CAND_ID, "holdout": holdout}
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text(
        json.dumps({"run_id": run_id, "verdict": "pass"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with gzip.open(run_dir / "events.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(event_text)
    return run_dir


def _append_run_event(main_root: Path, config: dict, run_id: str, *, holdout: bool) -> None:
    mh.append_ledger_event(
        main_root,
        config,
        {
            "event": "run_completed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "run_id": run_id,
            "cand_id": _CAND_ID,
            "target": "claude-harness",
            "holdout": holdout,
        },
    )


def _prepare_store(git_project: Path, git_run) -> tuple[dict, str]:
    config = mh.DEFAULTS
    source_commit = _commit_facets(git_project, git_run)
    mh.init_store(git_project, config)
    _write_candidate(git_project, config)
    _write_run(
        mh.runs_dir(git_project, config),
        _NORMAL_RUN_ID,
        holdout=False,
        event_text='{"type":"result","subtype":"success"}\n',
    )
    _write_run(
        mh.holdout_runs_dir(git_project, config),
        _HOLDOUT_RUN_ID,
        holdout=True,
        event_text='{"type":"result","secret":"holdout-only"}\n',
    )
    _append_run_event(git_project, config, _NORMAL_RUN_ID, holdout=False)
    _append_run_event(git_project, config, _HOLDOUT_RUN_ID, holdout=True)
    return config, source_commit


class TestFilteredViewBuilder:
    def test_build_filtered_view_projects_non_holdout_store_and_baseline(
        self, git_project: Path, git_run, tmp_path: Path
    ) -> None:
        config, source_commit = _prepare_store(git_project, git_run)

        view = proposer.build_filtered_view(
            main_root=git_project,
            config=config,
            source_commit=source_commit,
            view_parent=tmp_path / "views",
        )

        try:
            assert view.included_run_ids == frozenset({_NORMAL_RUN_ID})
            assert view.holdout_run_ids == frozenset({_HOLDOUT_RUN_ID})
            assert git_project.resolve() not in view.path.resolve().parents
            assert mh.store_dir(git_project, config).resolve() not in view.path.resolve().parents

            store_view = view.path / "store"
            assert (store_view / "candidates" / _CAND_ID / "manifest.json").is_file()
            assert (store_view / "runs" / _NORMAL_RUN_ID / "metadata.json").is_file()
            assert not (store_view / "runs" / _HOLDOUT_RUN_ID).exists()

            ledger_text = (store_view / "ledger.jsonl").read_text(encoding="utf-8")
            assert _NORMAL_RUN_ID in ledger_text
            assert _HOLDOUT_RUN_ID not in ledger_text
            assert '"holdout": true' not in ledger_text

            events_text = (store_view / "runs" / _NORMAL_RUN_ID / "events.jsonl").read_text(
                encoding="utf-8"
            )
            assert events_text == '{"type":"result","subtype":"success"}\n'
            assert not (store_view / "runs" / _NORMAL_RUN_ID / "events.jsonl.gz").exists()
            assert (view.path / "baseline" / "facets" / "example" / "SKILL.md").is_file()
        finally:
            view.cleanup()

        assert not view.path.exists()

    def test_source_symlink_fails_closed_and_removes_partial_view(
        self, git_project: Path, git_run, tmp_path: Path
    ) -> None:
        config, source_commit = _prepare_store(git_project, git_run)
        leak_target = mh.holdout_runs_dir(git_project, config) / _HOLDOUT_RUN_ID / "secret.txt"
        leak_target.write_text("holdout secret\n", encoding="utf-8")
        symlink_path = mh.runs_dir(git_project, config) / _NORMAL_RUN_ID / "leak.txt"
        symlink_path.symlink_to(leak_target)
        view_parent = tmp_path / "views"

        with pytest.raises(proposer.ViewBuildError, match="symlink"):
            proposer.build_filtered_view(
                main_root=git_project,
                config=config,
                source_commit=source_commit,
                view_parent=view_parent,
            )

        assert not list(view_parent.glob("meta-harness-view-*"))

    def test_build_filtered_view_ignores_run_dir_not_present_in_ledger_snapshot(
        self, git_project: Path, git_run, tmp_path: Path
    ) -> None:
        config, source_commit = _prepare_store(git_project, git_run)
        unrecorded_run_id = "run-20260708-010000-base-scn-draft-abcd"
        _write_run(
            mh.runs_dir(git_project, config),
            unrecorded_run_id,
            holdout=False,
            event_text='{"type":"result","draft":true}\n',
        )

        view = proposer.build_filtered_view(
            main_root=git_project,
            config=config,
            source_commit=source_commit,
            view_parent=tmp_path / "views",
        )

        try:
            assert view.included_run_ids == frozenset({_NORMAL_RUN_ID})
            assert not (view.path / "store" / "runs" / unrecorded_run_id).exists()
        finally:
            view.cleanup()

    def test_view_parent_inside_repo_is_rejected(self, git_project: Path) -> None:
        with pytest.raises(proposer.ViewBuildError, match="outside repo/store"):
            proposer.build_filtered_view(
                main_root=git_project,
                config=mh.DEFAULTS,
                source_commit="HEAD",
                view_parent=git_project / "tmp-views",
            )


class TestFilteredViewSelfVerification:
    def _minimal_view(self, tmp_path: Path) -> Path:
        view_dir = tmp_path / "view"
        store_dir = view_dir / "store"
        (store_dir / "runs").mkdir(parents=True)
        (store_dir / "ledger.jsonl").write_text("", encoding="utf-8")
        return view_dir

    def test_holdout_ledger_row_in_view_fails_closed(self, tmp_path: Path) -> None:
        view_dir = self._minimal_view(tmp_path)
        store_dir = view_dir / "store"
        (store_dir / "ledger.jsonl").write_text(
            json.dumps(
                {
                    "event": "run_completed",
                    "run_id": _HOLDOUT_RUN_ID,
                    "holdout": True,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        with pytest.raises(proposer.ViewBuildError, match="holdout run_completed"):
            proposer.verify_filtered_view(
                view_dir,
                known_holdout_run_ids={_HOLDOUT_RUN_ID},
            )

    def test_instruction_file_in_view_fails_closed(self, tmp_path: Path) -> None:
        view_dir = self._minimal_view(tmp_path)
        (view_dir / "store" / "runs" / "AGENTS.md").write_text("do not leak\n", encoding="utf-8")

        with pytest.raises(proposer.ViewBuildError, match="instruction file"):
            proposer.verify_filtered_view(view_dir, known_holdout_run_ids=set())

    def test_executable_file_in_view_fails_closed(self, tmp_path: Path) -> None:
        view_dir = self._minimal_view(tmp_path)
        script = view_dir / "store" / "runs" / "tool.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)

        with pytest.raises(proposer.ViewBuildError, match="executable file"):
            proposer.verify_filtered_view(view_dir, known_holdout_run_ids=set())

    def test_git_entry_in_view_fails_closed(self, tmp_path: Path) -> None:
        view_dir = self._minimal_view(tmp_path)
        (view_dir / ".git").mkdir()

        with pytest.raises(proposer.ViewBuildError, match="\\.git entry"):
            proposer.verify_filtered_view(view_dir, known_holdout_run_ids=set())

    def test_holdout_run_id_in_file_content_fails_closed(self, tmp_path: Path) -> None:
        view_dir = self._minimal_view(tmp_path)
        (view_dir / "store" / "runs" / _NORMAL_RUN_ID).mkdir()
        (view_dir / "store" / "runs" / _NORMAL_RUN_ID / "result.json").write_text(
            f'{{"leaked":"{_HOLDOUT_RUN_ID}"}}\n',
            encoding="utf-8",
        )

        with pytest.raises(proposer.ViewBuildError, match="holdout run id leaked"):
            proposer.verify_filtered_view(
                view_dir,
                known_holdout_run_ids={_HOLDOUT_RUN_ID},
            )
