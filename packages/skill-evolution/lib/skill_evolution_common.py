#!/usr/bin/env python3
"""skill-evolution v2 の設定・保存先・スキル版・実行範囲の記録。"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

PACKAGE_NAME = "skill-evolution"
CONFIG_FILENAME = "skill-evolution.yaml"
LOG_ROOT_RESOLUTION_TIMEOUT_SECONDS = 3.5
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600
MAX_STDIN_BYTES = 1024 * 1024
RUNS_TAIL_BYTES = 256 * 1024
RUN_EVENT_LOCK_WAIT_MAX_SECONDS = 2.0
NAME_PATTERN = re.compile(r"[A-Za-z0-9:_.-]{1,64}")
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
PROJECT_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "storage": {
        "dir": ".claude/skill-evolution",  # 非推奨。v2 では使わない。
        "logs_dir": ".claude/logs/skill-evolution",
    },
    "runs": {"hash_max_files": 200, "hash_max_bytes": 1048576},
    "ledger": {"excerpt_max_chars": 300, "lock_wait_seconds": 5},
    "draft": {
        "model": "sonnet",
        "max_budget_usd": 1.0,
        "timeout_seconds": 300,
        "max_files": 4,
        "max_file_bytes": 16384,
        "max_attempts_per_candidate": 3,
    },
    # 非推奨の v1 設定。既存の local.yaml を読めるよう残すが、v2 では使わない。
    "lessons": {"max_lines": 40, "inject_max_chars": 4000},
    "pending": {"stale_after_seconds": 600},
    "offline": {
        "max_iterations": 10,
        "max_cost_usd": 5.0,
        "holdout_ratio": 0.3,
        "stop": {"consecutive": 2, "accuracy_delta_pt": 3, "steps_pct": 10, "time_pct": 15},
        "guards": {"divergence_rounds": 3, "overfit_drop_pt": 15},
    },
    "trigger": {"lessons_threshold": 20},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """override で base を再帰的に上書きした新しい dict を返す。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_core_module(module_name: str) -> Any | None:
    """core/hooks 配下のモジュールを遅延 import する。解決不能時は None。

    探索順（優先度順）: AI_ORCHESTRA_DIR 由来パス → __file__ 相対フォールバック。
    絶対パスかつ実在する候補のみ sys.path を汚染する（cwd 相対の残留を防ぐ）。
    import に失敗した候補は次の候補にフォールバックし、全滅すれば None を返す
    （呼び出し側で fail-safe に扱う）。
    """
    try:
        orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        candidates = []
        if orchestra_dir:
            candidates.append(os.path.join(orchestra_dir, "packages", "core", "hooks"))
        candidates.append(
            os.path.normpath(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..",
                    "..",
                    "core",
                    "hooks",
                )
            )
        )
        for core_hooks in candidates:
            # 絶対パスかつ実在する場合のみ sys.path を汚染する（cwd 相対の残留を防ぐ）。
            if not os.path.isabs(core_hooks) or not os.path.isdir(core_hooks):
                continue
            if core_hooks not in sys.path:
                sys.path.insert(0, core_hooks)
            try:
                return __import__(module_name)
            except Exception:
                continue
    except Exception:
        return None
    return None


def _load_hook_common() -> Any | None:
    """core の hook_common を遅延 import する。解決不能時は None。

    実体は _load_core_module() への薄いラッパー（テスト・呼び出し側の patch シームとして維持）。
    """
    return _load_core_module("hook_common")


def load_config(project_dir: str) -> dict:
    """skill-evolution.yaml を読み込み DEFAULTS にマージする。

    hook_common.load_package_config が使える場合はそれを使い、無ければ DEFAULTS を返す。
    """
    try:
        hook_common = _load_hook_common()
        loaded = (
            hook_common.load_package_config(PACKAGE_NAME, CONFIG_FILENAME, project_dir)
            if hook_common is not None
            else {}
        )
    except Exception:
        loaded = {}
    return _deep_merge(DEFAULTS, loaded or {})


