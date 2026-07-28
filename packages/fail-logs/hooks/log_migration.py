#!/usr/bin/env python3
"""fail-logs の旧 worktree-local ログを root worktree へ移行する。"""

from __future__ import annotations

import fcntl
import os
import shutil
import sys

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


def migrate_legacy_worktree_log(
    project_dir: str,
    log_root: str,
    legacy_relative_dir: str,
    log_file_name: str,
) -> None:
    """project_dir 側に残る旧ログを log_root 側へ一回限り移行する。

    log_root と project_dir が同じ場合や旧ログが存在しない場合は何もしない。
    旧ログの内容を root 側へ生バイトのまま追記し、移行後の旧ファイルを
    ``<name>.migrated`` へリネームする。例外は握りつぶし、hook を止めない。
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

        migrating_path = f"{legacy_path}.migrating"
        os.rename(legacy_path, migrating_path)

        os.makedirs(os.path.dirname(destination_path), mode=LOG_DIR_MODE, exist_ok=True)
        with open(migrating_path, "rb") as source:
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
                finally:
                    fcntl.flock(destination.fileno(), fcntl.LOCK_UN)

        os.rename(migrating_path, f"{legacy_path}.migrated")
    except Exception:
        pass
