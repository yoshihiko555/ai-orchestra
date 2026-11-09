"""Phase 4 の昇格・証拠の参照・表示・記録保護の回帰テスト。"""

from __future__ import annotations

import copy
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import candidates_handoff as handoff
import candidates_model as model
import candidates_recurrence as recurrence
import candidates_runs
import candidates_scenarios as scenarios
import candidates_view as view
import pytest
import skill_evolution_common as common
import test_candidates_handoff as fixtures
from candidates_git import skill_tree_hash
from candidates_handoff_events import validate_handoff_event
from candidates_ingest import CandidatesContext
from candidates_safety import ValidationError
from candidates_scenario_events import latest_record
from test_candidates_handoff import (
    CID,
    COMMIT,
    PR,
    PROPOSAL,
    SKILL,
    evaluate_payload,
    evaluation,
    json_file,
    link_evaluations,
    promote_pr,
)
from test_candidates_recurrence import scope

improving_context = fixtures.improving_context
fake_commands = fixtures.fake_commands
no_real_gh = fixtures.no_real_gh
promoted_context = fixtures.promoted_context


@pytest.fixture
def candidate_context(improving_context: CandidatesContext) -> CandidatesContext:
    return improving_context


@pytest.mark.parametrize("verdict", ["fail", "error"])
def test_ev85_nonpassing_holdout_links_but_cli_and_transition_guard_refuse_promotion(
    improving_context: CandidatesContext, fake_commands: dict, verdict: str
) -> None:
    context = improving_context
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    linked = link_evaluations(context, evaluation(verdict=verdict))["events"][0]
    assert linked["data"]["evaluations"][0]["verdict"] == verdict
    snapshot = context.store.read()
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError, match="pass.*holdout"):
        handoff.link(context, CID, pr=PR)
    assert context.store.read() == snapshot
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before

    candidate = snapshot["candidates"][CID]
    promotion = model.event(
        "handoff.promotion_linked", CID, {"pr_url": PR, "pr_number": 42, "mh_cand_id": PROPOSAL}
    )
    snapshot["records"][promotion["eid"]] = promotion
    with pytest.raises(ValidationError, match="pass.*holdout"):
        model.validate_transition(
            snapshot,
            candidate,
            scenarios.state_event(
                candidate, "promotion_pr", refs=[linked["eid"], promotion["eid"]]
            ),
            context.orchestra,
        )


@pytest.mark.parametrize("verdict", ["fail", "error"])
@pytest.mark.parametrize("same_record", [False, True])
def test_ev85_newer_nonpassing_holdout_overrides_stale_pass_for_cli_and_transition_guard(
    improving_context: CandidatesContext,
    fake_commands: dict,
    verdict: str,
    same_record: bool,
) -> None:
    context = improving_context
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    passing = evaluation()
    nonpassing = {
        **evaluation(verdict=verdict),
        "evaluation_id": "eval-20261101-120001-1234abcd",
        "own_run_ids": ["run-20261101-120001-review-train-a1-1234abcd"],
    }
    if same_record:
        linked = link_evaluations(context, passing, nonpassing)["events"][0]
    else:
        linked = link_evaluations(context, passing)["events"][0]
        link_evaluations(context, nonpassing)
    snapshot = context.store.read()
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError, match="pass.*holdout"):
        handoff.link(context, CID, pr=PR)
    assert context.store.read() == snapshot
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before

    candidate = snapshot["candidates"][CID]
    promotion = model.event(
        "handoff.promotion_linked", CID, {"pr_url": PR, "pr_number": 42, "mh_cand_id": PROPOSAL}
    )
    with pytest.raises(ValidationError, match="pass.*holdout"):
        validate_handoff_event(snapshot, candidate, promotion)
    snapshot["records"][promotion["eid"]] = promotion
    with pytest.raises(ValidationError, match="pass.*holdout"):
        model.validate_transition(
            snapshot,
            candidate,
            scenarios.state_event(
                candidate, "promotion_pr", refs=[linked["eid"], promotion["eid"]]
            ),
            context.orchestra,
        )


