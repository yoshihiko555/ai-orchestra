"""API 境界を差し替え、探索・停止・根拠の受け渡しを検証する。"""

from __future__ import annotations

import importlib
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from lib.llm import tool_types
from lib.llm.tools.repo_read import RepoReadTools
from lib.llm.types import LLMError

from tests.module_loader import REPO_ROOT
from tests.unit.test_llm_repo_read import repo as repo_fixture

repo = repo_fixture


@pytest.fixture(autouse=True)
def profile_import_path(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "packages/core/hooks"))


def _limits(**overrides):
    profiles = importlib.import_module("api_profiles")
    config = {
        "api": {
            "providers": {"gemini": {"model": "test-model"}},
            "profiles": {"p": {"provider": "gemini", "limits": overrides}},
        }
    }
    return profiles.resolve_agent_settings(config, "p").limits


def _response(text="", *, calls=(), input_tokens=2, output_tokens=1):
    return tool_types.ToolTurnResponse(
        tool_types.AssistantMessage(text, calls),
        usage=tool_types.TokenUsage(input_tokens, output_tokens),
    )


def _read(call_id="read", path="a.py", **ranges):
    return tool_types.ToolCall(call_id, "read_file", {"path": path, **ranges})


def _run(call_turn, *, limits, tools, clock=lambda: 0, sleep=None):
    loop = importlib.import_module("lib.llm.agent_loop")
    return loop.run_agent_loop(
        "調べて",
        call_turn=call_turn,
        model="test-model",
        timeout_sec=17,
        max_output_tokens=123,
        limits=limits,
        tools=tools,
        on_progress=lambda *args: None,
        clock=clock,
        sleep=sleep if sleep is not None else Mock(),
    )


def test_executes_read_and_transfers_call_id_content_and_system_instruction(repo):
    (repo / "a.py").write_text("public\n", encoding="utf-8")
    turn = Mock(side_effect=[_response(calls=(_read(),)), _response("回答")])

    result = _run(turn, limits=_limits(), tools=RepoReadTools(repo))

    assert result.status == "complete"
    assert result.stop_reason is None
    assert result.turns == 2
    assert result.answer == "回答"
    assert list(result.files_read) == ["a.py"]
    initial, following = [call.args[0] for call in turn.call_args_list]
    assert isinstance(initial.messages[0], tool_types.SystemMessage)
    assert initial.messages[0].text.strip()
    assert initial.messages[-1] == tool_types.UserMessage("調べて")
    assert initial.tools
    assert initial.model == "test-model"
    assert initial.timeout_sec == 17
    assert initial.max_output_tokens == 123
    results = following.messages[-1].results
    assert [(r.call_id, r.content, r.is_error) for r in results] == [("read", "1\tpublic", False)]


def test_turn_limit_summarizes_once_without_tools_or_executing_pending_read(repo):
    (repo / "a.py").write_text("public\n", encoding="utf-8")
    tools = RepoReadTools(repo)
    execute = Mock(wraps=tools.execute)
    tools.execute = execute
    turn = Mock(
        side_effect=[_response(calls=(_read(str(i)),)) for i in range(3)]
        + [_response("途中まで", calls=(_read("unexpected"),))]
    )

    result = _run(turn, limits=_limits(max_turns=3), tools=tools)

    assert (result.status, result.stop_reason, result.answer) == (
        "partial",
        "max_turns",
        "途中まで",
    )
    assert turn.call_count == 4
    assert result.turns == 4
    assert execute.call_count == 2
    requests = [call.args[0] for call in turn.call_args_list]
    assert all(request.tools for request in requests[:3])
    assert requests[-1].tools == ()
    assert isinstance(requests[-1].messages[-1], tool_types.UserMessage)
    pending = requests[-1].messages[-2].results
    assert len(pending) == 1
    assert pending[0].call_id == "2"
    assert pending[0].is_error is True


