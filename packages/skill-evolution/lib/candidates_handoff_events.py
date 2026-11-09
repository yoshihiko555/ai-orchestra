"""受け渡しの入力と、遷移に必要な記録の検査。"""

from __future__ import annotations

import re

from candidates_safety import ValidationError, require_id, timestamp
from candidates_scenario_events import latest_record, validate_hash

HANDOFF_EVENTS = frozenset(
    {
        "handoff.proposal_linked",
        "handoff.evaluation_linked",
        "handoff.promotion_linked",
        "handoff.merged",
        "handoff.closed",
        "recurrence.confirmed",
    }
)
EVALUATION_FIELDS = {
    "evaluation_id",
    "cand_id",
    "holdout",
    "own_suite_hash",
    "evaluator_hash",
    "own_run_ids",
    "verdict",
    "evaluation_base_commit",
}


def require_pattern(value: object, pattern: str, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValidationError(f"{field} の形式が正しくありません")
    return value


def meta_candidate_id(value: object) -> str:
    return require_pattern(value, r"cand-[0-9]{8}-[0-9]{6}-[a-z0-9-]+", "meta-harness 候補 ID")


def skill_hash(value: object) -> None:
    if value is not None:
        require_pattern(value, r"sha256:[0-9a-f]{64}", "スキルの版")


def commit_id(value: object) -> str:
    return require_pattern(value, r"(?:[0-9a-f]{40}|[0-9a-f]{64})", "コミット")


def pr_coordinates(value: object) -> tuple[str, int]:
    url = require_pattern(
        value,
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*",
        "PR の URL",
    )
    owner, repo, _, number = url.removeprefix("https://github.com/").split("/")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise ValidationError("PR のリポジトリが正しくありません")
    return f"{owner}/{repo}", int(number)


def validate_evaluations(evaluations: object) -> None:
    if not isinstance(evaluations, list) or not evaluations:
        raise ValidationError("evaluations は空でない一覧が必要です")
    for evaluation in evaluations:
        if not isinstance(evaluation, dict) or evaluation.keys() != EVALUATION_FIELDS:
            raise ValidationError("評価の項目が正しくありません")
        require_pattern(
            evaluation["evaluation_id"], r"eval-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", "評価 ID"
        )
        meta_candidate_id(evaluation["cand_id"])
        commit_id(evaluation["evaluation_base_commit"])
        if type(evaluation["holdout"]) is not bool or evaluation["verdict"] not in (
            "pass",
            "fail",
            "error",
        ):
            raise ValidationError("評価の holdout または verdict が正しくありません")
        for field in ("own_suite_hash", "evaluator_hash"):
            validate_hash(evaluation[field])
        runs = evaluation["own_run_ids"]
        if not isinstance(runs, list) or not runs:
            raise ValidationError("評価の実行 ID は空でない一覧が必要です")
        for run in runs:
            require_pattern(
                run, r"run-[0-9]{8}-[0-9]{6}-[a-z0-9-]+-a[1-9][0-9]*-[0-9a-f]{8}", "実行 ID"
            )


def current_record(snapshot: dict, candidate: dict, kind: str) -> dict | None:
    record = latest_record(snapshot, candidate["cid"], kind)
    return (
        record
        if record and timestamp(record["ts"]) >= timestamp(candidate["state_since"])
        else None
    )


def verified_suite(snapshot: dict, candidate: dict) -> dict:
    verified = latest_record(snapshot, candidate["cid"], "scenario.suite_verified")
    if not verified:
        raise ValidationError("先に verify-suite を実行してください")
    return verified


def promotion_evaluation(snapshot: dict, candidate: dict, mh_cand_id: str) -> dict:
    meta_candidate_id(mh_cand_id)
    proposal = current_record(snapshot, candidate, "handoff.proposal_linked")
    suite_hash = verified_suite(snapshot, candidate)["data"]["suite_hash"]
    if not proposal or proposal["data"]["mh_cand_id"] != mh_cand_id:
        raise ValidationError("現在の改善で link した最新の提案が必要です")
    for record in reversed(list(snapshot["records"].values())):
        if (
            record["cid"] == candidate["cid"]
            and record["type"] == "handoff.evaluation_linked"
            and timestamp(record["ts"]) >= timestamp(candidate["state_since"])
        ):
            evaluation = next(
                (
                    item
                    for item in reversed(record["data"]["evaluations"])
                    if item["cand_id"] == mh_cand_id
                    and item["holdout"] is True
                    and item["own_suite_hash"] == suite_hash
                ),
                None,
            )
            if evaluation is not None:
                if evaluation["verdict"] == "pass":
                    return record
                break
    raise ValidationError(
        "昇格する提案候補の、確認済みスイートと一致する pass の holdout 評価が必要です"
    )


def validate_recurrence_ownership(snapshot: dict, candidate: dict, oid: str) -> None:
    if any(
        item["cid"] != candidate["cid"] and oid in item["observations"]
        for item in snapshot["candidates"].values()
    ):
        raise ValidationError("別の候補に結び付いた観測は再発として確定できません")


def validate_handoff_event(snapshot: dict, candidate: dict, record: dict) -> None:
    payload, kind = record["data"], record["type"]
    if record["actor"] != "human":
        raise ValidationError("受け渡しの操作には人の確認が必要です")
    fields = {
        "handoff.proposal_linked": {"mh_cand_id"},
        "handoff.evaluation_linked": {"evaluations", "skill_hash"},
        "handoff.promotion_linked": {"pr_url", "pr_number", "mh_cand_id"},
        "handoff.merged": {"pr_url", "merge_commit", "skill_hash", "merged_at"},
        "handoff.closed": {"pr_url"},
        "recurrence.confirmed": {"oid", "run_id"},
    }
    expected_state = (
        "promoted"
        if kind == "recurrence.confirmed"
        else "promotion_pr"
        if kind in {"handoff.merged", "handoff.closed"}
        else "improving"
    )
    expected_fields = fields[kind]
    if kind == "handoff.promotion_linked" and "pr_created_at" in payload:
        expected_fields = expected_fields | {"pr_created_at"}
        timestamp(payload["pr_created_at"])
    if payload.keys() != expected_fields or candidate["state"] != expected_state:
        raise ValidationError("受け渡しの項目または候補の状態が正しくありません")
    if "mh_cand_id" in payload:
        meta_candidate_id(payload["mh_cand_id"])
    if "skill_hash" in payload:
        skill_hash(payload["skill_hash"])
    if "pr_url" in payload:
        _, number = pr_coordinates(payload["pr_url"])
        if kind == "handoff.promotion_linked":
            if type(payload["pr_number"]) is not int or payload["pr_number"] != number:
                raise ValidationError("PR 番号が URL と一致しません")
            promotion_evaluation(snapshot, candidate, payload["mh_cand_id"])
        else:
            promotion = latest_record(snapshot, candidate["cid"], "handoff.promotion_linked")
            if not promotion or promotion["data"]["pr_url"] != payload["pr_url"]:
                raise ValidationError("記録した昇格 PR と一致しません")
    if kind == "handoff.evaluation_linked":
        validate_evaluations(payload["evaluations"])
        suite = verified_suite(snapshot, candidate)["data"]["suite_hash"]
        if any(item["own_suite_hash"] != suite for item in payload["evaluations"]):
            raise ValidationError("own_suite_hash が verify-suite の suite_hash と一致しません")
    if kind == "handoff.merged":
        commit_id(payload["merge_commit"])
        timestamp(payload["merged_at"])
    if kind == "recurrence.confirmed":
        oid = require_id(payload["oid"], "O")
        validate_recurrence_ownership(snapshot, candidate, oid)
        require_pattern(payload["run_id"], r"R-[0-9a-f]{12}", "実行範囲 ID")
        observation = snapshot["observations"].get(oid)
        merged = latest_record(snapshot, candidate["cid"], "handoff.merged")
        if (
            not observation
            or observation["dismissed"]
            or observation["attribution"] != "run"
            or observation["skill"] != candidate["skill"]
            or observation["run_id"] != payload["run_id"]
            or not merged
            or merged["data"]["skill_hash"] is None
            or timestamp(observation["ts"]) <= timestamp(merged["data"]["merged_at"])
        ):
            raise ValidationError("マージ後の実行範囲に帰属した観測を指定してください")
