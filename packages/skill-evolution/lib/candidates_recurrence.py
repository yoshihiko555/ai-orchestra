"""マージ後の実行範囲の集計、再発の確定、月別記録の整理。"""

from __future__ import annotations

import os
import re
import stat
from datetime import UTC, datetime

import candidates_model as model
from candidates_handoff import require_state
from candidates_handoff_events import validate_recurrence_ownership
from candidates_ingest import CandidatesContext
from candidates_runs import RunHistory
from candidates_safety import ValidationError, timestamp, validate_directory
from candidates_scenario_events import latest_record
from candidates_scenarios import state_event


def post_merge_scope(candidate: dict, merged: dict, scope: dict) -> bool:
    return bool(
        merged["data"]["skill_hash"] is not None
        and scope["skill"] == candidate["skill"]
        and scope["skill_hash"] == merged["data"]["skill_hash"]
        and timestamp(scope["ts"]) > timestamp(merged["data"]["merged_at"])
    )


def merge_history(context: CandidatesContext, merged: dict) -> RunHistory:
    first = timestamp(merged["data"]["merged_at"]).strftime("%Y%m")
    months = {
        match[1]
        for path in context.store.directory.glob("runs-*.jsonl")
        if (match := re.fullmatch(r"runs-(\d{4}(?:0[1-9]|1[0-2]))\.jsonl", path.name))
        and match[1] >= first
    }
    return context.history(months)


def recurrence_status(context: CandidatesContext, snapshot: dict, candidate: dict) -> dict:
    merged = latest_record(snapshot, candidate["cid"], "handoff.merged")
    if not merged or merged["data"]["skill_hash"] is None:
        return {
            "opportunities": None,
            "message": "マージ後のスキルの版が不明のため、該当する機会を数えられません",
        }
    history = merge_history(context, merged)
    opportunities = [
        scope
        for scope in history.scopes
        if post_merge_scope(candidate, merged, scope) and history.confirmed(scope)
    ]
    sessions = {scope["sid"] for scope in opportunities}
    unknown = [
        observation
        for observation in snapshot["observations"].values()
        if observation["attribution"] == "unknown"
        and not observation["dismissed"]
        and observation["sid"] in sessions
        and timestamp(observation["ts"]) > timestamp(merged["data"]["merged_at"])
    ]
    return {
        "opportunities": len(opportunities),
        "unknown_evidence": len(unknown),
        "message": f"{len(opportunities)} 回の該当する機会で、確認された再発なし（帰属 unknown の証拠 {len(unknown)} 件）",
    }


def recurrence_observations(
    context: CandidatesContext, snapshot: dict, candidate: dict
) -> list[dict]:
    merged = latest_record(snapshot, candidate["cid"], "handoff.merged")
    if not merged or merged["data"]["skill_hash"] is None:
        return []
    runs = {
        scope["run_id"]
        for scope in merge_history(context, merged).scopes
        if post_merge_scope(candidate, merged, scope)
    }
    attached = {oid for item in snapshot["candidates"].values() for oid in item["observations"]}
    return [
        observation
        for observation in snapshot["observations"].values()
        if observation["attribution"] == "run"
        and not observation["dismissed"]
        and observation["skill"] == candidate["skill"]
        and observation["run_id"] in runs
        and observation["oid"] not in attached
        and timestamp(observation["ts"]) > timestamp(merged["data"]["merged_at"])
    ]


def confirm_recurrence(context: CandidatesContext, cid: str, oid: str) -> dict:
    records: list[dict] = []

    def prepare(snapshot: dict) -> list[dict]:
        candidate = require_state(snapshot, cid, "promoted")
        observation = model.require_observation(snapshot, oid)
        validate_recurrence_ownership(snapshot, candidate, oid)
        merged = latest_record(snapshot, candidate["cid"], "handoff.merged")
        if not merged or not any(
            scope["run_id"] == observation.get("run_id")
            and scope["sid"] == observation["sid"]
            and timestamp(scope["ts"]) <= timestamp(observation["ts"])
            and post_merge_scope(candidate, merged, scope)
            for scope in merge_history(context, merged).scopes
        ):
            raise ValidationError("マージ後の同じスキルの版の実行範囲が必要です")
        confirmed = model.event(
            "recurrence.confirmed",
            candidate["cid"],
            {
                "oid": oid,
                "run_id": observation["run_id"],
            },
        )
        records.extend([confirmed, state_event(candidate, "candidate", refs=[confirmed["eid"]])])
        return records

    context.store.write(prepare)
    return {"events": records}


def prune_runs(
    context: CandidatesContext, before: str, *, apply: bool = False, now: datetime | None = None
) -> dict:
    if not re.fullmatch(r"[0-9]{4}(?:0[1-9]|1[0-2])", before) or before.startswith("0000"):
        raise ValidationError("--before は YYYYMM で指定してください")
    snapshot = context.store.read()
    protected = [(now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m")]
    for candidate in snapshot["candidates"].values():
        if candidate["state"] == "promoted":
            merged = latest_record(snapshot, candidate["cid"], "handoff.merged")
            if merged:
                protected.append(timestamp(merged["data"]["merged_at"]).strftime("%Y%m"))
        elif candidate["state"] == "promotion_pr":
            promotion = latest_record(snapshot, candidate["cid"], "handoff.promotion_linked")
            if promotion:
                linked_at = timestamp(promotion["ts"])
                created_at = timestamp(promotion["data"].get("pr_created_at", promotion["ts"]))
                protected.append(min(linked_at, created_at).strftime("%Y%m"))
    cutoff = min(before, *protected)
    directory = context.store.directory
    if not directory.exists():
        return {"selected": [], "deleted": [], "apply": apply}
    validate_directory(directory, context.root)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    selected: list[str] = []
    deleted: list[str] = []
    try:
        for name in sorted(os.listdir(descriptor)):
            match = re.fullmatch(r"runs-(\d{4}(?:0[1-9]|1[0-2]))\.jsonl", name)
            if match and match[1] < cutoff:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValidationError("通常ファイル以外の月別記録は整理できません")
                selected.append(name)
        if apply:
            for name in selected:
                file_descriptor = os.open(
                    name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor
                )
                try:
                    opened = os.fstat(file_descriptor)
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                        current.st_dev,
                        current.st_ino,
                    ):
                        raise ValidationError("整理する月別記録が変わりました")
                    os.unlink(name, dir_fd=descriptor)
                    deleted.append(name)
                finally:
                    os.close(file_descriptor)
    finally:
        os.close(descriptor)
    return {"selected": selected, "deleted": deleted, "apply": apply}