@pytest.mark.parametrize("structured", [False, True])
def test_truncated_final_answer_is_partial_and_keeps_read_evidence(repo, structured):
    (repo / "a.py").write_text("public\n", encoding="utf-8")
    text = (
        json.dumps({"answer": "途中まで", "citations": []}, ensure_ascii=False)
        if structured
        else '```json\n{"answer": "途中まで'
    )
    turn = Mock(
        side_effect=[
            _response(calls=(_read(),)),
            replace(_response(text), truncated=True),
        ]
    )

    result = _run(turn, limits=_limits(), tools=RepoReadTools(repo))

    assert (result.status, result.stop_reason) == ("partial", "output_truncated")
    assert result.answer == ("途中まで" if structured else text)
    assert result.citations_parse_failed is (not structured)
    assert result.unverified_count == (0 if structured else None)
    assert result.files_read == ("a.py",)
    assert result.turns == turn.call_count == 2
    assert (result.tokens.input_tokens, result.tokens.output_tokens) == (4, 2)


@pytest.mark.parametrize("text", ["途中までの要約", ""])
def test_truncated_summary_overrides_turn_limit_and_preserves_available_text(repo, text):
    turn = Mock(
        side_effect=[
            _response("探索中の回答", calls=(_read(),)),
            replace(_response(text), truncated=True),
        ]
    )

    result = _run(turn, limits=_limits(max_turns=1), tools=RepoReadTools(repo))

    assert (result.status, result.stop_reason) == ("partial", "output_truncated")
    assert result.answer == (text or "探索中の回答")
    assert result.turns == turn.call_count == 2
    assert turn.call_args.args[0].tools == ()


def test_truncated_tool_turn_stops_without_execution_retry_or_summary(repo):
    (repo / "a.py").write_text("public\n", encoding="utf-8")
    tools = RepoReadTools(repo)
    tools.execute = Mock(wraps=tools.execute)
    turn = Mock(return_value=replace(_response(calls=(_read(),)), truncated=True))
    sleep = Mock()

    result = _run(turn, limits=_limits(), tools=tools, sleep=sleep)

    assert (result.status, result.stop_reason) == ("partial", "output_truncated")
    assert result.turns == turn.call_count == 1
    assert result.files_read == ()
    tools.execute.assert_not_called()
    sleep.assert_not_called()


def test_token_limit_counts_accumulated_input_and_output_before_summary(repo):
    (repo / "a.py").write_text("public\n", encoding="utf-8")
    turn = Mock(
        side_effect=[
            _response(calls=(_read("1"),), input_tokens=4, output_tokens=1),
            _response(calls=(_read("2"),), input_tokens=4, output_tokens=1),
            _response("要約", input_tokens=2, output_tokens=3),
        ]
    )

    result = _run(turn, limits=_limits(max_total_tokens=10), tools=RepoReadTools(repo))

    assert (result.status, result.stop_reason) == ("partial", "token_limit")
    assert turn.call_count == 3
    assert turn.call_args.args[0].tools == ()
    assert result.tokens.input_tokens == 10
    assert result.tokens.output_tokens == 5


def test_duration_limit_summarizes_once_using_injected_clock(repo):
    (repo / "a.py").write_text("public\n", encoding="utf-8")
    elapsed = [0]

    def call(request):
        if request.tools:
            elapsed[0] = 5
            return _response(calls=(_read(),))
        return _response("時間切れの要約", calls=(_read("unused"),))

    turn = Mock(side_effect=call)
    result = _run(
        turn,
        limits=_limits(max_duration_sec=5),
        tools=RepoReadTools(repo),
        clock=lambda: elapsed[0],
    )

    assert (result.status, result.stop_reason) == ("partial", "timeout")
    assert result.answer == "時間切れの要約"
    assert turn.call_count == 2
    assert turn.call_args.args[0].tools == ()


