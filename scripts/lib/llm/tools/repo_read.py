"""Git の許可一覧と実パスで範囲を制限する読み取り専用の道具。"""

from __future__ import annotations

import fnmatch
import io
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .read_ledger import CitationCheck, ReadLedger, _normalize_path, parse_citation

_DEFAULT_DENY = (".env*", "*.pem", "*.key", "*credentials*", "*secret*", ".git")


@dataclass(frozen=True)
class ReadLimits:
    max_lines: int = 2000
    max_bytes: int = 200 * 1024
    max_grep_matches: int = 200
    max_entries: int = 1000

    def __post_init__(self) -> None:
        for value in (self.max_lines, self.max_bytes, self.max_grep_matches, self.max_entries):
            if type(value) is not int or value < 1:
                raise ValueError("出力上限は正の整数にしてください。")


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    text: str


class _Rejected(Exception):
    pass


def _glob_parts(pattern: str) -> tuple[str, ...]:
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        raise _Rejected("invalid_pattern")
    parts = PurePosixPath(pattern).parts
    if pattern.startswith("/") or ".." in parts:
        raise _Rejected("outside_root")
    return parts


def _matches_glob(path: str, parts: tuple[str, ...]) -> bool:
    # ** はゼロ個のディレクトリにも一致させ、通常の * は / を越えさせない。
    states = {0}
    for component in PurePosixPath(path).parts:
        for index in range(len(parts)):
            if index in states and parts[index] == "**":
                states.add(index + 1)
        following: set[int] = set()
        for index in states:
            if index < len(parts):
                if parts[index] == "**":
                    following.add(index)
                elif fnmatch.fnmatchcase(component, parts[index]):
                    following.add(index + 1)
        states = following
    for index in range(len(parts)):
        if index in states and parts[index] == "**":
            states.add(index + 1)
    return len(parts) in states


