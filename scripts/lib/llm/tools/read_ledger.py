"""セッション内で実際に返した行の範囲と、根拠の照合。"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass


def _normalize_path(path: str) -> str | None:
    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    normalized = posixpath.normpath(path)
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def parse_citation(citation: str) -> tuple[str, int, int] | None:
    """根拠のパスと包括的な行範囲を返し、不正な根拠は None にする。"""
    if not isinstance(citation, str):
        return None
    match = re.fullmatch(r"(.+):([0-9]+)-([0-9]+)", citation)
    if match is None or (path := _normalize_path(match[1])) is None:
        return None
    try:
        start, end = int(match[2]), int(match[3])
    except ValueError:
        return None
    if start < 1 or end < start:
        return None
    return path, start, end


@dataclass(frozen=True)
class CitationCheck:
    exists: bool
    in_bounds: bool
    within_read: bool

    @property
    def verified(self) -> bool:
        return self.exists and self.in_bounds and self.within_read


class ReadLedger:
    """1 インスタンスを 1 セッションとして扱う。"""

    def __init__(self) -> None:
        self._files: dict[str, tuple[int, tuple[tuple[int, int], ...]]] = {}

    def record(self, path: str, start: int, end: int, total_lines: int) -> None:
        normalized = _normalize_path(path)
        if (
            normalized is None
            or any(type(value) is not int for value in (start, end, total_lines))
            or not 1 <= start <= end <= total_lines
        ):
            raise ValueError("読取記録には相対パスと実在する行範囲が必要です。")
        previous = self._files.get(normalized)
        ranges = [*(previous[1] if previous else ()), (start, end)]
        merged: list[tuple[int, int]] = []
        for lower, upper in sorted(ranges):
            if merged and lower <= merged[-1][1] + 1:
                merged[-1] = merged[-1][0], max(merged[-1][1], upper)
            else:
                merged.append((lower, upper))
        self._files[normalized] = total_lines, tuple(merged)

    def check_citation(
        self, citation: str, *, exists: bool, total_lines: int | None = None
    ) -> CitationCheck:
        """存在情報は呼出側から受け、総行数の省略時だけ読取時の値を使う。"""
        parsed = parse_citation(citation)
        if parsed is None:
            return CitationCheck(bool(exists), False, False)
        path, start, end = parsed
        recorded = self._files.get(path)
        count = total_lines if total_lines is not None else recorded[0] if recorded else 0
        in_bounds = type(count) is int and 1 <= start <= end <= count
        within_read = recorded is not None and any(
            lower <= start and end <= upper for lower, upper in recorded[1]
        )
        return CitationCheck(bool(exists), in_bounds, within_read)
