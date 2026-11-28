"""回答の構造化と不正な根拠の扱いを検証する。"""

from __future__ import annotations

import importlib
import json
from unittest.mock import Mock

import pytest

from tests.unit.test_llm_agent_loop import _limits, _response, _run
from tests.unit.test_llm_cli import isolated_environment as isolated_environment


def test_parses_plain_json_answer_and_citations():
    citation = {"path": "src/a.py", "start_line": 10, "end_line": 20, "claim": "A は B"}
    parser = importlib.import_module("lib.llm.agent_answer")

    answer, citations = parser.parse_final_answer(
        json.dumps({"answer": "A は B", "citations": [citation]})
    )

    assert answer == "A は B"
    assert citations == [citation]


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"answer":"x","citations":[]}\n```',
        ' \n```json {"answer":"x","citations":[]} ```\t',
        '```json\n{"answer":"x","citations":[]}```',
        '```\n  {"answer":"x","citations":[]}  ```',
        ' \n``` {"answer":"x","citations":[]} ```\n',
    ],
)
def test_parses_json_fence_enclosing_entire_answer(text):
    parser = importlib.import_module("lib.llm.agent_answer")

    assert parser.parse_final_answer(text) == ("x", [])


def test_parses_fenced_answer_containing_literal_newline():
    parser = importlib.import_module("lib.llm.agent_answer")
    text = '```json\n{"answer":"first\nsecond","citations":[]}\n```'

    answer, citations = parser.parse_final_answer(text)

    assert answer.splitlines() == ["first", "second"]
    assert citations == []


@pytest.mark.parametrize(
    "text",
    [
        "詳細は src/a.py:10-20 を参照",
        '~~~\n{"answer":"x","citations":[]}\n~~~',
        '前置き\n{"answer":"x","citations":[]}',
        '```json\n{"answer":"x","citations":[]}',
        '前置き\n```json\n{"answer":"x","citations":[]}\n```',
    ],
)
def test_does_not_extract_citations_from_prose_or_partial_fences(text):
    result = _run(Mock(return_value=_response(text)), limits=_limits(), tools=None)

    assert result.answer == text
    assert result.citations == []
    assert result.unverified_count == 0
    assert result.to_dict()["citations_parse_failed"] is False


def test_malformed_citation_field_is_retained_as_null_and_unverified():
    citation = {"path": "src/a.py", "start_line": "10", "end_line": 20, "claim": "c"}
    result = _run(
        Mock(return_value=_response(json.dumps({"answer": "x", "citations": [citation]}))),
        limits=_limits(),
        tools=None,
    )

    assert result.citations == [{**citation, "start_line": None, "verified": False}]
    assert result.unverified_count == 1
    assert result.to_dict()["citations_parse_failed"] is False


def test_repairs_regex_escapes_in_answer_and_citation_claim() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = (
        r'{"answer":"`[A-Za-z0-9_.:/@+=\-]+` を使う","citations":'
        r'[{"path":"a.py","start_line":1,"end_line":2,"claim":"`\d+` に一致"}]}'
    )

    answer, citations = parser.parse_final_answer(text)

    assert answer == r"`[A-Za-z0-9_.:/@+=\-]+` を使う"
    assert citations == [{"path": "a.py", "start_line": 1, "end_line": 2, "claim": r"`\d+` に一致"}]


def test_does_not_repair_backslash_outside_json_strings() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r'{"answer":"x","citations":[]}\-'

    assert parser.parse_final_answer(text) == (text, None)


def test_preserves_valid_json_escapes() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r'{"answer":"a\\-b\n\"c\"\u00e9\/\b\f\r\t","citations":[]}'

    answer, citations = parser.parse_final_answer(text)

    assert answer == 'a\\-b\n"c"é/\b\f\r\t'
    assert citations == []


def test_preserves_valid_escapes_while_repairing_invalid_escapes() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r'{"answer":"\\- と \- と \"\d+\"\n\u00E9\/\b\f\r\t","citations":[]}'

    answer, citations = parser.parse_final_answer(text)

    assert answer == r'\- と \- と "\d+"' + "\né/\b\f\r\t"
    assert citations == []


def test_repairs_invalid_unicode_escapes_and_preserves_valid_unicode() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r'{"answer":"\u12 と \u12G4 と \u0041","citations":[]}'

    answer, citations = parser.parse_final_answer(text)

    assert answer == r"\u12 と \u12G4 と A"
    assert citations == []


def test_repairs_fenced_answer_containing_literal_newline() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r"""```json
{"answer":"first\-
second","citations":[]}
```"""

    answer, citations = parser.parse_final_answer(text)

    assert answer == "first\\-\nsecond"
    assert citations == []
