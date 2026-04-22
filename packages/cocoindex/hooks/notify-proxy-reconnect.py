#!/usr/bin/env python3
"""proxy ready 後に 1 回だけ reconnect を促す。"""

from __future__ import annotations

import os
import sys

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if _hooks_dir not in sys.path:
    sys.path.insert(0, _hooks_dir)

from hook_common import load_package_config, read_hook_input, safe_hook_execution
from proxy_manager import (
    get_proxy_state,
    mark_session_reconnect_notified,
    read_session_state,
)


@safe_hook_execution
def main() -> None:
    data = read_hook_input()
    project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()
    session_id = str(data.get("session_id") or "")
    if not project_dir or not session_id:
        return

    config = load_package_config("cocoindex", "cocoindex.yaml", project_dir)
    if not config:
        return

    proxy_cfg = config.get("proxy", {}) or {}
    if not (config.get("enabled", False) and proxy_cfg.get("enabled", False)):
        return

    session_state = read_session_state(project_dir, session_id)
    if not session_state.get("reconnect_required", False):
        return
    if session_state.get("reconnect_notified", False):
        return

    proxy_state = get_proxy_state(config, project_dir)
    if proxy_state.get("proxy_state") != "ready":
        return

    mark_session_reconnect_notified(project_dir, session_id)
    print(
        "[cocoindex] mcp-proxy is ready. "
        "このセッションで cocoindex を使うには /mcp で reconnect してください。"
    )


if __name__ == "__main__":
    main()
