"""観測・候補の畳み込みと状態遷移の検査。"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import skill_evolution_common as common
from candidates_handoff_events import HANDOFF_EVENTS, promotion_evaluation, validate_handoff_event
from candidates_safety import (
    ValidationError,
    require_id,
    timestamp,
    validate_strings,
    validate_text,
)
from candidates_scenario_events import SCENARIO_EVENTS, latest_record, validate_scenario_event

STATES = (
    "candidate",
    "drafting",
    "scenario_approved",
    "improving",
    "promotion_pr",
    "promoted",
    "rejected",
    "archived",
)
TRANSITIONS = {
    "candidate": {"drafting", "rejected", "archived"},
    "drafting": {"scenario_approved", "rejected"},
    "scenario_approved": {"drafting", "improving", "rejected"},
    "improving": {"drafting", "promotion_pr", "candidate", "rejected"},
    "promotion_pr": {"promoted", "candidate", "rejected"},
    "promoted": {"candidate", "rejected"},
    "rejected": {"candidate", "archived"},
    "archived": {"candidate"},
}
REQUIRED_RECORDS = {
    ("drafting", "scenario_approved"): ("scenario.approved",),
    ("scenario_approved", "improving"): ("scenario.suite_verified",),
    ("improving", "promotion_pr"): ("handoff.evaluation_linked", "handoff.promotion_linked"),
    ("promotion_pr", "promoted"): ("handoff.merged",),
    ("promotion_pr", "candidate"): ("handoff.closed",),
    ("promoted", "candidate"): ("recurrence.confirmed",),
}
EVENT_TYPES = (
    SCENARIO_EVENTS
    | HANDOFF_EVENTS
    | frozenset(
        {
            "observation.created",
            "observation.attributed",
            "observation.dismissed",
            "candidate.created",
            "candidate.observation_added",
            "candidate.merged",
            "candidate.state_changed",
        }
    )
)


def empty_projection() -> dict:
    return {
        "v": 1,
        "observations": {},
        "candidates": {},
        "aliases": {},
        "records": {},
        "applied_eids": set(),
        "ledger_offset": 0,
        "last_eid": None,
    }


def event(kind: str, object_id: str, payload: dict, *, actor: str = "human") -> dict:
    return {
        "v": 1,
        "ts": datetime.now(UTC).isoformat(),
        "eid": uuid.uuid4().hex[:12],
        "type": kind,
        "oid" if kind.startswith("observation.") else "cid": object_id,
        "actor": actor,
        "data": payload,
    }


def handoff_eligible(skill: str, orchestra: Path, *, own_project: bool = True) -> bool:
    return bool(
        own_project
        and re.fullmatch(r"[a-z0-9-]+", skill)
        and (orchestra / "facets/compositions/skills" / f"{skill}.yaml").is_file()
    )


def require_observation(snapshot: dict, oid: str) -> dict:
    require_id(oid, "O")
    if oid not in snapshot["observations"]:
        raise ValidationError("観測が見つかりません")
    return snapshot["observations"][oid]


def require_candidate(snapshot: dict, cid: str) -> dict:
    require_id(cid, "C")
    visited = set()
    while cid in snapshot["aliases"]:
        if cid in visited:
            raise ValidationError("まとめ先の参照が循環しています")
        visited.add(cid)
        cid = snapshot["aliases"][cid]
    if cid not in snapshot["candidates"]:
        raise ValidationError("候補が見つかりません")
    return snapshot["candidates"][cid]


def _require_human(record: dict) -> None:
    if record["actor"] != "human":
        raise ValidationError("この操作には人の確認が必要です")


def _validate_attribution(payload: dict) -> None:
    attribution = payload.get("attribution")
    if attribution == "unknown":
        if any(payload.get(key) is not None for key in ("skill", "run_id", "attributed_by")):
            raise ValidationError("unknown に帰属先は指定できません")
        return
    if attribution not in {"run", "skill"} or not common.valid_name(payload.get("skill")):
        raise ValidationError("帰属先のスキルが正しくありません")
    if payload.get("attributed_by") not in {"human", "auto"}:
        raise ValidationError("帰属の決定者が正しくありません")
    if attribution == "run" and not re.fullmatch(r"R-[0-9a-f]{12}", str(payload.get("run_id"))):
        raise ValidationError("実行範囲の ID が正しくありません")
    if attribution == "skill" and payload.get("run_id") is not None:
        raise ValidationError("スキルだけの帰属に実行範囲は指定できません")
    if payload.get("attributed_by") == "auto":
        conditions = payload.get("conditions")
        if (
            attribution != "run"
            or not isinstance(conditions, dict)
            or not all(
                conditions.get(key) is True
                for key in (
                    "unique_scope",
                    "recorded_boundary",
                    "no_nested_scope",
                    "no_running_subagent",
                )
            )
            or conditions.get("confirmed_scope", True) is not True
        ):
            raise ValidationError("自動帰属の条件が満たされていません")


def _create_observation(snapshot: dict, record: dict) -> None:
    oid, payload = require_id(record.get("oid"), "O"), record["data"]
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("kind") not in {"failure", "lesson"}:
        raise ValidationError("証拠の鍵が正しくありません")
    if evidence["kind"] == "failure":
        require_id(evidence.get("ref"))
    else:
        require_id(evidence.get("ref"), "L")
    if any(item["evidence"] == evidence for item in snapshot["observations"].values()):
        return
    if oid in snapshot["observations"]:
        raise ValidationError("観測の ID が重複しています")
    timestamp(payload.get("ts"))
    sid = payload.get("sid")
    if sid is not None and (not isinstance(sid, str) or not common.ID_PATTERN.fullmatch(sid)):
        raise ValidationError("セッション ID が正しくありません")
    if not common.valid_name(payload.get("failure_kind")):
        raise ValidationError("失敗の種類が正しくありません")
    contexts = payload.get("contexts", [])
    if not isinstance(contexts, list):
        raise ValidationError("根拠の文脈は一覧で指定してください")
    for context in contexts:
        if not isinstance(context, dict) or not isinstance(context.get("ref"), str):
            raise ValidationError("根拠の文脈が正しくありません")
        kind, ref = context.get("kind"), context["ref"]
        if kind == "session" and common.ID_PATTERN.fullmatch(ref):
            continue
        if kind == "pr" and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", ref):
            continue
        if kind == "agent" and common.ID_PATTERN.fullmatch(ref):
            continue
        raise ValidationError("根拠の文脈の形式が正しくありません")
    validate_text(payload.get("excerpt"), "抜粋", 100000, multiline=True)
    _validate_attribution(payload)
    if evidence["kind"] == "lesson" and payload.get("attributed_by") == "auto":
        raise ValidationError("教訓候補は自動帰属できません")
    failures = payload.get("failure_eids", [])
    if not isinstance(failures, list):
        raise ValidationError("根拠の失敗は一覧で指定してください")
    for eid in failures:
        require_id(eid)
    snapshot["observations"][oid] = {**copy.deepcopy(payload), "oid": oid, "dismissed": False}


def _attribute(snapshot: dict, record: dict) -> None:
    _require_human(record)
    observation = require_observation(snapshot, record.get("oid"))
    if observation["dismissed"]:
        raise ValidationError("却下済みの観測は帰属を変えられません")
    payload = record["data"]
    _validate_attribution(payload)
    if payload.get("attributed_by") != "human":
        raise ValidationError("人が帰属を確定してください")
    if any(
        observation["oid"] in candidate["observations"] and payload["skill"] != candidate["skill"]
        for candidate in snapshot["candidates"].values()
    ):
        raise ValidationError("候補に含まれる観測のスキルは変えられません")
    observation.update(copy.deepcopy(payload))


def _dismiss(snapshot: dict, record: dict) -> None:
    _require_human(record)
    observation = require_observation(snapshot, record.get("oid"))
    validate_text(record["data"].get("note", ""), "note", 200)
    observation.update(dismissed=True, note=record["data"].get("note", ""))


def _candidate_observations(snapshot: dict, oids: object, skill: str) -> list[str]:
    if not isinstance(oids, list) or not oids:
        raise ValidationError("根拠の観測が 1 件以上必要です")
    for oid in oids:
        observation = require_observation(snapshot, oid)
        if (
            observation["dismissed"]
            or observation["attribution"] == "unknown"
            or observation["skill"] != skill
        ):
            raise ValidationError("同じスキルに帰属が確定した観測を指定してください")
    return list(dict.fromkeys(oids))


def _create_candidate(snapshot: dict, record: dict) -> None:
    _require_human(record)
    cid, payload = require_id(record.get("cid"), "C"), record["data"]
    if cid in snapshot["candidates"] or cid in snapshot["aliases"]:
        raise ValidationError("候補の ID が重複しています")
    skill = payload.get("skill")
    if not common.valid_name(skill):
        raise ValidationError("スキル名が正しくありません")
    hypothesis = payload.get("hypothesis")
    if not isinstance(hypothesis, dict):
        raise ValidationError("仮説の見出しと説明が必要です")
    title = validate_text(hypothesis.get("title"), "仮説の見出し", 200)
    validate_text(hypothesis.get("description"), "仮説の説明", 1000, multiline=True)
    if not title.strip():
        raise ValidationError("仮説の見出しは空にできません")
    observations = _candidate_observations(snapshot, payload.get("observations"), skill)
    snapshot["candidates"][cid] = {
        "cid": cid,
        "skill": skill,
        "hypothesis": copy.deepcopy(hypothesis),
        "observations": observations,
        "handoff_eligible": payload.get("handoff_eligible") is True,
        "state": "candidate",
        "state_since": record["ts"],
        "created_at": record["ts"],
        "updated_at": record["ts"],
        "history": [],
    }


def _add_observation(snapshot: dict, record: dict) -> None:
    _require_human(record)
    candidate = require_candidate(snapshot, record.get("cid"))
    oids = _candidate_observations(snapshot, [record["data"].get("oid")], candidate["skill"])
    candidate["observations"] = list(dict.fromkeys(candidate["observations"] + oids))


def _merge(snapshot: dict, record: dict) -> None:
    _require_human(record)
    source = require_candidate(snapshot, record.get("cid"))
    target = require_candidate(snapshot, record["data"].get("into"))
    if source["cid"] == target["cid"] or source["skill"] != target["skill"]:
        raise ValidationError("別の、同じスキルの候補をまとめ先に指定してください")
    if source["state"] != "candidate" or target["state"] != "candidate":
        raise ValidationError("candidate 状態の候補同士だけをまとめられます")
    target["observations"] = list(dict.fromkeys(target["observations"] + source["observations"]))
    target.setdefault("merged_history", []).append(copy.deepcopy(source))
    snapshot["aliases"][source["cid"]] = target["cid"]
    del snapshot["candidates"][source["cid"]]


def validate_transition(
    snapshot: dict, candidate: dict, record: dict, orchestra: Path | None, own_project: bool = True
) -> None:
    payload, source = record["data"], candidate["state"]
    target = payload.get("to")
    if payload.get("from") != source or target not in TRANSITIONS[source]:
        raise ValidationError("許可されていない状態遷移です")
    validate_text(payload.get("note", ""), "note", 200)
    if (source, target) == ("improving", "drafting") and payload.get(
        "verification_mismatch"
    ) is not True:
        raise ValidationError("verify-suite の不一致確認が必要です")
    if target == "drafting" and source == "candidate":
        _require_human(record)
        eligible = candidate["handoff_eligible"]
        if orchestra is not None:
            eligible = handoff_eligible(candidate["skill"], orchestra, own_project=own_project)
        if not eligible:
            raise ValidationError("受け渡しの対象外です")
    required = REQUIRED_RECORDS.get((source, target), ())
    if required:
        refs = payload.get("refs", [])
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise ValidationError("遷移の根拠の記録を指定してください")
        records = [snapshot["records"].get(ref, {}) for ref in refs]
        for kind in required:
            if not any(
                item.get("type") == kind
                and item.get("cid") == candidate["cid"]
                and timestamp(item.get("ts")) >= timestamp(candidate["state_since"])
                for item in records
            ):
                raise ValidationError(f"遷移に必要な記録がありません: {kind}")
        if (source, target) == ("improving", "promotion_pr"):
            promotion = latest_record(snapshot, candidate["cid"], "handoff.promotion_linked")
            if not promotion or promotion["eid"] not in refs:
                raise ValidationError("最新の昇格 PR の参照が必要です")
            evaluation = promotion_evaluation(
                snapshot, candidate, promotion["data"].get("mh_cand_id")
            )
            if evaluation["eid"] not in refs:
                raise ValidationError("昇格する提案候補の holdout 評価の参照が必要です")
        if target in {"scenario_approved", "improving"}:
            draft = latest_record(snapshot, candidate["cid"], "scenario.drafted")
            approval = latest_record(snapshot, candidate["cid"], "scenario.approved")
            if not draft or not approval or approval["data"].get("draft_eid") != draft["eid"]:
                raise ValidationError("最新の下書きに対する承認が必要です")
            expected = (
                approval
                if target == "scenario_approved"
                else latest_record(snapshot, candidate["cid"], "scenario.suite_verified")
            )
            if not expected or expected["eid"] not in refs:
                raise ValidationError("最新の承認・確認の参照が必要です")
    else:
        _require_human(record)
        if target == "rejected" or (source, target) in {
            ("scenario_approved", "drafting"),
            ("improving", "candidate"),
            ("improving", "drafting"),
        }:
            if not payload.get("note", "").strip():
                raise ValidationError("この遷移には理由が必要です")


def _transition(snapshot: dict, record: dict, orchestra: Path | None, own_project: bool) -> None:
    candidate = require_candidate(snapshot, record.get("cid"))
    validate_transition(snapshot, candidate, record, orchestra, own_project)
    candidate.update(state=record["data"]["to"], state_since=record["ts"])


APPLIERS = {
    "observation.created": _create_observation,
    "observation.attributed": _attribute,
    "observation.dismissed": _dismiss,
    "candidate.created": _create_candidate,
    "candidate.observation_added": _add_observation,
    "candidate.merged": _merge,
}


def apply_event(
    snapshot: dict, record: dict, *, orchestra: Path | None = None, own_project: bool = True
) -> dict:
    if not isinstance(record, dict) or record.get("v") != 1:
        raise ValidationError("台帳の形式が正しくありません")
    require_id(record.get("eid"))
    timestamp(record.get("ts"))
    validate_strings(record)
    if record.get("type") not in EVENT_TYPES or record.get("actor") not in {"ingest", "human"}:
        raise ValidationError("台帳の出来事が正しくありません")
    if not isinstance(record.get("data"), dict):
        raise ValidationError("出来事の data が正しくありません")
    if record["eid"] in snapshot["applied_eids"]:
        return snapshot
    if record["type"].startswith("observation.") and "cid" in record:
        raise ValidationError("観測の出来事に候補 ID は指定できません")
    if record["type"] == "candidate.state_changed":
        _transition(snapshot, record, orchestra, own_project)
    elif record["type"] in SCENARIO_EVENTS:
        candidate = require_candidate(snapshot, record.get("cid"))
        validate_scenario_event(snapshot, candidate, record)
        snapshot["records"][record["eid"]] = copy.deepcopy(record)
    elif record["type"] in HANDOFF_EVENTS:
        candidate = require_candidate(snapshot, record.get("cid"))
        validate_handoff_event(snapshot, candidate, record)
        snapshot["records"][record["eid"]] = copy.deepcopy(record)
        if record["type"] == "recurrence.confirmed":
            candidate["observations"] = list(
                dict.fromkeys(candidate["observations"] + [record["data"]["oid"]])
            )
    else:
        APPLIERS[record["type"]](snapshot, record)
    if record.get("cid"):
        candidate = require_candidate(snapshot, record["cid"])
        candidate["updated_at"] = record["ts"]
        candidate["history"].append(copy.deepcopy(record))
    snapshot["applied_eids"].add(record["eid"])
    snapshot["last_eid"] = record["eid"]
    return snapshot


def evidence_count(snapshot: dict, oids: list[str]) -> int:
    keys = set()
    for oid in oids:
        observation = require_observation(snapshot, oid)
        if observation["attribution"] == "unknown" or observation["dismissed"]:
            continue
        evidence = observation["evidence"]
        if evidence["kind"] == "failure":
            keys.add(("failure", evidence["ref"]))
        elif observation.get("failure_eids"):
            keys.update(("failure", eid) for eid in observation["failure_eids"])
        else:
            keys.add(("lesson", evidence["ref"]))
    return len(keys)
