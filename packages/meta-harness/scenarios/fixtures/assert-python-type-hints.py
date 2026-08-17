#!/usr/bin/env python3
"""Assert every function defined in a candidate-authored Python file has complete type hints.

Checks ``coding-principles.md``'s "型ヒント必須" rule via AST inspection: every function
parameter (except ``self``/``cls`` on an actual class method) and every function's return type
must carry an explicit annotation.

``--function`` (one or more, required) additionally pins which function name(s) the scenario
actually asked the candidate to implement (e.g. ``slugify``, ``validate_username``) and fails
unless a module-level ``def``/``async def`` is the *sole* binding of that name anywhere in the
file (see ``_has_single_module_level_def_binding()``). This defends against decoy submissions:
(1) no function at all, e.g. a candidate submitting ``slugify = lambda title: ...`` instead of a
``def`` -- with no functions to inspect, the per-function annotation loop would otherwise
vacuously pass; (2) a ``def`` that exists but is not what actually runs -- e.g. a decoy class
method/nested def with the required name, or a ``def`` immediately shadowed by a later
``name = lambda ...`` reassignment at module top level; and (3) the same shadowing reassignment
hidden inside a conditional branch, e.g. ``if True: slugify = lambda title: ...`` (PR #381
review, round 4) -- earlier revisions only scanned direct ``tree.body`` statements, so a
rebinding one ``if``/``try``/``with`` block deep was invisible to the scan even though nothing
in Python's execution model treats that block as a separate scope, so it still rebinds the
module-level name. Rather than extending the scan to trace which branch of which control-flow
construct actually executes at runtime -- an open-ended game where every new decoy shape demands
one more construct to trace -- ``_has_single_module_level_def_binding()`` conservatively walks
the *entire* module AST (``ast.walk``) and rejects the submission if the name is bound anywhere
by anything other than a qualifying module-level ``def``. A legitimate implementation has no
reason to rebind its own required function name anywhere else in the file, so this is
effectively false-positive-free while ending the whack-a-mole for good.

Trust note (contrast with ``assert-issue-fix-decision.py`` / ``assert-effective-config.py``):
this fixture inspects the very file the scenario asked the candidate to write, not a separate
scenario-provided *expected value* the candidate could rewrite to trivially satisfy a looser
check. The AST parsed from that file *is* the artifact under test, so there is no sha256-pinned
expectation table here -- the "tamper" surface those other fixtures defend against does not
apply to a structural check of the candidate's own submission.
"""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path


