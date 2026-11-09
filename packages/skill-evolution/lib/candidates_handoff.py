"""meta-harness の公開 JSON と GitHub PR の確認による受け渡し。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import candidates_model as model
import candidates_scenarios as scenarios
from candidates_git import skill_tree_hash, tree_entries
from candidates_handoff_events import (
    commit_id,
    current_record,
    meta_candidate_id,
    pr_coordinates,
    promotion_evaluation,
    validate_evaluations,
)
from candidates_ingest import CandidatesContext
from candidates_safety import ValidationError, open_regular, timestamp, validate_strings
from candidates_scenario_events import latest_record
from candidates_schema import schema_errors

JSON_LIMIT_BYTES = 1024 * 1024


def gh_read(directory: Path, *arguments: str) -> dict:
    try:
        completed = subprocess.run(
            ["gh", *arguments],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode:
            raise ValidationError("gh の PR 読み取りに失敗しました")
        payload = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("gh の PR 読み取りに失敗しました") from exc
    if not isinstance(payload, dict):
        raise ValidationError("gh の応答が正しくありません")
    validate_strings(payload)
    return payload


def repository(directory: Path) -> str:
    remote = scenarios.git_read(directory, "remote", "get-url", "origin").stdout.strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
        remote,
        re.IGNORECASE,
    )
    if not match or any(part in {".", ".."} for part in match[1].split("/")):
        raise ValidationError("origin は GitHub のリポジトリである必要があります")
    return match[1]


def own_pr(directory: Path, url: object) -> tuple[str, int]:
    repo, number = pr_coordinates(url)
    if repo.casefold() != repository(directory).casefold():
        raise ValidationError("PR はこのリポジトリの URL を指定してください")
    return repo, number


def unique_fields(pairs: list[tuple[str, object]]) -> dict:
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValidationError("JSON の項目が重複しています")
        payload[key] = value
    return payload


def validate_runs(runs: object, orchestra: Path) -> None:
    if not isinstance(runs, list):
        raise ValidationError("runs は実行結果の一覧が必要です")
    with open_regular(orchestra / "packages/meta-harness/schemas/result.schema.json") as stream:
        schema = json.load(stream)
    with open_regular(
        orchestra / "packages/meta-harness/schemas/ledger.event.schema.json"
    ) as stream:
        ledger_schema = json.load(stream)
    schema["$defs"]["cost"] = ledger_schema["$defs"]["cost"]
    schema["properties"]["cost"] = {"$ref": "#/$defs/cost"}
    for run in runs:
        if schema_errors(run, schema):
            raise ValidationError("実行結果の項目・型が正しくありません")
        meta_candidate_id(run["cand_id"])


def load_link_payload(path: Path, orchestra: Path) -> dict:
    try:
        with open_regular(path) as stream:
            if os.fstat(stream.fileno()).st_size > JSON_LIMIT_BYTES:
                raise ValidationError("link の JSON が 1 MiB を超えています")
            raw = stream.read(JSON_LIMIT_BYTES + 1)
        if len(raw) > JSON_LIMIT_BYTES:
            raise ValidationError("link の JSON が 1 MiB を超えています")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValidationError("link は symlink でない通常ファイルの UTF-8 JSON が必要です") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValidationError("成功した meta-harness の JSON オブジェクトが必要です")
    validate_strings(payload)
    if payload.keys() == {"status", "cand_id"}:
        meta_candidate_id(payload["cand_id"])
    elif payload.keys() == {"status", "runs", "evaluations"}:
        validate_evaluations(payload["evaluations"])
        if len({item["evaluation_base_commit"] for item in payload["evaluations"]}) != 1:
            raise ValidationError("すべての評価の evaluation_base_commit が一致する必要があります")
        validate_runs(payload["runs"], orchestra)
        run_candidates: dict[str, set[str]] = {}
        for run in payload["runs"]:
            run_candidates.setdefault(run["run_id"], set()).add(run["cand_id"])
        for evaluation in payload["evaluations"]:
            if any(
                run_candidates.get(run_id) != {evaluation["cand_id"]}
                for run_id in evaluation["own_run_ids"]
            ):
                raise ValidationError("評価の実行 ID と候補 ID が runs と一致しません")
    else:
        raise ValidationError("meta-harness の JSON の形を認識できません")
    return payload


def require_state(snapshot: dict, cid: str, state: str) -> dict:
    candidate = model.require_candidate(snapshot, cid)
    if candidate["state"] != state:
        raise ValidationError(f"{state} 状態の候補を指定してください")
    return candidate


def link(
    context: CandidatesContext, cid: str, *, path: Path | None = None, pr: str | None = None
) -> dict:
    if (path is None) == (pr is None):
        raise ValidationError("--from-json または --pr の一方を指定してください")
    payload = load_link_payload(path, context.orchestra) if path is not None else None
    records: list[dict] = []

    def prepare(snapshot: dict) -> list[dict]:
        candidate = require_state(snapshot, cid, "improving")
        canonical = candidate["cid"]
        if payload is not None:
            reference_hash = None
            if "evaluations" in payload:
                commit = payload["evaluations"][-1]["evaluation_base_commit"]
                if (
                    scenarios.git_read(
                        context.project, "cat-file", "-e", f"{commit}^{{commit}}", check=False
                    ).returncode
                    == 0
                ):
                    reference_hash = skill_tree_hash(
                        context.project, candidate["skill"], commit, context.config
                    )
            record = (
                model.event(
                    "handoff.proposal_linked", canonical, {"mh_cand_id": payload["cand_id"]}
                )
                if "cand_id" in payload
                else model.event(
                    "handoff.evaluation_linked",
                    canonical,
                    {
                        "evaluations": payload["evaluations"],
                        "skill_hash": reference_hash,
                    },
                )
            )
            records.append(record)
            return records
        repo, number = own_pr(context.project, pr)
        proposal = current_record(snapshot, candidate, "handoff.proposal_linked")
        if not proposal:
            raise ValidationError("先に提案の JSON を link してください")
        mh_cand_id = proposal["data"]["mh_cand_id"]
        evaluation = promotion_evaluation(snapshot, candidate, mh_cand_id)
        confirmed = gh_read(
            context.project,
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "state,url,files,createdAt",
        )
        actual_repo, actual_number = pr_coordinates(confirmed.get("url"))
        if actual_repo.casefold() != repo.casefold() or actual_number != number:
            raise ValidationError("gh が返した PR の URL が指定と一致しません")
        if confirmed.get("state") not in {"OPEN", "MERGED"}:
            raise ValidationError("昇格 PR は OPEN または MERGED である必要があります")
        timestamp(confirmed.get("createdAt"))
        files = confirmed.get("files")
        prefix = f".claude/skills/{candidate['skill']}/"
        if not isinstance(files, list) or not any(
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and item["path"].startswith(prefix)
            and len(item["path"]) > len(prefix)
            for item in files
        ):
            raise ValidationError("昇格 PR に対象スキルのファイル変更がありません")
        promotion = model.event(
            "handoff.promotion_linked",
            canonical,
            {
                "pr_url": pr,
                "pr_number": number,
                "mh_cand_id": mh_cand_id,
                "pr_created_at": confirmed["createdAt"],
            },
        )
        records.extend(
            [
                promotion,
                scenarios.state_event(
                    candidate, "promotion_pr", refs=[evaluation["eid"], promotion["eid"]]
                ),
            ]
        )
        return records

    context.store.write(prepare)
    return {"events": records}


def inspect_pr(directory: Path, url: str, *, merged: bool) -> tuple[dict, str]:
    repo, number = own_pr(directory, url)
    fields = "state,mergeCommit,mergedAt,url" if merged else "state,mergedAt,url"
    payload = gh_read(directory, "pr", "view", str(number), "--repo", repo, "--json", fields)
    actual_repo, actual_number = pr_coordinates(payload.get("url"))
    if actual_repo.casefold() != repo.casefold() or actual_number != number:
        raise ValidationError("gh が返した PR の URL が記録と一致しません")
    if not merged:
        if payload.get("state") != "CLOSED" or payload.get("mergedAt", False) is not None:
            raise ValidationError("マージされず CLOSED になった PR を指定してください")
        return payload, ""
    if payload.get("state") != "MERGED":
        raise ValidationError("PR が MERGED ではありません")
    timestamp(payload.get("mergedAt"))
    merge = payload.get("mergeCommit")
    commit = commit_id(merge.get("oid") if isinstance(merge, dict) else None)
    default = scenarios.default_branch(directory)
    note = scenarios.TRACKING_NOTE.format(default=default)
    if scenarios.git_read(
        directory, "merge-base", "--is-ancestor", commit, f"origin/{default}", check=False
    ).returncode:
        raise ValidationError("マージのコミットが既定ブランチから辿れません。" + note)
    return payload, note


def confirm_merge(context: CandidatesContext, cid: str) -> dict:
    records: list[dict] = []
    guidance = ""

    def prepare(snapshot: dict) -> list[dict]:
        nonlocal guidance
        candidate = require_state(snapshot, cid, "promotion_pr")
        promotion = latest_record(snapshot, candidate["cid"], "handoff.promotion_linked")
        if not promotion:
            raise ValidationError("昇格 PR の記録がありません")
        url = promotion["data"]["pr_url"]
        payload, guidance = inspect_pr(context.project, url, merged=True)
        commit = payload["mergeCommit"]["oid"]
        merged = model.event(
            "handoff.merged",
            candidate["cid"],
            {
                "pr_url": url,
                "merge_commit": commit,
                "merged_at": payload["mergedAt"],
                "skill_hash": skill_tree_hash(
                    context.project, candidate["skill"], commit, context.config
                ),
            },
        )
        records.extend([merged, scenarios.state_event(candidate, "promoted", refs=[merged["eid"]])])
        return records

    context.store.write(prepare)
    return {"events": records, "guidance": guidance}


def close_pr(context: CandidatesContext, cid: str, note: str = "") -> dict:
    records: list[dict] = []

    def prepare(snapshot: dict) -> list[dict]:
        candidate = require_state(snapshot, cid, "promotion_pr")
        promotion = latest_record(snapshot, candidate["cid"], "handoff.promotion_linked")
        if not promotion:
            raise ValidationError("昇格 PR の記録がありません")
        url = promotion["data"]["pr_url"]
        inspect_pr(context.project, url, merged=False)
        closed = model.event("handoff.closed", candidate["cid"], {"pr_url": url})
        records.extend(
            [closed, scenarios.state_event(candidate, "candidate", refs=[closed["eid"]], note=note)]
        )
        return records

    context.store.write(prepare)
    return {"events": records}


def withdraw(context: CandidatesContext, cid: str, note: str) -> dict:
    records: list[dict] = []

    def prepare(snapshot: dict) -> list[dict]:
        candidate = require_state(snapshot, cid, "improving")
        records.append(scenarios.state_event(candidate, "candidate", note=note))
        return records

    context.store.write(prepare)
    return {"events": records}


def retire_scenario(context: CandidatesContext, cid: str, payload: dict) -> dict:
    if payload.keys() != {"files", "pr"}:
        raise ValidationError("files と pr を指定してください")
    records: list[dict] = []
    guidance = ""

    def prepare(snapshot: dict) -> list[dict]:
        nonlocal guidance
        candidate = model.require_candidate(snapshot, cid)
        files = payload["files"]
        approved = {
            name
            for record in snapshot["records"].values()
            if record["cid"] == candidate["cid"] and record["type"] == "scenario.approved"
            for name in record["data"]["files"]
        }
        if (
            not isinstance(files, list)
            or not files
            or any(
                not isinstance(name, str)
                or not re.fullmatch(r"sevo-[a-z0-9-]+\.yaml", name)
                or name not in approved
                for name in files
            )
            or len(set(files)) != len(files)
        ):
            raise ValidationError("この候補で承認した sevo-*.yaml の一覧を指定してください")
        confirmed, guidance = inspect_pr(context.project, payload["pr"], merged=True)
        commit = confirmed["mergeCommit"]["oid"]
        suite = f"packages/meta-harness/scenarios/skill/{candidate['skill']}/"
        for name in files:
            if scenarios.git_read(
                context.project, "cat-file", "-e", f"{commit}^1:{suite}{name}", check=False
            ).returncode:
                raise ValidationError("外すシナリオがマージのコミットの最初の親にありません")
            if list(tree_entries(context.project, commit, suite + name)):
                raise ValidationError("外すシナリオがマージのコミットに残っています")
        records.append(
            model.event(
                "scenario.retired",
                candidate["cid"],
                {
                    "files": files,
                    "pr_url": payload["pr"],
                    "merge_commit": commit,
                },
            )
        )
        return records

    context.store.write(prepare)
    return {"events": records, "guidance": guidance}
