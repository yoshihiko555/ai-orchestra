#!/usr/bin/env python3
"""fail-logs の旧 worktree-local ログを root worktree へ有界に移行する。

移行 claim/completion 名は試行ごとに一意にし、クラッシュで残った
``.migrating.*`` は上書き・削除せず、手動での確認と復旧対象として残す。
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sys
import time

# --- sys.path 設定（core/hooks を解決してから import する）---------------------
_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
_repo_core_hooks = os.path.abspath(os.path.join(_hook_dir, "..", "..", "core", "hooks"))
for _candidate in [
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    _repo_core_hooks,
]:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from hook_common import resolve_path_within  # noqa: E402

LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600

# Cap migration copy to the newest ~1 MiB of the legacy log. Readers only
# consume a bounded tail for recurrence summaries, so older history has no
# practical effect there; this prevents SessionStart from stalling on huge logs.
MIGRATION_MAX_BYTES = 1024 * 1024


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

        if os.path.realpath(destination_path) == os.path.realpath(legacy_path):
            return

        claim_suffix = f"{os.getpid()}-{time.monotonic_ns()}"
        migrating_path = f"{legacy_path}.migrating.{claim_suffix}"
        # rename は source を原子的に消すため、競合した別 process は
        # FileNotFoundError となり外側の fail-safe で安全に no-op になる。
        os.rename(legacy_path, migrating_path)

        os.makedirs(os.path.dirname(destination_path), mode=LOG_DIR_MODE, exist_ok=True)
        with open(migrating_path, "rb") as source:
            file_size = os.fstat(source.fileno()).st_size
            if file_size > MIGRATION_MAX_BYTES:
                source.seek(file_size - MIGRATION_MAX_BYTES)
                # 途中行を捨て、次の完全な行から移行する。改行が無ければ EOF に
                # 到達し、後続 copy は空になる（巨大な単一行の決定的な縮退動作）。
                source.readline()

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

        os.rename(migrating_path, f"{legacy_path}.migrated.{claim_suffix}")
    except Exception:
        pass
