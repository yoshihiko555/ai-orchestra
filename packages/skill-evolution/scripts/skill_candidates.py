#!/usr/bin/env python3
"""改善候補の読み取り CLI（Bash の許可リストに入れてよい）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from candidates_cli import Parser, execute
from candidates_ingest import CandidatesContext
from candidates_safety import timestamp
from candidates_view import listing, show


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="skill_candidates.py", description="スキル改善候補の一覧・詳細・実行範囲")
    parser.add_argument("--project")
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "show", "runs"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if command == "show":
            command_parser.add_argument("id")
        if command == "runs":
            command_parser.add_argument("--sid")
            command_parser.add_argument("--limit", type=int, default=20)
    return parser


def read_command(context: CandidatesContext, args: argparse.Namespace) -> dict:
    if args.command == "runs":
        history = context.history()
        scopes = history.for_session(args.sid) if args.sid else history.scopes
        return {
            "runs": sorted(scopes, key=lambda scope: timestamp(scope["ts"]), reverse=True)[
                : max(0, args.limit)
            ]
        }
    snapshot = context.store.read()
    return (
        listing(context, snapshot) if args.command == "list" else show(context, snapshot, args.id)
    )


def main(argv: list[str] | None = None) -> int:
    return execute(build_parser(), read_command, argv)


if __name__ == "__main__":
    sys.exit(main())
