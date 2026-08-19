"""Scenario fixture とコンテナ内 Python runtime の互換性テスト。"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "scenarios" / "fixtures"
TYPE_HINTS_FIXTURE = FIXTURES_DIR / "assert-python-type-hints.py"
CONVENTIONS_FIXTURE = FIXTURES_DIR / "assert-python-conventions.py"

VERSION_DEPENDENT_AST_ATTRIBUTES = {
    "Match",
    "MatchValue",
    "MatchSingleton",
    "MatchSequence",
    "MatchMapping",
    "MatchClass",
    "MatchStar",
    "MatchAs",
    "MatchOr",
    "TryStar",
    "TypeAlias",
    "TypeVar",
    "ParamSpec",
    "TypeVarTuple",
}


def test_type_hints_fixture_handles_missing_version_dependent_ast_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for attribute in ("TypeAlias", "MatchAs", "MatchStar", "MatchMapping"):
        monkeypatch.delattr(ast, attribute, raising=False)

    spec = importlib.util.spec_from_file_location(
        "assert_python_type_hints_missing_ast_nodes", TYPE_HINTS_FIXTURE
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    source = "def slugify(value: str) -> str:\n    return value\n"
    assert (
        fixture.check_type_hints(source, filename="sample.py", required_functions=["slugify"]) == []
    )


def test_type_hints_fixture_checks_bindings_on_real_interpreter() -> None:
    spec = importlib.util.spec_from_file_location(
        "assert_python_type_hints_real_ast_nodes", TYPE_HINTS_FIXTURE
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    source = "def slugify(value: str) -> str:\n    return value\n"
    assert (
        fixture.check_type_hints(source, filename="sample.py", required_functions=["slugify"]) == []
    )

    rebound_source = (
        "def slugify(value: str) -> str:\n"
        "    return value\n\n"
        "def capture(value: object) -> str:\n"
        "    match value:\n"
        "        case slugify:\n"
        "            return str(slugify)\n"
    )
    problems = fixture.check_type_hints(
        rebound_source, filename="rebound.py", required_functions=["slugify"]
    )
    assert any("not the sole module-level def binding" in problem for problem in problems)


def test_conventions_fixture_handles_missing_version_dependent_ast_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for attribute in ("TryStar", "Match"):
        monkeypatch.delattr(ast, attribute, raising=False)

    spec = importlib.util.spec_from_file_location(
        "assert_python_conventions_missing_ast_nodes", CONVENTIONS_FIXTURE
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    source = 'def slugify(value: str) -> str:\n    if value:\n        return value\n    return ""\n'
    assert (
        fixture.check_conventions(
            source,
            filename="sample.py",
            require_docstring=False,
            require_snake_case=False,
            max_function_lines=None,
            max_nesting_depth=1,
        )
        == []
    )


def test_conventions_fixture_checks_nesting_on_real_interpreter() -> None:
    spec = importlib.util.spec_from_file_location(
        "assert_python_conventions_real_ast_nodes", CONVENTIONS_FIXTURE
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    source = (
        "def slugify(value: str) -> str:\n"
        "    if value:\n"
        "        if value.strip():\n"
        "            return value\n"
        '    return ""\n'
    )
    assert (
        fixture.check_conventions(
            source,
            filename="sample.py",
            require_docstring=False,
            require_snake_case=False,
            max_function_lines=None,
            max_nesting_depth=2,
        )
        == []
    )
    assert fixture.check_conventions(
        source,
        filename="sample.py",
        require_docstring=False,
        require_snake_case=False,
        max_function_lines=None,
        max_nesting_depth=1,
    ) == ["slugify: nesting depth 2 exceeds the 1-level limit"]


def test_type_hints_fixture_runs_as_subprocess(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "def slugify(value: str) -> str:\n    return value\n", encoding="utf-8"
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(TYPE_HINTS_FIXTURE),
            "--file",
            "sample.py",
            "--function",
            "slugify",
        ],
        env={**os.environ, "AI_ORCHESTRA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_fixture_scripts_do_not_directly_access_version_dependent_ast_attributes() -> None:
    flagged_by_file: dict[str, list[str]] = {}
    for fixture_path in sorted(FIXTURES_DIR.glob("*.py")):
        tree = ast.parse(fixture_path.read_text(encoding="utf-8"), filename=str(fixture_path))
        flagged_attributes = sorted(
            {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ast"
                and node.attr in VERSION_DEPENDENT_AST_ATTRIBUTES
            }
        )
        if flagged_attributes:
            flagged_by_file[fixture_path.name] = flagged_attributes

    details = "; ".join(
        f"{filename}: {', '.join(attributes)}" for filename, attributes in flagged_by_file.items()
    )
    assert not flagged_by_file, f"direct version-dependent ast attributes found: {details}"