def test_fifth_turn_quota_preserves_fourth_answer_files_and_usage_without_retry(repo):
    (repo / "a.py").write_text("public\n", encoding="utf-8")
    turn = Mock(
        side_effect=[
            *[_response(f"回答{i}", calls=(_read(str(i)),)) for i in range(1, 5)],
            LLMError.for_category("quota_exhausted"),
        ]
    )
    sleep = Mock()

    result = _run(turn, limits=_limits(), tools=RepoReadTools(repo), sleep=sleep)

    assert (result.status, result.stop_reason) == ("partial", "quota_exhausted")
    assert result.answer == "回答4"
    assert result.turns == 4
    assert (result.tokens.input_tokens, result.tokens.output_tokens) == (8, 4)
    assert list(result.files_read) == ["a.py"]
    assert turn.call_count == 5
    assert all(call.args[0].tools for call in turn.call_args_list)
    sleep.assert_not_called()


def test_third_turn_rate_limit_recovers_with_configured_delay(repo):
    (repo / "a.py").write_text("public\n", encoding="utf-8")
    turn = Mock(
        side_effect=[
            _response(calls=(_read("1"),)),
            _response(calls=(_read("2"),)),
            LLMError.for_category("rate_limited"),
            _response("回復後の回答"),
        ]
    )
    sleep = Mock()

    result = _run(
        turn,
        limits=_limits(rate_limit_attempts=2, rate_limit_initial_delay_sec=0.25),
        tools=RepoReadTools(repo),
        sleep=sleep,
    )

    assert (result.status, result.stop_reason, result.answer) == ("complete", None, "回復後の回答")
    assert result.turns == 3
    assert turn.call_count == 4
    sleep.assert_called_once_with(0.25)
    assert turn.call_args_list[2].args[0] == turn.call_args_list[3].args[0]


def test_persistent_rate_limit_stops_after_configured_attempts_without_summary():
    turn = Mock(side_effect=LLMError.for_category("rate_limited"))
    sleep = Mock()

    result = _run(
        turn,
        limits=_limits(rate_limit_attempts=2, rate_limit_initial_delay_sec=0.5),
        tools=None,
        sleep=sleep,
    )

    assert (result.status, result.stop_reason) == ("partial", "rate_limited")
    assert turn.call_count == 2
    assert result.turns == 0
    sleep.assert_called_once_with(0.5)


def test_retry_wait_stops_at_duration_budget_before_another_exploration_request(repo):
    elapsed = [0]
    waits = []

    def sleep(delay):
        waits.append(delay)
        elapsed[0] += delay

    turn = Mock(side_effect=[LLMError.for_category("rate_limited"), _response("時間切れの要約")])
    result = _run(
        turn,
        limits=_limits(max_duration_sec=1, rate_limit_initial_delay_sec=5),
        tools=RepoReadTools(repo),
        clock=lambda: elapsed[0],
        sleep=sleep,
    )

    assert (result.status, result.stop_reason) == ("partial", "timeout")
    assert waits == [1]
    assert turn.call_count == 2
    assert turn.call_args_list[0].args[0].tools
    assert turn.call_args_list[1].args[0].tools == ()


@pytest.mark.parametrize("category", ["auth_error", "timeout", "api_error"])
def test_api_errors_stop_without_retry_or_summary(category):
    reason = "api_timeout" if category == "timeout" else category
    turn = Mock(side_effect=LLMError(category, "安全なエラー"))
    sleep = Mock()

    result = _run(turn, limits=_limits(), tools=None, sleep=sleep)

    assert (result.status, result.stop_reason) == ("partial", reason)
    assert turn.call_count == 1
    sleep.assert_not_called()


