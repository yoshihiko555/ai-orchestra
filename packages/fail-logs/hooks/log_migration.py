#!/usr/bin/env python3
"""fail-logs の旧 worktree-local ログを root worktree へ移行する。"""

from __future__ import annotations

import os
import shutil

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

        legacy_path = os.path.join(project_dir, legacy_relative_dir, log_file_name)
        if not os.path.isfile(legacy_path):
            return

        destination_path = os.path.join(log_root, legacy_relative_dir, log_file_name)
        if os.path.realpath(destination_path) == os.path.realpath(legacy_path):
            return

        os.makedirs(os.path.dirname(destination_path), mode=LOG_DIR_MODE, exist_ok=True)
        with open(legacy_path, "rb") as source:
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
                shutil.copyfileobj(source, destination)

        os.replace(legacy_path, f"{legacy_path}.migrated")
    except Exception:
        pass
