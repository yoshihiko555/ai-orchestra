"""教訓候補の CLI・hook が共有する前提（導入の確認・設定・保存先・終了コード）（設計 §10.1・§12）。

呼び出し側は、このモジュールを import する前に ``packages/core/hooks`` と ``packages/fail-logs/lib`` を
``sys.path`` に入れておくこと（``hook_common`` を使うため）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from hook_common import load_package_config, resolve_log_root, resolve_path_within
from lessons_store import LEDGER_FILE, SPOOL_DIR, LessonStore

PACKAGE_NAME = "fail-logs"
CONFIG_FILE = "fail-logs.yaml"
DEFAULT_LOGS_DIR = os.path.join(".claude", "logs", "fail-logs")
ROOT_RESOLVE_TIMEOUT_SECONDS = 3.0

# 終了コード（設計 §10.1。lessons_cli.py / lessons_admin.py 共通）。
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_MASKING = 2
EXIT_SPOOLED = 3
EXIT_DISABLED = 4
EXIT_INVALID = 5

DEFAULT_LESSONS = {
    "add_daily_limit": 20,
    "lock_wait_seconds": 5,
    "present": {
        "enabled": True,
        "threshold": 2,
        "max_items": 5,
        "max_chars": 1500,
        "promoting_stale_days": 14,
    },
}


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def resolve_lessons_config(config: dict) -> dict:
    """``lessons`` の設定を既定値で補って返す。"""
    raw = _as_dict(config.get("lessons"))
    raw_present = _as_dict(raw.get("present"))
    defaults = DEFAULT_LESSONS["present"]
    present = {
        "enabled": raw_present.get("enabled", True) is not False,
        "threshold": _positive_int(raw_present.get("threshold"), defaults["threshold"]),
        "max_items": _positive_int(raw_present.get("max_items"), defaults["max_items"]),
        "max_chars": _positive_int(raw_present.get("max_chars"), defaults["max_chars"]),
        "promoting_stale_days": _positive_int(
            raw_present.get("promoting_stale_days"), defaults["promoting_stale_days"]
        ),
    }
    return {
        "add_daily_limit": _positive_int(
            raw.get("add_daily_limit"), DEFAULT_LESSONS["add_daily_limit"]
        ),
        "lock_wait_seconds": _positive_int(
            raw.get("lock_wait_seconds"), DEFAULT_LESSONS["lock_wait_seconds"]
        ),
        "present": present,
    }


def is_installed(project_dir: str) -> bool:
    """fail-logs が導入されているか（``.claude/orchestra.json`` の ``installed_packages``）。"""
    path = os.path.join(project_dir, ".claude", "orchestra.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    packages = data.get("installed_packages") if isinstance(data, dict) else None
    return isinstance(packages, list) and PACKAGE_NAME in packages


def resolve_project_dir(cwd: str | None = None) -> str:
    """プロジェクトのルート。``CLAUDE_PROJECT_DIR`` → 作業ディレクトリ の順。"""
    return os.environ.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd()


def _logs_dir(config: dict) -> str:
    value = config.get("logs_dir")
    return value if isinstance(value, str) and value else DEFAULT_LOGS_DIR


def resolve_log_dir(base_dir: str, config: dict) -> str | None:
    """``base_dir`` 配下の fail-logs の保存先（``logs_dir`` が外を指せば既定へ戻す）。"""
    path = resolve_path_within(base_dir, _logs_dir(config), LEDGER_FILE) or resolve_path_within(
        base_dir, DEFAULT_LOGS_DIR, LEDGER_FILE
    )
    return os.path.dirname(path) if path else None


@dataclass
class LessonsContext:
    """CLI・hook が使う解決済みの前提。"""

    project_dir: str
    config: dict
    lessons: dict
    log_root: str
    log_dir: str
    spool_dir: str
    store: LessonStore


def build_context(project_dir: str, *, require_installed: bool = True) -> LessonsContext | None:
    """前提を解決する。導入されていない・無効・保存先が解決できないときは None（終了コード 4）。"""
    if require_installed and not is_installed(project_dir):
        return None
    config = load_package_config(PACKAGE_NAME, CONFIG_FILE, project_dir)
    if config.get("enabled", True) is False:
        return None
    log_root = resolve_log_root(project_dir, timeout=ROOT_RESOLVE_TIMEOUT_SECONDS)
    log_dir = resolve_log_dir(log_root, config)
    local_dir = resolve_log_dir(project_dir, config)
    if log_dir is None or local_dir is None:
        return None
    lessons = resolve_lessons_config(config)
    store = LessonStore(
        log_dir,
        lock_wait_seconds=float(lessons["lock_wait_seconds"]),
        threshold=lessons["present"]["threshold"],
        stale_days=lessons["present"]["promoting_stale_days"],
    )
    return LessonsContext(
        project_dir=project_dir,
        config=config,
        lessons=lessons,
        log_root=log_root,
        log_dir=log_dir,
        spool_dir=os.path.join(local_dir, SPOOL_DIR),
        store=store,
    )
