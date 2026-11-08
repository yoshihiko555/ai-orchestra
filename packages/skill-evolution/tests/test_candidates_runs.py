"""EV-50〜53: 区切りと、自動帰属で曖昧さを除く独立した条件。"""

from datetime import UTC, datetime

import pytest
from candidates_runs import RunHistory
from candidates_safety import timestamp


def test_ev50_main_boundary_ignores_same_prompt_and_compares_utc(scope_event):
    start = scope_event(at="2026-11-01T19:00:00+09:00")
    history = RunHistory(
        [
            start,
            scope_event("turn.started", "2026-11-01T10:01:00Z"),
            scope_event("turn.started", "2026-11-01T10:02:00Z", prompt="prompt-2"),
            scope_event("turn.stopped", "2026-11-01T10:03:00Z"),
        ]
    )
    assert history.scopes[0]["end"] == "2026-11-01T10:02:00Z"
    assert (
        history.attribute_failure("session-1234", "2026-11-01T10:00:00Z")["attributed_by"] == "auto"
    )
    assert (
        history.attribute_failure("session-1234", "2026-11-01T10:02:00Z")["attribution"]
        == "unknown"
    )


def test_ev50_first_stop_or_other_prompt_and_session_isolation(scope_event):
    history = RunHistory(
        [
            scope_event(),
            scope_event("turn.stopped", "2026-11-01T10:01:00Z", sid="another-session"),
            scope_event("turn.stopped", "2026-11-01T10:02:00Z"),
            scope_event("turn.started", "2026-11-01T10:03:00Z", prompt="prompt-2"),
        ]
    )
    assert history.scopes[0]["boundary"] == "turn.stopped"
    assert history.scopes[0]["end"] == "2026-11-01T10:02:00Z"


def test_ev50_duplicate_run_and_tool_events_count_once(scope_event):
    start = scope_event(
        trigger="skill_tool",
        tool_use_id="tool-1",
        src={
            "tool_use_id": "hook_input",
            "agent_id": "absent",
            "model": "absent",
            "skill_hash": "unresolved",
        },
    )
    duplicate = {**start, "run_id": "R-000000000002"}
    history = RunHistory(
        [
            start,
            start,
            duplicate,
            scope_event("run.confirmed", "2026-11-01T10:01:00Z", tool_use_id="tool-1"),
        ]
    )
    assert len(history.scopes) == 1
    assert history.confirmed(history.scopes[0])
    assert not RunHistory([start]).confirmed(start)
    assert RunHistory([scope_event()]).confirmed(scope_event())


def test_ev50_month_selection_and_malformed_lines(candidate_context, scope_event, write_runs):
    write_runs(
        [
            scope_event(at="2026-10-31T23:59:00Z"),
            scope_event("turn.stopped", "2026-11-01T00:01:00Z"),
        ]
    )
    path = candidate_context.store.directory / "runs-202611.jsonl"
    with path.open("a") as stream:
        stream.write('invalid\n[]\n{"v":2}\n')
    assert len(candidate_context.history({"202611"}).scopes) == 0
    history = candidate_context.history({"202610", "202611"})
    assert len(history.scopes) == 1
    assert history.scopes[0]["end"] == "2026-11-01T00:01:00Z"


def test_ev51_background_scope_survives_main_stop_until_matching_agent_stop(scope_event):
    history = RunHistory(
        [
            scope_event(agent="agent-1"),
            scope_event("turn.stopped", "2026-11-01T10:01:00Z"),
            scope_event("agent.stopped", "2026-11-01T10:02:00Z", agent="agent-2"),
            scope_event("agent.stopped", "2026-11-01T10:03:00Z", agent="agent-1"),
        ]
    )
    assert history.scopes[0]["end"] == "2026-11-01T10:03:00Z"


def test_ev52_unique_closed_unnested_scope_is_auto_without_tool_id(scope_event):
    history = RunHistory([scope_event(), scope_event("turn.stopped", "2026-11-01T10:03:00Z")])
    attribution = history.attribute_failure("session-1234", "2026-11-01T10:01:00Z")
    assert attribution["attributed_by"] == "auto"
    assert attribution["run_id"] == "R-000000000001"
    assert all(attribution["conditions"].values())


def test_ev53_overlapping_scopes_are_unknown(scope_event):
    history = RunHistory(
        [
            scope_event(),
            scope_event(number=2, at="2026-11-01T10:01:00Z"),
            scope_event("turn.stopped", "2026-11-01T10:03:00Z"),
        ]
    )
    assert (
        history.attribute_failure("session-1234", "2026-11-01T10:02:00Z")["attribution"]
        == "unknown"
    )


def test_ev53_later_nested_start_disqualifies_earlier_failure(scope_event):
    history = RunHistory(
        [
            scope_event(),
            scope_event(number=2, at="2026-11-01T10:02:00Z"),
            scope_event("turn.stopped", "2026-11-01T10:03:00Z"),
        ]
    )
    attribution = history.attribute_failure("session-1234", "2026-11-01T10:01:00Z")
    assert attribution["conditions"]["unique_scope"]
    assert not attribution["conditions"]["no_nested_scope"]
    assert attribution["attribution"] == "unknown"


def test_ev53_a_b_a_continuation_is_unknown(scope_event):
    history = RunHistory(
        [
            scope_event(),
            scope_event(number=2, at="2026-11-01T10:01:00Z", agent="agent-1"),
            scope_event("agent.stopped", "2026-11-01T10:02:00Z", agent="agent-1"),
            scope_event("turn.stopped", "2026-11-01T10:04:00Z"),
        ]
    )
    attribution = history.attribute_failure("session-1234", "2026-11-01T10:03:00Z")
    assert attribution["conditions"]["unique_scope"]
    assert attribution["attribution"] == "unknown"


