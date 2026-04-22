"""notify-proxy-reconnect.py のユニットテスト。"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)

_cocoindex_hooks = str(REPO_ROOT / "packages" / "cocoindex" / "hooks")
_core_hooks = str(REPO_ROOT / "packages" / "core" / "hooks")
for p in [_cocoindex_hooks, _core_hooks]:
    if p not in sys.path:
        sys.path.insert(0, p)

notify_hook = load_module(
    "notify_proxy_reconnect",
    "packages/cocoindex/hooks/notify-proxy-reconnect.py",
)


class TestMain:
    def _invoke(self, payload: dict, monkeypatch: pytest.MonkeyPatch) -> str:
        buffer = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "stdout", buffer)
        notify_hook.main()
        return buffer.getvalue()

    def test_notifies_once_when_proxy_becomes_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify_hook, "load_package_config", lambda *_: {"enabled": True, "proxy": {"enabled": True}})
        monkeypatch.setattr(
            notify_hook,
            "read_session_state",
            lambda *_: {
                "session_id": "sess-1",
                "reconnect_required": True,
                "reconnect_notified": False,
            },
        )
        monkeypatch.setattr(notify_hook, "get_proxy_state", lambda *_: {"proxy_state": "ready"})

        marked: list[tuple[str, str]] = []

        def _mark(project_dir: str, session_id: str) -> dict:
            marked.append((project_dir, session_id))
            return {"reconnect_notified": True}

        monkeypatch.setattr(notify_hook, "mark_session_reconnect_notified", _mark)

        output = self._invoke({"cwd": str(tmp_path), "session_id": "sess-1"}, monkeypatch)

        assert "mcp-proxy is ready" in output
        assert "reconnect" in output
        assert marked == [(str(tmp_path), "sess-1")]

    def test_skips_before_ready(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(notify_hook, "load_package_config", lambda *_: {"enabled": True, "proxy": {"enabled": True}})
        monkeypatch.setattr(
            notify_hook,
            "read_session_state",
            lambda *_: {
                "session_id": "sess-2",
                "reconnect_required": True,
                "reconnect_notified": False,
            },
        )
        monkeypatch.setattr(notify_hook, "get_proxy_state", lambda *_: {"proxy_state": "starting"})
        monkeypatch.setattr(notify_hook, "mark_session_reconnect_notified", lambda *_: {})

        output = self._invoke({"cwd": str(tmp_path), "session_id": "sess-2"}, monkeypatch)

        assert output == ""

    def test_notifies_when_proxy_is_idle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify_hook, "load_package_config", lambda *_: {"enabled": True, "proxy": {"enabled": True}})
        monkeypatch.setattr(
            notify_hook,
            "read_session_state",
            lambda *_: {
                "session_id": "sess-idle",
                "reconnect_required": True,
                "reconnect_notified": False,
            },
        )
        monkeypatch.setattr(notify_hook, "get_proxy_state", lambda *_: {"proxy_state": "idle"})

        marked: list[tuple[str, str]] = []

        def _mark(project_dir: str, session_id: str) -> dict:
            marked.append((project_dir, session_id))
            return {"reconnect_notified": True}

        monkeypatch.setattr(notify_hook, "mark_session_reconnect_notified", _mark)

        output = self._invoke({"cwd": str(tmp_path), "session_id": "sess-idle"}, monkeypatch)

        assert "mcp-proxy is ready" in output
        assert marked == [(str(tmp_path), "sess-idle")]

    def test_skips_when_already_notified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify_hook, "load_package_config", lambda *_: {"enabled": True, "proxy": {"enabled": True}})
        monkeypatch.setattr(
            notify_hook,
            "read_session_state",
            lambda *_: {
                "session_id": "sess-3",
                "reconnect_required": True,
                "reconnect_notified": True,
            },
        )
        monkeypatch.setattr(notify_hook, "get_proxy_state", lambda *_: {"proxy_state": "ready"})

        called = {"mark": False}

        def _mark(*_args) -> dict:
            called["mark"] = True
            return {}

        monkeypatch.setattr(notify_hook, "mark_session_reconnect_notified", _mark)

        output = self._invoke({"cwd": str(tmp_path), "session_id": "sess-3"}, monkeypatch)

        assert output == ""
        assert called["mark"] is False
