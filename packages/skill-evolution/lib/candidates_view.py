"""確認待ちの一覧、似た観測の提案、帰属確認用の監査記録。"""

from __future__ import annotations

import json
import re
import subprocess
from itertools import combinations

import candidates_model as model
import skill_evolution_common as common
from candidates_git import skill_tree_hash
from candidates_ingest import CandidatesContext
from candidates_recurrence import recurrence_observations, recurrence_status
from candidates_safety import ValidationError, open_regular, timestamp, validate_directory
from candidates_scenarios import draft_review

PENDING_STATES = ("improving", "promotion_pr", "promoted")


def words(text: str) -> set[str]:
    english = set(re.findall(r"[a-z0-9]+", text.casefold()))
    cjk = re.findall(r"[\u3040-\u30ff\u3400-\u9fff]+", text)
    return english | {span[index : index + 2] for span in cjk for index in range(len(span) - 1)}


def suggestions(snapshot: dict) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    attached = {
        oid for candidate in snapshot["candidates"].values() for oid in candidate["observations"]
    }
    for observation in snapshot["observations"].values():
        if (
            observation["dismissed"]
            or observation["attribution"] == "unknown"
            or observation["oid"] in attached
        ):
            continue
        groups.setdefault((observation["skill"], observation["failure_kind"]), []).append(
            observation
        )
    proposed = []
    for (skill, kind), observations in groups.items():
        adjacent: dict[str, set[str]] = {item["oid"]: set() for item in observations}
        for left, right in combinations(observations, 2):
            left_words, right_words = words(left["excerpt"]), words(right["excerpt"])
            overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
            if overlap >= 0.5:
                adjacent[left["oid"]].add(right["oid"])
                adjacent[right["oid"]].add(left["oid"])
        remaining = set(adjacent)
        while remaining:
            component, frontier = set(), {min(remaining)}
            while frontier:
                component |= frontier
                frontier = {neighbor for oid in frontier for neighbor in adjacent[oid]} - component
            remaining -= component
            members = [item for item in observations if item["oid"] in component]
            origins = {
                (origin["kind"], origin["ref"])
                for item in members
                for origin in item.get("contexts", [])
                if origin.get("kind") in {"session", "pr"} and origin.get("ref")
            }
            if len(members) >= 2 and len(origins) >= 2:
                proposed.append(
                    {
                        "label": "候補にするかの提案",
                        "suggestion_only": True,
                        "skill": skill,
                        "failure_kind": kind,
                        "observations": sorted(component),
                    }
                )
    return sorted(
        proposed, key=lambda item: (item["skill"], item["failure_kind"], item["observations"])
    )


def candidate_summary(
    context: CandidatesContext,
    snapshot: dict,
    candidate: dict,
    *,
    hash_cache: dict[tuple[str, str], str | None] | None = None,
) -> dict:
    if hash_cache is None:
        hash_cache = {}
    eligible = context.eligible(candidate["skill"])
    summary = {
        **candidate,
        "event_count": model.evidence_count(snapshot, candidate["observations"]),
        "handoff_eligible": eligible,
        "handoff_status": "受け渡し可能" if eligible else "受け渡しの対象外",
    }
    reference = next(
        (
            record
            for record in reversed(list(snapshot["records"].values()))
            if record["cid"] == candidate["cid"]
            and record["type"] in {"handoff.merged", "handoff.evaluation_linked"}
        ),
        None,
    )
    if reference and reference["data"].get("skill_hash") is not None:
        key = (candidate["skill"], "HEAD")
        try:
            if key not in hash_cache:
                hash_cache[key] = skill_tree_hash(
                    context.project, candidate["skill"], "HEAD", context.config
                )
            current = hash_cache[key]
            if current is not None and current != reference["data"]["skill_hash"]:
                summary["rerun_hint"] = "シナリオの再実行を勧める"
        except (OSError, ValidationError, subprocess.SubprocessError):
            hash_cache[key] = None
    if candidate["state"] == "promoted":
        observations = []
        try:
            summary["recurrence"] = recurrence_status(context, snapshot, candidate)
            evidence_words = set().union(
                *(
                    words(snapshot["observations"][oid]["excerpt"])
                    for oid in candidate["observations"]
                )
            )
            observations = recurrence_observations(context, snapshot, candidate)
            observations.sort(
                key=lambda item: (-len(evidence_words & words(item["excerpt"])), item["oid"])
            )
        except (ValidationError, OSError):
            summary["recurrence"] = {
                "opportunities": None,
                "message": "再発の確認に必要な記録を読めません",
            }
        summary["recurrence_suggestions"] = {
            "suggestion_only": True,
            "observations": observations,
        }
    return summary


