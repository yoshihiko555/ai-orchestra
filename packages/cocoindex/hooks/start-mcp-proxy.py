#!/usr/bin/env python3
"""mcp-proxy をバックグラウンド helper から同期起動する。"""

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

from hook_common import load_package_config, safe_hook_execution
from proxy_manager import start_proxy


@safe_hook_execution
def main() -> None:
    project_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    if not project_dir:
        return

    config = load_package_config("cocoindex", "cocoindex.yaml", project_dir)
    if not config:
        return

    proxy_cfg = config.get("proxy", {}) or {}
    if not proxy_cfg.get("enabled", False):
        return

    start_proxy(config, project_dir)


if __name__ == "__main__":
    main()