class RepoReadTools:
    """追加拒否は大小文字を区別せず、末尾の / を除いて照合する。

    / が残らないパターンは各パス要素へ、残るものは先頭の / を除いて
    ルートからのパスと親ディレクトリへ glob として照合する。
    * は階層を越えず、** はゼロ個以上の階層に一致する。
    空・NUL・.. 要素を含むパターンは構築時に ValueError にする。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        extra_deny_patterns: tuple[str, ...] = (),
        limits: ReadLimits = ReadLimits(),
        ledger: ReadLedger | None = None,
    ) -> None:
        self.root = Path(os.path.realpath(root))
        self.limits = limits
        self.ledger = ledger if ledger is not None else ReadLedger()
        deny = list(_DEFAULT_DENY)
        deny_paths: list[tuple[str, ...]] = []
        for pattern in extra_deny_patterns:
            if not isinstance(pattern, str):
                raise ValueError("拒否パターンは文字列にしてください。")
            normalized = pattern.casefold().rstrip("/")
            try:
                parts = _glob_parts(normalized.lstrip("/"))
            except _Rejected:
                raise ValueError("拒否パターンには空・NUL・.. 要素を含められません。") from None
            if "/" in normalized:
                deny_paths.append(parts)
            else:
                deny.append(normalized)
        self._deny = tuple(deny)
        self._deny_paths = tuple(deny_paths)

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> ToolResult:
        arguments: dict[str, object] = {"path": path, "start_line": start_line}
        if end_line is not None:
            arguments["end_line"] = end_line
        return self.execute("read_file", arguments)

    def list_dir(self, path: str) -> ToolResult:
        return self.execute("list_dir", {"path": path})

    def glob(self, pattern: str) -> ToolResult:
        return self.execute("glob", {"pattern": pattern})

    def grep(self, pattern: str, path: str = ".", glob: str | None = None) -> ToolResult:
        arguments: dict[str, object] = {"pattern": pattern, "path": path}
        if glob is not None:
            arguments["glob"] = glob
        return self.execute("grep", arguments)

    def check_citation(self, citation: str) -> CitationCheck:
        """現在安全に読めるファイルの行数と、返却済みの範囲を照合する。"""
        parsed = parse_citation(citation)
        if parsed is None:
            return CitationCheck(False, False, False)
        path, start, end = parsed
        try:
            relative = self._resolve(path)
            total = 0
            for number, _ in self._text_lines(relative):
                total = number
        except (_Rejected, OSError, RuntimeError, ValueError):
            return CitationCheck(False, False, False)
        return self.ledger.check_citation(
            f"{relative}:{start}-{end}", exists=True, total_lines=total
        )

    def execute(self, name: str, arguments: dict[str, object]) -> ToolResult:
        """直接呼出しとモデルからの呼出しの入力・例外変換を一か所に揃える。"""
        try:
            self._validate_call(name, arguments)
            if name == "read_file":
                return self._read_file(**arguments)
            if name == "list_dir":
                return self._list_dir(**arguments)
            if name == "glob":
                return self._glob(**arguments)
            return self._grep(**arguments)
        except _Rejected as exc:
            return ToolResult(
                False, f"{exc}: 読み取りを拒否しました。条件を変えて再試行してください。"
            )
        except UnicodeError:
            return ToolResult(False, "binary: UTF-8 テキスト以外の内容は返せません。")
        except (OSError, RuntimeError, ValueError):
            return ToolResult(False, "unreadable: 対象を安全に読み取れません。")

    @staticmethod
    def _validate_call(name: str, arguments: dict[str, object]) -> None:
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise _Rejected("invalid_arguments")
        definition = next((item for item in TOOL_DEFINITIONS if item["name"] == name), None)
        if definition is None:
            raise _Rejected("unknown_tool")
        schema = definition["parameters"]
        if (
            set(arguments) - schema["properties"].keys()
            or not set(schema["required"]) <= arguments.keys()
        ):
            raise _Rejected("invalid_arguments")
        for key, value in arguments.items():
            kind = schema["properties"][key]["type"]
            if kind == "string" and (not isinstance(value, str) or not value or "\x00" in value):
                raise _Rejected("invalid_arguments")
            if kind == "integer" and (type(value) is not int or value < 1):
                raise _Rejected("invalid_arguments")
        if "end_line" in arguments and arguments["end_line"] < arguments.get("start_line", 1):
            raise _Rejected("invalid_range")

    def _allowed_files(self) -> set[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
        }
        try:
            included = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                check=True,
                timeout=10,
                env=env,
            )
            # --exclude-standard は cached の追跡済みファイルを除外しない。
            ignored = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--ignored",
                    "--exclude-standard",
                ],
                capture_output=True,
                check=True,
                timeout=10,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            raise _Rejected("git_unavailable") from None
        paths = {os.fsdecode(path) for path in included.stdout.split(b"\0") if path}
        paths.difference_update(os.fsdecode(path) for path in ignored.stdout.split(b"\0") if path)
        return paths

    def _denied(self, path: str) -> bool:
        candidate = PurePosixPath(path.casefold())
        if any(
            fnmatch.fnmatchcase(component, pattern)
            for component in candidate.parts
            for pattern in self._deny
        ):
            return True
        return any(
            _matches_glob(parent.as_posix(), parts)
            for parent in (candidate, *candidate.parents)
            for parts in self._deny_paths
        )

    def _resolve(
        self, path: str, allowed: set[str] | None = None, *, directory: bool = False
    ) -> str:
        raw = self.root / path
        lexical = Path(os.path.abspath(raw))
        resolved = raw.resolve()
        if not lexical.is_relative_to(self.root) or not resolved.is_relative_to(self.root):
            raise _Rejected("outside_root")
        relative = _normalize_path(resolved.relative_to(self.root).as_posix())
        original = lexical.relative_to(self.root).as_posix()
        if relative is None or self._denied(original) or self._denied(relative):
            raise _Rejected("deny_list")
        files = self._allowed_files() if allowed is None else allowed
        if directory:

            def present(candidate: str) -> bool:
                return (
                    candidate == "."
                    or candidate in files
                    or any(item.startswith(candidate + "/") for item in files)
                )

            permitted = present(original) and present(relative)
        else:
            permitted = original in files and relative in files
        if not permitted:
            raise _Rejected("not_allowed")
        return relative

    def _safe_files(self, allowed: set[str]) -> Iterator[str]:
        seen: set[str] = set()
        for candidate in sorted(allowed):
            try:
                relative = self._resolve(candidate, allowed)
                if relative not in seen and (self.root / relative).is_file():
                    seen.add(relative)
                    yield relative
            except (_Rejected, OSError, RuntimeError, ValueError):
                continue

    @contextmanager
    def _open_file(self, relative: str) -> Iterator[BinaryIO]:
        """解決後の各要素を no-follow で開き、検証後のリンク差し替えも拒否する。"""
        directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = PurePosixPath(relative).parts
            for part in parts[:-1]:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                os.close(directory_fd)
                directory_fd = next_fd
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise _Rejected("not_regular_file")
                stream = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            with stream:
                yield stream
        finally:
            os.close(directory_fd)

    def _text_lines(self, relative: str) -> Iterator[tuple[int, str | None]]:
        with self._open_file(relative) as raw, io.TextIOWrapper(raw, encoding="utf-8") as stream:
            number = 0
            while first := stream.readline(self.limits.max_bytes + 1):
                number += 1
                chunk = first
                oversized = len(first.encode("utf-8")) > self.limits.max_bytes
                while True:
                    if "\0" in chunk:
                        raise _Rejected("binary")
                    if chunk.endswith("\n"):
                        break
                    chunk = stream.readline(self.limits.max_bytes + 1)
                    if not chunk:
                        break
                    oversized = True
                yield number, None if oversized else first.removesuffix("\n")

    def _read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> ToolResult:
        relative = self._resolve(path)
        output: list[str] = []
        size = 0
        total = 0
        next_line: int | None = None
        line_too_large = False
        for number, line in self._text_lines(relative):
            total = number
            if number < start_line or (end_line is not None and number > end_line):
                continue
            if next_line is not None:
                continue
            rendered = f"{number}\t{line}" if line is not None else ""
            cost = len(rendered.encode("utf-8")) + 1
            if (
                line is None
                or len(output) >= self.limits.max_lines
                or size + cost > self.limits.max_bytes
            ):
                next_line = number
                line_too_large = line is None or cost > self.limits.max_bytes
                continue
            output.append(rendered)
            size += cost
        if output:
            self.ledger.record(relative, start_line, start_line + len(output) - 1, total)
        if next_line is not None:
            if line_too_large:
                output.append(f"[truncated: {next_line} 行目はバイト上限を超えるため返せません。]")
                next_line += 1
            if next_line <= total and (end_line is None or next_line <= end_line):
                output.append(f"[truncated: continue with start_line={next_line}]")
        return ToolResult(True, "\n".join(output))

    def _entries_result(self, entries: Iterator[str], continuation: str) -> ToolResult:
        output: list[str] = []
        size = 0
        for entry in entries:
            cost = len(entry.encode("utf-8")) + 1
            if len(output) >= self.limits.max_entries or size + cost > self.limits.max_bytes:
                output.append(f"[truncated: {continuation}]")
                break
            output.append(entry)
            size += cost
        return ToolResult(True, "\n".join(output))

    def _list_dir(self, path: str) -> ToolResult:
        allowed = self._allowed_files()
        relative = self._resolve(path, allowed, directory=True)
        if not (self.root / relative).is_dir():
            raise _Rejected("not_directory")
        prefix = "" if relative == "." else relative + "/"
        children = set()
        for candidate in self._safe_files(allowed):
            if candidate.startswith(prefix):
                child, separator, _ = candidate[len(prefix) :].partition("/")
                children.add(child + ("/" if separator else ""))
        if relative != "." and not children:
            raise _Rejected("not_allowed")
        return self._entries_result(
            iter(sorted(children)), "path で絞り込むか max_entries / max_bytes を増やしてください。"
        )

    def _glob(self, pattern: str) -> ToolResult:
        parts = _glob_parts(pattern)
        candidates = (
            candidate
            for candidate in self._safe_files(self._allowed_files())
            if _matches_glob(candidate, parts)
        )
        return self._entries_result(
            candidates, "pattern を絞り込むか max_entries / max_bytes を増やしてください。"
        )

    def _grep(self, pattern: str, path: str = ".", glob: str | None = None) -> ToolResult:
        try:
            expression = re.compile(pattern)
        except re.error:
            raise _Rejected("invalid_pattern") from None
        allowed = self._allowed_files()
        relative = self._resolve(path, allowed, directory=True)
        parts = _glob_parts(glob) if glob is not None else ("**",)
        matches: list[tuple[str, int, str, int]] = []
        size = 0
        truncated = False
        for candidate in self._safe_files(allowed):
            if (
                relative != "."
                and candidate != relative
                and not candidate.startswith(relative + "/")
            ):
                continue
            if not _matches_glob(candidate, parts):
                continue
            pending: list[tuple[int, str]] = []
            pending_size = 0
            file_truncated = False
            total = 0
            try:
                for number, line in self._text_lines(candidate):
                    total = number
                    if line is None:
                        file_truncated = True
                        continue
                    if not expression.search(line):
                        continue
                    rendered = f"{candidate}:{number}:{line}"
                    cost = len(rendered.encode("utf-8")) + 1
                    if (
                        len(matches) + len(pending) >= self.limits.max_grep_matches
                        or size + pending_size + cost > self.limits.max_bytes
                    ):
                        file_truncated = True
                        continue
                    pending.append((number, rendered))
                    pending_size += cost
            except (UnicodeError, _Rejected, OSError):
                continue
            matches.extend((candidate, number, rendered, total) for number, rendered in pending)
            size += pending_size
            truncated |= file_truncated
            if truncated:
                break
        for candidate, number, _, total in matches:
            self.ledger.record(candidate, number, number, total)
        output = [rendered for _, _, rendered, _ in matches]
        if truncated:
            output.append("[truncated: path / glob で絞り込むか max_bytes を増やしてください。]")
        return ToolResult(True, "\n".join(output))


TOOL_DEFINITIONS = (
    {
        "name": "read_file",
        "description": "許可された UTF-8 ファイルの指定範囲を行番号付きで読む。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_dir",
        "description": "許可されたディレクトリの直下の子を列挙する。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "glob",
        "description": "許可されたファイルを相対 glob パターンで探す。** は再帰検索。",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "grep",
        "description": "許可されたテキストを正規表現で検索し、path:行:内容 を返す。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
)
