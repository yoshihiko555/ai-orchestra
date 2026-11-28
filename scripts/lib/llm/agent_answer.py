"""最終回答の構造化と、読取記録を使った根拠の検証。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from .tools.read_ledger import CitationCheck

_UNICODE_ESCAPE_DIGITS = 4
_HEX_DIGITS = "0123456789abcdefABCDEF"


def _escape_invalid_backslashes(candidate: str) -> str:
    parts: list[str] = []
    in_string = False
    index = 0
    while index < len(candidate):
        char = candidate[index]
        if char == '"':
            in_string = not in_string
        if char != "\\" or not in_string:
            parts.append(char)
            index += 1
            continue
        escape = candidate[index + 1 : index + 2]
        digits = candidate[index + 2 : index + 2 + _UNICODE_ESCAPE_DIGITS]
        if escape and escape in '"\\/bfnrt':
            width = 2
        elif (
            escape == "u"
            and len(digits) == _UNICODE_ESCAPE_DIGITS
            and all(digit in _HEX_DIGITS for digit in digits)
        ):
            width = 2 + _UNICODE_ESCAPE_DIGITS
        else:
            parts.append("\\\\")
            index += 1
            continue
        parts.append(candidate[index : index + width])
        index += width
    return "".join(parts)


def _load_answer_json(candidate: str) -> object | None:
    try:
        return json.loads(candidate, strict=False)
    except (ValueError, RecursionError):
        # モデルが引用したコードの不正なエスケープだけを救済する。
        try:
            return json.loads(_escape_invalid_backslashes(candidate), strict=False)
        except (ValueError, RecursionError):
            return None


def parse_final_answer(text: str) -> tuple[str, list[object] | None]:
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate)
    if fenced:
        candidate = fenced[1].strip()
    parsed = _load_answer_json(candidate)
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("answer"), str)
        or not isinstance(parsed.get("citations"), list)
    ):
        return text, None
    return parsed["answer"], parsed["citations"]


def verify_citations(
    raw: list[object], check: Callable[[str], CitationCheck] | None
) -> list[dict[str, object]]:
    citations = []
    for item in raw:
        fields = item if isinstance(item, dict) else {}
        citation = {
            name: fields.get(name) if type(fields.get(name)) is kind else None
            for name, kind in (
                ("path", str),
                ("start_line", int),
                ("end_line", int),
                ("claim", str),
            )
        }
        valid = all(value is not None for value in citation.values())
        citation["verified"] = bool(
            valid
            and check is not None
            and check(
                f"{citation['path']}:{citation['start_line']}-{citation['end_line']}"
            ).verified
        )
        citations.append(citation)
    return citations
