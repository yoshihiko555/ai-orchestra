#!/usr/bin/env python3
"""Assert every function defined in a candidate-authored Python file has complete type hints.

Checks ``coding-principles.md``'s "型ヒント必須" rule via AST inspection: every function
parameter (except ``self``/``cls``) and every function's return type must carry an explicit
annotation.

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


def _iter_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def check_type_hints(source: str, *, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    problems: list[str] = []
    for fn in _iter_functions(tree):
        args = fn.args
        positional = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        variadic = [a for a in (args.vararg, args.kwarg) if a is not None]
        for arg in [*positional, *variadic]:
            if arg.arg in ("self", "cls"):
                continue
            if arg.annotation is None:
                problems.append(f"{fn.name}: parameter '{arg.arg}' is missing a type annotation")
        if fn.returns is None:
            problems.append(f"{fn.name}: missing a return type annotation")
    return problems


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)

    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    target = project_root / args.file
    assert target.is_file() and not target.is_symlink(), f"missing regular file: {args.file}"

    problems = check_type_hints(target.read_text(encoding="utf-8"), filename=str(args.file))
    assert not problems, "missing type hints:\n" + "\n".join(problems)


if __name__ == "__main__":
    main()
