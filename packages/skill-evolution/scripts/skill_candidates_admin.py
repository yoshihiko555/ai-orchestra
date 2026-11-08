#!/usr/bin/env python3
"""人が承認した改善候補の操作。Bash の許可リストには入れない。"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import candidates_model as model
import skill_evolution_common as common
from candidates_cli import Parser, execute
from candidates_ingest import CandidatesContext, ingest
from candidates_safety import MaskingUnavailable, ValidationError, load_masker, validate_text

STATE_COMMANDS = {
    "reject": "rejected",
    "unreject": "candidate",
    "archive": "archived",
    "restore": "candidate",
}
SOURCE_STATES = {"unreject": {"rejected"}, "restore": {"archived"}}


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="skill_candidates_admin.py",
        description="人が承認した改善候補の変更（許可リストに入れない）",
    )
    parser.add_argument("--project")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ingest").add_argument("--json", action="store_true", default=True)
    attribute = subparsers.add_parser("attribute")
    attribute.add_argument("oid")
    target = attribute.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id")
    target.add_argument("--skill")
    promote = subparsers.add_parser("promote-observation")
    promote.add_argument("oid")
    promote.add_argument("--json", dest="json_input", choices=["-"], required=True)
    add = subparsers.add_parser("add-observation")
    add.add_argument("cid")
    add.add_argument("--observation", required=True)
    merge = subparsers.add_parser("merge")
    merge.add_argument("cid")
    merge.add_argument("--into", required=True)
    dismiss = subparsers.add_parser("dismiss")
    dismiss.add_argument("oid")
    dismiss.add_argument("--json", dest="json_input", choices=["-"])
    for command in STATE_COMMANDS:
        state = subparsers.add_parser(command)
        state.add_argument("cid")
        state.add_argument("--json", dest="json_input", choices=["-"], required=command == "reject")
    return parser


def read_text_input(args: argparse.Namespace) -> None:
    payload = {}
    if getattr(args, "json_input", None) == "-":
        raw = sys.stdin.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise ValidationError("入力 JSON が上限を超えています")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValidationError("標準入力の JSON が正しくありません") from exc
        allowed = {"title", "description"} if args.command == "promote-observation" else {"note"}
        if not isinstance(payload, dict) or payload.keys() - allowed:
            raise ValidationError("入力 JSON の項目が正しくありません")
    if args.command == "promote-observation":
        args.title, args.description = payload.get("title"), payload.get("description")
    args.note = payload.get("note", "")


def _human_text(value: str, field: str, limit: int, *, multiline: bool = False) -> str:
    validate_text(value, field, limit, multiline=multiline)
    if not value:
        return value
    masker = load_masker()
    if masker is None:
        raise MaskingUnavailable("人が入力した文を伏せ字にできません")
    try:
        masked = masker(value)
    except Exception as exc:
        raise MaskingUnavailable("人が入力した文を伏せ字にできません") from exc
    return validate_text(masked, field, limit, multiline=multiline)


def attribution_event(context: CandidatesContext, snapshot: dict, args: argparse.Namespace) -> dict:
    observation = model.require_observation(snapshot, args.oid)
    if args.run_id:
        scope = context.history().require_human_scope(
            args.run_id, observation["sid"], observation["ts"]
        )
        skill, run_id = scope["skill"], scope["run_id"]
    else:
        skill, run_id = args.skill, None
        if not common.valid_name(skill):
            raise ValidationError("スキル名が正しくありません")
    return model.event(
        "observation.attributed",
        args.oid,
        {
            "attribution": "run" if run_id else "skill",
            "run_id": run_id,
            "skill": skill,
            "attributed_by": "human",
            "conditions": {},
        },
    )


def mutation_events(
    context: CandidatesContext, snapshot: dict, args: argparse.Namespace
) -> list[dict]:
    if args.command == "attribute":
        return [attribution_event(context, snapshot, args)]
    if args.command == "promote-observation":
        observation = model.require_observation(snapshot, args.oid)
        return [
            model.event(
                "candidate.created",
                "C-" + uuid.uuid4().hex[:8],
                {
                    "skill": observation["skill"],
                    "observations": [args.oid],
                    "hypothesis": {
                        "title": _human_text(args.title, "仮説の見出し", 200),
                        "description": _human_text(
                            args.description, "仮説の説明", 1000, multiline=True
                        ),
                    },
                    "handoff_eligible": bool(
                        observation["skill"] and context.eligible(observation["skill"])
                    ),
                },
            )
        ]
    if args.command == "add-observation":
        return [model.event("candidate.observation_added", args.cid, {"oid": args.observation})]
    if args.command == "merge":
        return [model.event("candidate.merged", args.cid, {"into": args.into})]
    note = _human_text(args.note, "note", 200)
    if args.command == "dismiss":
        return [model.event("observation.dismissed", args.oid, {"note": note})]
    candidate = model.require_candidate(snapshot, args.cid)
    if args.command in SOURCE_STATES and candidate["state"] not in SOURCE_STATES[args.command]:
        raise ValidationError("現在の状態にはこの操作を使えません")
    return [
        model.event(
            "candidate.state_changed",
            candidate["cid"],
            {
                "from": candidate["state"],
                "to": STATE_COMMANDS[args.command],
                "note": note,
            },
        )
    ]


def admin_command(context: CandidatesContext, args: argparse.Namespace) -> dict:
    if args.command == "ingest":
        return ingest(context)
    read_text_input(args)
    records = []

    def prepare(snapshot: dict) -> list[dict]:
        records.extend(mutation_events(context, snapshot, args))
        return records

    context.store.write(prepare)
    return {"events": records}


def main(argv: list[str] | None = None) -> int:
    return execute(build_parser(), admin_command, argv)


if __name__ == "__main__":
    sys.exit(main())
