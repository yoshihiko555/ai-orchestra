"""facets/scripts/reverse/*.py の CLI 入出力を検証する unit test。

docs/evaluation/reverse.md の評価観点のうち、自動テスト化可能な範囲のみを対象とする。

対応 EV:
- EV-05: collect-stats.py / find-entrypoints.py — CLI 引数 → stdout JSON
- EV-06: generate-mermaid.py — JSON → Mermaid 変換
- EV-09: collect-todos.py — CLI 引数 → stdout JSON、exit code 0/1/2

EV-13（antigravity.model 解決・allowlist 判定）は reverse-coordinator.md の自然言語指示に
埋め込まれており、スクリプトに独立関数化されていないためテスト対象外。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from tests.module_loader import REPO_ROOT

SCRIPTS_DIR = REPO_ROOT / "facets" / "scripts" / "reverse"
COLLECT_STATS = SCRIPTS_DIR / "collect-stats.py"
FIND_ENTRYPOINTS = SCRIPTS_DIR / "find-entrypoints.py"
GENERATE_MERMAID = SCRIPTS_DIR / "generate-mermaid.py"
COLLECT_TODOS = SCRIPTS_DIR / "collect-todos.py"


def _run(
    script: Path, args: list[str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        input=stdin,
    )


# ---------------------------------------------------------------------------
# EV-05: collect-stats.py
# ---------------------------------------------------------------------------


def test_collect_stats_reports_language_stats(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import os\nprint(os)\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("const x = 1;\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("x = 1\n", encoding="utf-8")

    proc = _run(COLLECT_STATS, [str(tmp_path)])

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["total_files"] == 3
    assert data["by_language"]["Python"]["files"] == 2
    assert data["by_language"]["TypeScript"]["files"] == 1
    assert any(d["path"] == "sub" for d in data["by_directory"])


def test_collect_stats_nonexistent_directory_exits_2(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    proc = _run(COLLECT_STATS, [str(missing)])

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "not a directory" in proc.stderr


def test_find_entrypoints_detects_pyproject_scripts(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project.scripts]\nfoo = "pkg.mod:main"\n', encoding="utf-8"
    )

    proc = _run(FIND_ENTRYPOINTS, [str(tmp_path)])

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["by_language"]["Python"] == 1
    assert data["entrypoints"][0]["name"] == "foo"
    assert data["entrypoints"][0]["entry"] == "pkg.mod:main"


def test_find_entrypoints_nonexistent_directory_exits_2(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    proc = _run(FIND_ENTRYPOINTS, [str(missing)])

    assert proc.returncode == 2
    assert "is not a directory" in proc.stderr


# ---------------------------------------------------------------------------
# EV-06: generate-mermaid.py
# ---------------------------------------------------------------------------


def test_generate_mermaid_converts_json_graph(tmp_path: Path) -> None:
    imports = {
        "nodes": [{"id": "a.py", "label": "a"}, {"id": "b.py", "label": "b"}],
        "edges": [{"from": "a.py", "to": "b.py"}],
    }
    input_file = tmp_path / "imports.json"
    input_file.write_text(json.dumps(imports), encoding="utf-8")

    proc = _run(GENERATE_MERMAID, [str(input_file)])

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("graph TD\n")
    assert 'N0["a"]' in proc.stdout
    assert 'N1["b"]' in proc.stdout
    assert "N0 --> N1" in proc.stdout


def test_generate_mermaid_reads_stdin_and_respects_direction() -> None:
    imports = {"nodes": [{"id": "a.py"}], "edges": []}

    proc = _run(GENERATE_MERMAID, ["-", "--direction", "LR"], stdin=json.dumps(imports))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("graph LR\n")


def test_generate_mermaid_invalid_json_exits_1(tmp_path: Path) -> None:
    input_file = tmp_path / "broken.json"
    input_file.write_text("{not valid json", encoding="utf-8")

    proc = _run(GENERATE_MERMAID, [str(input_file)])

    assert proc.returncode == 1
    assert proc.stdout == ""


# ---------------------------------------------------------------------------
# EV-06: generate-mermaid.py — escape_label / sanitize_cluster_name (security)
#
# These sanitizers are the only guard against Mermaid syntax injection when
# node labels / module names originate from untrusted source-file content
# (e.g. a docstring containing quotes, or a module path containing control
# characters). Each test feeds a syntax-breaking payload and asserts the
# *sanitized* output structure, not merely that the process exits 0.
# ---------------------------------------------------------------------------


def test_generate_mermaid_escape_label_neutralizes_unescaped_quotes() -> None:
    """A label containing '"' must not be able to close the `N0["..."]`
    string early and inject a fabricated edge/node into the graph."""
    dangerous_label = 'x" ] --> N9["evil'
    imports = {"nodes": [{"id": "a.py", "label": dangerous_label}], "edges": []}

    proc = _run(GENERATE_MERMAID, ["-"], stdin=json.dumps(imports))

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.rstrip("\n").splitlines()
    # No injected extra syntax lines: header + exactly one node line.
    assert lines[0] == "graph TD"
    assert len(lines) == 2
    node_line = lines[1]
    assert re.match(r'^  N0\[".*"\]$', node_line), node_line
    # Only the two delimiting quotes of N0["..."] may remain unescaped;
    # every quote coming from the payload must be backslash-escaped.
    unescaped_quotes = re.findall(r'(?<!\\)"', node_line)
    assert len(unescaped_quotes) == 2, node_line
    assert dangerous_label not in node_line


def test_generate_mermaid_escape_label_doubles_backslashes() -> None:
    """A label containing '\\' must be doubled so that, combined with quote
    escaping, the resulting `\\"` sequence cannot be misread as an escaped
    quote (which would re-open the string to injection)."""
    dangerous_label = "C:\\evil\\path"
    imports = {"nodes": [{"id": "a.py", "label": dangerous_label}], "edges": []}

    proc = _run(GENERATE_MERMAID, ["-"], stdin=json.dumps(imports))

    assert proc.returncode == 0, proc.stderr
    node_line = proc.stdout.rstrip("\n").splitlines()[1]
    assert "C:\\\\evil\\\\path" in node_line
    assert dangerous_label not in node_line


def test_generate_mermaid_escape_label_neutralizes_newlines_and_control_chars() -> None:
    """A label containing a raw newline or control character must not be able
    to split the single-line `N0["..."]` node definition across multiple
    physical lines or inject a raw control byte into the output.

    `escape_label` strips characters in `\\x00`-`\\x1f`/`\\x7f` (matching
    `sanitize_cluster_name`'s control-char handling) so the node statement
    stays on a single line. See PR #214 / Issue #135 review thread."""
    dangerous_label = "evil\ninjected line\x01ctrl"
    imports = {"nodes": [{"id": "a.py", "label": dangerous_label}], "edges": []}

    proc = _run(GENERATE_MERMAID, ["-"], stdin=json.dumps(imports))

    assert proc.returncode == 0, proc.stderr
    # Fixed: the newline/control char are stripped, so the node statement
    # stays on exactly one output line.
    lines = proc.stdout.rstrip("\n").splitlines()
    assert lines[0] == "graph TD"
    assert len(lines) == 2, lines
    assert lines[1] == '  N0["evilinjected linectrl"]'
    # The control character does not survive into the output.
    assert "\x01" not in proc.stdout
    assert "\n" not in lines[1]


def test_generate_mermaid_sanitize_cluster_name_strips_injection_chars() -> None:
    """A 'module' field must not be able to break out of
    `subgraph "<name>"` via quotes/newlines and inject extra
    subgraph/end directives."""
    dangerous_module = 'evil"\nend\nsubgraph "x'
    imports = {
        "nodes": [{"id": "a.py", "label": "a", "module": dangerous_module}],
        "edges": [],
    }

    proc = _run(GENERATE_MERMAID, ["-", "--cluster"], stdin=json.dumps(imports))

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.rstrip("\n").splitlines()
    subgraph_lines = [line for line in lines if line.strip().startswith("subgraph ")]
    assert len(subgraph_lines) == 1, lines
    match = re.match(r'^\s*subgraph "([^"]*)"$', subgraph_lines[0])
    assert match, subgraph_lines[0]
    sanitized_name = match.group(1)
    assert re.fullmatch(r"[A-Za-z0-9_\-./ ]+", sanitized_name), sanitized_name
    assert '"' not in sanitized_name
    # No injected extra 'end' line beyond the single legitimate cluster close.
    assert lines.count("  end") == 1


def test_generate_mermaid_sanitize_cluster_name_falls_back_when_only_control_chars() -> None:
    """A module name that is non-empty but sanitizes to an empty string
    (only control characters) must fall back to 'uncategorized' rather than
    emitting a malformed `subgraph ""` block."""
    imports = {
        "nodes": [{"id": "a.py", "label": "a", "module": "\x00"}],
        "edges": [],
    }

    proc = _run(GENERATE_MERMAID, ["-", "--cluster"], stdin=json.dumps(imports))

    assert proc.returncode == 0, proc.stderr
    assert 'subgraph "uncategorized"' in proc.stdout
    assert 'subgraph ""' not in proc.stdout


# ---------------------------------------------------------------------------
# EV-09: collect-todos.py
# ---------------------------------------------------------------------------


def test_collect_todos_finds_tagged_comments(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "# TODO: fix this\nx = 1\n# FIXME(bob): urgent\n", encoding="utf-8"
    )

    proc = _run(COLLECT_TODOS, [str(tmp_path)])

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["total"] == 2
    assert data["count_by_tag"] == {"TODO": 1, "FIXME": 1}
    tags = {item["tag"] for item in data["items"]}
    assert tags == {"TODO", "FIXME"}


def test_collect_todos_nonexistent_directory_exits_1(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    proc = _run(COLLECT_TODOS, [str(missing)])

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "not found" in proc.stderr


def test_collect_todos_missing_argument_exits_2() -> None:
    proc = _run(COLLECT_TODOS, [])

    assert proc.returncode == 2
    assert proc.stdout == ""
