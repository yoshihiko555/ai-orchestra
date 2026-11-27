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


def test_parses_json_fence_enclosing_entire_answer():
    parser = importlib.import_module("lib.llm.agent_answer")

    assert parser.parse_final_answer('```json\n{"answer":"x","citations":[]}\n```') == ("x", [])


@pytest.mark.parametrize(
    "text",
    [
        "詳細は src/a.py:10-20 を参照",
        '~~~\n{"answer":"x","citations":[]}\n~~~',
        '前置き\n{"answer":"x","citations":[]}',
        '```json\n{"answer":"x","citations":[]}',
    ],
)
def test_does_not_extract_citations_from_prose_or_partial_fences(text):
    result = _run(Mock(return_value=_response(text)), limits=_limits(), tools=None)

    assert result.answer == text
    assert result.citations == []
    assert result.unverified_count == 0


def test_malformed_citation_field_is_retained_as_null_and_unverified():
    citation = {"path": "src/a.py", "start_line": "10", "end_line": 20, "claim": "c"}
    result = _run(
        Mock(return_value=_response(json.dumps({"answer": "x", "citations": [citation]}))),
        limits=_limits(),
        tools=None,
    )

    assert result.citations == [{**citation, "start_line": None, "verified": False}]
    assert result.unverified_count == 1