def _is_staticmethod(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if ``fn`` carries a ``@staticmethod`` decorator (bare or via a dotted attribute)."""
    for decorator in fn.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == "staticmethod":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "staticmethod":
            return True
    return False


def _iter_functions_with_context(
    tree: ast.AST,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool]]:
    """Return every function paired with whether it implicitly receives ``self``/``cls``.

    ``self``/``cls`` are only conventionally-implicit parameters for a function whose *immediate*
    AST parent is a ``ClassDef`` (an actual class method) *and* which is not decorated with
    ``@staticmethod``. A free/module-level function -- the kind these scenarios require -- could
    otherwise name its one real parameter ``self`` purely to dodge the annotation requirement
    (PR #381 review, round 3): ``def slugify(self) -> str: ...`` would then be exempt from needing
    an annotation on its only parameter, even though nothing binds it to an instance. Tracking the
    immediate parent (rather than "is this function anywhere inside a class body") also correctly
    denies the exemption to a function nested inside a method body, which is not itself a method.
    ``@staticmethod`` is excluded from the exemption separately (PR #381 review, round 4): unlike
    an ordinary instance method or ``@classmethod``, Python never implicitly injects ``self``/
    ``cls`` into a static method's call, so ``@staticmethod def transform(self) -> str: ...``
    called from the required function still leaves ``self`` genuinely unannotated at runtime; only
    the parent-is-``ClassDef`` check previously decided the exemption, so this decoy shape slipped
    through untyped.
    """
    results: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool]] = []

    def visit(node: ast.AST, parent: ast.AST | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            implicit_self_cls = isinstance(parent, ast.ClassDef) and not _is_staticmethod(node)
            results.append((node, implicit_self_cls))
        for child in ast.iter_child_nodes(node):
            visit(child, node)

    visit(tree, None)
    return results


def _collect_name_targets(target: ast.expr) -> list[str]:
    """Recursively collect ``ast.Name`` ids from a (possibly tuple/list/starred) assign target."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_collect_name_targets(elt))
        return names
    if isinstance(target, ast.Starred):
        return _collect_name_targets(target.value)
    return []


def _has_single_module_level_def_binding(tree: ast.Module, name: str) -> bool:
    """Whether ``name``'s *only* binding anywhere in the module is a module-level ``def``.

    See the module docstring's "``--function``" paragraph for why this replaced tracing which
    control-flow branch actually executes (PR #381 review, round 4): rather than a scoped scan
    of ``tree.body`` (or an extension of it into ``if``/``try``/``with`` branches, which would
    just invite the next decoy one level deeper), this walks the *entire* module AST via
    ``ast.walk`` and fails closed the moment it finds *any* other binding of ``name`` -- at any
    position, any nesting depth -- via: an ``Assign``/``AnnAssign``/``AugAssign`` target, an
    ``import``/``from ... import`` alias, a ``class`` statement, a ``for``/``async for`` loop
    target, a ``with`` target, a comprehension target, an ``except ... as`` name, a ``global``/
    ``nonlocal`` declaration, a walrus (``:=``) target, a ``match``/``case`` capture pattern
    (``case slugify:``/``case [*slugify]:``/``case {**slugify}:``), a ``type`` statement
    (``type slugify = ...``), or another ``def``/``async def`` (nested or otherwise) sharing the
    name. Only a bare module-level ``def``/``async def`` -- the actual top-level statement, not
    merely "a def somewhere" -- counts as the qualifying binding.
    """
    top_level_defs = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    if not top_level_defs:
        return False
    allowed_ids = {id(node) for node in top_level_defs}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name and id(node) not in allowed_ids:
                return False
        elif isinstance(node, ast.ClassDef) and node.name == name:
            return False
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if name in _collect_name_targets(target):
                    return False
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if name in _collect_name_targets(node.target):
                return False
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return False
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if name in _collect_name_targets(node.target):
                return False
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None and name in _collect_name_targets(node.optional_vars):
                return False
        elif isinstance(node, ast.comprehension):
            if name in _collect_name_targets(node.target):
                return False
        elif isinstance(node, ast.ExceptHandler):
            if node.name == name:
                return False
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            if name in node.names:
                return False
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return False
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name == name:
                return False
        elif isinstance(node, ast.MatchMapping):
            if node.rest == name:
                return False
        elif isinstance(node, ast.TypeAlias):
            if isinstance(node.name, ast.Name) and node.name.id == name:
                return False
    return True


def check_type_hints(source: str, *, filename: str, required_functions: list[str]) -> list[str]:
    tree = ast.parse(source, filename=filename)
    functions_with_context = _iter_functions_with_context(tree)
    problems: list[str] = [
        f"required function '{name}' is not the sole module-level def binding of that name "
        "(a class method, nested def, or any other rebinding anywhere in the file -- including "
        "inside an if/try/with branch -- disqualifies it)"
        for name in required_functions
        if not _has_single_module_level_def_binding(tree, name)
    ]
    for fn, is_method in functions_with_context:
        args = fn.args
        positional = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        variadic = [a for a in (args.vararg, args.kwarg) if a is not None]
        for arg in [*positional, *variadic]:
            if is_method and arg.arg in ("self", "cls"):
                continue
            if arg.annotation is None:
                problems.append(f"{fn.name}: parameter '{arg.arg}' is missing a type annotation")
        if fn.returns is None:
            problems.append(f"{fn.name}: missing a return type annotation")
    return problems


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument(
        "--function",
        action="append",
        required=True,
        help="required function name (repeatable); at least one must be defined via def",
    )
    args = parser.parse_args(argv)

    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    target = project_root / args.file
    assert target.is_file() and not target.is_symlink(), f"missing regular file: {args.file}"

    problems = check_type_hints(
        target.read_text(encoding="utf-8"),
        filename=str(args.file),
        required_functions=args.function,
    )
    assert not problems, "missing type hints:\n" + "\n".join(problems)


if __name__ == "__main__":
    main()
