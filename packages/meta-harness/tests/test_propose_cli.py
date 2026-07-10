"""Phase 2 M4: `meta propose` CLI の mocked backend E2E テスト。"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_propose_cli",
    "packages/meta-harness/lib/meta_harness_common.py",
)
propose_cli = load_module(
    "meta_harness_propose_cli_test",
    "packages/meta-harness/lib/propose_cli.py",
)

_PARENT_ID = "cand-20260708-020000-parent-abcd"
_RUN_ID = "run-20260708-020000-parent-scn-a1-abcd"
_HOLDOUT_RUN_ID = "run-20260708-020000-parent-scn-h1-abcd"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _install_stub_tools(bin_dir: Path, proposal: dict) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    proposal_json = json.dumps(proposal, ensure_ascii=False)
    _write_executable(
        bin_dir / "srt",
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("@anthropic-ai/sandbox-runtime 0.0.64")
    raise SystemExit(0)
if args[:1] == ["--settings"]:
    args = args[2:]
base = os.path.basename(args[0]) if args else ""
if base == "codex":
    os.execvp(args[0], args)
if base == "cat":
    print("cat: Operation not permitted", file=sys.stderr)
    raise SystemExit(1)
if base == "curl":
    print("curl: (56) connection reset by proxy", file=sys.stderr)
    raise SystemExit(56)
raise SystemExit(1)
""",
    )
    _write_executable(
        bin_dir / "codex",
        f"""#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
out = args[args.index("-o") + 1]
with open(out, "w", encoding="utf-8") as handle:
    handle.write({proposal_json!r})
    handle.write("\\n")
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":7,"output_tokens":3}}}}))
raise SystemExit(0)
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env python3
print("canary reachable")
raise SystemExit(0)
""",
    )


def _prepare_codex_auth(home: Path) -> None:
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")


def _commit_facets(git_project: Path, git_run) -> str:
    facet = git_project / "facets" / "example" / "SKILL.md"
    facet.parent.mkdir(parents=True, exist_ok=True)
    facet.write_text("# Example\n\nBaseline.\n", encoding="utf-8")
    git_run("add", "facets/example/SKILL.md", cwd=git_project)
    git_run("commit", "-m", "add facets", cwd=git_project)
    return git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()


def _prepare_store(git_project: Path, git_run) -> None:
    config = mh.DEFAULTS
    source_commit = _commit_facets(git_project, git_run)
    mh.init_store(git_project, config)
    cand_dir = mh.candidates_dir(git_project, config) / _PARENT_ID
    overlay_file = cand_dir / "overlay" / "facets" / "example" / "SKILL.md"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("# Example\n\nParent overlay.\n", encoding="utf-8")
    inherited_file = cand_dir / "overlay" / "facets" / "parent-only" / "SKILL.md"
    inherited_file.parent.mkdir(parents=True)
    inherited_file.write_text("# Parent only\n\nInherited content.\n", encoding="utf-8")
    overlay_dir = cand_dir / "overlay"
    manifest = {
        "schema_version": "1.0",
        "cand_id": _PARENT_ID,
        "parent_id": None,
        "generation": 0,
        "created_at": mh.now_iso(),
        "created_by": "human",
        "target": "claude-harness",
        "source_commit": source_commit,
        "config_hash": mh.compute_config_hash(overlay_dir, config),
        "model_versions": {},
        "overlay_files": mh.list_overlay_files(overlay_dir),
        "description": "parent",
    }
    (cand_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    run_dir = mh.runs_dir(git_project, config) / _RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"run_id": _RUN_ID, "holdout": False}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text('{"verdict":"fail"}\n', encoding="utf-8")
    with gzip.open(run_dir / "events.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write('{"type":"result"}\n')
    mh.write_frontier_cache(
        git_project,
        config,
        {
            "schema_version": "1.0",
            "generated_at": mh.now_iso(),
            "ledger_line_count": 1,
            "suite_hash": "c" * 64,
            "evaluator_hash": "d" * 64,
            "cost_axis": "total_tokens",
            "points": [
                {
                    "cand_id": _PARENT_ID,
                    "quality_mean": 80.0,
                    "quality_var": 0.0,
                    "quality_min": 80.0,
                    "cost_mean": 100.0,
                    "runs": 1,
                }
            ],
            "frontier": [_PARENT_ID],
            "dominated": [],
        },
    )
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "run_completed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "run_id": _RUN_ID,
            "cand_id": _PARENT_ID,
            "target": "claude-harness",
            "holdout": False,
        },
    )


def _events(project: Path) -> list[dict]:
    return mh.read_ledger_events(project, mh.DEFAULTS)


