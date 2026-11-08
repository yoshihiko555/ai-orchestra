"""fail-logs の公開記録と CLI から証拠を取り込む。"""

from __future__ import annotations

import copy
import json
import os
import selectors
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import candidates_model as model
import skill_evolution_common as common
from candidates_runs import RunHistory
from candidates_safety import (
    CandidatesError,
    FailLogsUnavailable,
    Masker,
    MaskingUnavailable,
    ValidationError,
    excerpt,
    load_masker,
    open_regular,
    orchestra_directory,
    require_id,
    timestamp,
    validate_directory,
)
from candidates_store import CandidateStore

LESSONS_TIMEOUT_SECONDS = 30
LESSONS_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
FAILURE_LINE_LIMIT_BYTES = 256 * 1024


def _skip(reasons: dict[str, int], source: str, reason: str) -> None:
    key = f"{source}: {reason}"
    reasons[key] = reasons.get(key, 0) + 1


@dataclass
class CandidatesContext:
    project: Path
    root: Path
    orchestra: Path
    config: dict
    store: CandidateStore

    def eligible(self, skill: str) -> bool:
        orchestra_root = Path(common._resolve_log_root_cached(str(self.orchestra)))
        return model.handoff_eligible(
            skill, self.orchestra, own_project=self.root == orchestra_root
        )

    def history(self, months: set[str] | None = None) -> RunHistory:
        return RunHistory.load(self.store.directory, self.root, months)


