"""教訓候補の出来事の検査・畳み込み・提示の選定（I/O を持たない部分。設計 §8.1〜§8.2・§9・§10.3）。

台帳（``lessons.jsonl``）の 1 行が出来事で、写像（``lessons-state.json``）はその畳み込みの結果。
ファイルの読み書きとロックは ``lessons_store.py`` が受け持つ。
"""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sanitize import (
    ValidationError,
    check_branch,
    check_evidence_ref,
    check_note,
    check_target_ref,
    check_text,
)

SCHEMA_VERSION = 1

STATE_CANDIDATE = "candidate"
STATE_PROMOTING = "promoting"
STATE_PROMOTED = "promoted"
STATE_REJECTED = "rejected"
STATES = (STATE_CANDIDATE, STATE_PROMOTING, STATE_PROMOTED, STATE_REJECTED)

# 状態の遷移（設計 §10.3）。表に無い遷移は拒否する。
TRANSITIONS = frozenset(
    {
        (STATE_CANDIDATE, STATE_PROMOTING),
        (STATE_CANDIDATE, STATE_PROMOTED),
        (STATE_PROMOTING, STATE_PROMOTED),
        (STATE_PROMOTING, STATE_CANDIDATE),
        (STATE_PROMOTED, STATE_CANDIDATE),
        (STATE_CANDIDATE, STATE_REJECTED),
        (STATE_REJECTED, STATE_CANDIDATE),
    }
)

EV_CREATED = "lesson.created"
EV_EVIDENCE = "lesson.evidence_added"
EV_STATE = "lesson.state_changed"
EV_REVISED = "lesson.revised"
EV_ACK = "lesson.acknowledged"
EV_MERGED = "lesson.merged"
EVENT_TYPES = frozenset({EV_CREATED, EV_EVIDENCE, EV_STATE, EV_REVISED, EV_ACK, EV_MERGED})

ACTOR_SPOOL = "spool"
ACTORS = frozenset({"worker", "skill", "agent", "review", ACTOR_SPOOL})
SOURCES = frozenset({"distill", "review", "agent", "import"})
SCOPES = ("claude", "all")
SEVERITIES = ("normal", "critical")
SIGNALS = frozenset(
    {
        "intervention",
        "drift",
        "tool_error",
        "test_failure",
        "lint_failure",
        "permission_denied",
    }
)
TARGET_KINDS = frozenset({"agents_md", "rules", "guard"})
EVIDENCE_KINDS = frozenset({"session", "pr", "agent"})

# 字数の上限（設計 §7.5 の出力スキーマ）。
TEXT_LIMITS = {"title": 80, "event": 400, "cause": 400, "avoidance": 600}
MAX_FAILURE_REFS = 50
MAX_CHANGES_KEPT = 20
MAX_ALIAS_HOPS = 10

_LID = re.compile(r"L-[0-9a-f]{8}")
_EID = re.compile(r"[0-9a-f]{12}|[0-9a-f]{32}")
_FAILURE_EID = re.compile(r"[0-9a-f]{12}")

Masker = Callable[[str], str]


