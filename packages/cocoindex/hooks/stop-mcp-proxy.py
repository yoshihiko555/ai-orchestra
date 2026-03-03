#!/usr/bin/env python3
"""SessionEnd hook: mcp-proxy の状態をログ出力する。

proxy はセッション間で永続化し、次セッションで再利用する。
SessionEnd では停止しない（start_proxy の冪等チェックで管理）。
手動停止: orchestra-manager.py proxy stop --project .
"""

from __future__ import annotations

import os
import sys

# hook_common を import するため core/hooks を sys.path に追加
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

# proxy_manager を import するため自ディレクトリを sys.path に追加
_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if _hooks_dir not in sys.path:
    sys.path.insert(0, _hooks_dir)

from hook_common import load_package_config, read_hook_input, safe_hook_execution
from proxy_manager import is_proxy_running


@safe_hook_execution
def main() -> None:
    data = read_hook_input()
    project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()
    if not project_dir:
        return

    config = load_package_config("cocoindex", "cocoindex.yaml", project_dir)
    if not config:
        return

    proxy_cfg = config.get("proxy", {})
    if not proxy_cfg.get("enabled", False):
        return

    if is_proxy_running(config, project_dir):
        print("[cocoindex] mcp-proxy persisted for next session")


if __name__ == "__main__":
    main()