def _prepare_stubbed_codex(tmp_path: Path, proposal: dict) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    _prepare_codex_auth(fake_home)
    _install_stub_tools(bin_dir, proposal)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(fake_home),
        "CODEX_HOME": str(fake_home / ".codex"),
        "TMPDIR": str(temp_root),
    }


def _set_env(monkeypatch, values: dict[str, str]) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _assert_no_srt_settings_dirs(tmp_path: Path) -> None:
    assert not list((tmp_path / "tmp").glob("meta-harness-srt-*"))


def _add_candidate_manifest(
    git_project: Path, cand_id: str, *, target: str, quality_mean: float
) -> dict:
    parent_manifest = json.loads(
        (mh.candidates_dir(git_project, mh.DEFAULTS) / _PARENT_ID / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = {**parent_manifest, "cand_id": cand_id, "target": target}
    cand_dir = mh.candidates_dir(git_project, mh.DEFAULTS) / cand_id
    cand_dir.mkdir(parents=True)
    (cand_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "cand_id": cand_id,
        "quality_mean": quality_mean,
        "quality_var": 0.0,
        "quality_min": quality_mean,
        "cost_mean": 100.0,
        "runs": 1,
    }


def _write_holdout_run(git_project: Path) -> None:
    holdout_dir = mh.holdout_runs_dir(git_project, mh.DEFAULTS) / _HOLDOUT_RUN_ID
    holdout_dir.mkdir(parents=True)
    (holdout_dir / "metadata.json").write_text(
        json.dumps({"run_id": _HOLDOUT_RUN_ID, "holdout": True}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (holdout_dir / "result.json").write_text('{"verdict":"fail"}\n', encoding="utf-8")
    mh.append_ledger_event(
        git_project,
        mh.DEFAULTS,
        {
            "event": "run_completed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "run_id": _HOLDOUT_RUN_ID,
            "cand_id": _PARENT_ID,
            "target": "claude-harness",
            "holdout": True,
        },
    )


def _valid_proposal(*, content: str = "# Example\n\nImproved by proposer.\n") -> dict:
    return {
        "schema_version": "1.0",
        "hypothesis": "Tighten the example facet.",
        "theme": "tighten example facet",
        "changes": [
            {
                "path": "facets/example/SKILL.md",
                "new_content": content,
            }
        ],
        "based_on_runs": [_RUN_ID],
        "expected_effect": "The failing run should pass.",
        "risk_notes": "Low risk fixture.",
    }


def test_propose_registers_candidate_from_stubbed_codex_backend(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_store(git_project, git_run)
    proposal = _valid_proposal()

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        "--json",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
        check=True,
    )

    payload = json.loads(result.stdout)
    cand_id = payload["cand_id"]
    cand_dir = mh.candidates_dir(git_project, mh.DEFAULTS) / cand_id
    manifest = json.loads((cand_dir / "manifest.json").read_text(encoding="utf-8"))
    overlay = cand_dir / "overlay" / "facets" / "example" / "SKILL.md"
    inherited = cand_dir / "overlay" / "facets" / "parent-only" / "SKILL.md"
    events = _events(git_project)
    registered = [event for event in events if event.get("cand_id") == cand_id]

    assert manifest["created_by"] == "proposer"
    assert manifest["parent_id"] == _PARENT_ID
    assert manifest["generation"] == 1
    assert overlay.read_text(encoding="utf-8") == "# Example\n\nImproved by proposer.\n"
    assert inherited.read_text(encoding="utf-8") == "# Parent only\n\nInherited content.\n"
    assert manifest["overlay_files"] == [
        "facets/example/SKILL.md",
        "facets/parent-only/SKILL.md",
    ]
    assert registered[-1]["created_by"] == "proposer"
    assert registered[-1]["proposal"]["based_on_runs"] == [_RUN_ID]
    assert registered[-1]["proposal"]["tokens_used"] == 10
    _assert_no_srt_settings_dirs(tmp_path)


def test_propose_rejects_invalid_proposal_and_saves_rejected_file(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_store(git_project, git_run)
    proposal = {
        "schema_version": "1.0",
        "hypothesis": "Unsafe change.",
        "theme": "unsafe",
        "changes": [{"path": "docs/evaluation/meta-harness.md", "new_content": "bad"}],
        "based_on_runs": [_RUN_ID],
        "expected_effect": "none",
        "risk_notes": "unsafe",
    }

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    rejected_files = sorted(mh.rejected_dir(git_project, mh.DEFAULTS).glob("*-proposal.json"))
    assert result.returncode == 2
    assert rejected_files
    rejected = json.loads(rejected_files[-1].read_text(encoding="utf-8"))
    assert "proposal schema mismatch" in rejected["reason"]
    _assert_no_srt_settings_dirs(tmp_path)


def test_propose_rejects_overlay_size_excess_and_saves_rejected_file(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_store(git_project, git_run)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    proposal = _valid_proposal(content="x" * 200001)

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    rejected_files = sorted(mh.rejected_dir(git_project, mh.DEFAULTS).glob("*-proposal.json"))
    assert result.returncode == 2
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before
    assert rejected_files
    rejected = json.loads(rejected_files[-1].read_text(encoding="utf-8"))
    assert "max_overlay_bytes" in rejected["reason"]


def test_propose_rejects_holdout_based_on_run_and_saves_rejected_file(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_store(git_project, git_run)
    _write_holdout_run(git_project)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    proposal = _valid_proposal()
    proposal["based_on_runs"] = [_HOLDOUT_RUN_ID]

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    rejected_files = sorted(mh.rejected_dir(git_project, mh.DEFAULTS).glob("*-proposal.json"))
    assert result.returncode == 2
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before
    assert rejected_files
    rejected = json.loads(rejected_files[-1].read_text(encoding="utf-8"))
    assert "based_on_runs references holdout run_id" in rejected["reason"]


def test_parent_selection_filters_mixed_target_frontier(git_project: Path, git_run) -> None:
    _prepare_store(git_project, git_run)
    other_id = "cand-20260710-010000-other-target-abcd"
    other_point = _add_candidate_manifest(
        git_project,
        other_id,
        target="skill:other",
        quality_mean=99.0,
    )
    frontier_doc = {
        "frontier": [other_id, _PARENT_ID],
        "points": [other_point, {"cand_id": _PARENT_ID, "quality_mean": 80.0}],
    }

    selected = propose_cli._select_proposal_parent(
        git_project,
        mh.DEFAULTS,
        frontier_doc,
        target="claude-harness",
        focus_candidate=None,
    )

    assert selected == _PARENT_ID


def test_parent_selection_returns_none_without_same_target(git_project: Path, git_run) -> None:
    _prepare_store(git_project, git_run)

    selected = propose_cli._select_proposal_parent(
        git_project,
        mh.DEFAULTS,
        {"frontier": [_PARENT_ID], "points": []},
        target="skill:missing",
        focus_candidate=None,
    )

    assert selected is None


def test_focus_candidate_target_mismatch_exits_2_before_backend(
    git_project: Path, git_run, monkeypatch, capsys
) -> None:
    _prepare_store(git_project, git_run)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))

    def fail_if_launched(**_kwargs):
        raise AssertionError("backend must not launch")

    monkeypatch.setattr(propose_cli.pb, "launch_proposer_backend", fail_if_launched)

    exit_code = propose_cli.cmd_propose(
        str(git_project),
        "skill:other",
        None,
        _PARENT_ID,
        False,
    )

    assert exit_code == 2
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before
    assert "expected skill:other, got claude-harness" in capsys.readouterr().err


def test_propose_rolls_back_candidate_when_ledger_append_fails(
    git_project: Path, git_run, tmp_path: Path, monkeypatch, capsys
) -> None:
    _prepare_store(git_project, git_run)
    before_candidates = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    before_events = _events(git_project)
    _set_env(monkeypatch, _prepare_stubbed_codex(tmp_path, _valid_proposal()))

    def fail_append(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(propose_cli.mh, "append_ledger_event", fail_append)

    exit_code = propose_cli.cmd_propose(
        str(git_project),
        "claude-harness",
        None,
        None,
        False,
    )

    assert exit_code == 2
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before_candidates
    assert _events(git_project) == before_events
    assert "rolled back candidate after ledger append failure" in capsys.readouterr().err
    _assert_no_srt_settings_dirs(tmp_path)


def test_propose_rejects_empty_citable_run_set_before_backend(
    git_project: Path, git_run, monkeypatch, capsys
) -> None:
    _commit_facets(git_project, git_run)
    mh.init_store(git_project, mh.DEFAULTS)

    def fail_if_launched(**_kwargs):
        raise AssertionError("backend must not launch")

    monkeypatch.setattr(propose_cli.pb, "launch_proposer_backend", fail_if_launched)

    exit_code = propose_cli.cmd_propose(
        str(git_project),
        "claude-harness",
        None,
        None,
        False,
    )

    assert exit_code == 2
    assert mh.list_candidate_ids(git_project, mh.DEFAULTS) == []
    assert "no citable non-holdout runs for target: claude-harness" in capsys.readouterr().err
