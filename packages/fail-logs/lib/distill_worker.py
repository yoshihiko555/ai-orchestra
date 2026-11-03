"""教訓化のワーカー（distill-launch.py が切り離して起動する。設計 docs/design/fail-logs.md §7）。

標準入力で {mode, session_id, transcript_path, project_dir} を受け取り、spool を取り込んでから、
教訓化が有効なら前段判定・上限・ロックを経て教訓化セッション（``claude -p``）を呼び、出力を検査して台帳に入れる。
どの分岐でも、理由付きの実行記録を ``distill-runs.jsonl`` に 1 件残す（起動しない条件は起動役が除く）。
"""

from __future__ import annotations

import fcntl
import fnmatch
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

_lib_dir = os.path.dirname(os.path.abspath(__file__))
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
for _candidate in (
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    os.path.abspath(os.path.join(_lib_dir, "..", "..", "core", "hooks")),
    _lib_dir,
):
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import distill_prompt as prompt
import distill_signals as signals_lib
import lessons_context as ctx
import lessons_model as model
import lessons_store as store_lib
import masking
import transcript_reader as reader
from hook_common import sanitized_git_env
from sanitize import ValidationError

MODE_DISTILL = "distill"
MODE_IMPORT_ONLY = "import_only"
RUNS_FILE = "distill-runs.jsonl"
DAILY_FILE = "distill-daily.json"
LOCK_FILE = "distill.lock"
FAILURES_FILE = "failures.jsonl"
MAX_STDIN_BYTES = 64 * 1024
TAIL_SCAN_BYTES = 2 * 1024 * 1024
MAX_FAILURE_REFS = 50
USER_TEXT_CHARS = 2000
TOOL_TEXT_CHARS = 1500
LOCK_POLL_SECONDS = 0.5
KILL_GRACE_SECONDS = 5
_SESSION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
_MODEL_NAME = re.compile(r"[A-Za-z0-9.\-\[\]]{1,100}")

DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_*",
    "TMPDIR",
    "TERM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_*",
    "CLAUDE_CODE_USE_*",
)
FIXED_CHILD_ENV = {
    "FAIL_LOGS_DISTILL": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}

DEFAULT_DISTILL = {
    "enabled": False,
    "model": "sonnet",
    "language": "ja",
    "include_noninteractive": False,
    "daily_limit": 10,
    "reserve_for_intervention": 2,
    "lock_wait_seconds": 600,
    "timeout_seconds": 300,
    "max_budget_usd": 0.5,
    "max_transcript_bytes": 8 * 1024 * 1024,
    "max_input_chars": 60000,
    "max_known_lessons": 80,
    "max_lessons_per_session": 5,
    "context_before": 6,
    "context_after": 4,
    "repeat_threshold": 2,
    "repeat_threshold_test": 3,
    "reject_markers": list(signals_lib.DEFAULT_REJECT_MARKERS),
    "interrupt_markers": list(signals_lib.DEFAULT_INTERRUPT_MARKERS),
    "correction_cues": list(signals_lib.DEFAULT_CORRECTION_CUES),
    "env_allowlist": list(DEFAULT_ENV_ALLOWLIST),
}


# --- 設定 ---------------------------------------------------------------------


def _str_list(value: object, default: list[str]) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return list(default)


# プロジェクトの設定（fail-logs.local.yaml）でも越えられない上限。悪意のあるリポジトリが費用・時間・メモリを
# 際限なく使わせないため（設計 §12 の受け入れるリスクを有界にする）。
HARD_CAPS: dict[str, float] = {
    "daily_limit": 50,
    "reserve_for_intervention": 50,
    "lock_wait_seconds": 3600,
    "timeout_seconds": 900,
    "max_budget_usd": 2.0,
    "max_transcript_bytes": 32 * 1024 * 1024,
    "max_input_chars": 200_000,
    "max_known_lessons": 200,
    "max_lessons_per_session": 20,
    "context_before": 50,
    "context_after": 50,
    "repeat_threshold": 100,
    "repeat_threshold_test": 100,
}
# 環境変数の許可リストに書ける形（英大文字・数字・`_` と、末尾だけの `*`。全件を渡す `*` などは拒否する）。
_ENV_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}\*?")
_UUID_LIKE = re.compile(r"[0-9A-Za-z-]{8,64}")