def build_context(project: str | None = None) -> CandidatesContext:
    directory = Path(project or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    config = common.load_config(str(directory))
    if not config.get("enabled", True):
        raise CandidatesError("skill-evolution は無効です（enabled: false）")
    root = Path(common._resolve_log_root_cached(str(directory)))
    orchestra = orchestra_directory()
    try:
        wait = float(config["ledger"]["lock_wait_seconds"])
        limit = config["ledger"]["excerpt_max_chars"]
        if type(limit) is not int or limit < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("ledger の設定が正しくありません") from exc
    store = CandidateStore(
        root,
        Path(common.logs_dir(str(directory), config)),
        lock_wait=wait,
        orchestra=orchestra,
        own_project=root == Path(common._resolve_log_root_cached(str(orchestra))),
    )
    return CandidatesContext(directory, root, orchestra, config, store)


def _failures_path(context: CandidatesContext) -> Path:
    try:
        with open_regular(context.project / ".claude/orchestra.json") as stream:
            installed = json.load(stream)
    except (OSError, ValueError):
        installed = {}
    if (
        not isinstance(installed, dict)
        or not isinstance(installed.get("installed_packages"), list)
        or "fail-logs" not in installed["installed_packages"]
    ):
        raise FailLogsUnavailable("fail-logs が導入されていません")
    core = common._load_hook_common()
    if core is None:
        raise FailLogsUnavailable("fail-logs の設定を読み込めません")
    config = core.load_package_config("fail-logs", "fail-logs.yaml", str(context.project))
    if not config.get("enabled", True):
        raise FailLogsUnavailable("fail-logs は無効です（enabled: false）")
    relative = config.get("logs_dir") or ".claude/logs/fail-logs"
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValidationError("fail-logs.logs_dir は root 内の相対パスで指定してください")
    resolved = core.resolve_path_within(str(context.root), relative, "failures.jsonl")
    if resolved is None:
        raise ValidationError("fail-logs の保存先が root の外を指しています")
    path = context.root / relative / "failures.jsonl"
    if path.parent.exists():
        validate_directory(path.parent, context.root)
    return path


def _lessons_command(context: CandidatesContext, arguments: list[str], *, deadline: float) -> dict:
    script = orchestra_directory() / "packages/fail-logs/scripts/lessons_cli.py"
    if not script.is_file():
        raise FailLogsUnavailable("fail-logs の CLI がありません")
    if time.monotonic() >= deadline:
        raise CandidatesError("fail-logs の CLI 全体の実行が時間切れになりました")
    environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    environment.update(
        AI_ORCHESTRA_DIR=str(orchestra_directory()), CLAUDE_PROJECT_DIR=str(context.project)
    )
    output = bytearray()
    try:
        with subprocess.Popen(
            [sys.executable, str(script), *arguments, "--json"],
            cwd=context.project,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        ) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    selector.register(process.stderr, selectors.EVENT_READ)
                    output_size = 0
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(process.args, LESSONS_TIMEOUT_SECONDS)
                        for key, _ in selector.select(remaining):
                            chunk = os.read(key.fd, 64 * 1024)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            output_size += len(chunk)
                            if output_size > LESSONS_OUTPUT_LIMIT_BYTES:
                                raise CandidatesError("fail-logs の CLI の出力量が上限を超えました")
                            if key.fileobj is process.stdout:
                                output.extend(chunk)
                returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            except BaseException:
                process.kill()
                process.wait()
                raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidatesError(
            "fail-logs の CLI を実行できませんでした（起動失敗または時間切れ）"
        ) from exc
    if returncode == 4:
        raise FailLogsUnavailable("fail-logs が導入されていない、または無効です")
    if returncode == 2:
        raise MaskingUnavailable("fail-logs の伏せ字が使えません")
    if returncode:
        raise CandidatesError("fail-logs の CLI が失敗しました")
    try:
        payload = json.loads(output)
        if isinstance(payload, dict):
            return payload
    except (ValueError, UnicodeError):
        pass
    raise ValidationError("fail-logs の CLI が返した JSON が正しくありません")


def read_lessons(context: CandidatesContext, skipped: dict[str, int]) -> list[dict]:
    deadline = time.monotonic() + LESSONS_TIMEOUT_SECONDS
    lessons = _lessons_command(context, ["list"], deadline=deadline).get("lessons")
    if not isinstance(lessons, list):
        raise ValidationError("fail-logs の候補一覧が正しくありません")
    detailed = []
    for lesson in lessons:
        try:
            if not isinstance(lesson, dict):
                raise ValidationError("fail-logs の教訓候補が正しくありません")
            lid = require_id(lesson.get("lid"), "L")
            # list の概要だけでは根拠の失敗を数えられないので、公開 CLI の詳細を読む。
            if "evidence" not in lesson:
                lesson = _lessons_command(context, ["show", lid], deadline=deadline).get("lesson")
                if not isinstance(lesson, dict) or lesson.get("lid") != lid:
                    raise ValidationError("fail-logs の候補詳細が正しくありません")
            detailed.append(lesson)
        except ValidationError as exc:
            _skip(skipped, "lesson", str(exc))
    return detailed


def read_failures(path: Path, state: dict, skipped: dict[str, int]) -> tuple[list[dict], dict]:
    try:
        with open_regular(path) as stream:
            metadata = os.fstat(stream.fileno())
            offset = state.get("failure_offset", 0)
            identity = f"{metadata.st_dev}:{metadata.st_ino}"
            if (
                type(offset) is not int
                or offset < 0
                or offset > metadata.st_size
                or state.get("failure_source", str(path)) != str(path)
                or state.get("failure_identity", identity) != identity
            ):
                offset = 0
            stream.seek(offset)
            failures = []
            while line := stream.readline(FAILURE_LINE_LIMIT_BYTES + 1):
                if len(line) > FAILURE_LINE_LIMIT_BYTES:
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(FAILURE_LINE_LIMIT_BYTES + 1)
                    offset = stream.tell()
                    _skip(skipped, "failure", "行のバイト数が上限を超えています")
                    continue
                if not line.endswith(b"\n"):
                    break
                offset = stream.tell()
                try:
                    failure = json.loads(line)
                    if not isinstance(failure, dict) or failure.get("type") != "failure":
                        raise ValidationError("失敗の記録の形式が正しくありません")
                    require_id(failure.get("eid"))
                    timestamp(failure.get("ts"))
                    if not isinstance(failure.get("sid"), str) or not common.ID_PATTERN.fullmatch(
                        failure["sid"]
                    ):
                        raise ValidationError("セッション ID が正しくありません")
                    if not isinstance(failure.get("data"), dict):
                        raise ValidationError("失敗の data が正しくありません")
                    failures.append(failure)
                except ValidationError as exc:
                    _skip(skipped, "failure", str(exc))
                except (ValueError, UnicodeError, RecursionError):
                    _skip(skipped, "failure", "JSON の形式が正しくありません")
            return failures, {
                "failure_offset": offset,
                "failure_source": str(path),
                "failure_identity": identity,
            }
    except FileNotFoundError:
        return [], {"failure_offset": 0, "failure_source": str(path)}


def _observation(payload: dict, text: str, masker: Masker, limit: int) -> dict:
    masked, unavailable = excerpt(text, masker, limit)
    if unavailable:
        raise MaskingUnavailable("伏せ字が使えないため取り込みを止めました")
    return model.event(
        "observation.created",
        "O-" + uuid.uuid4().hex[:8],
        {**payload, "excerpt": masked, "excerpt_status": ""},
        actor="ingest",
    )


def failure_observation(failure: dict, history: RunHistory, masker: Masker, limit: int) -> dict:
    details = failure["data"]
    return _observation(
        {
            "evidence": {"kind": "failure", "ref": failure["eid"]},
            "sid": failure["sid"],
            "ts": failure["ts"],
            "failure_kind": details["failure_type"],
            "failure_eids": [failure["eid"]],
            "contexts": [{"kind": "session", "ref": failure["sid"]}],
            **history.attribute_failure(failure["sid"], failure["ts"]),
        },
        details["error_excerpt"],
        masker,
        limit,
    )


def lesson_observation(lesson: dict, masker: Masker, limit: int) -> dict:
    evidence = lesson.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
        raise ValidationError("教訓候補の根拠が正しくありません")
    sessions = set()
    failures = set()
    for item in evidence:
        if not isinstance(item.get("ref"), str):
            raise ValidationError("教訓候補の根拠の参照が正しくありません")
        if item.get("kind") == "session":
            sessions.add(item["ref"])
        failure_eids = item.get("failures", [])
        if not isinstance(failure_eids, list):
            raise ValidationError("教訓候補の根拠の失敗は一覧で指定してください")
        for eid in failure_eids:
            failures.add(require_id(eid))
    at = lesson.get("created_at") or lesson.get("updated_at")
    if not at and evidence:
        at = evidence[0].get("ts")
    timestamp(at)
    return _observation(
        {
            "evidence": {"kind": "lesson", "ref": lesson["lid"]},
            "sid": next(iter(sessions)) if len(sessions) == 1 else None,
            "ts": at,
            "failure_kind": "lesson",
            "failure_eids": sorted(failures),
            "contexts": [{"kind": item.get("kind"), "ref": item.get("ref")} for item in evidence],
            "attribution": "unknown",
            "skill": None,
            "run_id": None,
            "attributed_by": None,
            "conditions": {},
        },
        _lesson_text(lesson),
        masker,
        limit,
    )


def _lesson_text(lesson: dict) -> str:
    parts = [lesson[field] for field in ("title", "event", "cause", "avoidance")]
    if not all(isinstance(part, str) for part in parts):
        raise ValidationError("教訓候補の文は文字列で指定してください")
    return "\n".join(parts)


def ingest(context: CandidatesContext) -> dict:
    path = _failures_path(context)
    masker = load_masker()
    if masker is None:
        raise MaskingUnavailable("伏せ字が使えないため取り込みを止めました")
    skipped: dict[str, int] = {}
    lessons = read_lessons(context, skipped)
    with context.store.locked():
        snapshot = context.store.current_locked()
        initial_count = len(snapshot["observations"])
        failures, position = read_failures(path, context.store.ingest_state(), skipped)
        known = {
            (item["evidence"]["kind"], item["evidence"]["ref"])
            for item in snapshot["observations"].values()
        }
        failures = [failure for failure in failures if ("failure", failure["eid"]) not in known]
        history = RunHistory.for_evidence(context.store.directory, context.root, failures)
        limit = context.config["ledger"]["excerpt_max_chars"]
        records = []
        checked = copy.deepcopy(snapshot)
        for kind, entries in (("failure", failures), ("lesson", lessons)):
            for entry in entries:
                try:
                    ref = entry["eid" if kind == "failure" else "lid"]
                    if (kind, ref) in known:
                        continue
                    record = (
                        failure_observation(entry, history, masker, limit)
                        if kind == "failure"
                        else lesson_observation(entry, masker, limit)
                    )
                    model.apply_event(checked, record)
                except ValidationError as exc:
                    _skip(skipped, kind, str(exc))
                    continue
                except (KeyError, TypeError, ValueError):
                    _skip(skipped, kind, "記録の必須項目または型が正しくありません")
                    continue
                records.append(record)
                known.add((kind, ref))
        updated = context.store.append_locked(snapshot, records)
        context.store.save_ingest_state(position)
    return {
        "ingested": len(updated["observations"]) - initial_count,
        "failure_offset": position["failure_offset"],
        "skipped": sum(skipped.values()),
        "skip_reasons": skipped,
    }
