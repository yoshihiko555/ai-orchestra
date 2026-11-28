"""回答の構造化と不正な根拠の扱いを検証する。"""

from __future__ import annotations

import importlib
import json
import sys
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


def test_parses_markdown_body_and_line_citations() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = '## 結果\n`config["api"]` と `[A-Z\\-]+` を確認。\n\nCITATIONS:\n- src/a.py:10-20 | A は B\nsrc/b.py:1-1 | C'

    answer, citations = parser.parse_final_answer(text)

    assert answer == '## 結果\n`config["api"]` と `[A-Z\\-]+` を確認。'
    assert citations == [
        {"path": "src/a.py", "start_line": 10, "end_line": 20, "claim": "A は B"},
        {"path": "src/b.py", "start_line": 1, "end_line": 1, "claim": "C"},
    ]


def test_parses_backticked_line_citation_with_list_marker_and_whitespace() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = " \n本文\n\n  CITATIONS:  \n\n* `src/a.py:1-2` | `a | b` を返す\n"

    answer, citations = parser.parse_final_answer(text)

    assert answer == "本文"
    assert citations == [
        {"path": "src/a.py", "start_line": 1, "end_line": 2, "claim": "`a | b` を返す"}
    ]


def test_does_not_treat_citations_marker_in_backtick_fence_as_delimiter() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = "例:\n```text\nCITATIONS:\nsrc/x.py:1-2 | 例示\n```\n以上"

    assert parser.parse_final_answer(text) == (text, None)


def test_does_not_treat_citations_marker_in_tilde_fence_as_delimiter() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = "例:\n~~~text\nCITATIONS:\nsrc/x.py:1-2 | 例示\n~~~\n以上"

    assert parser.parse_final_answer(text) == (text, None)


def test_prefers_line_citations_over_json_example_in_markdown_body() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    body = """例:
```json
{"answer":"例示","citations":[{"path":"example.py","start_line":1,"end_line":2,"claim":"例"}]}
```
```text
CITATIONS:
src/example.py:1-2 | 例示
```"""
    text = body + "\n\nCITATIONS:\nsrc/a.py:3-4 | 実際の根拠"

    answer, citations = parser.parse_final_answer(text)

    assert answer == body
    assert citations == [
        {"path": "src/a.py", "start_line": 3, "end_line": 4, "claim": "実際の根拠"}
    ]


def test_uses_last_citations_delimiter_outside_fences() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    body = "区切りの例:\nCITATIONS:\nここまでは説明。"
    text = body + "\n\nCITATIONS:\na.py:1-1 | 最後の一覧"

    answer, citations = parser.parse_final_answer(text)

    assert answer == body
    assert citations == [{"path": "a.py", "start_line": 1, "end_line": 1, "claim": "最後の一覧"}]


def test_salvages_shortened_spec_reviewer_answer_with_quotes_and_regex_escape() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    # spec-reviewer-result.json の引用符と不正エスケープによる壊れ方を残す。
    text = r"""```json
{
  "answer": "`_safe_name` (`[A-Za-z0-9_.:/@+=\-]+`) checks `config["api"]`.",
  "citations": [
    {"path": "docs/reference/configuration.md", "start_line": 19, "end_line": 35,
     "claim": "Configuration uses a base `{name}.yaml` and a local override `{name}.local.yaml` combined via deep merge."},
    {"path": "packages/core/hooks/api_profiles.py", "start_line": 10, "end_line": 17,
     "claim": "Defines `_SAFE_NAME` regex `[A-Za-z0-9_.:/@+=\-]+`."}
  ]
}
```"""

    answer, citations = parser.parse_final_answer(text)

    assert answer == text
    assert citations == [
        {
            "path": "docs/reference/configuration.md",
            "start_line": 19,
            "end_line": 35,
            "claim": "Configuration uses a base `{name}.yaml` and a local override `{name}.local.yaml` combined via deep merge.",
        },
        {
            "path": "packages/core/hooks/api_profiles.py",
            "start_line": 10,
            "end_line": 17,
            "claim": r"Defines `_SAFE_NAME` regex `[A-Za-z0-9_.:/@+=\-]+`.",
        },
    ]