def test_ev85_newer_passing_holdout_allows_promotion_after_failure(
    improving_context: CandidatesContext, fake_commands: dict
) -> None:
    context = improving_context
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    link_evaluations(context, evaluation(verdict="fail"))
    passing = link_evaluations(context)["events"][0]
    result = handoff.link(context, CID, pr=PR)
    assert passing["eid"] in result["events"][-1]["data"]["refs"]
    assert context.store.read()["candidates"][CID]["state"] == "promotion_pr"


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "CLOSED"),
        ("files", []),
        ("files", [{"path": ".claude/skills/reviewer/SKILL.md"}]),
        ("files", [{"path": "README.md"}]),
        ("files", None),
        ("url", "https://github.com/foreign/repo/pull/42"),
        ("url", "https://github.com/owner/repo/pull/43"),
    ],
)
def test_ev85_pr_link_refuses_closed_unrelated_or_foreign_pr_without_events(
    improving_context: CandidatesContext, fake_commands: dict, field: str, value: object
) -> None:
    context = improving_context
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    link_evaluations(context)
    fake_commands[field] = value
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError):
        handoff.link(context, CID, pr=PR)
    assert context.store.read()["candidates"][CID]["state"] == "improving"
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    assert (
        "gh",
        ("pr", "view", "42", "--repo", "owner/repo", "--json", "state,url,files,createdAt"),
    ) in fake_commands["calls"]


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
def test_ev85_pr_link_accepts_open_or_merged_pr_with_nested_skill_files(
    improving_context: CandidatesContext, fake_commands: dict, state: str
) -> None:
    fake_commands.update(
        state=state,
        files=[{"path": ".claude/skills/review/references/guide.md"}],
        url="https://github.com/OWNER/REPO/pull/42",
    )
    promote_pr(improving_context)
    linked = latest_record(improving_context.store.read(), CID, "handoff.promotion_linked")
    assert linked["data"] == {
        "pr_url": PR,
        "pr_number": 42,
        "mh_cand_id": PROPOSAL,
        "pr_created_at": fake_commands["createdAt"],
    }


def test_ev74_retirement_refuses_scenario_that_never_existed_on_first_parent(
    improving_context: CandidatesContext, fake_commands: dict
) -> None:
    context = improving_context
    fake_commands["parent_scenarios"] = set()
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError, match="最初の親"):
        handoff.retire_scenario(context, CID, {"files": ["sevo-00000001-train.yaml"], "pr": PR})
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    assert (
        "git",
        (
            "cat-file",
            "-e",
            f"{COMMIT}^1:packages/meta-harness/scenarios/skill/review/sevo-00000001-train.yaml",
        ),
    ) in fake_commands["calls"]


@pytest.mark.parametrize(
    "sizes,limits",
    [
        ([1, 1], {"hash_max_files": 1}),
        ([11], {"hash_max_bytes": 10}),
        ([6, 6], {"hash_max_bytes": 10}),
    ],
    ids=["file-count", "oversize-blob", "summed-blob-sizes"],
)
def test_ev86_over_limit_tree_never_reads_any_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sizes: list[int], limits: dict
) -> None:
    calls = []

    def git(directory: Path, *arguments: str, **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(arguments)
        assert arguments == ("ls-tree", "-r", "-z", "-l", COMMIT, "--", ".claude/skills/review/")
        listing = b"".join(
            f"100644 blob {COMMIT} {size}\t.claude/skills/review/file-{index}\0".encode()
            for index, size in enumerate(sizes)
        )
        return subprocess.CompletedProcess(arguments, 0, listing, b"")

    monkeypatch.setattr(scenarios, "git_read", git)
    assert skill_tree_hash(tmp_path, "review", COMMIT, {"runs": limits}) is None
    assert len(calls) == 1


@pytest.mark.parametrize("length", [40, 64])
def test_ev73_evaluation_reference_uses_base_commit_and_detects_changed_head(
    improving_context: CandidatesContext, fake_commands: dict, length: int
) -> None:
    context = improving_context
    base = "e" * length
    fake_commands["available_commits"].add(base)
    fake_commands["skill_by_commit"][base] = SKILL
    fake_commands["skill"] = b"changed HEAD\n"
    linked = link_evaluations(context, evaluation(base_commit=base))["events"][0]
    assert linked["data"]["skill_hash"] == common.hash_skill_files([("SKILL.md", SKILL)])
    assert linked["data"]["evaluations"][0]["evaluation_base_commit"] == base
    assert ("git", ("cat-file", "-e", f"{base}^{{commit}}")) in fake_commands["calls"]
    listings = [
        args for kind, args in fake_commands["calls"] if kind == "git" and args[0] == "ls-tree"
    ]
    assert [args[4] for args in listings] == [base]
    assert (
        view.show(context, context.store.read(), CID)["candidate"]["rerun_hint"]
        == "シナリオの再実行を勧める"
    )


def test_ev73_missing_local_evaluation_base_records_null_without_reading_tree(
    improving_context: CandidatesContext, fake_commands: dict
) -> None:
    context = improving_context
    linked = link_evaluations(context, evaluation(base_commit="e" * 40))["events"][0]
    assert linked["data"]["skill_hash"] is None
    assert not any(args[0] == "ls-tree" for kind, args in fake_commands["calls"] if kind == "git")
    assert "rerun_hint" not in view.show(context, context.store.read(), CID)["candidate"]


@pytest.mark.parametrize(
    "base", [None, 42, "f" * 39, "f" * 41, "f" * 63, "f" * 65, "g" * 40, "missing"]
)
@pytest.mark.parametrize("index", [0, 1])
def test_ev73_every_evaluation_requires_valid_base_commit(
    improving_context: CandidatesContext, fake_commands: dict, base: object, index: int
) -> None:
    context = improving_context
    payload = evaluate_payload(evaluation(holdout=False), evaluation())
    if base == "missing":
        del payload["evaluations"][index]["evaluation_base_commit"]
    else:
        payload["evaluations"][index]["evaluation_base_commit"] = base
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError):
        handoff.link(context, CID, path=json_file(context, payload))
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "case", ["missing-run", "wrong-candidate", "second-missing-run", "conflicting-duplicate"]
)
def test_ev73_evaluation_run_ids_and_candidate_must_match_payload_runs(
    improving_context: CandidatesContext, fake_commands: dict, case: str
) -> None:
    context = improving_context
    payload = evaluate_payload()
    if case == "missing-run":
        payload["runs"] = []
    elif case == "second-missing-run":
        payload["evaluations"][0]["own_run_ids"].append(
            "run-20261101-120000-review-train-a2-1234abcd"
        )
    elif case == "wrong-candidate":
        payload["runs"][0]["cand_id"] = fixtures.BASELINE
    else:
        other = copy.deepcopy(payload["runs"][0])
        other["cand_id"] = fixtures.BASELINE
        payload["runs"].insert(0, other)
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError, match="runs"):
        handoff.link(context, CID, path=json_file(context, payload))
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before