def _env_allowlist(value: object) -> list[str]:
    items = _str_list(value, list(DEFAULT_ENV_ALLOWLIST))
    return [item for item in items if _ENV_PATTERN.fullmatch(item)] or list(DEFAULT_ENV_ALLOWLIST)


def resolve_distill_config(config: dict) -> dict:
    """``distill`` の設定を既定値で補って返す。数値はコード側の上限（HARD_CAPS）を越えない。"""
    raw = config.get("distill") if isinstance(config.get("distill"), dict) else {}
    resolved = dict(DEFAULT_DISTILL)
    for key, default in DEFAULT_DISTILL.items():
        value = raw.get(key, default)
        if isinstance(default, bool):
            resolved[key] = value if isinstance(value, bool) else default
        elif isinstance(default, int):
            resolved[key] = (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else default
            )
        elif isinstance(default, float):
            resolved[key] = (
                float(value)
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value > 0
                else default
            )
        elif isinstance(default, list):
            resolved[key] = _str_list(value, default)
        elif isinstance(default, str):
            resolved[key] = value if isinstance(value, str) and value else default
    for key, cap in HARD_CAPS.items():
        if resolved[key] > cap:
            resolved[key] = type(DEFAULT_DISTILL[key])(cap)
    resolved["env_allowlist"] = _env_allowlist(raw.get("env_allowlist"))
    if not _MODEL_NAME.fullmatch(resolved["model"]):
        resolved["model"] = DEFAULT_DISTILL["model"]
    return resolved


# --- 実行記録 -------------------------------------------------------------------


