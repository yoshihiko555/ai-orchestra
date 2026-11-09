"""シナリオの出来事と承認参照の契約。"""

from __future__ import annotations

import re

from candidates_safety import ValidationError, require_id, timestamp, validate_text

SCENARIO_EVENTS = frozenset(
    {"scenario.drafted", "scenario.approved", "scenario.suite_verified", "scenario.retired"}
)
ANSWER_KEYS = {"reproduces", "holdout_distinct", "publishable", "human_lines_reviewed"}


def latest_record(snapshot: dict, cid: str, kind: str) -> dict | None:
    return next(
        (
            record
            for record in reversed(list(snapshot["records"].values()))
            if record["cid"] == cid and record["type"] == kind
        ),
        None,
    )


def validate_hash(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError("sha256 の形式が正しくありません")


def validate_files(files: object) -> None:
    if not isinstance(files, dict) or not files:
        raise ValidationError("シナリオのファイルと sha256 が必要です")
    for name, digest in files.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+\.yaml", name):
            raise ValidationError("シナリオのファイル名が正しくありません")
        validate_hash(digest)


def validate_answers(answers: object, lines: object) -> None:
    if (
        not isinstance(answers, dict)
        or answers.keys() != ANSWER_KEYS
        or any(type(value) is not bool for value in answers.values())
    ):
        raise ValidationError("4 項目の確認に真偽値で回答してください")
    if not all(answers[key] for key in ANSWER_KEYS - {"human_lines_reviewed"}):
        raise ValidationError("再現・holdout・公開の確認が必要です")
    if not isinstance(lines, list) or (lines and not answers["human_lines_reviewed"]):
        raise ValidationError("人が書いた実行行の確認が必要です")
    for line in lines:
        if (
            not isinstance(line, dict)
            or line.keys() != {"file", "path", "line"}
            or not all(isinstance(value, str) for value in line.values())
        ):
            raise ValidationError("人が書いた行は file・path・line で指定してください")


def validate_scenario_event(snapshot: dict, candidate: dict, record: dict) -> None:
    payload, kind = record["data"], record["type"]
    if record["actor"] != "human":
        raise ValidationError("シナリオの操作には人の確認が必要です")
    if kind != "scenario.retired":
        validate_files(payload.get("files"))
    if kind in {"scenario.drafted", "scenario.approved"}:
        if (
            candidate["state"] != "drafting"
            or type(payload.get("draft")) is not int
            or payload["draft"] < 1
        ):
            raise ValidationError("drafting 状態と下書き番号が必要です")
    if kind == "scenario.drafted":
        previous = latest_record(snapshot, candidate["cid"], kind)
        if previous and payload["draft"] <= previous["data"]["draft"]:
            raise ValidationError("下書き番号が古いか重複しています")
        validate_hash(payload.get("input_sha256"))
        validate_text(payload.get("model"), "モデル", 100)
        if type(payload.get("budget")) not in (int, float) or not 0 < payload["budget"] < float(
            "inf"
        ):
            raise ValidationError("費用の上限が正しくありません")
        timestamp(payload.get("ts"))
    elif kind == "scenario.approved":
        draft = latest_record(snapshot, candidate["cid"], "scenario.drafted")
        if (
            not draft
            or payload.get("draft_eid") != draft["eid"]
            or payload["draft"] != draft["data"]["draft"]
        ):
            raise ValidationError("最新の下書きを承認してください")
        if payload["files"].keys() != draft["data"]["files"].keys():
            raise ValidationError("下書きのファイル構成が変わっています")
        validate_answers(payload.get("answers"), payload.get("human_written_lines"))
    elif kind == "scenario.suite_verified":
        approval = latest_record(snapshot, candidate["cid"], "scenario.approved")
        if (
            candidate["state"] not in {"scenario_approved", "improving"}
            or not approval
            or payload.get("approval_eid") != approval["eid"]
            or payload["files"] != approval["data"]["files"]
        ):
            raise ValidationError("現在の承認と一致するスイート確認が必要です")
        validate_hash(payload.get("suite_hash"))
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(payload.get("commit"))):
            raise ValidationError("コミットの形式が正しくありません")
    elif kind == "scenario.retired":
        files = payload.get("files")
        approved = {
            name
            for item in snapshot["records"].values()
            if item["type"] == "scenario.approved" and item["cid"] == candidate["cid"]
            for name in item["data"]["files"]
        }
        if (
            payload.keys() != {"files", "pr_url", "merge_commit"}
            or not isinstance(files, list)
            or not files
            or any(
                not isinstance(name, str)
                or not re.fullmatch(r"sevo-[a-z0-9-]+\.yaml", name)
                or name not in approved
                for name in files
            )
            or len(set(files)) != len(files)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(payload.get("merge_commit")))
        ):
            raise ValidationError("外す対象は、この候補で承認したシナリオを指定してください")
        if not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*",
            str(payload.get("pr_url")),
        ):
            raise ValidationError("シナリオを外した PR の URL が必要です")
    require_id(record["cid"], "C")
