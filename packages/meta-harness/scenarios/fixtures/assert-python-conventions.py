#!/usr/bin/env python3
"""Assert ``coding-principles.md`` conventions on a candidate-authored Python file via AST.

Each check is opt-in via a CLI flag so a single fixture can back differently-focused graded
items without duplicating the AST plumbing:

- ``--require-docstring``: every function has a non-empty docstring.
- ``--require-snake-case``: function names and assigned variable/parameter names are
  ``snake_case`` and not a single meaningless character (``for``/``async for`` loop targets are
  exempt, matching the "ループ変数除く" carve-out). Module-top-level constant assignments (e.g.
  ``_WHITESPACE_RE = re.compile(...)``) may instead be ``UPPER_SNAKE_CASE``, per
  ``coding-principles.md``'s constant-naming rule; the same name reassigned inside a function
  body still must be ``snake_case``.
- ``--max-function-lines N``: no function body spans more than ``N`` source lines.
- ``--max-nesting-depth N``: no function nests control-flow blocks (``if``/``for``/``while``/
  ``try``/``with``) deeper than ``N`` levels (rewarding an early-return style over deep nesting).

Trust note: like ``assert-python-type-hints.py``, this fixture inspects the file the candidate
was asked to write. There is no separate scenario-provided expected value to tamper with, so
(unlike ``assert-effective-config.py``) no sha256-pinned expectation table is needed here.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
from pathlib import Path

_SNAKE_CASE = re.compile(r"^_?[a-z][a-z0-9_]*$")
_UPPER_SNAKE_CASE = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
_NESTING_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)


def _iter_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _check_docstrings(tree: ast.AST) -> list[str]:
    return [
        f"{fn.name}: missing a docstring"
        for fn in _iter_functions(tree)
        if not (ast.get_docstring(fn) or "").strip()
    ]


def _collect_loop_targets(tree: ast.AST) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            targets.update(name.id for name in ast.walk(node.target) if isinstance(name, ast.Name))
    return targets


def _collect_module_level_assigned_names(tree: ast.AST) -> set[str]:
    """Names assigned directly at module top-level (not inside a function/class/branch).

    ``coding-principles.md`` requires module-level constants to be ``UPPER_SNAKE_CASE``, which
    conflicts with the general ``snake_case`` variable-naming check below. Only names assigned in
    ``tree.body`` itself (the module's direct statement list) count as module-level constants; a
    same-cased name reassigned inside a function body is a regular local variable and must still
    follow ``snake_case``.
    """
    if not isinstance(tree, ast.Module):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _check_snake_case(tree: ast.AST) -> list[str]:
    problems: list[str] = []
    loop_targets = _collect_loop_targets(tree)
    module_constants = _collect_module_level_assigned_names(tree)
    for fn in _iter_functions(tree):
        if not _SNAKE_CASE.match(fn.name):
            problems.append(f"function name '{fn.name}' is not snake_case")
        args = fn.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if arg.arg in ("self", "cls"):
                continue
            if not _SNAKE_CASE.match(arg.arg) or len(arg.arg.lstrip("_")) <= 1:
                problems.append(
                    f"{fn.name}: parameter '{arg.arg}' is not a meaningful snake_case name"
                )

    seen: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)):
            continue
        name = node.id
        if name in seen or name == "_" or name in loop_targets:
            continue
        seen.add(name)
        if name in module_constants and _UPPER_SNAKE_CASE.match(name):
            continue
        if not _SNAKE_CASE.match(name):
            problems.append(f"variable name '{name}' is not snake_case")
        elif len(name.lstrip("_")) <= 1:
            problems.append(f"variable name '{name}' is a single character (not meaningful)")
    return problems


def _function_line_count(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    end = fn.end_lineno or fn.lineno
    return end - fn.lineno + 1


def _check_max_function_lines(tree: ast.AST, max_lines: int) -> list[str]:
    problems = []
    for fn in _iter_functions(tree):
        length = _function_line_count(fn)
        if length > max_lines:
            problems.append(f"{fn.name}: {length} lines exceeds the {max_lines}-line limit")
    return problems


def _max_nesting_depth(node: ast.AST, depth: int) -> int:
    deepest = depth
    for child in ast.iter_child_nodes(node):
        next_depth = depth + 1 if isinstance(child, _NESTING_NODES) else depth
        deepest = max(deepest, _max_nesting_depth(child, next_depth))
    return deepest


def _check_max_nesting_depth(tree: ast.AST, max_depth: int) -> list[str]:
    problems = []
    for fn in _iter_functions(tree):
        depth = _max_nesting_depth(fn, 0)
        if depth > max_depth:
            problems.append(f"{fn.name}: nesting depth {depth} exceeds the {max_depth}-level limit")
    return problems


def check_conventions(
    source: str,
    *,
    filename: str,
    require_docstring: bool,
    require_snake_case: bool,
    max_function_lines: int | None,
    max_nesting_depth: int | None,
) -> list[str]:
    tree = ast.parse(source, filename=filename)
    problems: list[str] = []
    if require_docstring:
        problems.extend(_check_docstrings(tree))
    if require_snake_case:
        problems.extend(_check_snake_case(tree))
    if max_function_lines is not None:
        problems.extend(_check_max_function_lines(tree, max_function_lines))
    if max_nesting_depth is not None:
        problems.extend(_check_max_nesting_depth(tree, max_nesting_depth))
    return problems


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--require-docstring", action="store_true")
    parser.add_argument("--require-snake-case", action="store_true")
    parser.add_argument("--max-function-lines", type=int, default=None)
    parser.add_argument("--max-nesting-depth", type=int, default=None)
    args = parser.parse_args(argv)

    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    target = project_root / args.file
    assert target.is_file() and not target.is_symlink(), f"missing regular file: {args.file}"

    problems = check_conventions(
        target.read_text(encoding="utf-8"),
        filename=str(args.file),
        require_docstring=args.require_docstring,
        require_snake_case=args.require_snake_case,
        max_function_lines=args.max_function_lines,
        max_nesting_depth=args.max_nesting_depth,
    )
    assert not problems, "coding-principles convention violations:\n" + "\n".join(problems)


if __name__ == "__main__":
    main()
