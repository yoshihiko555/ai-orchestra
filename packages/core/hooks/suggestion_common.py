"""任意の相談提案の再帰防止と、セッション単位の通知抑制。"""

from __future__ import annotations

import hashlib
import os


def is_subagent_context(payload: dict) -> bool:
    # agent_type だけでは --agent で起動したメインセッションと区別できない。
    agent_id = payload.get("agent_id")
    return isinstance(agent_id, str) and bool(agent_id.strip())


def claim_suggestion(payload: dict, project_dir: str, namespace: str, target: str) -> bool:
    """同一セッション・対象の通知権を一度だけ取得する。相談成功は記録しない。"""
    if is_subagent_context(payload):
        return False
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return True
    if not project_dir:
        return False

    session_hash = hashlib.sha256(session_id.encode()).hexdigest()
    target_hash = hashlib.sha256(f"{namespace}\0{target}".encode()).hexdigest()
    directory_fd = None
    try:
        # パス検査後の差し替えでも symlink を辿らないよう、各階層を dir_fd で開く。
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(os.path.realpath(project_dir), directory_flags)
        for part in (".claude", "state", "suggestions", session_hash):
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        marker_fd = os.open(
            target_hash,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.close(marker_fd)
        return True
    except (OSError, ValueError):
        # 任意の提案のために、状態保存失敗時の再通知やツール停止は行わない。
        return False
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