def test_summary_quota_becomes_stop_reason_and_preserves_previous_answer(repo):
    turn = Mock(
        side_effect=[
            _response("最後の回答", calls=(_read(),)),
            LLMError.for_category("quota_exhausted"),
        ]
    )

    result = _run(turn, limits=_limits(max_turns=1), tools=RepoReadTools(repo))

    assert (result.status, result.stop_reason, result.answer) == (
        "partial",
        "quota_exhausted",
        "最後の回答",
    )
    assert result.turns == 1
    assert turn.call_count == 2


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("fenced", [False, True])
def test_citations_use_real_read_evidence_for_complete_and_partial_results(repo, partial, fenced):
    (repo / "src").mkdir()
    (repo / "src/a.py").write_text("public\n" * 200, encoding="utf-8")
    citations = [
        {"path": path, "start_line": start, "end_line": end, "claim": "主張"}
        for path, start, end in [
            ("src/a.py", 10, 20),
            ("src/a.py", 120, 130),
            ("src/missing.py", 1, 5),
        ]
    ]
    responses = [_response(calls=(_read(path="src/a.py", end_line=50),))]
    if partial:
        responses.append(_response(calls=(_read("pending"),)))
    payload = json.dumps({"answer": "回答", "citations": citations})
    responses.append(_response(f"```json\n{payload}\n```" if fenced else payload))

    result = _run(
        Mock(side_effect=responses), limits=_limits(max_turns=2), tools=RepoReadTools(repo)
    )

    assert result.status == ("partial" if partial else "complete")
    assert result.answer == "回答"
    assert result.citations == [
        {**citation, "verified": index == 0} for index, citation in enumerate(citations)
    ]
    assert result.unverified_count == 2
    assert list(result.files_read) == ["src/a.py"]


def test_files_read_contains_only_successfully_returned_files_in_sorted_order(repo):
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    tools = RepoReadTools(repo)
    assert tools.read_file("b.py").ok
    assert not tools.read_file("missing.py").ok
    assert tools.glob("*.py").ok
    assert tools.read_file("./a.py").ok
    assert tools.read_file("b.py").ok

    assert tools.ledger.files() == ("a.py", "b.py")


@pytest.mark.parametrize("language", ["json", ""])
def test_fenced_answer_verifies_two_citations_against_actual_reads(repo, language):
    (repo / "a.py").write_text("first\nsecond\n", encoding="utf-8")
    citations = [
        {"path": "a.py", "start_line": line, "end_line": line, "claim": "主張"} for line in (1, 2)
    ]
    payload = json.dumps({"answer": "回答本文", "citations": citations}, ensure_ascii=False)
    turn = Mock(
        side_effect=[
            _response(calls=(_read(end_line=2),)),
            _response(f" \n```{language}\n  {payload}  ```\n"),
        ]
    )

    result = _run(turn, limits=_limits(), tools=RepoReadTools(repo))

    assert result.answer == "回答本文"
    assert result.citations == [{**citation, "verified": True} for citation in citations]
    assert result.unverified_count == 0
    assert result.to_dict()["citations_parse_failed"] is False


@pytest.mark.parametrize(
    "text",
    [
        "JSON ではない回答",
        '{"answer":"根拠フィールドなし"}',
        "[]",
        '~~~\n{"answer":"x","citations":[]}\n~~~',
        '```python\n{"answer":"x","citations":[]}\n```',
    ],
)
def test_unstructured_answer_with_tools_reports_unknown_citation_count(repo, text):
    result = _run(Mock(return_value=_response(text)), limits=_limits(), tools=RepoReadTools(repo))

    public = result.to_dict()
    assert public["answer"] == text
    assert public["citations"] == []
    assert public["citations_parse_failed"] is True
    assert public["unverified_count"] is None


def test_structured_answer_without_citations_is_not_a_parse_failure(repo):
    result = _run(
        Mock(return_value=_response(json.dumps({"answer": "根拠なし", "citations": []}))),
        limits=_limits(),
        tools=RepoReadTools(repo),
    )

    public = result.to_dict()
    assert public["citations_parse_failed"] is False
    assert public["unverified_count"] == 0
