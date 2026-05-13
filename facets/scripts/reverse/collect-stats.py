#!/usr/bin/env python3
"""Walk a directory tree and report file count and lines-of-code per language."""

import argparse
import json
import sys
from pathlib import Path

EXT_TO_LANG: dict[str, str] = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".md": "Markdown",
    ".mdx": "Markdown",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".toml": "TOML",
    ".sql": "SQL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".sass": "CSS",
}

EXCLUDED_DIRS: set[str] = {
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

MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024
BINARY_CHECK_BYTES: int = 8192


def is_excluded_dir(path: Path) -> bool:
    return path.name in EXCLUDED_DIRS


def is_valid_file(path: Path, target_root: Path) -> bool:
    if path.is_symlink():
        try:
            resolved = path.resolve()
            target_resolved = target_root.resolve()
            resolved.relative_to(target_resolved)
        except ValueError:
            return False

    try:
        size = path.stat().st_size
    except OSError:
        return False

    if size > MAX_FILE_SIZE_BYTES:
        return False

    try:
        with path.open("rb") as f:
            chunk = f.read(BINARY_CHECK_BYTES)
        if b"\x00" in chunk:
            return False
    except OSError:
        return False

    return True


def count_loc(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0

    if not text:
        return 0

    # Count newlines; add 1 if file has no trailing newline (last line still counts)
    loc = text.count("\n")
    if not text.endswith("\n"):
        loc += 1
    return loc


def _init_lang_entry() -> dict[str, int]:
    return {"files": 0, "loc": 0}


def collect_stats(target: Path) -> dict:
    lang_stats: dict[str, dict[str, int]] = {}
    dir_stats: dict[str, dict[str, int]] = {}
    total_files = 0
    total_loc = 0

    # Resolve once for symlink boundary checks
    target_resolved = target.resolve()

    for item in target.iterdir():
        if item.is_dir() and not is_excluded_dir(item):
            dir_stats[item.name] = {"files": 0, "loc": 0}

    def walk(path: Path) -> None:
        nonlocal total_files, total_loc

        try:
            entries = list(path.iterdir())
        except PermissionError:
            print(f"Permission denied: {path}", file=sys.stderr)
            return

        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                # Skip directory symlinks to prevent traversal outside target boundary
                if entry.is_symlink():
                    continue
                if not is_excluded_dir(entry):
                    walk(entry)
                continue

            if not entry.is_file(follow_symlinks=False):
                continue

            if not is_valid_file(entry, target_resolved):
                continue

            lang = EXT_TO_LANG.get(entry.suffix.lower(), "Other")
            loc = count_loc(entry)

            if lang not in lang_stats:
                lang_stats[lang] = _init_lang_entry()
            lang_stats[lang]["files"] += 1
            lang_stats[lang]["loc"] += loc

            total_files += 1
            total_loc += loc

            # Attribute to top-level child directory if applicable
            try:
                rel = entry.relative_to(target)
                top_dir = rel.parts[0] if len(rel.parts) > 1 else None
            except ValueError:
                top_dir = None

            if top_dir and top_dir in dir_stats:
                dir_stats[top_dir]["files"] += 1
                dir_stats[top_dir]["loc"] += loc

    walk(target)

    by_directory = sorted(
        [{"path": name, **stats} for name, stats in dir_stats.items()],
        key=lambda x: x["loc"],
        reverse=True,
    )

    return {
        "target": str(target_resolved),
        "total_files": total_files,
        "total_loc": total_loc,
        "by_language": lang_stats,
        "by_directory": by_directory,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report file count and LOC per language for a directory."
    )
    parser.add_argument("directory", help="Target directory to analyze")
    args = parser.parse_args()

    target = Path(args.directory)
    if not target.is_dir():
        print(f"Error: '{args.directory}' is not a directory.", file=sys.stderr)
        sys.exit(2)

    try:
        result = collect_stats(target)
    except OSError as exc:
        print(f"IO error: {exc}", file=sys.stderr)
        sys.exit(1)

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False, sort_keys=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
