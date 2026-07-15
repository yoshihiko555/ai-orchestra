"""stop-mcp-proxy.py のユニットテスト。

対象観点（docs/evaluation/cocoindex.md）:
- EV-09（must）: proxy.enabled: true のとき、SessionEnd では proxy プロセスを停止せず、
  次セッションで再利用するために起動状態を維持する。
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.module_loader import REPO_ROOT, load_module

os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)

_cocoindex_hooks = str(REPO_ROOT / "packages" / "cocoindex" / "hooks")
_core_hooks = str(REPO_ROOT / "packages" / "core" / "hooks")
for p in [_cocoindex_hooks, _core_hooks]:
    if p not in sys.path:
        sys.path.insert(0, p)

stop_hook = load_module(
    "stop_mcp_proxy",
    "packages/cocoindex/hooks/stop-mcp-proxy.py",
)

import proxy_manager  # noqa: E402  (sys.path 設定後に import する必要がある)

PROXY_CONFIG: dict = {
    "enabled": True,
    "server_name": "cocoindex-code",
    "command": "uvx",
    "args": [],
    "targets": {},
    "proxy": {"enabled": True},
}


class TestMain:
    def _invoke(self, payload: dict, monkeypatch: pytest.MonkeyPatch) -> str:
        buffer = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "stdout", buffer)
        stop_hook.main()
        return buffer.getvalue()

    def test_does_not_import_stop_proxy(self) -> None:
        """stop-mcp-proxy.py は proxy_manager.stop_proxy を（alias import 含め）
        一切 import しない（EV-09）。

        `from proxy_manager import stop_proxy as _stop_proxy` のような alias import は
        `hasattr(stop_hook, "stop_proxy")` では検出できないため、モジュール内の全属性を
        object identity で proxy_manager.stop_proxy と比較する。
        """
        assert not hasattr(stop_hook, "stop_proxy")
        aliased = [
            name for name, value in vars(stop_hook).items() if value is proxy_manager.stop_proxy
        ]
        assert aliased == [], f"stop_proxy が alias import されている: {aliased}"

    def test_main_never_calls_stop_proxy_when_proxy_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """proxy 稼働中に SessionEnd が発火しても stop_proxy() は呼ばれない（EV-09）。

        `import proxy_manager; proxy_manager.stop_proxy(...)` 経由の呼び出しだけでなく、
        `from proxy_manager import stop_proxy as _stop_proxy` のような alias import 経由の
        呼び出しも見逃さないよう、stop_hook モジュール内で元の stop_proxy 関数オブジェクトを
        参照している全属性（alias 名不問）にも同じ spy を当てる。
        """
        monkeypatch.setattr(stop_hook, "load_package_config", lambda *_: PROXY_CONFIG)
        monkeypatch.setattr(stop_hook, "is_proxy_running", lambda *_: True)
        original_stop_proxy = proxy_manager.stop_proxy
        stop_proxy_spy = MagicMock()
        monkeypatch.setattr(proxy_manager, "stop_proxy", stop_proxy_spy)
        for name, value in vars(stop_hook).items():
            if value is original_stop_proxy:
                monkeypatch.setattr(stop_hook, name, stop_proxy_spy)

        output = self._invoke({"cwd": str(tmp_path), "session_id": "sess-persist"}, monkeypatch)

        stop_proxy_spy.assert_not_called()
        assert "persisted for next session" in output

    def test_main_clears_session_state_on_session_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stop_hook, "load_package_config", lambda *_: PROXY_CONFIG)
        monkeypatch.setattr(stop_hook, "is_proxy_running", lambda *_: True)
        monkeypatch.setattr(proxy_manager, "stop_proxy", MagicMock())

        cleared: list[tuple[str, str]] = []

        def _clear(project_dir: str, session_id: str) -> None:
            cleared.append((project_dir, session_id))

        monkeypatch.setattr(stop_hook, "clear_session_state", _clear)

        self._invoke({"cwd": str(tmp_path), "session_id": "sess-clear"}, monkeypatch)

        assert cleared == [(str(tmp_path), "sess-clear")]