@pytest.mark.parametrize("index", [0, 1, 2])
def test_ev73_mixed_evaluation_base_commits_refuse_entire_link_without_events(
    improving_context: CandidatesContext, fake_commands: dict, index: int
) -> None:
    context = improving_context
    entries = [evaluation(holdout=False), evaluation(), evaluation()]
    entries[index]["evaluation_base_commit"] = "e" * 40
    snapshot = context.store.read()
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError, match="evaluation_base_commit"):
        handoff.link(context, CID, path=json_file(context, evaluate_payload(*entries)))
    assert context.store.read() == snapshot
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    assert fake_commands["calls"] == []


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("created_at", ["2026-01-15T12:00:00Z", "2026-02-01T08:30:00+09:00"])
def test_ev76_prune_protects_pr_creation_month_before_later_link(
    improving_context: CandidatesContext,
    fake_commands: dict,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    apply: bool,
    created_at: str,
) -> None:
    context = improving_context
    fake_commands.update(state=state, createdAt=created_at, mergedAt="2026-01-31T23:45:00Z")
    promote_pr(context)
    snapshot = context.store.read()
    promotion = latest_record(snapshot, CID, "handoff.promotion_linked")
    assert promotion["data"]["pr_created_at"] == created_at
    promotion["ts"] = "2026-03-01T00:00:00Z"
    monkeypatch.setattr(context.store, "read", lambda: snapshot)
    for month in ("202512", "202601", "202602", "202603", "202604"):
        (context.store.directory / f"runs-{month}.jsonl").write_text("")
    result = recurrence.prune_runs(
        context, "202603", apply=apply, now=datetime(2026, 4, 5, tzinfo=UTC)
    )
    assert result["selected"] == ["runs-202512.jsonl"]
    assert result["deleted"] == (["runs-202512.jsonl"] if apply else [])
    for month in ("202601", "202602", "202603", "202604"):
        assert (context.store.directory / f"runs-{month}.jsonl").exists()