def _append_jsonl(path: str, record: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(
            fd,
            (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _tail_jsonl(path: str, max_bytes: int = TAIL_SCAN_BYTES) -> list[dict]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - max_bytes)
        handle.seek(start)
        chunk = handle.read()
    if start > 0:
        chunk = chunk[chunk.find(b"\n") + 1 :]
    records: list[dict] = []
    for raw in chunk.split(b"\n"):
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


@dataclass
class Run:
    """1 回の実行の記録を組み立てる。"""

    log_dir: str
    sid: str
    model: str
    started_at: float = field(default_factory=time.monotonic)
    signals: dict = field(default_factory=dict)

    def record(self, status: str, reason: str = "", **extra: object) -> None:
        payload = {
            "v": 1,
            "ts": datetime.now(UTC).isoformat(),
            "sid": self.sid,
            "status": status,
            "reason": reason,
            "signals": self.signals,
            "model": self.model,
            "prompt_version": prompt.PROMPT_VERSION,
            "duration_ms": int((time.monotonic() - self.started_at) * 1000),
            **extra,
        }
        try:
            _append_jsonl(os.path.join(self.log_dir, RUNS_FILE), payload)
        except OSError:
            return


@dataclass
class Progress:
    """同じ sid の前回までの進み具合（設計 §7.6）。"""

    offset: int | None
    known_uuids: set[str]
    last_ts: str


def previous_progress(log_dir: str, sid: str) -> Progress:
    """同じ sid の最後に進めた位置と日時、既知の last_uuid の集合（フォークの複写を飛ばすため）。"""
    offset: int | None = None
    last_ts = ""
    uuids: set[str] = set()
    for record in _tail_jsonl(os.path.join(log_dir, RUNS_FILE)):
        last = record.get("last_uuid")
        if isinstance(last, str) and last:
            uuids.add(last)
        value = record.get("transcript_offset")
        if record.get("sid") == sid and isinstance(value, int):
            offset = value
            last_ts = str(record.get("ts", ""))
    return Progress(offset, uuids, last_ts)


# --- 1 日の上限 -------------------------------------------------------------------


def _today() -> str:
    return datetime.now(UTC).astimezone().date().isoformat()


def read_daily(log_dir: str) -> dict:
    try:
        fd = os.open(os.path.join(log_dir, DAILY_FILE), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"date": _today(), "started": 0, "limit_logged": False}
    if not isinstance(data, dict) or data.get("date") != _today():
        return {"date": _today(), "started": 0, "limit_logged": False}
    started = data.get("started")
    if not isinstance(started, int) or isinstance(started, bool) or started < 0:
        # 壊れた・負の回数は、上限に達したものとして扱う（書き換えで上限を外させない）。
        return {"date": _today(), "started": HARD_CAPS["daily_limit"], "limit_logged": True}
    return data


def write_daily(log_dir: str, data: dict) -> None:
    store_lib.atomic_write(os.path.join(log_dir, DAILY_FILE), json.dumps(data))


# --- 抜粋 -------------------------------------------------------------------------


def build_excerpt(
    messages: list[reader.Message],
    found: list[signals_lib.Signal],
    masker: masking.Masker,
    cfg: dict,
) -> list[prompt.ExcerptItem]:
    """合図の前後の窓を取り、伏せてから切り詰め、全体を上限に収める（強い合図の窓を優先）。"""
    windows: list[tuple[int, int, bool]] = []
    for item in found:
        start = max(0, item.index - cfg["context_before"])
        end = min(len(messages) - 1, item.index + cfg["context_after"])
        windows.append((start, end, item.kind in signals_lib.STRONG_SIGNALS))
    windows.sort(key=lambda window: (not window[2], window[0]))
    signal_by_index = {item.index: item.kind for item in found}
    selected: set[int] = set()
    budget = cfg["max_input_chars"]
    rendered: dict[int, prompt.ExcerptItem] = {}
    for start, end, _strong in windows:
        for index in range(start, end + 1):
            if index in selected:
                continue
            message = messages[index]
            limit = USER_TEXT_CHARS if message.role == reader.ROLE_USER else TOOL_TEXT_CHARS
            text = masking.mask_then_truncate(message.text, limit, masker)
            if len(text) > budget:
                break
            budget -= len(text)
            selected.add(index)
            rendered[index] = prompt.ExcerptItem(
                message.role, message.kind, signal_by_index.get(index, ""), text
            )
    return [rendered[index] for index in sorted(rendered)]


# --- 教訓化セッションの呼び出し ---------------------------------------------------


def claude_executable() -> str | None:
    """``CLAUDE_CODE_EXECPATH`` が絶対パスの実行可能ファイルならそれを、無ければ PATH の ``claude``。"""
    candidate = os.environ.get("CLAUDE_CODE_EXECPATH", "")
    if (
        candidate
        and os.path.isabs(candidate)
        and os.path.isfile(candidate)
        and os.access(candidate, os.X_OK)
    ):
        return candidate
    return shutil.which("claude")


def child_env(allowlist: list[str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if any(fnmatch.fnmatchcase(key, pattern) for pattern in allowlist)
    }
    env.update(FIXED_CHILD_ENV)
    return env


def build_command(executable: str, cfg: dict) -> list[str]:
    """教訓化セッションの引数（シェルを介さない配列）（設計 §7.5）。"""
    return [
        executable,
        "-p",
        "--restricted",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--settings",
        json.dumps({"disableAllHooks": True}),
        "--model",
        cfg["model"],
        "--max-budget-usd",
        str(cfg["max_budget_usd"]),
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(prompt.OUTPUT_SCHEMA),
        "--append-system-prompt",
        prompt.instructions(cfg["language"]),
    ]


@dataclass
class CallResult:
    status: str
    reason: str = ""
    output: dict | None = None
    exit_code: int | None = None
    cost_usd: float | None = None


def call_session(command: list[str], stdin_text: str, cfg: dict) -> CallResult:
    """``claude -p`` を空の一時ディレクトリで呼ぶ。時間切れならプロセスグループを止める。"""
    workdir = tempfile.mkdtemp(prefix="fail-logs-distill-")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=workdir,
            env=child_env(cfg["env_allowlist"]),
            start_new_session=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, _ = process.communicate(stdin_text, timeout=cfg["timeout_seconds"])
        except subprocess.TimeoutExpired:
            _kill_group(process)
            return CallResult("failed", "timeout")
        if process.returncode != 0:
            return CallResult("failed", "llm_error", exit_code=process.returncode)
        try:
            payload = json.loads(stdout)
        except ValueError:
            return CallResult("failed", "invalid_output", exit_code=0)
        cost = payload.get("total_cost_usd") if isinstance(payload, dict) else None
        cost_value = float(cost) if isinstance(cost, (int, float)) else None
        if (
            not isinstance(payload, dict)
            or payload.get("is_error") is True
            or payload.get("subtype") != "success"
        ):
            return CallResult("failed", "llm_error", exit_code=0, cost_usd=cost_value)
        output = payload.get("structured_output")
        if not isinstance(output, dict):
            return CallResult("failed", "invalid_output", exit_code=0, cost_usd=cost_value)
        return CallResult("done", output=output, exit_code=0, cost_usd=cost_value)
    except OSError:
        return CallResult("failed", "llm_error")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kill_group(process: subprocess.Popen) -> None:
    """プロセスグループを止める。SIGTERM の後、残った子孫のために SIGKILL も送る。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=KILL_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        return


# --- 出力の検査と台帳への追加 -------------------------------------------------------


def validate_output(
    output: dict,
    masker: masking.Masker,
    max_lessons: int,
    evidence: dict,
    known_ids: frozenset[str] = frozenset(),
) -> list[tuple[str | None, dict]]:
    """出力を形式検査し、(merge_into, data) の列にする。1 件でも形式に合わなければ ValidationError。

    合流先は、教訓化セッションに見せた既存の候補（known_ids）に限る（記録の中の文で任意の候補に合流させない）。
    """
    lessons = output.get("lessons")
    if not isinstance(lessons, list) or len(lessons) > max_lessons:
        raise ValidationError("lessons must be a list within the limit")
    result: list[tuple[str | None, dict]] = []
    for item in lessons:
        if not isinstance(item, dict) or item.get("action") not in ("new", "merge"):
            raise ValidationError("invalid lesson item")
        if not all(key in item for key in ("merge_into", "scope", "severity", "signals")):
            raise ValidationError("lesson item lacks required keys")
        for name, limit in model.TEXT_LIMITS.items():
            if not isinstance(item.get(name), str) or len(item[name]) > limit:
                raise ValidationError(f"{name} exceeds the limit")
        merge_into = item.get("merge_into")
        if item["action"] != "merge" or not model.is_lid(merge_into) or merge_into not in known_ids:
            merge_into = None
        data = model.build_lesson_data(item, masker, source="distill", evidence=evidence)
        result.append((merge_into, data))
    return result


def append_lessons(
    store: store_lib.LessonStore, items: list[tuple[str | None, dict]], sid: str
) -> tuple[list[str], list[str]]:
    """候補を台帳に入れる。merge_into が台帳に無い ID なら新しい候補にする（設計 §7.5）。"""
    new_ids: list[str] = []
    merged_ids: list[str] = []

    def build(snapshot: dict) -> list[dict]:
        events: list[dict] = []
        for merge_into, data in items:
            target = model.resolve_lid(snapshot, merge_into) if merge_into else None
            if target and target in snapshot.get("lessons", {}):
                events.append(model.make_event(model.EV_EVIDENCE, target, "worker", sid, data))
                merged_ids.append(target)
            else:
                lid = model.new_lid()
                events.append(model.make_event(model.EV_CREATED, lid, "worker", sid, data))
                new_ids.append(lid)
        return events

    store.append_events(build)
    return new_ids, merged_ids


# --- 本体 ---------------------------------------------------------------------------


def _session_failures(log_dir: str, sid: str, since_ts: str = "") -> list[dict]:
    """そのセッションの失敗イベント。since_ts より後のものだけ（処理済みの範囲を数え直さない）。"""
    return [
        record
        for record in _tail_jsonl(os.path.join(log_dir, FAILURES_FILE))
        if record.get("sid") == sid and str(record.get("ts", "")) > since_ts
    ]


def _signal_config(cfg: dict, summary: dict) -> signals_lib.SignalConfig:
    expected = summary.get("expected_nonzero_commands") if isinstance(summary, dict) else None
    return signals_lib.SignalConfig(
        reject_markers=tuple(cfg["reject_markers"]),
        interrupt_markers=tuple(cfg["interrupt_markers"]),
        correction_cues=tuple(cfg["correction_cues"]),
        expected_nonzero_commands=frozenset(
            expected if isinstance(expected, list) else signals_lib.DEFAULT_EXPECTED_NONZERO
        ),
        repeat_threshold=max(1, cfg["repeat_threshold"]),
        repeat_threshold_test=max(1, cfg["repeat_threshold_test"]),
    )


@dataclass
class Prepared:
    messages: list[reader.Message]
    found: list[signals_lib.Signal]
    start_offset: int | None
    end_offset: int
    last_uuid: str
    since_ts: str


def prepare(
    transcript_path: object, log_dir: str, sid: str, cfg: dict, summary: dict, run: Run
) -> Prepared | None:
    """前段判定まで（読み取りだけ）。記録が読めない・合図が無ければ、理由を記録して None。"""
    fd = reader.open_transcript(transcript_path, store_lib.transcript_roots())
    if fd is None:
        run.record("skipped", "no_transcript")
        return None
    try:
        progress = previous_progress(log_dir, sid)
        piece = reader.read_slice(fd, progress.offset or 0, cfg["max_transcript_bytes"])
    finally:
        os.close(fd)
    if piece.parsed == 0 and piece.skipped > 0:
        run.record("skipped", "transcript_parse")
        return None
    records = piece.records
    if progress.offset is None:
        records = reader.skip_copied_prefix(records, progress.known_uuids)
    messages = reader.classify(records, frozenset(cfg["interrupt_markers"]))
    failures = _session_failures(log_dir, sid, progress.last_ts)
    # 前回の位置から読むときは、それより前にエージェントが発言している（再開直後の訂正も数える）。
    found = signals_lib.detect(
        messages, failures, _signal_config(cfg, summary), agent_acted_before=bool(progress.offset)
    )
    run.signals = {
        "intervention": sum(1 for item in found if item.kind in signals_lib.STRONG_SIGNALS),
        "machine": sum(1 for item in found if item.kind == signals_lib.SIGNAL_MACHINE),
    }
    last = reader.last_uuid(piece.records)
    last = last if _UUID_LIKE.fullmatch(last) else ""
    if not found:
        run.record("skipped", "no_signal", transcript_offset=piece.end_offset, last_uuid=last)
        return None
    return Prepared(messages, found, progress.offset, piece.end_offset, last, progress.last_ts)


def _acquire(path: str, wait_seconds: int) -> int | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(LOCK_POLL_SECONDS)


def _daily_limit_reached(log_dir: str, cfg: dict, run: Run) -> dict | None:
    """1 日の上限に達していれば、その日の最初の 1 回だけ記録して None。達していなければ回数の状態。"""
    daily = read_daily(log_dir)
    if daily["started"] < cfg["daily_limit"]:
        return daily
    if not daily.get("limit_logged"):
        run.record("skipped", "daily_limit")
        write_daily(log_dir, {**daily, "limit_logged": True})
    return None


def distill(context: ctx.LessonsContext, request: dict, masker: masking.Masker | None) -> None:
    """教訓化の本体（設計 §7.1 の手順 2〜11）。"""
    cfg = resolve_distill_config(context.config)
    sid = str(request.get("session_id") or "")
    run = Run(context.log_dir, sid if _SESSION_ID.fullmatch(sid) else "", cfg["model"])
    if not run.sid:
        run.record("skipped", "no_transcript")
        return
    daily = _daily_limit_reached(context.log_dir, cfg, run)
    if daily is None:
        return
    summary = (
        context.config.get("summary") if isinstance(context.config.get("summary"), dict) else {}
    )
    transcript_path = request.get("transcript_path")
    prepared = prepare(transcript_path, context.log_dir, run.sid, cfg, summary, run)
    if prepared is None:
        return
    if masker is None:
        run.record("skipped", "no_masking")
        return
    remaining = cfg["daily_limit"] - daily["started"]
    if remaining <= cfg["reserve_for_intervention"] and not signals_lib.has_intervention(
        prepared.found
    ):
        run.record("skipped", "reserved")
        return
    executable = claude_executable()
    if executable is None:
        run.record("skipped", "no_executable")
        return
    lock_fd = _acquire(os.path.join(context.log_dir, LOCK_FILE), cfg["lock_wait_seconds"])
    if lock_fd is None:
        run.record("skipped", "locked")
        return
    try:
        # ロックを待つ間に、同じセッションの別の実行（/resume の後の終了など）が位置を進めていたら、読み直す。
        if previous_progress(context.log_dir, run.sid).offset != prepared.start_offset:
            prepared = prepare(transcript_path, context.log_dir, run.sid, cfg, summary, run)
            if prepared is None:
                return
        _distill_locked(context, cfg, run, prepared, masker, executable)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _distill_locked(
    context: ctx.LessonsContext,
    cfg: dict,
    run: Run,
    prepared: Prepared,
    masker: masking.Masker,
    executable: str,
) -> None:
    daily = _daily_limit_reached(context.log_dir, cfg, run)
    if daily is None:
        return
    write_daily(context.log_dir, {**daily, "started": daily["started"] + 1})
    run.record("started")
    progress = {"transcript_offset": prepared.end_offset, "last_uuid": prepared.last_uuid}
    try:
        _call_and_store(context, cfg, run, prepared, masker, executable, progress)
    except Exception:  # noqa: BLE001 - 回数を使った後の想定外の失敗も、終わりの記録を残して位置を進める
        run.record("failed", "llm_error", **progress)


def _call_and_store(
    context: ctx.LessonsContext,
    cfg: dict,
    run: Run,
    prepared: Prepared,
    masker: masking.Masker,
    executable: str,
    progress: dict,
) -> None:
    excerpt = build_excerpt(prepared.messages, prepared.found, masker, cfg)
    snapshot = context.store.read() or model.empty_snapshot()
    excerpt_text = " ".join(item.text for item in excerpt)
    known = prompt.select_known_lessons(snapshot, excerpt_text, cfg["max_known_lessons"])
    stdin_text = prompt.build_input(
        excerpt, known, prompt.new_nonce(), cfg["max_lessons_per_session"]
    )
    result = call_session(build_command(executable, cfg), stdin_text, cfg)
    meta = {
        "input_chars": len(stdin_text),
        "exit_code": result.exit_code,
        "cost_usd": result.cost_usd,
        **progress,
    }
    if result.status != "done" or result.output is None:
        run.record("failed", result.reason, **meta)
        return
    failures = [
        str(record.get("eid"))
        for record in _session_failures(context.log_dir, run.sid, prepared.since_ts)
        if isinstance(record.get("eid"), str)
    ]
    evidence = {"kind": "session", "ref": run.sid, "failures": failures[-MAX_FAILURE_REFS:]}
    try:
        known_ids = frozenset(item["id"] for item in known)
        items = validate_output(
            result.output, masker, cfg["max_lessons_per_session"], evidence, known_ids
        )
    except ValidationError:
        run.record("failed", "invalid_output", **meta)
        return
    try:
        new_ids, merged_ids = append_lessons(context.store, items, run.sid) if items else ([], [])
    except (store_lib.LockTimeout, store_lib.LedgerUnwritable, ValidationError, OSError):
        run.record("failed", "ledger_error", **meta)
        return
    run.record("done", lids_new=new_ids, lids_merged=merged_ids, **meta)


def _import_spool(context: ctx.LessonsContext, masker: masking.Masker | None) -> None:
    if masker is None:
        return
    spool_dirs = []
    for worktree in store_lib.list_worktree_dirs(context.log_root, sanitized_git_env()):
        log_dir = ctx.resolve_log_dir(worktree, context.config)
        if log_dir:
            spool_dirs.append(os.path.join(log_dir, store_lib.SPOOL_DIR))
    try:
        store_lib.import_spool(context.store, spool_dirs, masker)
    except (store_lib.LockTimeout, store_lib.LedgerUnwritable, OSError):
        return


def read_request() -> dict:
    data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(data) > MAX_STDIN_BYTES:
        return {}
    try:
        request = json.loads(data.decode("utf-8"))
    except ValueError:
        return {}
    return request if isinstance(request, dict) else {}


def main() -> int:
    request = read_request()
    project_dir = request.get("project_dir")
    if not isinstance(project_dir, str) or not os.path.isdir(project_dir):
        return 0
    context = ctx.build_context(project_dir, require_installed=False)
    if context is None:
        return 0
    store_lib.ensure_dir(context.log_dir)
    masker = masking.load_masker()
    _import_spool(context, masker)
    if request.get("mode") != MODE_DISTILL:
        return 0
    distill(context, request, masker)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - 切り離した処理。利用者のセッションには影響しない
        sys.exit(0)