def test_salvages_shortened_reverse_answer_with_unescaped_command_quotes() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    # reverse-result.json のコマンド引用にある未エスケープの引用符を残す。
    text = r"""```json
{
  "answer": "Builds `claude -p --restricted --tools "" --settings {"disableAllHooks": true}`.",
  "citations": [
    {"path": "packages/fail-logs/manifest.json", "start_line": 33, "end_line": 46,
     "claim": "Declares the three package CLI scripts: scripts/lessons_cli.py, scripts/lessons_admin.py, and scripts/eval_distill.py."},
    {"path": "packages/fail-logs/lib/distill_worker.py", "start_line": 347, "end_line": 459,
     "claim": "Resolves the claude executable, builds the restricted claude -p command."}
  ]
}
```"""

    answer, citations = parser.parse_final_answer(text)

    assert answer == text
    assert citations == [
        {
            "path": "packages/fail-logs/manifest.json",
            "start_line": 33,
            "end_line": 46,
            "claim": "Declares the three package CLI scripts: scripts/lessons_cli.py, scripts/lessons_admin.py, and scripts/eval_distill.py.",
        },
        {
            "path": "packages/fail-logs/lib/distill_worker.py",
            "start_line": 347,
            "end_line": 459,
            "claim": "Resolves the claude executable, builds the restricted claude -p command.",
        },
    ]


def test_salvage_excludes_path_example_before_citations_array() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r"""{"answer":"引用 {"path":"z.py","start_line":3,"end_line":4,"claim":"例"}",
"citations":[{"path":"a.py","start_line":1,"end_line":2,"claim":"根拠"}]}"""

    answer, citations = parser.parse_final_answer(text)

    assert answer == text
    assert citations == [{"path": "a.py", "start_line": 1, "end_line": 2, "claim": "根拠"}]


def test_salvage_retains_citation_with_string_start_line() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r"""{"answer":"`config["api"]`","citations":[
{"path":"a.py","start_line":1,"end_line":2,"claim":"読んだ範囲"},
{"path":"a.py","start_line":"3","end_line":3,"claim":"文字列の行番号"}]}"""

    answer, citations = parser.parse_final_answer(text)

    assert answer == text
    assert citations is not None
    assert len(citations) == 2
    assert citations[0] == {
        "path": "a.py",
        "start_line": 1,
        "end_line": 2,
        "claim": "読んだ範囲",
    }
    assert citations[1]["path"] == "a.py"
    assert citations[1].get("start_line") is None
    assert citations[1]["end_line"] == 3
    assert citations[1]["claim"] == "文字列の行番号"


def test_salvage_retains_citation_when_line_number_exceeds_integer_digit_limit() -> None:
    previous_limit = sys.get_int_max_str_digits()
    limit = sys.int_info.str_digits_check_threshold
    oversized_line = "9" * (limit + 1)
    text = (
        '{"answer":"`config["api"]`","citations":[{"path":"a.py","start_line":'
        + oversized_line
        + ',"end_line":2,"claim":"根拠"}]}'
    )

    try:
        sys.set_int_max_str_digits(limit)
        result = _run(Mock(return_value=_response(text)), limits=_limits(), tools=None)
    finally:
        sys.set_int_max_str_digits(previous_limit)

    assert result.answer == text
    assert result.citations == [
        {"path": "a.py", "start_line": None, "end_line": 2, "claim": "根拠", "verified": False}
    ]
    assert result.unverified_count == 1
    assert result.to_dict()["citations_parse_failed"] is False


def test_salvage_excludes_malformed_path_example_before_citations_array() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r"""{"answer":"引用 {"path":"z.py","start_line":"9"}",
"citations":[{"path":"a.py","start_line":1,"end_line":2,"claim":"根拠"}]}"""

    answer, citations = parser.parse_final_answer(text)

    assert answer == text
    assert citations == [{"path": "a.py", "start_line": 1, "end_line": 2, "claim": "根拠"}]


def test_salvages_citation_fields_without_json_envelope() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = '回答が途中で壊れた。\n{"path":"a.py","start_line":1,"end_line":2,"claim":"根拠"}'

    answer, citations = parser.parse_final_answer(text)

    assert answer == text
    assert citations == [{"path": "a.py", "start_line": 1, "end_line": 2, "claim": "根拠"}]


def test_salvages_reordered_fields_and_decodes_claim_escapes() -> None:
    parser = importlib.import_module("lib.llm.agent_answer")
    text = r"""{"answer":"`config["api"]`",
"citations":[{"path":"a.py","end_line":2,"claim":"\"api\"\n\u6839\u62e0","start_line":1}]}"""

    answer, citations = parser.parse_final_answer(text)

    assert answer == text
    assert citations == [{"path": "a.py", "start_line": 1, "end_line": 2, "claim": '"api"\n根拠'}]
