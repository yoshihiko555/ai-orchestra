#!/usr/bin/env python3
"""SessionEnd hook: mcp-proxy プロセスを停止する。"""

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
from proxy_manager import stop_proxy


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

    if stop_proxy(config, project_dir):
        print("[cocoindex] mcp-proxy stopped")
    else:
        print("[cocoindex] mcp-proxy stop failed", file=sys.stderr)


if __name__ == "__main__":
    main()
