#!/usr/bin/env python3
"""Assert ``coding-principles.md`` conventions on a candidate-authored Python file via AST.

Each check is opt-in via a CLI flag so a single fixture can back differently-focused graded
items without duplicating the AST plumbing:

- ``--require-docstring``: every function has a non-empty docstring.
- ``--require-snake-case``: function names and assigned variable/parameter names are
  ``snake_case`` and not a single meaningless character. This applies to ``for``/``async for``
  loop targets too: ``coding-principles.md`` requires meaningful names (e.g. ``user_count`` over
  ``x``) and defines no loop-variable carve-out, so a single-character loop target is flagged like
  any other single-character variable (PR #381 review). Variadic parameters (``*args``/
  ``**kwargs``) are checked too (PR #381 review, round 4): the parameter scan previously only
  walked ``posonlyargs``/``args``/``kwonlyargs``, so a candidate could name a catch-all positional
  or keyword parameter something like ``*X`` and never trip the snake_case check even though
  ``coding-principles.md``'s naming rule draws no such exception for variadic parameters.
  Module-top-level constant assignments
  (e.g. ``_WHITESPACE_RE = re.compile(...)``) may instead be ``UPPER_SNAKE_CASE``, per
  ``coding-principles.md``'s constant-naming rule; the same name reassigned inside a function
  body still must be ``snake_case`` (scope is determined per-assignment-node, not by name alone,
  so a module-level constant no longer shadows-and-skips a later function-local reassignment of
  the same name).
- ``--max-function-lines N``: no function body spans more than ``N`` source lines.
- ``--max-nesting-depth N``: no function nests control-flow blocks (``if``/``for``/``while``/
  ``try``/``with``/``match``) deeper than ``N`` levels (rewarding an early-return style over deep
  nesting). ``match``/``case`` (PR #381 review, round 3) counts the same as any other
  control-flow block -- a candidate could otherwise dodge the nesting-depth check entirely by
  writing an equivalently deep chain of nested ``match`` statements instead of ``if``/``elif``.
  An ``elif`` chain counts as a single level, not one level per ``elif``: ``ast`` represents
  ``elif`` as a nested ``If`` inside the parent ``If``'s ``orelse``, indistinguishable in node
  type from an ``if`` written inside an explicit ``else:`` block, so this is detected via column
  offset (a true ``elif`` keeps the same indentation column as the ``if``/``elif`` it continues;
  see ``_is_elif_chain_link()``) rather than penalizing a shallow, functionally correct
  ``if/elif/elif/...`` implementation as if it were sequentially nested (PR #381 review).

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
_NESTING_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.Match,
)


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


def _collect_name_targets(target: ast.expr) -> list[ast.Name]:
    """Recursively collect ``ast.Name`` store targets from a (possibly tuple/list/starred)
    assignment target, e.g. ``A, B = ...`` or ``A, (B, C) = ...``."""
    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[ast.Name] = []
        for elt in target.elts:
            names.extend(_collect_name_targets(elt))
        return names
    if isinstance(target, ast.Starred):
        return _collect_name_targets(target.value)
    return []


def _collect_module_level_constant_ids(tree: ast.AST) -> set[int]:
    """``id()`` of each ``ast.Name`` store target assigned directly at module top-level.

    ``coding-principles.md`` requires module-level constants to be ``UPPER_SNAKE_CASE``, which
    conflicts with the general ``snake_case`` variable-naming check below. Only names assigned in
    ``tree.body`` itself (the module's direct statement list) count as module-level constants; a
    same-cased name reassigned inside a function body is a regular local variable and must still
    follow ``snake_case``.

    Tuple/list/starred assignment targets are unpacked recursively (PR #381 review, round 3):
    ``WHITESPACE_RE, HYPHEN_RE = re.compile(...), re.compile(...)`` is a legitimate way to define
    two module constants in one statement, and previously only a bare ``ast.Name`` target was
    recognized, so both names in a tuple-unpack were misreported as non-constant ``snake_case``
    violations.

    Returns node identities (``id()``) rather than name strings: a set of names would let a
    module-level ``VALUE = 1`` permanently mark the bare string ``"VALUE"`` as an already-checked
    constant, silently skipping a later, unrelated function-local ``VALUE = 2`` that should still
    be flagged for using UPPER_SNAKE_CASE outside module scope (PR #381 review). Each store
    occurrence is judged by its own AST identity, not by name alone.
    """
    if not isinstance(tree, ast.Module):
        return set()
    ids: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                ids.update(id(name) for name in _collect_name_targets(target))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ids.add(id(node.target))
    return ids


def _check_snake_case(tree: ast.AST) -> list[str]:
    problems: list[str] = []
    module_constant_ids = _collect_module_level_constant_ids(tree)
    for fn in _iter_functions(tree):
        if not _SNAKE_CASE.match(fn.name):
            problems.append(f"function name '{fn.name}' is not snake_case")
        args = fn.args
        variadic = [a for a in (args.vararg, args.kwarg) if a is not None]
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, *variadic]:
            if arg.arg in ("self", "cls"):
                continue
            if not _SNAKE_CASE.match(arg.arg) or len(arg.arg.lstrip("_")) <= 1:
                problems.append(
                    f"{fn.name}: parameter '{arg.arg}' is not a meaningful snake_case name"
                )

    # Dedupe by (name, is_module_constant) rather than by name alone: a module-level constant
    # and a same-named function-local variable are different scopes and must be judged
    # independently (PR #381 review; see `_collect_module_level_constant_ids()` docstring).
    seen: set[tuple[str, bool]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)):
            continue
        name = node.id
        if name == "_":
            continue
        is_module_constant = id(node) in module_constant_ids and bool(_UPPER_SNAKE_CASE.match(name))
        key = (name, is_module_constant)
        if key in seen:
            continue
        seen.add(key)
        if is_module_constant:
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


def _is_elif_chain_link(parent: ast.AST, child: ast.AST) -> bool:
    """True if ``child`` is the sole ``elif`` continuation of an ``if``/``elif`` ``parent``.

    ``ast`` represents ``elif`` as a nested ``If`` inside the parent ``If``'s ``orelse`` --
    indistinguishable in node *type* from an ``if`` written inside an explicit ``else:`` block
    (e.g. ``else:\\n    if b: ...``), which genuinely is one nesting level deeper. The two are
    told apart by column offset: a true ``elif`` keeps the same indentation column as the
    ``if``/``elif`` it continues, while an ``if`` nested inside ``else:`` is indented one level
    deeper (PR #381 review: an ``if``/``elif``/``elif``/... chain must count as a single level,
    not accrue one extra level per ``elif``).
    """
    if not (isinstance(parent, ast.If) and isinstance(child, ast.If)):
        return False
    if parent.orelse != [child]:
        return False
    return child.col_offset == parent.col_offset


def _max_nesting_depth(node: ast.AST, depth: int) -> int:
    deepest = depth
    for child in ast.iter_child_nodes(node):
        if _is_elif_chain_link(node, child):
            next_depth = depth
        else:
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
