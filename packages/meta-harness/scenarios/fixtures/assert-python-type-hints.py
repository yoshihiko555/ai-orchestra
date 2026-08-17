#!/usr/bin/env python3
"""Assert every function defined in a candidate-authored Python file has complete type hints.

Checks ``coding-principles.md``'s "型ヒント必須" rule via AST inspection: every function
parameter (except ``self``/``cls`` on an actual class method) and every function's return type
must carry an explicit annotation.

``--function`` (one or more, required) additionally pins which function name(s) the scenario
actually asked the candidate to implement (e.g. ``slugify``, ``validate_username``) and fails
unless the *final* module-level binding of that name (in source order) is a ``def``/``async def``
(see ``_final_module_def_bindings()``). This defends against two decoy submissions: (1) no
function at all, e.g. a candidate submitting ``slugify = lambda title: ...`` instead of a ``def``
-- with no functions to inspect, the per-function annotation loop would otherwise vacuously pass;
and (2) a ``def`` that exists but is not what actually runs -- e.g. a decoy class method/nested
def with the required name, or a ``def`` immediately shadowed by a later ``name = lambda ...``
reassignment -- either of which the behavior oracle would still execute unannotated while this
check looked only for *any* def with that name, anywhere, ever (PR #381 review).

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


def _iter_functions_with_context(
    tree: ast.AST,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool]]:
    """Return every function paired with whether it is a direct method of a class.

    ``self``/``cls`` are only conventionally-implicit parameters for a function whose *immediate*
    AST parent is a ``ClassDef`` (an actual class method). A free/module-level function -- the
    kind these scenarios require -- could otherwise name its one real parameter ``self`` purely to
    dodge the annotation requirement (PR #381 review, round 3): ``def slugify(self) -> str: ...``
    would then be exempt from needing an annotation on its only parameter, even though nothing
    binds it to an instance. Tracking the immediate parent (rather than "is this function anywhere
    inside a class body") also correctly denies the exemption to a function nested inside a method
    body, which is not itself a method.
    """
    results: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool]] = []

    def visit(node: ast.AST, parent: ast.AST | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            results.append((node, isinstance(parent, ast.ClassDef)))
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


def _final_module_def_bindings(tree: ast.Module) -> dict[str, bool]:
    """For each name bound at module level, whether the *last* (source-order) binding is a def.

    A candidate could satisfy an earlier, narrower "is there a module-level def with this name"
    check with ``def slugify(title: str) -> str: ...`` and then immediately shadow it with
    ``slugify = lambda title: ...`` (or a rebinding ``import``) -- the decoy def would never run,
    only the final unannotated binding would (PR #381 review, round 3). Only what a later
    statement in ``tree.body`` actually rebinds the name to matters, not whether a def with that
    name appeared *somewhere* earlier. This walks direct module-level statements only (not into
    ``if``/``try``/``with`` branches), which is sufficient for the decoy pattern under test: a
    straight-line sequence of top-level statements.
    """
    bindings: dict[str, bool] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings[node.name] = True
        elif isinstance(node, ast.ClassDef):
            bindings[node.name] = False
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _collect_name_targets(target):
                    bindings[name] = False
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for name in _collect_name_targets(node.target):
                bindings[name] = False
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = False
    return bindings


def check_type_hints(source: str, *, filename: str, required_functions: list[str]) -> list[str]:
    tree = ast.parse(source, filename=filename)
    functions_with_context = _iter_functions_with_context(tree)
    final_bindings = _final_module_def_bindings(tree)
    problems: list[str] = [
        f"required function '{name}' is not the final module-level binding of that name via a "
        "def (a class method, nested def, or a later reassignment/import/class overriding it "
        "does not count)"
        for name in required_functions
        if not final_bindings.get(name, False)
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