def test_ev53_running_subagent_without_skill_is_unknown(scope_event):
    history = RunHistory(
        [
            scope_event(),
            scope_event("agent.started", "2026-11-01T10:01:00Z", agent="agent-1"),
            scope_event("turn.stopped", "2026-11-01T10:03:00Z"),
        ]
    )
    attribution = history.attribute_failure("session-1234", "2026-11-01T10:02:00Z")
    assert attribution["conditions"]["unique_scope"]
    assert attribution["conditions"]["no_nested_scope"]
    assert not attribution["conditions"]["no_running_subagent"]
    assert attribution["attribution"] == "unknown"


def test_ev53_missing_boundary_is_unknown(scope_event):
    history = RunHistory([scope_event()])
    attribution = history.attribute_failure("session-1234", "2026-11-01T10:02:00Z")
    assert not attribution["conditions"]["recorded_boundary"]
    assert attribution["attribution"] == "unknown"


def test_ev53_no_scope_and_resumed_turn_are_unknown(scope_event):
    history = RunHistory(
        [scope_event(), scope_event("turn.started", "2026-11-01T10:01:00Z", prompt="resume")]
    )
    assert (
        history.attribute_failure("session-1234", "2026-11-01T10:02:00Z")["attribution"]
        == "unknown"
    )
    assert (
        history.attribute_failure("different-session", "2026-11-01T10:00:00Z")["attribution"]
        == "unknown"
    )


def test_ev53_agent_interval_is_start_inclusive_end_exclusive(scope_event):
    history = RunHistory(
        [
            scope_event("agent.started", "2026-11-01T10:01:00Z", agent="agent-1"),
            scope_event("agent.stopped", "2026-11-01T10:02:00Z", agent="agent-1"),
        ]
    )
    assert history.running_subagents("session-1234", timestamp("2026-11-01T10:01:00Z")) == [
        "agent-1"
    ]
    assert history.running_subagents("session-1234", datetime(2026, 11, 1, 10, 2, tzinfo=UTC)) == []


def test_ev50_ingest_reads_following_month_only_until_boundary(
    candidate_context, scope_event, write_runs, monkeypatch
):
    import candidates_runs

    write_runs(
        [
            scope_event(at="2026-10-31T23:59:00Z"),
            scope_event("turn.stopped", "2026-11-01T00:01:00Z"),
            scope_event(number=2, at="2026-12-01T00:00:00Z"),
        ]
    )
    opened = []
    original = candidates_runs.open_regular

    def track(path):
        opened.append(path.name)
        return original(path)

    monkeypatch.setattr(candidates_runs, "open_regular", track)
    history = RunHistory.for_evidence(
        candidate_context.store.directory,
        candidate_context.root,
        [{"sid": "session-1234", "ts": "2026-10-31T23:59:30Z"}],
    )
    assert history.scopes[0]["end"] == "2026-11-01T00:01:00Z"
    assert opened == ["runs-202610.jsonl", "runs-202611.jsonl"]


def test_ev53_subagent_started_in_older_month_still_prevents_attribution(
    candidate_context, scope_event, write_runs
):
    write_runs(
        [
            scope_event("agent.started", "2026-08-01T00:00:00Z", agent="agent-1"),
            scope_event(),
            scope_event("turn.stopped", "2026-11-01T10:03:00Z"),
        ]
    )
    history = RunHistory.for_evidence(
        candidate_context.store.directory,
        candidate_context.root,
        [{"sid": "session-1234", "ts": "2026-11-01T10:01:00Z"}],
    )
    assert (
        history.attribute_failure("session-1234", "2026-11-01T10:01:00Z")["attribution"]
        == "unknown"
    )


@pytest.mark.parametrize("confirmation", ["absent", "wrong-session", "wrong-tool", "matching"])
def test_ev53_skill_tool_requires_matching_confirmation(scope_event, confirmation):
    start = scope_event(
        trigger="skill_tool",
        tool_use_id="skill-call-1",
        src={
            "tool_use_id": "hook_input",
            "agent_id": "absent",
            "model": "absent",
            "skill_hash": "unresolved",
        },
    )
    events = [start, scope_event("turn.stopped", "2026-11-01T10:03:00Z")]
    if confirmation != "absent":
        events.append(
            scope_event(
                "run.confirmed",
                "2026-11-01T10:00:01Z",
                sid="other-session" if confirmation == "wrong-session" else start["sid"],
                tool_use_id="other-tool" if confirmation == "wrong-tool" else start["tool_use_id"],
            )
        )
    attribution = RunHistory(events).attribute_failure("session-1234", "2026-11-01T10:01:00Z")
    assert attribution["conditions"]["confirmed_scope"] == (confirmation == "matching")
    assert attribution["attribution"] == ("run" if confirmation == "matching" else "unknown")


def test_ev53_unconfirmed_nested_scope_prevents_attribution(scope_event):
    nested = scope_event(
        at="2026-11-01T10:02:00Z",
        number=2,
        trigger="skill_tool",
        tool_use_id="denied-call",
        src={
            "tool_use_id": "hook_input",
            "agent_id": "absent",
            "model": "absent",
            "skill_hash": "unresolved",
        },
    )
    history = RunHistory(
        [scope_event(), nested, scope_event("turn.stopped", "2026-11-01T10:03:00Z")]
    )
    for at in ("2026-11-01T10:01:00Z", "2026-11-01T10:02:00Z"):
        assert history.attribute_failure("session-1234", at)["attribution"] == "unknown"
