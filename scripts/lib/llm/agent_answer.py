"""最終回答の構造化と、読取記録を使った根拠の検証。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from .tools.read_ledger import CitationCheck

_UNICODE_ESCAPE_DIGITS = 4
_HEX_DIGITS = "0123456789abcdefABCDEF"
_CITATIONS_DELIMITER = "CITATIONS:"
_CODE_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_LINE_CITATION = re.compile(r"(?:[-*]\s+)?`?(.+?):([0-9]+)-([0-9]+)`?\s*\|\s*(.+)")
_JSON_STRING = r'"(?:[^"\\]|\\.)*"'
_JSON_CITATIONS = re.compile(r'"citations"\s*:\s*\[')
_JSON_PATH = re.compile(r'"path"\s*:\s*(' + _JSON_STRING + r")")
_JSON_RANGE = re.compile(r'"(start_line|end_line)"\s*:\s*(-?[0-9]+)(?=\s*(?:[,}]|$))')
_JSON_CLAIM = re.compile(r'"claim"\s*:\s*(' + _JSON_STRING + r")")


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


def _parse_line_citations(text: str) -> tuple[str, list[object]] | None:
    lines = text.splitlines(keepends=True)
    delimiter = None
    fence = None
    for index, line in enumerate(lines):
        marker = _CODE_FENCE.fullmatch(line.rstrip("\r\n"))
        if marker is not None:
            if fence is None:
                fence = marker[1]
            elif (
                marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip()
            ):
                fence = None
            continue
        if fence is None and line.strip() == _CITATIONS_DELIMITER:
            delimiter = index
    if delimiter is None:
        return None
    citations: list[object] = []
    for line in lines[delimiter + 1 :]:
        if not line.strip():
            continue
        match = _LINE_CITATION.fullmatch(line.strip())
        if match is None:
            citations.append({})
            continue
        try:
            start, end = int(match[2]), int(match[3])
        except ValueError:
            citations.append({})
            continue
        citations.append(
            {"path": match[1], "start_line": start, "end_line": end, "claim": match[4]}
        )
    return "".join(lines[:delimiter]).strip(), citations


def _parse_json_answer(text: str) -> tuple[str, list[object] | None]:
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


def _salvage_citations(text: str) -> list[object] | None:
    candidate = text
    for marker in _JSON_CITATIONS.finditer(text):
        candidate = text[marker.end() :]
    paths = list(_JSON_PATH.finditer(candidate))
    citations: list[object] = []
    for index, match in enumerate(paths):
        path = _load_answer_json(match[1])
        citation: dict[str, object] = {}
        if isinstance(path, str):
            citation["path"] = path
        stop = paths[index + 1].start() if index + 1 < len(paths) else len(candidate)
        fields = {
            field[1]: field[2] for field in _JSON_RANGE.finditer(candidate, match.end(), stop)
        }
        for name in ("start_line", "end_line"):
            if name in fields:
                try:
                    citation[name] = int(fields[name])
                except ValueError:
                    # 桁数制限を超えた行番号も、不完全な根拠として残す。
                    continue
        claim = _JSON_CLAIM.search(candidate, match.end(), stop)
        decoded_claim = _load_answer_json(claim[1]) if claim is not None else None
        if claim is not None and not isinstance(decoded_claim, str):
            # claim のエスケープが救済できなくても、主張の原文を失わないようにする。
            decoded_claim = claim[1][1:-1]
        citation["claim"] = decoded_claim
        citations.append(citation)
    return citations if citations else None


def parse_final_answer(text: str) -> tuple[str, list[object] | None]:
    line_answer = _parse_line_citations(text)
    if line_answer is not None:
        return line_answer
    answer, citations = _parse_json_answer(text)
    if citations is not None:
        return answer, citations
    # 壊れた JSON の answer 境界を推測せず、原文と救済した根拠を返す。
    return text, _salvage_citations(text)


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
