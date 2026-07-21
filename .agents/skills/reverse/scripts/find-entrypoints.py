#!/usr/bin/env python3
"""find-entrypoints.py — discover programming language entrypoints from config files."""

import argparse
import configparser
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

# --- constants ---
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

Entry = dict[str, Any]


# --- helpers ---


def _make_entry(
    language: str,
    config_file: str,
    config_path: str,
    entry: str | None,
    kind: str,
    name: str | None,
) -> Entry:
    return {
        "language": language,
        "config_file": config_file,
        "config_path": config_path,
        "entry": entry,
        "kind": kind,
        "name": name,
    }


def _warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


# --- handlers ---


def handle_pyproject_toml(path: Path, rel: str) -> list[Entry]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    entries: list[Entry] = []
    for section in ("scripts", "gui-scripts"):
        scripts = data.get("project", {}).get(section, {})
        for name, entry in scripts.items():
            entries.append(_make_entry("Python", "pyproject.toml", rel, entry, "script", name))
    return entries


def handle_setup_py(path: Path, rel: str) -> list[Entry]:
    name = path.parent.name
    return [_make_entry("Python", "setup.py", rel, None, "main_file", name)]


def handle_setup_cfg(path: Path, rel: str) -> list[Entry]:
    cfg = configparser.ConfigParser()
    try:
        cfg.read(path, encoding="utf-8")
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    entries: list[Entry] = []
    raw = cfg.get("options.entry_points", "console_scripts", fallback="")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\S+)\s*=\s*(\S+)$", line)
        if m:
            entries.append(
                _make_entry("Python", "setup.cfg", rel, m.group(2), "script", m.group(1))
            )
    return entries


def handle_package_json(path: Path, rel: str) -> list[Entry]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    entries: list[Entry] = []
    pkg_name = data.get("name") or path.parent.name

    bin_val = data.get("bin")
    if isinstance(bin_val, str):
        entries.append(_make_entry("JavaScript", "package.json", rel, bin_val, "binary", pkg_name))
    elif isinstance(bin_val, dict):
        for bin_name, bin_path in bin_val.items():
            entries.append(
                _make_entry("JavaScript", "package.json", rel, bin_path, "binary", bin_name)
            )

    main_val = data.get("main")
    if main_val:
        entries.append(
            _make_entry("JavaScript", "package.json", rel, main_val, "main_file", pkg_name)
        )
    elif data.get("module"):
        entries.append(
            _make_entry("JavaScript", "package.json", rel, data["module"], "module", pkg_name)
        )

    return entries


def handle_cargo_toml(path: Path, rel: str) -> list[Entry]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    entries: list[Entry] = []
    pkg_name = data.get("package", {}).get("name")
    if pkg_name:
        entries.append(_make_entry("Rust", "Cargo.toml", rel, pkg_name, "binary", pkg_name))

    for bin_item in data.get("bin", []):
        bin_name = bin_item.get("name")
        if bin_name:
            entries.append(_make_entry("Rust", "Cargo.toml", rel, bin_name, "binary", bin_name))

    return entries


def handle_go_mod(path: Path, rel: str) -> list[Entry]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    entries: list[Entry] = []
    m = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
    if m:
        entries.append(_make_entry("Go", "go.mod", rel, m.group(1), "module", m.group(1)))

    main_go = path.parent / "main.go"
    if main_go.exists():
        entries.append(
            _make_entry(
                "Go",
                "main.go",
                str(main_go.parent / "main.go"),
                None,
                "main_file",
                path.parent.name,
            )
        )

    return entries


def handle_gemfile(path: Path, rel: str) -> list[Entry]:
    name = path.parent.name
    return [_make_entry("Ruby", "Gemfile", rel, None, "main_file", name)]


def handle_gemspec(path: Path, rel: str) -> list[Entry]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    m = re.search(r'\.name\s*=\s*["\']([^"\']+)["\']', text)
    name = m.group(1) if m else path.parent.name
    return [_make_entry("Ruby", path.name, rel, None, "binary", name)]