@functools.cache
def _resolve_log_root_cached(project_dir: str) -> str:
    """蓄積データ用 root worktree をプロセス中 1 回だけ解決する。

    単一 project_dir・短命プロセス（1 hook 呼び出し = 1 プロセス）前提のキャッシュ。
    長寿命プロセスから複数 project を切り替えて処理する用途では、キャッシュが
    古い project_dir の解決結果を返し続け陳腐化するため使用しないこと。
    """
    fallback = os.path.abspath(project_dir)
    # `.git` がディレクトリなら通常の checkout（root worktree）なので、git を起動しない。
    # 全ターンで動く hook が毎回 git を呼ぶと、遅い環境で hook の timeout に近づくため。
    if os.path.isdir(os.path.join(fallback, ".git")):
        return fallback
    try:
        hook_common = _load_hook_common()
        if hook_common is None:
            return fallback
        resolved = hook_common.resolve_log_root(
            fallback,
            timeout=LOG_ROOT_RESOLUTION_TIMEOUT_SECONDS,
        )
        return os.path.abspath(resolved) if resolved else fallback
    except Exception:
        return fallback


def _is_path_within_real_base(real_base: str, candidate: str) -> bool:
    """candidate の実体が real_base 配下に収まるかを返す。"""
    real_candidate = os.path.realpath(candidate)
    return real_candidate == real_base or real_candidate.startswith(real_base + os.sep)


def _resolve_logs_subdir(base: str, config: dict | None) -> str:
    """base 配下に storage.logs_dir を安全に解決する。

    symlink 経由で base の外を指す設定も realpath 解決で検出し、既定値へ
    フォールバックする（字句比較の abspath/commonpath では symlink 経由の脱出を
    検出できないため）。既定値も base 外へ解決される場合は、安全な保存先がないため
    ValueError を送出する。hook 呼び出しでは core の `safe_hook_execution` が例外を捕捉して
    そのターンの書き込みだけを fail-open でスキップし、同じ保護がない CLI 呼び出し
    では安全でない保存先へ黙って書かず、明示的なエラーとして表面化する。
    """
    cfg = config or {}
    rel = (cfg.get("storage") or {}).get("logs_dir") or DEFAULTS["storage"]["logs_dir"]
    real_base = os.path.realpath(base)
    candidate = os.path.abspath(os.path.join(base, rel))
    if not _is_path_within_real_base(real_base, candidate):
        candidate = os.path.abspath(os.path.join(base, DEFAULTS["storage"]["logs_dir"]))
        if not _is_path_within_real_base(real_base, candidate):
            raise ValueError("storage.logs_dir の安全な保存先を base 配下に解決できません")
    return candidate


def logs_dir(project_dir: str, config: dict | None = None) -> str:
    """実行範囲記録用の root worktree 解決済みルートを返す。

    config の storage.logs_dir が解決済み root の外（symlink 経由含む）を指す場合は
    既定値に戻す。root 解決または hook_common import に失敗した場合は project_dir へ戻る。
    """
    base = _resolve_log_root_cached(os.path.abspath(project_dir))
    return _resolve_logs_subdir(base, config)


def valid_name(value: object) -> bool:
    return isinstance(value, str) and NAME_PATTERN.fullmatch(value) is not None


def valid_scope_input(payload: dict) -> bool:
    for field in ("session_id", "sid", "prompt_id", "tool_use_id", "agent_id"):
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None
        ):
            return False
    agent_type = payload.get("agent_type")
    return agent_type is None or valid_name(agent_type)


def resolve_skill_directory(project_dir: str, skill: str) -> tuple[Path, str] | None:
    if not valid_name(skill) or ":" in skill or skill in {".", ".."}:
        return None
    for base, source in (
        (Path(project_dir), "project_skill_dir"),
        (Path.home(), "user_skill_dir"),
    ):
        directory = base / ".claude" / "skills" / skill
        if directory.is_dir():
            return directory.resolve(), source
    return None


def _excluded_skill_path(relative: PurePosixPath) -> bool:
    return any(part.startswith(".") or part == "__pycache__" for part in relative.parts)