class InvalidTransition(ValidationError):
    """§10.3 の表に無い状態の遷移を表す。"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_lid() -> str:
    return "L-" + uuid.uuid4().hex[:8]


def new_eid() -> str:
    return uuid.uuid4().hex[:12]


def is_lid(value: object) -> bool:
    return isinstance(value, str) and bool(_LID.fullmatch(value))


def marker_for(lid: str) -> str:
    """昇格先に書くマーカー。ID から作り、入力としては受け付けない（設計 §8.1）。"""
    if not is_lid(lid):
        raise ValidationError("invalid lesson id")
    return f"<!-- lesson:{lid} -->"


def _normalize_ts(value: object) -> str:
    """ISO 8601 の日時を UTC の ISO 文字列にそろえる。無ければ現在時刻。"""
    if value is None or value == "":
        return now_iso()
    if not isinstance(value, str):
        raise ValidationError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, OverflowError) as exc:
        raise ValidationError("timestamp is not a valid ISO 8601 value") from exc


def _choice(value: object, allowed: tuple[str, ...] | frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{field} must be one of {sorted(allowed)}")
    return value


def normalize_text_fields(fields: dict, masker: Masker, *, required: bool) -> dict:
    """見出し・事象・原因・回避策を、文字種を検査し、伏せてから字数の上限で切り詰める（設計 §8.3）。"""
    result: dict = {}
    for name, limit in TEXT_LIMITS.items():
        if name not in fields:
            if required:
                raise ValidationError(f"{name} is required")
            continue
        raw = check_text(fields[name], name, limit, allow_newline=name != "title")
        text = masker(raw).strip()[:limit]
        if required and not text:
            raise ValidationError(f"{name} must not be empty")
        result[name] = text
    return result


def normalize_signals(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("signals must be a list of strings")
    unknown = [item for item in value if item not in SIGNALS]
    if unknown:
        raise ValidationError(f"unknown signals: {unknown}")
    return sorted(set(value))


def normalize_evidence(value: object) -> dict:
    """根拠を検査する（kind ごとの参照の形・日時・失敗イベントの eid）。"""
    if not isinstance(value, dict):
        raise ValidationError("evidence must be an object")
    kind = _choice(value.get("kind"), EVIDENCE_KINDS, "evidence.kind")
    ref = check_evidence_ref(kind, value.get("ref"))
    failures = value.get("failures") or []
    if not isinstance(failures, list) or len(failures) > MAX_FAILURE_REFS:
        raise ValidationError("evidence.failures must be a short list")
    if not all(isinstance(item, str) and _FAILURE_EID.fullmatch(item) for item in failures):
        raise ValidationError("evidence.failures must be failure event ids")
    evidence = {
        "kind": kind,
        "ref": ref,
        "ts": _normalize_ts(value.get("ts")),
        "failures": failures,
    }
    if value.get("verified") is True:
        evidence["verified"] = True
    return evidence


def normalize_target(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("target must be an object")
    target = {"kind": _choice(value.get("kind"), TARGET_KINDS, "target.kind")}
    if value.get("ref") is not None:
        target["ref"] = check_target_ref(value.get("ref"))
    if value.get("branch") is not None:
        target["branch"] = check_branch(value.get("branch"))
    return target


def make_event(
    event_type: str, lid: str, actor: str, sid: str, data: dict, *, eid: str = ""
) -> dict:
    """出来事のレコードを作る（data は検査済みであること）。"""
    if event_type not in EVENT_TYPES:
        raise ValidationError("unknown event type")
    if not is_lid(lid):
        raise ValidationError("invalid lesson id")
    if actor not in ACTORS:
        raise ValidationError("unknown actor")
    if eid and not _EID.fullmatch(eid):
        raise ValidationError("invalid event id")
    return {
        "v": SCHEMA_VERSION,
        "ts": now_iso(),
        "eid": eid or new_eid(),
        "type": event_type,
        "lid": lid,
        "actor": actor,
        "sid": sid if isinstance(sid, str) else "",
        "data": data,
    }


def build_lesson_data(payload: dict, masker: Masker, *, source: str, evidence: dict) -> dict:
    """候補の追加（lesson.created / lesson.evidence_added）の data を作る。"""
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    data = normalize_text_fields(payload, masker, required=True)
    data["scope"] = _choice(payload.get("scope", "claude"), SCOPES, "scope")
    data["severity"] = _choice(payload.get("severity", "normal"), SEVERITIES, "severity")
    data["signals"] = normalize_signals(payload.get("signals"))
    data["source"] = _choice(source, SOURCES, "source")
    data["evidence"] = normalize_evidence(evidence)
    return data


def build_state_data(current: str, to: str, target: object, note: object) -> dict:
    to_state = _choice(to, STATES, "to")
    if (current, to_state) not in TRANSITIONS:
        raise InvalidTransition(f"transition {current} -> {to_state} is not allowed")
    data: dict = {"from": current, "to": to_state, "target": normalize_target(target)}
    if note:
        data["note"] = check_note(note)
    return data


def build_revised_data(payload: dict, masker: Masker) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    data = normalize_text_fields(payload, masker, required=False)
    if "scope" in payload:
        data["scope"] = _choice(payload["scope"], SCOPES, "scope")
    if "severity" in payload:
        data["severity"] = _choice(payload["severity"], SEVERITIES, "severity")
    if not data:
        raise ValidationError("nothing to revise")
    data["confirmed"] = True
    return data


# --- 畳み込み -----------------------------------------------------------------


def empty_snapshot() -> dict:
    return {
        "v": SCHEMA_VERSION,
        "ledger_offset": 0,
        "last_eid": "",
        "aliases": {},
        "daily_adds": {"date": "", "counts": {}},
        "lessons": {},
    }


def resolve_lid(snapshot: dict, lid: str) -> str:
    """まとめられた候補の ID を、まとめ先の ID にたどる。"""
    aliases = snapshot.get("aliases", {})
    current = lid
    for _ in range(MAX_ALIAS_HOPS):
        if current not in aliases:
            return current
        current = aliases[current]
    return current


def _evidence_counted(evidence: dict, actor: str) -> bool:
    """再発回数に数える根拠か（設計 §8.2）。自己申告の根拠と確かめられない spool の根拠は数えない。"""
    if evidence["kind"] == "agent":
        return False
    if actor == ACTOR_SPOOL:
        return evidence["kind"] == "session" and evidence.get("verified") is True
    return True


def _recompute(lesson: dict) -> None:
    """再発回数と昇格後の再発回数を根拠から計算し直す。"""
    first_seen: dict[tuple[str, str], str] = {}
    for evidence in lesson["evidence"]:
        if not evidence.get("counted"):
            continue
        key = (evidence["kind"], evidence["ref"])
        if key not in first_seen or evidence["ts"] < first_seen[key]:
            first_seen[key] = evidence["ts"]
    lesson["recurrence"] = len(first_seen)
    promoted_at = lesson.get("promoted_at")
    lesson["post_promotion_recurrence"] = (
        sum(1 for ts in first_seen.values() if ts > promoted_at) if promoted_at else 0
    )


def _add_evidence(lesson: dict, evidence: dict, eid: str, actor: str) -> bool:
    """根拠を足す。同じ eid の出来事は 1 回だけ適用する（spool の二重取り込み対策）。"""
    if any(existing.get("eid") == eid for existing in lesson["evidence"]):
        return False
    lesson["evidence"].append(
        {
            "kind": evidence["kind"],
            "ref": evidence["ref"],
            "ts": evidence["ts"],
            "failures": list(evidence.get("failures") or []),
            "eid": eid,
            "counted": _evidence_counted(evidence, actor),
        }
    )
    return True


def _raise_severity_and_scope(lesson: dict, data: dict, actor: str) -> None:
    """重大度は高い方へ、対象範囲は広い方へだけ変える。人が確定した値は変えない（設計 §8.2）。"""
    severity = data.get("severity")
    scope = data.get("scope")
    if actor == ACTOR_SPOOL:
        # spool から取り込んだ重大度・対象範囲は提案として残す（人が revise で確定するまで反映しない）。
        if severity == "critical":
            lesson["proposed_severity"] = "critical"
        if scope == "all":
            lesson["proposed_scope"] = "all"
        return
    if severity == "critical" and lesson["severity"] == "normal":
        if lesson.get("severity_confirmed"):
            lesson["severity_alert"] = True
        else:
            lesson["severity"] = "critical"
    if scope == "all" and lesson["scope"] == "claude" and not lesson.get("scope_confirmed"):
        lesson["scope"] = "all"


def _note_change(lesson: dict, event: dict) -> None:
    lesson["updated_at"] = event["ts"]
    changes = lesson.setdefault("changes", [])
    changes.append({"ts": event["ts"], "actor": event["actor"], "type": event["type"]})
    del changes[:-MAX_CHANGES_KEPT]


def _new_lesson(event: dict) -> dict:
    data = event["data"]
    from_spool = event["actor"] == ACTOR_SPOOL
    return {
        "lid": event["lid"],
        "title": data["title"],
        "event": data["event"],
        "cause": data["cause"],
        "avoidance": data["avoidance"],
        "scope": "claude" if from_spool else data["scope"],
        "scope_confirmed": False,
        "severity": "normal" if from_spool else data["severity"],
        "severity_confirmed": False,
        "severity_alert": False,
        "signals": list(data.get("signals") or []),
        "source": data["source"],
        "state": STATE_CANDIDATE,
        "state_changed_at": event["ts"],
        "created_at": event["ts"],
        "updated_at": event["ts"],
        "target": None,
        "note": "",
        "evidence": [],
        "recurrence": 0,
        "post_promotion_recurrence": 0,
        "ack_baseline": 0,
        "promoted_at": None,
        "changes": [],
    }


def _apply_created_or_evidence(snapshot: dict, event: dict) -> None:
    lessons = snapshot["lessons"]
    lid = resolve_lid(snapshot, event["lid"])
    lesson = lessons.get(lid)
    data = event["data"]
    if lesson is not None and any(item.get("eid") == event["eid"] for item in lesson["evidence"]):
        return  # 同じ eid の出来事は 1 回だけ適用する（spool の二重取り込み対策）
    if lesson is None:
        if event["type"] != EV_CREATED:
            return  # 無い候補への合流は捨てる（作り直した台帳など）
        lesson = _new_lesson(event)
        lessons[lid] = lesson
        if event["actor"] == ACTOR_SPOOL:
            _raise_severity_and_scope(lesson, data, ACTOR_SPOOL)
    else:
        _raise_severity_and_scope(lesson, data, event["actor"])
        lesson["signals"] = sorted(set(lesson["signals"]) | set(data.get("signals") or []))
    _add_evidence(lesson, data["evidence"], event["eid"], event["actor"])
    _bump_daily(snapshot, event)
    _recompute(lesson)
    _note_change(lesson, event)


def _bump_daily(snapshot: dict, event: dict) -> None:
    """追加元ごとの日ごとの追加件数（ローカル日付）。最新の日付だけを持つ（設計 §8.3）。

    spool から取り込んだ出来事は数えない（退避した側の上限で抑える。取り込みで正規の追加を締め出さない）。
    """
    if event["actor"] == ACTOR_SPOOL:
        return
    day = datetime.fromisoformat(event["ts"]).astimezone().date().isoformat()
    daily = snapshot.setdefault("daily_adds", {"date": "", "counts": {}})
    if daily.get("date") != day:
        if daily.get("date", "") > day:
            return
        daily["date"] = day
        daily["counts"] = {}
    source = event["data"].get("source", "")
    daily["counts"][source] = daily["counts"].get(source, 0) + 1


def _apply_state(lesson: dict, event: dict, strict: bool) -> None:
    data = event["data"]
    if (lesson["state"], data["to"]) not in TRANSITIONS or data.get("from") != lesson["state"]:
        if strict:
            raise InvalidTransition(f"transition {lesson['state']} -> {data['to']} is not allowed")
        return
    lesson["state"] = data["to"]
    lesson["state_changed_at"] = event["ts"]
    if data.get("target") is not None:
        lesson["target"] = data["target"]
    lesson["note"] = data.get("note", "")
    if data["to"] == STATE_PROMOTED:
        lesson["promoted_at"] = event["ts"]
        lesson["ack_baseline"] = 0
    _recompute(lesson)


def _apply_revised(lesson: dict, event: dict) -> None:
    data = event["data"]
    for name in TEXT_LIMITS:
        if name in data:
            lesson[name] = data[name]
    if "scope" in data:
        lesson["scope"] = data["scope"]
        lesson["scope_confirmed"] = True
        lesson.pop("proposed_scope", None)
    if "severity" in data:
        lesson["severity"] = data["severity"]
        lesson["severity_confirmed"] = True
        lesson.pop("proposed_severity", None)


def _apply_merged(snapshot: dict, source_lid: str, event: dict) -> None:
    lessons = snapshot["lessons"]
    into = resolve_lid(snapshot, str(event["data"].get("into", "")))
    source = lessons.get(source_lid)
    target = lessons.get(into)
    if source is None or target is None or into == source_lid:
        return
    for evidence in source["evidence"]:
        if not any(existing.get("eid") == evidence.get("eid") for existing in target["evidence"]):
            target["evidence"].append(dict(evidence))
    target["signals"] = sorted(set(target["signals"]) | set(source["signals"]))
    _raise_severity_and_scope(
        target,
        {"severity": source["severity"], "scope": source["scope"]},
        event["actor"],
    )
    del lessons[source_lid]
    aliases = snapshot.setdefault("aliases", {})
    for old, new in list(aliases.items()):
        if new == source_lid:
            aliases[old] = into
    aliases[source_lid] = into
    _recompute(target)
    _note_change(target, event)


def apply_event(snapshot: dict, event: dict, *, strict: bool = False) -> None:
    """出来事を 1 件、写像に畳み込む。strict のときは不正な遷移を例外にする（書き込み時の検査）。

    strict でないとき（追いつき・作り直し）は、適用の途中で失敗した出来事を飛ばし、写像を適用前に戻す
    （壊れた 1 行で読み取りと作り直しが止まり続けないようにする）。
    """
    if not is_wellformed(event):
        if strict:
            raise ValidationError("malformed event")
        return
    if strict:
        _dispatch(snapshot, event, strict=True)
        return
    touched = {resolve_lid(snapshot, event["lid"])}
    if event["type"] == EV_MERGED:
        touched.add(resolve_lid(snapshot, str(event["data"].get("into", ""))))
    saved_lessons = {lid: copy.deepcopy(snapshot["lessons"].get(lid)) for lid in touched}
    saved_daily = copy.deepcopy(snapshot.get("daily_adds"))
    saved_aliases = dict(snapshot.get("aliases", {}))
    try:
        _dispatch(snapshot, event, strict=False)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        for lid, lesson in saved_lessons.items():
            if lesson is None:
                snapshot["lessons"].pop(lid, None)
            else:
                snapshot["lessons"][lid] = lesson
        snapshot["daily_adds"] = saved_daily
        snapshot["aliases"] = saved_aliases


def _dispatch(snapshot: dict, event: dict, *, strict: bool) -> None:
    if event["type"] in (EV_CREATED, EV_EVIDENCE):
        _apply_created_or_evidence(snapshot, event)
        return
    lid = resolve_lid(snapshot, event["lid"])
    lesson = snapshot["lessons"].get(lid)
    if lesson is None:
        if strict:
            raise ValidationError(f"unknown lesson {event['lid']}")
        return
    if event["type"] == EV_STATE:
        _apply_state(lesson, event, strict)
    elif event["type"] == EV_REVISED:
        _apply_revised(lesson, event)
    elif event["type"] == EV_ACK:
        value = event["data"].get("post_promotion_recurrence", 0)
        lesson["ack_baseline"] = value if isinstance(value, int) and value >= 0 else 0
        lesson["severity_alert"] = False
    elif event["type"] == EV_MERGED:
        _apply_merged(snapshot, lid, event)
        return
    _note_change(lesson, event)


def is_wellformed(event: object) -> bool:
    """台帳の 1 行が出来事の形をしているか（壊れた行・古い形式を畳み込みで飛ばすため）。"""
    if not isinstance(event, dict):
        return False
    if event.get("type") not in EVENT_TYPES or event.get("actor") not in ACTORS:
        return False
    if not is_lid(event.get("lid")) or not isinstance(event.get("data"), dict):
        return False
    if not isinstance(event.get("eid"), str) or not _is_iso(event.get("ts")):
        return False
    data = event["data"]
    if event["type"] == EV_CREATED:
        if not all(
            isinstance(data.get(key), str) for key in (*TEXT_LIMITS, "scope", "severity", "source")
        ):
            return False
        return _is_evidence(data.get("evidence"))
    if event["type"] == EV_EVIDENCE:
        return _is_evidence(data.get("evidence"))
    if event["type"] == EV_STATE:
        return data.get("to") in STATES and data.get("from") in STATES
    return True


def _is_iso(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_evidence(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("kind") in EVIDENCE_KINDS
        and isinstance(value.get("ref"), str)
        and _is_iso(value.get("ts"))
        and isinstance(value.get("failures", []), list)
    )


# --- 提示の選定（設計 §9）-------------------------------------------------------


REASON_CRITICAL_SINGLE = "critical_single"
REASON_SEVERITY_ALERT = "severity_alert"
REASON_NOT_EFFECTIVE = "not_effective"
REASON_PROMOTING_STALE = "promoting_stale"
REASON_RECURRING = "recurring"
ID_ONLY_REASONS = frozenset({REASON_CRITICAL_SINGLE, REASON_SEVERITY_ALERT})


def _days_since(ts: str, now: datetime) -> float:
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed) / timedelta(days=1)


def presentable(
    snapshot: dict, threshold: int, stale_days: int, now: datetime | None = None
) -> list:
    """提示の対象を、提示の順に (lesson, reason) で返す（設計 §9 の 2・3）。"""
    current = now or datetime.now(UTC)
    critical_recurring: list = []
    not_effective: list = []
    stale: list = []
    recurring: list = []
    id_only: list = []
    for lesson in snapshot.get("lessons", {}).values():
        state = lesson.get("state")
        if state == STATE_REJECTED:
            continue
        recurrence = lesson.get("recurrence", 0)
        if state == STATE_CANDIDATE and recurrence >= threshold:
            bucket = critical_recurring if lesson.get("severity") == "critical" else recurring
            bucket.append((lesson, REASON_RECURRING))
        elif state == STATE_CANDIDATE and lesson.get("severity") == "critical" and recurrence == 1:
            id_only.append((lesson, REASON_CRITICAL_SINGLE))
        elif lesson.get("severity_alert"):
            id_only.append((lesson, REASON_SEVERITY_ALERT))
        if state == STATE_PROMOTED:
            delta = lesson.get("post_promotion_recurrence", 0) - lesson.get("ack_baseline", 0)
            if delta >= threshold:
                not_effective.append((lesson, REASON_NOT_EFFECTIVE))
        if (
            state == STATE_PROMOTING
            and _days_since(lesson.get("state_changed_at", ""), current) > stale_days
        ):
            stale.append((lesson, REASON_PROMOTING_STALE))

    def by_recurrence(item: tuple) -> int:
        return -item[0].get("recurrence", 0)

    return [
        *sorted(critical_recurring, key=by_recurrence),
        *not_effective,
        *stale,
        *sorted(recurring, key=by_recurrence),
        *id_only,
    ]