def test_ev76_legacy_promotion_without_creation_time_validates_and_protects_link_month(
    improving_context: CandidatesContext, fake_commands: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = improving_context
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    linked = link_evaluations(context)["events"][0]
    snapshot = context.store.read()
    candidate = snapshot["candidates"][CID]
    promotion = model.event(
        "handoff.promotion_linked", CID, {"pr_url": PR, "pr_number": 42, "mh_cand_id": PROPOSAL}
    )
    context.store.write(
        lambda _: [
            promotion,
            scenarios.state_event(
                candidate, "promotion_pr", refs=[linked["eid"], promotion["eid"]]
            ),
        ]
    )
    snapshot = context.store.read()
    latest_record(snapshot, CID, "handoff.promotion_linked")["ts"] = "2026-03-01T00:00:00Z"
    monkeypatch.setattr(context.store, "read", lambda: snapshot)
    for month in ("202601", "202602", "202603", "202604"):
        (context.store.directory / f"runs-{month}.jsonl").write_text("")
    result = recurrence.prune_runs(
        context, "202604", apply=True, now=datetime(2026, 4, 5, tzinfo=UTC)
    )
    assert result["deleted"] == ["runs-202601.jsonl", "runs-202602.jsonl"]
    for month in ("202603", "202604"):
        assert (context.store.directory / f"runs-{month}.jsonl").exists()


@pytest.mark.parametrize("created_at", [None, 42, "bad", "2026-01-15T12:00:00"])
def test_ev76_pr_creation_time_requires_timezone_for_cli_and_event_guard(
    improving_context: CandidatesContext, fake_commands: dict, created_at: object
) -> None:
    context = improving_context
    handoff.link(context, CID, path=json_file(context, {"status": "ok", "cand_id": PROPOSAL}))
    link_evaluations(context)
    fake_commands["createdAt"] = created_at
    snapshot = context.store.read()
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError, match="タイムゾーン"):
        handoff.link(context, CID, pr=PR)
    assert context.store.read() == snapshot
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    with pytest.raises(ValidationError, match="タイムゾーン"):
        validate_handoff_event(
            snapshot,
            snapshot["candidates"][CID],
            model.event(
                "handoff.promotion_linked",
                CID,
                {
                    "pr_url": PR,
                    "pr_number": 42,
                    "mh_cand_id": PROPOSAL,
                    "pr_created_at": created_at,
                },
            ),
        )


@pytest.mark.parametrize("apply", [False, True])
def test_ev76_prune_protects_latest_promotion_link_month_and_later_months(
    improving_context: CandidatesContext,
    fake_commands: dict,
    monkeypatch: pytest.MonkeyPatch,
    apply: bool,
) -> None:
    context = improving_context
    promote_pr(context)
    snapshot = context.store.read()
    linked = latest_record(snapshot, CID, "handoff.promotion_linked")
    linked["ts"] = "2026-08-31T23:00:00Z"
    newer = {**linked, "eid": "abcdef123456", "ts": "2026-09-01T00:30:00+00:00"}
    snapshot["records"][newer["eid"]] = newer
    monkeypatch.setattr(context.store, "read", lambda: snapshot)
    for month in ("202607", "202608", "202609", "202610", "202611", "202612"):
        (context.store.directory / f"runs-{month}.jsonl").write_text("")
    result = recurrence.prune_runs(
        context, "202701", apply=apply, now=datetime(2026, 12, 5, tzinfo=UTC)
    )
    assert result["selected"] == ["runs-202607.jsonl", "runs-202608.jsonl"]
    assert result["deleted"] == (result["selected"] if apply else [])
    for month in ("202609", "202610", "202611", "202612"):
        assert (context.store.directory / f"runs-{month}.jsonl").exists()


@pytest.mark.parametrize("error", [PermissionError, ValidationError])
@pytest.mark.parametrize("reader", ["list", "show"])
@pytest.mark.parametrize("stage", ["status", "suggestions"])
def test_ev77_unreadable_runs_degrades_recurrence_without_failing_view(
    promoted_context: CandidatesContext,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
    reader: str,
    stage: str,
) -> None:
    context = promoted_context
    runs = context.store.directory / "runs-202611.jsonl"
    runs.write_text("")
    original = candidates_runs.open_regular
    calls = 0

    def unreadable(path: Path) -> Any:
        nonlocal calls
        if path == runs:
            calls += 1
            if stage == "status" or calls == 2:
                raise error("記録を読めません")
        return original(path)

    monkeypatch.setattr(candidates_runs, "open_regular", unreadable)
    snapshot = context.store.read()
    shown = (
        view.listing(context, snapshot)["candidates"][0]
        if reader == "list"
        else view.show(context, snapshot, CID)["candidate"]
    )
    assert shown["recurrence"] == {
        "opportunities": None,
        "message": "再発の確認に必要な記録を読めません",
    }
    assert shown["recurrence_suggestions"] == {"suggestion_only": True, "observations": []}
    assert shown["cid"] == CID and shown["state"] == "promoted"
    assert context.store.read() == snapshot


