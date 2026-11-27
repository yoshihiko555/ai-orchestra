"""最終回答の構造化と、読取記録を使った根拠の検証。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from .tools.read_ledger import CitationCheck


def parse_final_answer(text: str) -> tuple[str, list[object] | None]:
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate)
    if fenced:
        candidate = fenced[1].strip()
    try:
        parsed = json.loads(candidate, strict=False)
    except (ValueError, RecursionError):
        return text, None
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