def hash_skill_files(
    files: Iterable[tuple[str, bytes]], *, max_files: int = 200, max_bytes: int = 1048576
) -> str | None:
    """POSIX 相対パスの UTF-8 + NUL + 内容の sha256 生バイトを、パス順に連結してハッシュする。"""
    selected: list[tuple[str, bytes]] = []
    byte_count = 0
    for relative, content in files:
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            return None
        if _excluded_skill_path(path):
            continue
        byte_count += len(content)
        selected.append((path.as_posix(), content))
        if len(selected) > max_files or byte_count > max_bytes:
            return None
    digest = hashlib.sha256()
    for relative, content in sorted(selected):
        digest.update(relative.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    return "sha256:" + digest.hexdigest()


def _walk_skill_files(directory: Path, max_bytes: int) -> Iterator[tuple[str, bytes]]:
    def raise_scan_error(error: OSError) -> None:
        raise error

    for base, directories, filenames in os.walk(
        directory, followlinks=False, onerror=raise_scan_error
    ):
        directories[:] = [
            name
            for name in directories
            if not _excluded_skill_path(PurePosixPath(name))
            and not (Path(base) / name).is_symlink()
        ]
        for name in filenames:
            if _excluded_skill_path(PurePosixPath(name)):
                continue
            path = Path(base) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                yield path.relative_to(directory).as_posix(), stream.read(max_bytes + 1)


def hash_skill_directory(directory: Path, config: dict | None = None) -> str | None:
    try:
        settings = (config or {}).get("runs") or {}
        max_files = int(settings.get("hash_max_files", DEFAULTS["runs"]["hash_max_files"]))
        max_bytes = int(settings.get("hash_max_bytes", DEFAULTS["runs"]["hash_max_bytes"]))
    except (AttributeError, TypeError, ValueError):
        return None
    try:
        if max_files < 0 or max_bytes < 0:
            return None
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            return None
        return hash_skill_files(
            _walk_skill_files(directory, max_bytes),
            max_files=max_files,
            max_bytes=max_bytes,
        )
    except (OSError, ValueError, RuntimeError):
        return None


_EVENT_FIELDS = {
    "run.started": {
        "run_id",
        "sid",
        "prompt_id",
        "skill",
        "trigger",
        "tool_use_id",
        "agent_id",
        "agent_type",
        "model",
        "skill_hash",
        "project",
        "src",
    },
    "run.confirmed": {"sid", "tool_use_id"},
    "turn.started": {"sid", "prompt_id"},
    "turn.stopped": {"sid", "prompt_id"},
    "agent.started": {"sid", "agent_id", "agent_type"},
    "agent.stopped": {"sid", "agent_id"},
}


def build_scope_event(event_type: str, payload: dict) -> dict:
    return {
        "v": 2,
        "ts": datetime.now(UTC).isoformat(),
        "type": event_type,
        **{
            field: payload.get("session_id" if field == "sid" else field)
            for field in _EVENT_FIELDS[event_type]
        },
    }


def _valid_event(event: dict) -> bool:
    fields = _EVENT_FIELDS.get(str(event.get("type")))
    if fields is None or set(event) != fields | {"v", "ts", "type"} or event["v"] != 2:
        return False
    if not valid_scope_input(event):
        return False
    try:
        timestamp = datetime.fromisoformat(event["ts"])
        if timestamp.utcoffset() is None:
            return False
    except (ValueError, TypeError):
        return False
    if event["type"] != "run.started":
        return True
    if not valid_name(event["skill"]):
        return False
    if not isinstance(event["project"], str) or not PROJECT_PATTERN.fullmatch(event["project"]):
        return False
    if not isinstance(event["run_id"], str) or not re.fullmatch(r"R-[0-9a-f]{12}", event["run_id"]):
        return False
    if event["trigger"] not in {"skill_tool", "direct"} or event["model"] is not None:
        return False
    skill_hash = event["skill_hash"]
    if skill_hash is not None and (
        not isinstance(skill_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", skill_hash)
    ):
        return False
    source = event["src"]
    return (
        isinstance(source, dict)
        and set(source) == {"tool_use_id", "agent_id", "model", "skill_hash"}
        and source["model"] == "absent"
        and all(
            source[field] == ("absent" if event[field] is None else "hook_input")
            for field in ("tool_use_id", "agent_id")
        )
        and source["skill_hash"] in {"project_skill_dir", "user_skill_dir", "unresolved"}
        and (source["skill_hash"] == "unresolved") == (skill_hash is None)
        and (event["trigger"] != "direct" or event["tool_use_id"] is None)
    )


def append_run_event(project_dir: str, event: dict, config: dict | None = None) -> bool:
    """検証済みの出来事を UTC 月別の JSONL に追記する。失敗・ロック待ち切れは False。"""
    try:
        config = load_config(project_dir) if config is None else config
        if not config.get("enabled", True) or not _valid_event(event):
            return False
        timestamp = datetime.fromisoformat(event["ts"]).astimezone(UTC)
        directory = logs_dir(project_dir, config)
        os.makedirs(directory, mode=LOG_DIR_MODE, exist_ok=True)
        os.chmod(directory, LOG_DIR_MODE)
        path = os.path.join(directory, f"runs-{timestamp:%Y%m}.jsonl")
        descriptor = os.open(
            path,
            # 末尾の改行を確かめる pread のため読み書き両用で開く（書き込みは O_APPEND で末尾にだけ行う）
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            LOG_FILE_MODE,
        )
        with os.fdopen(descriptor, "ab", buffering=0) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            wait_seconds = float((config.get("ledger") or {}).get("lock_wait_seconds", 5))
            if not math.isfinite(wait_seconds) or wait_seconds < 0:
                return False
            # hook の timeout（manifest で 10 秒）の中に、root の解決（git）と合わせて収める
            wait_seconds = min(wait_seconds, RUN_EVENT_LOCK_WAIT_MAX_SECONDS)
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    time.sleep(min(0.01, remaining))
            try:
                os.fchmod(stream.fileno(), LOG_FILE_MODE)
                payload = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                # 前回の書き込みが途中で失敗して行末に改行が無いとき、次の行がつながらないよう先に改行を足す
                size = os.fstat(stream.fileno()).st_size
                if size and os.pread(stream.fileno(), 1, size - 1) != b"\n":
                    payload = b"\n" + payload
                while payload:
                    written = os.write(stream.fileno(), payload)
                    if written <= 0:
                        return False
                    payload = payload[written:]
                return True
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except (AttributeError, OSError, ValueError, TypeError):
        return False


def record_run_started(
    project_dir: str, payload: dict, skill: str, trigger: str, config: dict
) -> bool:
    if not valid_name(skill) or not valid_scope_input(payload):
        return False
    resolved = resolve_skill_directory(project_dir, skill)
    if trigger == "direct" and resolved is None:
        return False
    skill_hash = hash_skill_directory(resolved[0], config) if resolved else None
    tool_use_id = payload.get("tool_use_id") if trigger == "skill_tool" else None
    event = build_scope_event("run.started", payload)
    event.update(
        run_id="R-" + uuid.uuid4().hex[:12],
        skill=skill,
        trigger=trigger,
        tool_use_id=tool_use_id,
        model=None,
        skill_hash=skill_hash,
        project=Path(_resolve_log_root_cached(os.path.abspath(project_dir))).name,
        src={
            "tool_use_id": "hook_input" if tool_use_id is not None else "absent",
            "agent_id": "hook_input" if payload.get("agent_id") is not None else "absent",
            "model": "absent",
            "skill_hash": resolved[1] if resolved and skill_hash is not None else "unresolved",
        },
    )
    return append_run_event(project_dir, event, config)


def _read_runs_tail(path: Path) -> list[dict]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return []
            size = os.fstat(stream.fileno()).st_size
            offset = max(0, size - RUNS_TAIL_BYTES)
            stream.seek(max(0, offset - 1))
            previous = stream.read(1) if offset else b"\n"
            tail = stream.read(RUNS_TAIL_BYTES)
        if offset and previous != b"\n":
            tail = tail.partition(b"\n")[2]
        records = []
        for line in tail.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    records.append(event)
            except (ValueError, UnicodeError):
                continue
        return records
    except OSError:
        return []


def has_started_run(
    project_dir: str, sid: str | None, tool_use_id: str | None, config: dict
) -> bool:
    if sid is None or tool_use_id is None:
        return False
    now = datetime.now(UTC)
    previous = now.replace(day=1) - timedelta(days=1)
    directory = Path(logs_dir(project_dir, config))
    for month in (now, previous):
        for event in _read_runs_tail(directory / f"runs-{month:%Y%m}.jsonl"):
            if (
                event.get("v") == 2
                and event.get("type") == "run.started"
                and event.get("sid") == sid
                and event.get("tool_use_id") == tool_use_id
            ):
                return True
    return False


def execute_hook(handler: Callable[[dict, str, dict], None]) -> None:
    hook_common = _load_hook_common()
    if hook_common is None:
        return

    @hook_common.safe_hook_execution
    def execute() -> None:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        if len(raw) > MAX_STDIN_BYTES:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not valid_scope_input(payload):
            return
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
        config = load_config(project_dir)
        if config.get("enabled", True):
            handler(payload, project_dir, config)

    execute()
