#!/usr/bin/env python3
"""Deterministic offline ``gh`` fixture for issue-create scenarios."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _option(args: list[str], name: str) -> str:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return ""


def main() -> int:
    args = sys.argv[1:]
    if args[:2] == ["label", "list"]:
        print(
            json.dumps(
                [
                    {"name": "bug", "description": "Bug report"},
                    {"name": "feature", "description": "Feature request"},
                    {"name": "task", "description": "Task"},
                ]
            )
        )
        return 0
    if args[:2] == ["label", "create"]:
        return 0
    if args[:2] != ["issue", "create"]:
        print(f"unsupported fake gh invocation: {args}", file=sys.stderr)
        return 2

    body = _option(args, "--body")
    body_file = _option(args, "--body-file")
    if body_file:
        body = Path(body_file).read_text(encoding="utf-8")
    record = {
        "args": args,
        "title": _option(args, "--title"),
        "label": _option(args, "--label"),
        "body": body,
    }
    output = Path(".meta-harness/issue-create-call.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("https://github.example.invalid/example/repo/issues/42")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
