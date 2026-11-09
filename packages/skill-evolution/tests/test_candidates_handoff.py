"""EV-73〜80・85・86 と受け渡しの戻り道の契約。"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import candidates_handoff as handoff
import candidates_model as model
import candidates_scenarios as scenarios
import pytest
import skill_evolution_common as common
from candidates_git import skill_tree_hash
from candidates_handoff_events import validate_handoff_event
from candidates_ingest import CandidatesContext
from candidates_safety import ValidationError
from candidates_scenario_events import latest_record
from candidates_view import listing, show

CID = "C-00000001"
PROPOSAL = "cand-20261101-120000-review-abcd"
BASELINE = "cand-20261101-110000-baseline-abcd"
PR = "https://github.com/owner/repo/pull/42"
COMMIT = "a" * 40
SUITE = "b" * 64
SKILL = b"review instructions\n"
MERGED_AT = "2026-11-02T10:00:00Z"


def evaluation(
    *,
    candidate: str = PROPOSAL,
    holdout: bool = True,
    suite: str = SUITE,
    verdict: str = "pass",
    base_commit: str = COMMIT,
) -> dict:
    return {
        "evaluation_id": "eval-20261101-120000-1234abcd",
        "cand_id": candidate,
        "holdout": holdout,
        "own_suite_hash": suite,
        "evaluator_hash": "c" * 64,
        "own_run_ids": ["run-20261101-120000-review-train-a1-1234abcd"],
        "verdict": verdict,
        "evaluation_base_commit": base_commit,
    }


def run_result(evaluation: dict, run_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "cand_id": evaluation["cand_id"],
        "scenario_id": "review-train",
        "verdict": evaluation["verdict"],
        "critical": [],
        "critical_pass_rate": 1,
        "checks": [],
        "self_report": None,
        "penalty": 0,
        "quality_score": 100,
        "cost": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "tool_uses": 0,
            "duration_ms": 0,
            "total_cost_usd": 0,
            "num_turns": 0,
        },
        "attempt": 1,
        "attempts_total": 1,
        "claude_version": "test",
        "errors": [],
    }


def evaluate_payload(*evaluations: dict) -> dict:
    entries = list(evaluations) or [evaluation()]
    return {
        "status": "ok",
        "runs": [run_result(item, run_id) for item in entries for run_id in item["own_run_ids"]]
        if all(isinstance(item["own_run_ids"], list) for item in entries)
        else [],
        "evaluations": entries,
    }


def json_file(context: CandidatesContext, payload: dict) -> Path:
    path = context.project / "output.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def link_evaluations(context: CandidatesContext, *evaluations: dict) -> dict:
    return handoff.link(
        context,
        CID,
        path=json_file(context, evaluate_payload(*evaluations)),
    )


@pytest.fixture(autouse=True)
def no_real_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> dict:
        pytest.fail("テストで実際の gh は呼び出せません")

    monkeypatch.setattr(handoff, "gh_read", refuse)


@pytest.fixture
def improving_context(
    candidate_context_factory: Callable,
    scenario_template: Path,
) -> CandidatesContext:
    context = candidate_context_factory(scenario_template)
    candidate = context.store.read()["candidates"][CID]
    entering = scenarios.state_event(candidate, "drafting")
    drafted = model.event(
        "scenario.drafted",
        CID,
        {
            "draft": 1,
            "files": {"sevo-00000001-train.yaml": "d" * 64},
            "input_sha256": "e" * 64,
            "model": "test",
            "budget": 0.1,
            "ts": "2026-11-01T10:00:00Z",
        },
    )
    approved = model.event(
        "scenario.approved",
        CID,
        {
            "draft": 1,
            "files": drafted["data"]["files"],
            "draft_eid": drafted["eid"],
            "answers": {
                "reproduces": True,
                "holdout_distinct": True,
                "publishable": True,
                "human_lines_reviewed": True,
            },
            "human_written_lines": [],
        },
    )
    approving = model.event(
        "candidate.state_changed",
        CID,
        {"from": "drafting", "to": "scenario_approved", "refs": [approved["eid"]]},
    )
    verified = model.event(
        "scenario.suite_verified",
        CID,
        {
            "files": drafted["data"]["files"],
            "approval_eid": approved["eid"],
            "suite_hash": SUITE,
            "commit": COMMIT,
        },
    )
    context.store.write(
        lambda _: [
            entering,
            drafted,
            approved,
            approving,
            verified,
            model.event(
                "candidate.state_changed",
                CID,
                {"from": "scenario_approved", "to": "improving", "refs": [verified["eid"]]},
            ),
        ]
    )
    return context


@pytest.fixture
def fake_commands(monkeypatch: pytest.MonkeyPatch, no_real_gh: None) -> dict:
    state: dict = {
        "remote": "git@github.com:Owner/Repo.git",
        "ancestor": True,
        "state": "MERGED",
        "mergedAt": MERGED_AT,
        "createdAt": "2026-11-01T10:00:00Z",
        "url": PR,
        "commit": COMMIT,
        "skill": SKILL,
        "skill_by_commit": {},
        "available_commits": {COMMIT},
        "blobs": {},
        "files": [{"path": ".claude/skills/review/SKILL.md"}],
        "parent_scenarios": {"sevo-00000001-train.yaml"},
        "present_scenarios": set(),
        "calls": [],
    }

    def git(
        directory: Path, *arguments: str, check: bool = True, binary: bool = False
    ) -> subprocess.CompletedProcess:
        state["calls"].append(("git", arguments))
        if arguments == ("remote", "get-url", "origin"):
            stdout = state["remote"]
        elif arguments == ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"):
            stdout = "refs/remotes/origin/trunk"
        elif arguments[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(arguments, 0 if state["ancestor"] else 1, "", "")
        elif arguments[:2] == ("cat-file", "-e"):
            revision = arguments[-1]
            exists = (
                revision.removesuffix("^{commit}") in state["available_commits"]
                if revision.endswith("^{commit}")
                else revision.split(":", 1)[-1].rsplit("/", 1)[-1] in state["parent_scenarios"]
            )
            return subprocess.CompletedProcess(arguments, 0 if exists else 1, "", "")
        elif arguments[:3] == ("ls-tree", "-r", "-z"):
            assert arguments[3] == "-l"
            path = arguments[-1]
            if path.startswith(".claude/skills/"):
                commit = arguments[4]
                content = state["skill_by_commit"].get(commit, state["skill"])
                sha = COMMIT if commit == "HEAD" else commit
                state["blobs"][sha] = content
                stdout = f"100644 blob {sha} {len(content)}\t{path}SKILL.md\0".encode()
            elif Path(path).name in state["present_scenarios"]:
                stdout = f"100644 blob {COMMIT} 1\t{path}\0".encode()
            else:
                stdout = b""
        elif arguments[:2] == ("cat-file", "blob"):
            stdout = state["blobs"][arguments[-1]]
        else:
            pytest.fail(f"許可していない git 呼び出し: {arguments}")
        return subprocess.CompletedProcess(arguments, 0, stdout, b"" if binary else "")

    def gh(directory: Path, *arguments: str) -> dict:
        state["calls"].append(("gh", arguments))
        payload = {"state": state["state"], "mergedAt": state["mergedAt"], "url": state["url"]}
        if "mergeCommit" in arguments[-1]:
            payload["mergeCommit"] = {"oid": state["commit"]}
        if "files" in arguments[-1]:
            payload["files"] = state["files"]
        if "createdAt" in arguments[-1]:
            payload["createdAt"] = state["createdAt"]
        return payload

    monkeypatch.setattr(scenarios, "git_read", git)
    monkeypatch.setattr(handoff, "gh_read", gh)
    return state


def promote_pr(context: CandidatesContext) -> None:
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    link_evaluations(context)
    handoff.link(context, CID, pr=PR)


@pytest.fixture
def promoted_context(
    improving_context: CandidatesContext, fake_commands: dict
) -> CandidatesContext:
    promote_pr(improving_context)
    handoff.confirm_merge(improving_context, CID)
    return improving_context


@pytest.mark.parametrize("bad_index", [0, 1])
def test_ev73_any_suite_hash_mismatch_refuses_entire_link(
    improving_context: CandidatesContext,
    fake_commands: dict,
    bad_index: int,
) -> None:
    context = improving_context
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    evaluations = [evaluation(holdout=False), evaluation()]
    evaluations[bad_index]["own_suite_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="own_suite_hash"):
        link_evaluations(context, *evaluations)
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    assert context.store.read()["candidates"][CID]["state"] == "improving"


def test_ev74_show_traces_approval_evaluation_proposal_pr_and_merge(
    promoted_context: CandidatesContext,
    fake_commands: dict,
) -> None:
    context = promoted_context
    handoff.retire_scenario(context, CID, {"files": ["sevo-00000001-train.yaml"], "pr": PR})
    for path in (context.store.directory / "candidates-state.json",):
        path.unlink()
    snapshot = context.store.read()
    trace = show(context, snapshot, CID)["trace"]
    by_kind = {record["type"]: record["data"] for record in trace}
    assert by_kind["scenario.approved"]["files"] == {"sevo-00000001-train.yaml": "d" * 64}
    assert by_kind["scenario.suite_verified"]["suite_hash"] == SUITE
    assert by_kind["scenario.suite_verified"]["commit"] == COMMIT
    assert by_kind["handoff.proposal_linked"]["mh_cand_id"] == PROPOSAL
    assert by_kind["handoff.evaluation_linked"]["evaluations"] == [evaluation()]
    assert by_kind["handoff.promotion_linked"]["pr_url"] == PR
    assert by_kind["handoff.merged"]["merge_commit"] == COMMIT
    assert by_kind["handoff.merged"]["skill_hash"] == common.hash_skill_files([("SKILL.md", SKILL)])
    assert by_kind["handoff.merged"]["merged_at"] == MERGED_AT
    assert by_kind["scenario.retired"]["files"] == ["sevo-00000001-train.yaml"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "OPEN"),
        ("state", "CLOSED"),
        ("url", "https://github.com/other/repo/pull/42"),
        ("url", "https://github.com/owner/repo/pull/43"),
        ("ancestor", False),
        ("mergedAt", "2026-11-02T10:00:00"),
        ("mergedAt", None),
        ("commit", "bad"),
    ],
)
def test_ev75_merge_refusals_preserve_state_and_ledger(
    improving_context: CandidatesContext,
    fake_commands: dict,
    field: str,
    value: object,
) -> None:
    context = improving_context
    promote_pr(context)
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    fake_commands[field] = value
    with pytest.raises(ValidationError):
        handoff.confirm_merge(context, CID)
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    assert context.store.read()["candidates"][CID]["state"] == "promotion_pr"


def test_ev75_confirm_merge_uses_default_branch_and_allows_unresolved_hash(
    improving_context: CandidatesContext,
    fake_commands: dict,
) -> None:
    context = improving_context
    promote_pr(context)
    context.config["runs"]["hash_max_files"] = 0
    completed = handoff.confirm_merge(context, CID)
    assert "git fetch origin trunk" in completed["guidance"]
    assert ("git", ("merge-base", "--is-ancestor", COMMIT, "origin/trunk")) in fake_commands[
        "calls"
    ]
    assert latest_record(context.store.read(), CID, "handoff.merged")["data"]["skill_hash"] is None
    assert (
        show(context, context.store.read(), CID)["candidate"]["recurrence"]["opportunities"] is None
    )


def test_ev79_repeated_similar_evidence_is_suggested_before_candidates(
    improving_context: CandidatesContext,
    observation_event: Callable,
) -> None:
    context = improving_context
    context.store.write(
        lambda _: [
            observation_event(2),
            observation_event(3, contexts=[{"kind": "pr", "ref": "owner/repo#42"}]),
        ]
    )
    shown = listing(context, context.store.read())
    assert list(shown)[0] == "suggestions"
    assert shown["suggestions"][0]["observations"] == ["O-00000002", "O-00000003"]
    assert shown["suggestions"][0]["suggestion_only"] is True


@pytest.mark.parametrize(
    "case", ["no-proposal", "baseline", "train", "old-proposal", "old-cycle", "suite-changed"]
)
def test_ev85_promotion_requires_current_proposal_holdout_and_verified_suite(
    improving_context: CandidatesContext,
    fake_commands: dict,
    case: str,
) -> None:
    context = improving_context
    if case != "no-proposal":
        handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    link_evaluations(
        context,
        evaluation(candidate=BASELINE if case == "baseline" else PROPOSAL, holdout=case != "train"),
    )
    if case == "old-proposal":
        handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": BASELINE}))
    if case == "old-cycle":
        snapshot = context.store.read()
        snapshot["candidates"][CID]["state_since"] = "2099-01-01T00:00:00Z"
        context.store._atomic_json("candidates-state.json", snapshot)
    if case == "suite-changed":
        snapshot = context.store.read()
        verified = latest_record(snapshot, CID, "scenario.suite_verified")
        context.store.write(
            lambda _: [
                model.event(
                    "scenario.suite_verified", CID, {**verified["data"], "suite_hash": "f" * 64}
                )
            ]
        )
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError):
        handoff.link(context, CID, pr=PR)
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before


def test_ev85_transition_guard_checks_holdout_candidate_not_only_record_types(
    improving_context: CandidatesContext,
    fake_commands: dict,
) -> None:
    context = improving_context
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    link_evaluations(context, evaluation(candidate=BASELINE))
    snapshot = context.store.read()
    candidate = snapshot["candidates"][CID]
    promotion = model.event(
        "handoff.promotion_linked", CID, {"pr_url": PR, "pr_number": 42, "mh_cand_id": PROPOSAL}
    )
    snapshot["records"][promotion["eid"]] = promotion
    evaluation_record = latest_record(snapshot, CID, "handoff.evaluation_linked")
    with pytest.raises(ValidationError, match="holdout"):
        model.validate_transition(
            snapshot,
            candidate,
            scenarios.state_event(
                candidate, "promotion_pr", refs=[promotion["eid"], evaluation_record["eid"]]
            ),
            context.orchestra,
        )


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/repo.git",
        "ssh://git@github.com/OWNER/REPO.git",
        "git@github.com:Owner/Repo.git",
    ],
)
def test_ev85_matching_proposal_holdout_links_repository_pr(
    improving_context: CandidatesContext,
    fake_commands: dict,
    remote: str,
) -> None:
    fake_commands["remote"] = remote
    promote_pr(improving_context)
    assert improving_context.store.read()["candidates"][CID]["state"] == "promotion_pr"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other/repo/pull/42",
        PR + "/",
        PR + "?x=1",
        "http://github.com/owner/repo/pull/42",
        "https://github.com/owner/repo/pull/0",
    ],
)
def test_ev85_foreign_or_noncanonical_pr_urls_are_refused(
    improving_context: CandidatesContext,
    fake_commands: dict,
    url: str,
) -> None:
    context = improving_context
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    link_evaluations(context)
    with pytest.raises(ValidationError):
        handoff.link(context, CID, pr=url)
    assert context.store.read()["candidates"][CID]["state"] == "improving"


def test_ev86_git_tree_hash_matches_hooks_with_modes_exclusions_symlinks_and_limits(
    tmp_path: Path,
) -> None:
    project = tmp_path / "git-project"
    project.mkdir()

    def git(*args: str, input: str | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(project), *args],
            input=input,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    git("init")
    directory = project / ".claude/skills/review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_bytes(SKILL)
    executable = directory / "run.py"
    executable.write_bytes(b"print('review')\n")
    executable.chmod(0o755)
    for name in (".hidden", "__pycache__/cache.py", "references/.hidden/text"):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"excluded" * 100)
    (directory / "file-link").symlink_to("SKILL.md")
    (directory / "dir-link").symlink_to("references", target_is_directory=True)
    git("add", "-f", ".claude")
    tree = git("write-tree")
    commit = git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree,
        input="fixture\n",
    )
    git("update-ref", "HEAD", commit)
    assert skill_tree_hash(project, "review", commit, {}) == common.hash_skill_directory(directory)
    assert skill_tree_hash(project, "review", "HEAD", {"runs": {"hash_max_files": 1}}) is None
    assert common.hash_skill_directory(directory, {"runs": {"hash_max_files": 1}}) is None
    assert skill_tree_hash(project, "review", commit, {"runs": {"hash_max_bytes": 1}}) is None
    assert common.hash_skill_directory(directory, {"runs": {"hash_max_bytes": 1}}) is None


def test_ev86_git_reader_skips_submodules_and_symlinks(
    improving_context: CandidatesContext,
    fake_commands: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import candidates_git

    monkeypatch.setattr(
        candidates_git,
        "tree_entries",
        lambda *args: iter(
            [
                ("160000", "commit", COMMIT, ".claude/skills/review/submodule", None),
                ("120000", "blob", COMMIT, ".claude/skills/review/link", 8),
                ("100755", "blob", COMMIT, ".claude/skills/review/SKILL.md", len(SKILL)),
            ]
        ),
    )
    fake_commands["blobs"][COMMIT] = SKILL
    assert skill_tree_hash(
        improving_context.project, "review", COMMIT, {}
    ) == common.hash_skill_files([("SKILL.md", SKILL)])


@pytest.mark.parametrize(
    "case",
    ["symlink", "oversize", "malformed", "utf8", "array", "error", "unknown", "duplicate", "fifo"],
)
def test_ev73_link_rejects_unsafe_files_and_unknown_payloads_without_events(
    improving_context: CandidatesContext,
    fake_commands: dict,
    case: str,
) -> None:
    import os

    context = improving_context
    path = json_file(context, {"status": "ok", "cand_id": PROPOSAL})
    if case == "symlink":
        link = context.project / "symlink.json"
        link.symlink_to(path)
        path = link
    elif case == "oversize":
        path.write_bytes(b" " * (handoff.JSON_LIMIT_BYTES + 1))
    elif case == "malformed":
        path.write_text("{")
    elif case == "utf8":
        path.write_bytes(b"\xff")
    elif case == "array":
        path.write_text("[]")
    elif case == "error":
        path.write_text('{"status":"error","cand_id":"' + PROPOSAL + '"}')
    elif case == "unknown":
        path.write_text('{"status":"ok","other":42}')
    elif case == "duplicate":
        path.write_text('{"status":"ok","status":"ok","cand_id":"' + PROPOSAL + '"}')
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path)
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError):
        handoff.link(context, CID, path=path)
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("evaluation_id", "bad"),
        ("cand_id", "bad"),
        ("holdout", 1),
        ("own_suite_hash", "bad"),
        ("evaluator_hash", "bad"),
        ("own_run_ids", "bad"),
        ("own_run_ids", ["bad"]),
        ("verdict", "other"),
        ("evaluation_base_commit", "bad"),
        ("extra", "bad"),
    ],
)
def test_ev73_link_validates_every_evaluation_field(
    improving_context: CandidatesContext,
    fake_commands: dict,
    field: str,
    value: object,
) -> None:
    payload = evaluation()
    payload[field] = value
    before = (improving_context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError):
        link_evaluations(improving_context, payload)
    assert (improving_context.store.directory / "candidates.jsonl").read_bytes() == before


def test_ev73_evaluation_links_head_hash_and_validates_run_result_types(
    improving_context: CandidatesContext,
    fake_commands: dict,
) -> None:
    context = improving_context
    linked = link_evaluations(context)
    assert linked["events"][0]["data"]["skill_hash"] == common.hash_skill_files(
        [("SKILL.md", SKILL)]
    )
    with pytest.raises(ValidationError, match="実行結果"):
        handoff.link(
            context,
            CID,
            path=json_file(
                context, {"status": "ok", "runs": [{"run_id": 123}], "evaluations": [evaluation()]}
            ),
        )


@pytest.mark.parametrize("invalid", [None, "quality", "self-report", "cost", "unknown", "run-id"])
def test_ev73_complete_evaluate_payload_checks_nested_run_fields(
    improving_context: CandidatesContext,
    fake_commands: dict,
    invalid: str | None,
) -> None:
    context = improving_context
    run = {
        "schema_version": "1.0",
        "run_id": evaluation()["own_run_ids"][0],
        "cand_id": PROPOSAL,
        "scenario_id": "review-train",
        "verdict": "pass",
        "critical": [],
        "critical_pass_rate": 1,
        "checks": [],
        "self_report": None,
        "penalty": 0,
        "quality_score": 100,
        "cost": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "tool_uses": 0,
            "duration_ms": 0,
            "total_cost_usd": 0,
            "num_turns": 0,
        },
        "attempt": 1,
        "attempts_total": 1,
        "claude_version": "test",
        "errors": [],
    }
    if invalid == "quality":
        run["quality_score"] = 101
    elif invalid == "self-report":
        run["self_report"] = {"retries": True}
    elif invalid == "cost":
        run["cost"]["total_tokens"] = "wrong"
    elif invalid == "unknown":
        run["extra"] = "wrong"
    elif invalid == "run-id":
        run["run_id"] = "wrong"
    path = json_file(context, {"status": "ok", "runs": [run], "evaluations": [evaluation()]})
    if invalid is None:
        assert (
            handoff.link(context, CID, path=path)["events"][0]["type"]
            == "handoff.evaluation_linked"
        )
    else:
        before = (context.store.directory / "candidates.jsonl").read_bytes()
        with pytest.raises(ValidationError):
            handoff.link(context, CID, path=path)
        assert (context.store.directory / "candidates.jsonl").read_bytes() == before


def test_ev75_close_pr_requires_unmerged_closed_and_withdraw_requires_reason(
    improving_context: CandidatesContext,
    fake_commands: dict,
) -> None:
    context = improving_context
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError, match="理由"):
        handoff.withdraw(context, CID, "")
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    promote_pr(context)
    for state, merged_at in (("OPEN", None), ("MERGED", MERGED_AT), ("CLOSED", MERGED_AT)):
        fake_commands.update(state=state, mergedAt=merged_at)
        before = (context.store.directory / "candidates.jsonl").read_bytes()
        with pytest.raises(ValidationError):
            handoff.close_pr(context, CID)
        assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    fake_commands.update(state="CLOSED", mergedAt=None)
    handoff.close_pr(context, CID, "閉じたため戻す")
    assert context.store.read()["candidates"][CID]["state"] == "candidate"
    assert latest_record(context.store.read(), CID, "handoff.closed")["data"]["pr_url"] == PR


def test_ev75_withdraw_preserves_approved_scenarios(
    improving_context: CandidatesContext,
) -> None:
    context = improving_context
    handoff.withdraw(context, CID, "採用基準を満たさなかった")
    snapshot = context.store.read()
    assert snapshot["candidates"][CID]["state"] == "candidate"
    assert latest_record(snapshot, CID, "scenario.approved")
    assert snapshot["candidates"][CID]["history"][-1]["data"]["note"] == "採用基準を満たさなかった"


@pytest.mark.parametrize(
    "case",
    ["unapproved", "empty", "path", "duplicate", "present", "open", "foreign", "unreachable"],
)
def test_ev74_retirement_refuses_unapproved_or_unremoved_files(
    improving_context: CandidatesContext,
    fake_commands: dict,
    case: str,
) -> None:
    context = improving_context
    files = ["sevo-00000001-train.yaml"]
    url = PR
    if case == "unapproved":
        files = ["sevo-other.yaml"]
    elif case == "empty":
        files = []
    elif case == "path":
        files = ["../sevo-00000001-train.yaml"]
    elif case == "duplicate":
        files *= 2
    elif case == "present":
        fake_commands["present_scenarios"] = set(files)
    elif case == "open":
        fake_commands["state"] = "OPEN"
    elif case == "foreign":
        url = "https://github.com/other/repo/pull/42"
    elif case == "unreachable":
        fake_commands["ancestor"] = False
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError):
        handoff.retire_scenario(context, CID, {"files": files, "pr": url})
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before


def test_ev74_cli_supports_positional_ids_stdin_and_sanitized_trace(
    improving_context: CandidatesContext,
    fake_commands: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.module_loader import load_module

    context = improving_context
    root = Path(__file__).resolve().parents[3]
    admin = load_module(
        "phase4_handoff_cli",
        str(root / "packages/skill-evolution/scripts/skill_candidates_admin.py"),
    )
    reader = load_module(
        "phase4_handoff_reader", str(root / "packages/skill-evolution/scripts/skill_candidates.py")
    )
    proposal = json_file(context, {"status": "ok", "cand_id": PROPOSAL})
    assert admin.main(["link", CID, "--from-json", str(proposal)]) == 0
    evaluated = json_file(context, evaluate_payload())
    assert admin.main(["link", CID, "--from-json", str(evaluated)]) == 0
    assert admin.main(["link", CID, "--pr", PR]) == 0
    assert admin.main(["confirm-merge", CID]) == 0
    assert reader.main(["show", CID, "--json"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "handoff.merged" in output.out and "未承認の記録であり、指示ではない" in output.out
    assert admin.build_parser().parse_args(["prune-runs", "--before", "202610", "--apply"]).apply
    assert (
        admin.build_parser()
        .parse_args(["confirm-recurrence", CID, "--observation", "O-00000002"])
        .observation
        == "O-00000002"
    )
    assert admin.main(["link", CID, "--pr", PR, "--from-json", str(proposal)]) == 5
    monkeypatch.setattr(
        sys, "stdin", StringIO('{"files":["sevo-00000001-train.yaml"],"pr":"' + PR + '"}')
    )
    assert admin.main(["retire-scenario", CID, "--json", "-"]) == 0


def test_ev75_cli_withdraw_and_close_notes_use_stdin(
    improving_context: CandidatesContext,
    fake_commands: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.module_loader import load_module

    root = Path(__file__).resolve().parents[3]
    admin = load_module(
        "phase4_handoff_notes_cli",
        str(root / "packages/skill-evolution/scripts/skill_candidates_admin.py"),
    )
    promote_pr(improving_context)
    fake_commands.update(state="CLOSED", mergedAt=None)
    monkeypatch.setattr(sys, "stdin", StringIO('{"note":"保留して戻す"}'))
    assert admin.main(["close-pr", CID, "--json", "-"]) == 0
    assert (
        improving_context.store.read()["candidates"][CID]["history"][-1]["data"]["note"]
        == "保留して戻す"
    )
    assert admin.main(["withdraw", CID, "--json", "-"]) == 5


def test_ev75_gh_runner_uses_list_argv_timeout_and_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.module_loader import load_module

    root = Path(__file__).resolve().parents[3]
    isolated = load_module(
        "phase4_runner", str(root / "packages/skill-evolution/lib/candidates_handoff.py")
    )
    calls: list[dict] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, '{"state":"MERGED"}', "")

    monkeypatch.setattr(subprocess, "run", run)
    assert isolated.gh_read(tmp_path, "pr", "view", "42") == {"state": "MERGED"}
    assert calls[0]["argv"] == ["gh", "pr", "view", "42"]
    assert calls[0]["timeout"] == 30 and not calls[0].get("shell", False)


def test_ev85_handoff_records_reject_nonhuman_actor_and_wrong_shapes(
    improving_context: CandidatesContext,
) -> None:
    snapshot = improving_context.store.read()
    candidate = snapshot["candidates"][CID]
    record = model.event("handoff.proposal_linked", CID, {"mh_cand_id": PROPOSAL}, actor="ingest")
    with pytest.raises(ValidationError):
        validate_handoff_event(snapshot, candidate, record)
    record.update(actor="human")
    record["data"]["extra"] = "bad"
    with pytest.raises(ValidationError):
        validate_handoff_event(snapshot, candidate, record)
