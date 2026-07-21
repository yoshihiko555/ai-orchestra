#!/usr/bin/env python3
"""collect-todos.py — Walk a directory tree and collect TODO-like comments.

Usage:
    python3 collect-todos.py <directory>

Exit codes: 0 = success, 1 = IO failure, 2 = bad args.
Output: JSON to stdout (UTF-8, indent=2). Errors to stderr.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".cache",
        ".idea",
        ".vscode",
        "coverage",
        ".worktrees",
    }
)

BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".class",
        ".jar",
        ".war",
        ".o",
        ".a",
        ".pyc",
        ".pyo",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".mp3",
        ".mp4",
        ".mov",
        ".wav",
        ".ogg",
    }
)

MAX_FILE_SIZE_BYTES: int = 5 * 1024 * 1024  # 5 MB
BINARY_PROBE_BYTES: int = 8 * 1024  # 8 KB
LARGE_FILE_THRESHOLD: int = 1 * 1024 * 1024  # 1 MB — use generator above this

TAGS: tuple[str, ...] = ("TODO", "FIXME", "HACK", "XXX", "DEPRECATED")

# Single compiled regex.
# \b provides word boundary (not preceded/followed by [A-Za-z0-9_]).
# After tag: optional (name) or :, then message until EOL.
_TAG_PATTERN: str = (
    r"\b(?P<tag>" + "|".join(TAGS) + r")\b"
    r"(?:\([^)]*\))?"  # optional (name) — consumed but not captured as message
    r"(?::[ \t]?|[ \t]+|$)"
    r"(?P<message>[^\r\n]*)"
)
_TODO_RE: re.Pattern[str] = re.compile(_TAG_PATTERN)

_TRAILING_JUNK_RE: re.Pattern[str] = re.compile(r"[\s*/!\->)]+$")


# --- File filtering helpers -------------------------------------------------


def _is_binary_extension(path: Path) -> bool:
    return path.suffix.lower() in BINARY_EXTENSIONS


def _is_binary_content(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(BINARY_PROBE_BYTES)
        return b"\x00" in chunk
    except OSError:
        return True


def _should_skip_file(path: Path, size: int) -> bool:
    if _is_binary_extension(path):
        return True
    if size > MAX_FILE_SIZE_BYTES:
        return True
    return _is_binary_content(path)


# --- Line iteration ---------------------------------------------------------


def _iter_lines(path: Path, size: int) -> list[str]:
    """Return lines as a list for small files, generator-backed for large."""
    if size <= LARGE_FILE_THRESHOLD:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    return _read_lines_generator(path)


def _read_lines_generator(path: Path) -> list[str]:
    lines: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            lines.append(line.rstrip("\r\n"))
    return lines


# --- Regex application ------------------------------------------------------


def _extract_todos_from_line(line: str, lineno: int) -> list[dict]:
    results: list[dict] = []
    for m in _TODO_RE.finditer(line):
        tag = m.group("tag")
        raw_msg = m.group("message") or ""
        message = _TRAILING_JUNK_RE.sub("", raw_msg).strip()
        results.append({"line": lineno, "tag": tag, "message": message})
    return results


# --- Directory walk ---------------------------------------------------------


def _collect_from_file(path: Path, target: Path, size: int) -> list[dict]:
    if _should_skip_file(path, size):
        return []
    try:
        lines = _iter_lines(path, size)
    except OSError as exc:
        print(f"Warning: cannot read {path}: {exc}", file=sys.stderr)
        return []
    rel = path.relative_to(target).as_posix()
    items: list[dict] = []
    for lineno, line in enumerate(lines, start=1):
        for hit in _extract_todos_from_line(line, lineno):
            items.append({"file": rel, **hit})
    return items


def _walk(target: Path) -> list[dict]:
    all_items: list[dict] = []

    def _recurse(path: Path) -> None:
        try:
            entries = list(path.iterdir())
        except (PermissionError, OSError) as exc:
            print(f"Warning: cannot read dir {path}: {exc}", file=sys.stderr)
            return
        for entry in sorted(entries):
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in EXCLUDED_DIRS:
                    _recurse(entry)
                continue
            if not entry.is_file():
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            all_items.extend(_collect_from_file(entry, target, size))

    _recurse(target)
    return sorted(all_items, key=lambda x: (x["file"], x["line"]))


# --- Result builder ---------------------------------------------------------


def _build_result(target: Path, items: list[dict]) -> dict:
    count_by_tag: dict[str, int] = {tag: 0 for tag in TAGS}
    for item in items:
        count_by_tag[item["tag"]] += 1
    count_by_tag = {k: v for k, v in count_by_tag.items() if v > 0}
    return {
        "target": str(target.resolve()),
        "total": len(items),
        "count_by_tag": count_by_tag,
        "items": items,
    }


# --- CLI --------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect TODO-like comments from a directory tree."
    )
    parser.add_argument("directory", help="Root directory to scan")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    target = Path(args.directory)

    if not target.exists():
        print(f"Error: directory not found: {target}", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"Error: not a directory: {target}", file=sys.stderr)
        return 1

    try:
        items = _walk(target)
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    result = _build_result(target, items)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"Fatal: {exc}", file=sys.stderr)
        sys.exit(1)