def handle_composer_json(path: Path, rel: str) -> list[Entry]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    entries: list[Entry] = []
    bin_list = data.get("bin", [])
    if isinstance(bin_list, list) and bin_list:
        for item in bin_list:
            entries.append(
                _make_entry("PHP", "composer.json", rel, item, "binary", Path(item).name)
            )
        return entries

    pkg_name = data.get("name")
    if pkg_name:
        entries.append(_make_entry("PHP", "composer.json", rel, pkg_name, "module", pkg_name))
    return entries


def handle_pom_xml(path: Path, rel: str) -> list[Entry]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    m = re.search(r"<artifactId>([^<]+)</artifactId>", text)
    if not m:
        return []
    artifact = m.group(1).strip()
    return [_make_entry("Java", "pom.xml", rel, artifact, "module", artifact)]


def handle_build_gradle(path: Path, rel: str) -> list[Entry]:
    name = path.parent.name
    return [_make_entry("Java", path.name, rel, None, "main_file", name)]


def handle_makefile(path: Path, rel: str) -> list[Entry]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        _warn(f"skipping {rel}: {e}")
        return []

    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*:", line)
        if m:
            target = m.group(1)
            return [_make_entry("Make", "Makefile", rel, None, "script", target)]
    return []


# Registry: config filename -> handler function
HANDLERS: dict[str, Any] = {
    "pyproject.toml": handle_pyproject_toml,
    "setup.py": handle_setup_py,
    "setup.cfg": handle_setup_cfg,
    "package.json": handle_package_json,
    "Cargo.toml": handle_cargo_toml,
    "go.mod": handle_go_mod,
    "Gemfile": handle_gemfile,
    "composer.json": handle_composer_json,
    "pom.xml": handle_pom_xml,
    "build.gradle": handle_build_gradle,
    "build.gradle.kts": handle_build_gradle,
    "Makefile": handle_makefile,
}


def _scan_directory(directory: Path, target: Path) -> list[tuple[Path, str]]:
    """Return (config_path, relative_path) pairs found in a single directory."""
    found: list[tuple[Path, str]] = []

    for filename, handler in HANDLERS.items():
        p = directory / filename
        if p.is_file():
            rel = str(p.relative_to(target))
            found.append((p, rel))

    # gemspec files (wildcard match)
    for p in directory.glob("*.gemspec"):
        rel = str(p.relative_to(target))
        found.append((p, rel))

    return found


def find_config_files(target: Path) -> list[tuple[Path, str]]:
    """Find config files in target root and one level deep."""
    results = _scan_directory(target, target)

    for child in sorted(target.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name in EXCLUDED_DIRS:
            continue
        results.extend(_scan_directory(child, target))

    return results


def _dispatch(path: Path, rel: str) -> list[Entry]:
    """Dispatch config file to the appropriate handler."""
    if path.name in HANDLERS:
        return HANDLERS[path.name](path, rel)
    if path.suffix == ".gemspec":
        return handle_gemspec(path, rel)
    return []


def collect_entries(target: Path) -> list[Entry]:
    """Collect all entries from discovered config files."""
    all_entries: list[Entry] = []
    seen: set[tuple[str, str, str | None]] = set()

    for path, rel in find_config_files(target):
        for entry in _dispatch(path, rel):
            key = (entry["language"], entry["config_path"], entry["entry"])
            if key in seen:
                continue
            seen.add(key)
            all_entries.append(entry)

    return sorted(all_entries, key=lambda e: (e["language"], e["config_path"]))


def build_output(target: Path, entries: list[Entry]) -> dict[str, Any]:
    by_language: dict[str, int] = {}
    for e in entries:
        lang = e["language"]
        by_language[lang] = by_language.get(lang, 0) + 1

    return {
        "target": str(target),
        "entrypoints": entries,
        "by_language": by_language,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover entrypoints from config files.")
    parser.add_argument("directory", help="Target directory to scan")
    args = parser.parse_args()

    target = Path(args.directory).resolve()
    if not target.is_dir():
        print(f"Error: {args.directory!r} is not a directory", file=sys.stderr)
        sys.exit(2)

    try:
        entries = collect_entries(target)
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    output = build_output(target, entries)
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