@pytest.mark.parametrize("failure", [False, True])
def test_ev80_view_hash_cache_is_shared_within_listing_and_expires_between_calls(
    improving_context: CandidatesContext,
    fake_commands: dict,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    context = improving_context
    link_evaluations(context)
    snapshot = context.store.read()
    second = copy.deepcopy(snapshot["candidates"][CID])
    second["cid"] = "C-00000002"
    snapshot["candidates"][second["cid"]] = second
    linked = copy.deepcopy(latest_record(snapshot, CID, "handoff.evaluation_linked"))
    linked.update(cid=second["cid"], eid="abcdef123456")
    snapshot["records"][linked["eid"]] = linked
    calls = []

    def calculate(directory: Path, slug: str, commit: str, config: dict) -> str | None:
        calls.append((slug, commit))
        if failure:
            raise ValidationError("git failed")
        return "sha256:" + "f" * 64

    monkeypatch.setattr(view, "skill_tree_hash", calculate)
    shown = view.listing(context, snapshot)["candidates"]
    assert calls == [("review", "HEAD")]
    assert all(("rerun_hint" in item) is not failure for item in shown)
    view.listing(context, snapshot)
    assert len(calls) == 2
    view.show(context, snapshot, CID)
    view.show(context, snapshot, CID)
    assert len(calls) == 4


@pytest.mark.parametrize("other_owner", [True, False])
def test_ev78_recurrence_cli_and_event_guard_reject_observation_owned_by_another_candidate(
    promoted_context: CandidatesContext,
    scope_event: Callable,
    write_runs: Callable,
    observation_event: Callable,
    candidate_event: Callable,
    other_owner: bool,
) -> None:
    context = promoted_context
    write_runs([scope(scope_event)])
    context.store.write(
        lambda _: [
            observation_event(
                2, attribution="run", run_id="R-000000000001", ts="2026-11-03T10:01:00Z"
            )
        ]
    )
    context.store.write(
        lambda _: (
            [candidate_event(2)]
            if other_owner
            else [model.event("candidate.observation_added", CID, {"oid": "O-00000002"})]
        )
    )
    snapshot = context.store.read()
    candidate = snapshot["candidates"][CID]
    confirmed = model.event(
        "recurrence.confirmed", CID, {"oid": "O-00000002", "run_id": "R-000000000001"}
    )
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    if other_owner:
        with pytest.raises(ValidationError, match="別の候補"):
            validate_handoff_event(snapshot, candidate, confirmed)
        with pytest.raises(ValidationError, match="別の候補"):
            recurrence.confirm_recurrence(context, CID, "O-00000002")
        assert context.store.read() == snapshot
        assert (context.store.directory / "candidates.jsonl").read_bytes() == before
    else:
        validate_handoff_event(snapshot, candidate, confirmed)
        recurrence.confirm_recurrence(context, CID, "O-00000002")
        candidate = context.store.read()["candidates"][CID]
        assert candidate["observations"].count("O-00000002") == 1
        assert candidate["state"] == "candidate"


@pytest.mark.parametrize("protect_merge", [True, False])
def test_ev76_invalid_month_filenames_are_ignored_by_listing_and_never_pruned(
    promoted_context: CandidatesContext, monkeypatch: pytest.MonkeyPatch, protect_merge: bool
) -> None:
    context = promoted_context
    for name in ("runs-abc.jsonl", "runs-202613.jsonl", "runs-202611.jsonl", "runs-202610.jsonl"):
        (context.store.directory / name).write_text("保持する\n")
    selected = []
    original = context.history

    def history(months: set[str] | None = None) -> candidates_runs.RunHistory:
        selected.append(months)
        return original(months)

    monkeypatch.setattr(context, "history", history)
    merged = latest_record(context.store.read(), CID, "handoff.merged")
    recurrence.merge_history(context, merged)
    assert selected == [{"202611"}]
    assert context.history().events == []
    assert (
        candidates_runs.RunHistory.for_evidence(
            context.store.directory,
            context.root,
            [{"ts": "2026-11-03T10:00:00Z", "sid": "session-1234"}],
        ).events
        == []
    )
    if not protect_merge:
        snapshot = context.store.read()
        snapshot["candidates"][CID]["state"] = "candidate"
        monkeypatch.setattr(context.store, "read", lambda: snapshot)
    result = recurrence.prune_runs(
        context, "202701", apply=True, now=datetime(2027, 2, 5, tzinfo=UTC)
    )
    assert result["deleted"] == (
        ["runs-202610.jsonl"] if protect_merge else ["runs-202610.jsonl", "runs-202611.jsonl"]
    )
    for name in ("runs-abc.jsonl", "runs-202613.jsonl"):
        assert (context.store.directory / name).read_text() == "保持する\n"