def listing(context: CandidatesContext, snapshot: dict) -> dict:
    pending_observations = [
        item
        for item in snapshot["observations"].values()
        if item["attribution"] == "unknown" and not item["dismissed"]
    ]
    hash_cache: dict[tuple[str, str], str | None] = {}
    candidates = [
        candidate_summary(context, snapshot, item, hash_cache=hash_cache)
        for item in snapshot["candidates"].values()
    ]
    candidates.sort(key=lambda item: (model.STATES.index(item["state"]), item["cid"]))
    return {
        "suggestions": suggestions(snapshot),
        "pending_confirmation": {
            "unknown": pending_observations,
            "candidates": [item for item in candidates if item["state"] in PENDING_STATES],
        },
        "candidates": candidates,
        "observations": sorted(
            snapshot["observations"].values(), key=lambda item: (item["dismissed"], item["oid"])
        ),
    }


def audit_context(context: CandidatesContext, sid: str | None, at: str) -> list[dict]:
    if not sid or not common.ID_PATTERN.fullmatch(sid):
        return []
    core = common._load_hook_common()
    config = (
        core.load_package_config("audit", "audit-flags.json", str(context.project)) if core else {}
    )
    relative = (config.get("paths") or {}).get("logs_dir", ".claude/logs/audit")
    directory = context.root / relative / "sessions"
    if not directory.exists():
        return []
    validate_directory(directory, context.root)
    events = []
    try:
        with open_regular(directory / f"{sid}.jsonl") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    if record.get("sid") != sid:
                        continue
                    details = record.get("data") or {}
                    actual = details.get("actual") or {}
                    if record.get("type") not in {"subagent_start", "subagent_end"} and actual.get(
                        "tool"
                    ) not in {"skill", "task", "agent"}:
                        continue
                    distance = abs((timestamp(record["ts"]) - timestamp(at)).total_seconds())
                    events.append(
                        (
                            distance,
                            {
                                "ts": record["ts"],
                                "type": record["type"],
                                "agent_id": record.get("aid"),
                                "agent_type": details.get("agent_type"),
                                "skill": (record.get("ctx") or {}).get("skill"),
                                "route": actual,
                            },
                        )
                    )
                except (
                    ValueError,
                    UnicodeError,
                    KeyError,
                    AttributeError,
                    TypeError,
                    ValidationError,
                ):
                    continue
    except FileNotFoundError:
        return []
    return sorted(
        [record for _, record in sorted(events, key=lambda item: item[0])[:20]],
        key=lambda record: timestamp(record["ts"]),
    )


def show(context: CandidatesContext, snapshot: dict, object_id: str) -> dict:
    if object_id.startswith("C-"):
        candidate = model.require_candidate(snapshot, object_id)
        return {
            "candidate": candidate_summary(context, snapshot, candidate),
            "draft_review": draft_review(context, snapshot, candidate),
            "trace": [
                record
                for record in snapshot["records"].values()
                if record["cid"] == candidate["cid"]
            ],
        }
    observation = model.require_observation(snapshot, object_id)
    return {
        "observation": observation,
        "candidate_scopes": context.history().human_choices(observation["sid"], observation["ts"]),
        "audit_context": audit_context(context, observation["sid"], observation["ts"]),
    }
