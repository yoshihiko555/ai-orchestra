"""EV-76〜80 と昇格後の観測・実行記録の整理。"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import candidates_handoff as handoff
import candidates_model as model
import pytest
import skill_evolution_common as common
import test_candidates_handoff as handoff_fixtures
from candidates_ingest import CandidatesContext
from candidates_recurrence import confirm_recurrence, prune_runs
from candidates_safety import ValidationError, display
from candidates_scenario_events import latest_record
from candidates_view import listing, show
from test_candidates_handoff import (
    CID,
    MERGED_AT,
    SKILL,
    link_evaluations,
    promote_pr,
)

improving_context = handoff_fixtures.improving_context
fake_commands = handoff_fixtures.fake_commands
no_real_gh = handoff_fixtures.no_real_gh
promoted_context = handoff_fixtures.promoted_context


@pytest.fixture
def candidate_context(improving_context: CandidatesContext) -> CandidatesContext:
    return improving_context


def scope(
    factory: Callable,
    number: int = 1,
    *,
    at: str = "2026-11-03T10:00:00Z",
    **fields: Any,
) -> dict:
    return factory(
        number=number,
        at=at,
        skill_hash=common.hash_skill_files([("SKILL.md", SKILL)]),
        src={
            "tool_use_id": "absent",
            "agent_id": "absent",
            "model": "absent",
            "skill_hash": "project_skill_dir",
        },
        **fields,
    )


def test_ev76_opportunities_require_post_merge_same_skill_hash_and_execution(
    promoted_context: CandidatesContext,
    scope_event: Callable,
    write_runs: Callable,
) -> None:
    events = [
        scope(scope_event),
        scope(scope_event, 2, at=MERGED_AT),
        scope(scope_event, 3, at="2026-11-01T10:00:00Z"),
        scope(scope_event, 4, skill="other"),
    ]
    old = scope(scope_event, 5)
    old["skill_hash"] = "sha256:" + "f" * 64
    unresolved = scope(scope_event, 6)
    unresolved.update(skill_hash=None, src={**unresolved["src"], "skill_hash": "unresolved"})
    unconfirmed = scope(scope_event, 7, trigger="skill_tool", tool_use_id="tool-7")
    unconfirmed["src"]["tool_use_id"] = "hook_input"
    confirmed = copy.deepcopy(unconfirmed)
    confirmed.update(run_id="R-000000000008", tool_use_id="tool-8")
    events.extend(
        [
            old,
            unresolved,
            unconfirmed,
            confirmed,
            scope_event("run.confirmed", at="2026-11-03T10:01:00Z", tool_use_id="tool-8"),
        ]
    )
    write_runs(events + [events[0]])
    shown = show(promoted_context, promoted_context.store.read(), CID)["candidate"]
    assert shown["recurrence"]["opportunities"] == 2


def test_ev77_message_counts_only_unknown_evidence_in_matching_sessions_after_merge(
    promoted_context: CandidatesContext,
    scope_event: Callable,
    write_runs: Callable,
    observation_event: Callable,
) -> None:
    context = promoted_context
    write_runs([scope(scope_event)])
    context.store.write(
        lambda _: [
            observation_event(2, skill=None, ts="2026-11-03T10:01:00Z"),
            observation_event(3, skill=None, ts="2026-11-01T10:01:00Z"),
            observation_event(4, skill=None, sid="other-session", ts="2026-11-03T10:01:00Z"),
            observation_event(5, ts="2026-11-03T10:01:00Z"),
        ]
    )
    rendered = display(listing(context, context.store.read()))
    assert "1 回の該当する機会で、確認された再発なし（帰属 unknown の証拠 1 件）" in rendered
    assert "%" not in rendered and "率" not in rendered


def test_ev78_human_confirmation_attaches_observation_and_reopens_with_history(
    promoted_context: CandidatesContext,
    scope_event: Callable,
    write_runs: Callable,
    observation_event: Callable,
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
    before = context.store.read()
    assert show(context, before, CID)["candidate"]["state"] == "promoted"
    assert context.store.read() == before
    confirm_recurrence(context, CID, "O-00000002")
    (context.store.directory / "candidates-state.json").unlink()
    snapshot = context.store.read()
    assert snapshot["candidates"][CID]["state"] == "candidate"
    assert snapshot["candidates"][CID]["observations"].count("O-00000002") == 1
    assert latest_record(snapshot, CID, "handoff.merged")
    assert latest_record(snapshot, CID, "recurrence.confirmed")["data"] == {
        "oid": "O-00000002",
        "run_id": "R-000000000001",
    }


@pytest.mark.parametrize(
    "reason",
    ["skill-only", "before", "hash", "skill", "missing", "dismissed", "null-hash", "wrong-session"],
)
def test_ev78_invalid_recurrence_is_refused_without_events(
    promoted_context: CandidatesContext,
    scope_event: Callable,
    observation_event: Callable,
    write_runs: Callable,
    reason: str,
) -> None:
    context = promoted_context
    run = scope(scope_event)
    observation = observation_event(
        2, attribution="run", run_id=run["run_id"], ts="2026-11-03T10:01:00Z"
    )
    if reason == "skill-only":
        observation["data"].update(attribution="skill", run_id=None)
    if reason == "before":
        run["ts"] = MERGED_AT
    if reason == "hash":
        run["skill_hash"] = "sha256:" + "f" * 64
    if reason == "skill":
        run["skill"] = "other"
    if reason == "wrong-session":
        run["sid"] = "other-session"
    write_runs([] if reason == "missing" else [run])
    context.store.write(
        lambda _: (
            [observation]
            + (
                [model.event("observation.dismissed", "O-00000002", {})]
                if reason == "dismissed"
                else []
            )
        )
    )
    if reason == "null-hash":
        snapshot = context.store.read()
        latest_record(snapshot, CID, "handoff.merged")["data"]["skill_hash"] = None
        context.store._atomic_json("candidates-state.json", snapshot)
    before = (context.store.directory / "candidates.jsonl").read_bytes()
    with pytest.raises(ValidationError):
        confirm_recurrence(context, CID, "O-00000002")
    assert (context.store.directory / "candidates.jsonl").read_bytes() == before


def test_ev80_post_merge_suggestions_use_existing_words_and_never_confirm(
    promoted_context: CandidatesContext,
    scope_event: Callable,
    write_runs: Callable,
    observation_event: Callable,
) -> None:
    context = promoted_context
    write_runs([scope(scope_event)])
    context.store.write(
        lambda _: [
            observation_event(
                2,
                attribution="run",
                run_id="R-000000000001",
                ts="2026-11-03T10:01:00Z",
                excerpt="unrelated words",
            ),
            observation_event(
                3, attribution="run", run_id="R-000000000001", ts="2026-11-03T10:02:00Z"
            ),
            observation_event(4),
        ]
    )
    before = context.store.read()
    suggestions = show(context, before, CID)["candidate"]["recurrence_suggestions"]
    assert suggestions["suggestion_only"] is True
    assert [item["oid"] for item in suggestions["observations"]] == ["O-00000003", "O-00000002"]
    assert context.store.read() == before


def test_ev76_prune_lists_then_deletes_only_unprotected_old_months(
    promoted_context: CandidatesContext,
) -> None:
    context = promoted_context
    for month in ("202609", "202610", "202611", "202612", "202701"):
        (context.store.directory / f"runs-{month}.jsonl").write_text("")
    now = datetime(2026, 12, 5, tzinfo=UTC)
    preview = prune_runs(context, "202702", now=now)
    assert preview["selected"] == ["runs-202609.jsonl", "runs-202610.jsonl"]
    assert preview["deleted"] == []
    assert (context.store.directory / "runs-202609.jsonl").exists()
    applied = prune_runs(context, "202702", apply=True, now=now)
    assert applied["deleted"] == preview["selected"]
    assert not (context.store.directory / "runs-202609.jsonl").exists()
    assert (context.store.directory / "runs-202611.jsonl").exists()
    assert (context.store.directory / "runs-202612.jsonl").exists()


def test_ev76_prune_protects_current_month_and_respects_before(
    improving_context: CandidatesContext,
) -> None:
    context = improving_context
    for month in ("202609", "202610", "202611", "202612"):
        (context.store.directory / f"runs-{month}.jsonl").write_text("")
    now = datetime(2026, 11, 5, tzinfo=UTC)
    assert prune_runs(context, "202610", now=now)["selected"] == ["runs-202609.jsonl"]
    assert prune_runs(context, "202701", now=now)["selected"] == [
        "runs-202609.jsonl",
        "runs-202610.jsonl",
    ]
    with pytest.raises(ValidationError):
        prune_runs(context, "202613", now=now)


@pytest.mark.parametrize("apply", [False, True])
def test_ev76_prune_refuses_symlinks_without_deleting_any_file(
    improving_context: CandidatesContext,
    apply: bool,
) -> None:
    context = improving_context
    target = context.project / "keep.txt"
    target.write_text("keep")
    regular = context.store.directory / "runs-202608.jsonl"
    regular.write_text("")
    link = context.store.directory / "runs-202609.jsonl"
    link.symlink_to(target)
    with pytest.raises(ValidationError):
        prune_runs(context, "202610", apply=apply, now=datetime(2026, 11, 5, tzinfo=UTC))
    assert target.read_text() == "keep"
    assert regular.exists() and link.is_symlink()


def test_ev80_ft17_rerun_hint_uses_latest_reference_hash_and_omits_git_failure(
    improving_context: CandidatesContext,
    fake_commands: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = improving_context
    link_evaluations(context)
    assert "rerun_hint" not in show(context, context.store.read(), CID)["candidate"]

    fake_commands["skill"] = b"changed skill\n"
    assert (
        show(context, context.store.read(), CID)["candidate"]["rerun_hint"]
        == "シナリオの再実行を勧める"
    )

    def broken(*args: Any, **kwargs: Any) -> str:
        raise ValidationError("git failed")

    import candidates_view

    monkeypatch.setattr(candidates_view, "skill_tree_hash", broken)
    assert "rerun_hint" not in show(context, context.store.read(), CID)["candidate"]


def test_ev80_ft17_merge_hash_replaces_evaluation_reference(
    improving_context: CandidatesContext,
    fake_commands: dict,
) -> None:
    context = improving_context
    promote_pr(context)
    fake_commands["skill"] = b"merged revision\n"
    handoff.confirm_merge(context, CID)
    assert "rerun_hint" not in show(context, context.store.read(), CID)["candidate"]
    fake_commands["skill"] = SKILL
    assert (
        show(context, context.store.read(), CID)["candidate"]["rerun_hint"]
        == "シナリオの再実行を勧める"
    )
