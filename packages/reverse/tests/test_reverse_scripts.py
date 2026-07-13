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
