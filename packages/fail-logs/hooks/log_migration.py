#!/usr/bin/env python3
"""fail-logs の旧 worktree-local ログを root worktree へ有界に移行する。

有界移行の共通部分（claim・行境界決定・確定 rename・stale claim 非破壊）は
core の ``file_migration.migrate_bounded_file`` に委譲する。ここでは境界検証
（``resolve_path_within``）・log_root==project_dir の no-op 判定・実際の書き込み
方式（flock 排他下でのストリームコピー）・fail-open の維持だけを担う。
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sys
from typing import BinaryIO

# --- sys.path 設定（core/hooks を解決してから import する）---------------------
_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
_repo_core_hooks = os.path.abspath(os.path.join(_hook_dir, "..", "..", "core", "hooks"))
# 優先度順（AI_ORCHESTRA_DIR 側が最優先）を維持するため、逆順で insert(0, ...) する。
# 順方向ループで insert(0) すると、後に処理される __file__ 相対フォールバックが
# 先頭に来てしまい優先順位が逆転する。
for _candidate in reversed(
    [
        os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
        _repo_core_hooks,
    ]
):
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from file_migration import migrate_bounded_file  # noqa: E402
from hook_common import resolve_path_within  # noqa: E402

LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600

# Cap migration copy to the newest ~1 MiB of the legacy log. Readers only
# consume a bounded tail for recurrence summaries, so older history has no
# practical effect there; this prevents SessionStart from stalling on huge logs.
MIGRATION_MAX_BYTES = 1024 * 1024


def _copy_stream_writer(source: BinaryIO, destination_path: str) -> None:
    """flock 排他下で source の残りを destination_path へストリームコピーする。

    複数回の write を許容する代わりに flock で排他制御する方式。skill-evolution の
    単発 write writer とは前提が異なるため、両者は意図的に非同期・非共有。
    """
    destination_fd = os.open(
        destination_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        LOG_FILE_MODE,
    )
    try:
        os.fchmod(destination_fd, LOG_FILE_MODE)
    except OSError:
        pass
    with os.fdopen(destination_fd, "ab") as destination:
        fcntl.flock(destination.fileno(), fcntl.LOCK_EX)
        try:
            shutil.copyfileobj(source, destination)
            destination.flush()
        finally:
            fcntl.flock(destination.fileno(), fcntl.LOCK_UN)


def migrate_legacy_worktree_log(
    project_dir: str,
    log_root: str,
    legacy_relative_dir: str,
    log_file_name: str,
) -> None:
    """project_dir 側に残る旧ログを log_root 側へ一回限り移行する。

    log_root と project_dir が同じ場合や旧ログが存在しない場合は何もしない。
    旧ログの新しい方から最大 1 MiB を行境界で切って root 側へ追記し、移行後の
    旧ファイルを一意な ``<name>.migrated.<pid>-<monotonic_ns>`` へリネームする。
    stale な ``.migrating.*`` は触れず、手動での確認・復旧対象として残す。
    例外は握りつぶし、hook を止めない。
    """
    try:
        if os.path.realpath(log_root) == os.path.realpath(project_dir):
            return

        legacy_path = resolve_path_within(project_dir, legacy_relative_dir, log_file_name)
        destination_path = resolve_path_within(log_root, legacy_relative_dir, log_file_name)
        if legacy_path is None or destination_path is None:
            return

        if not os.path.isfile(legacy_path):
            return

        migrate_bounded_file(
            legacy_path,
            destination_path,
            max_bytes=MIGRATION_MAX_BYTES,
            writer=_copy_stream_writer,
        )
    except Exception:
        pass
