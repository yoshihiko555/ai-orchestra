"""orchestra-manager.py の `proxy stop` サブコマンドテスト。

対象観点（docs/evaluation/cocoindex.md）:
- EV-10（should）: proxy の手動停止は `orchestra-manager.py proxy stop --project .`
  の実行によってのみ行われる（hook からは停止されない）。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.module_loader import REPO_ROOT, load_module

manager_mod = load_module("orchestra_manager_proxy", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager

SAMPLE_CONFIG: dict = {
    "enabled": True,
    "server_name": "cocoindex-code",
    "command": "uvx",
    "args": [],
    "targets": {},
    "proxy": {"enabled": True},
}


def _make_manager() -> OrchestraManager:
    """実際の cocoindex hook ファイルを import できるよう REPO_ROOT を orchestra_dir にする。"""
    return OrchestraManager(REPO_ROOT)


class TestProxyStop:
    def test_stops_running_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        manager = _make_manager()
        _hook_common, proxy_manager = manager._load_proxy_modules()

        monkeypatch.setattr(
            _hook_common, "load_package_config", lambda *_args, **_kwargs: SAMPLE_CONFIG
        )
        monkeypatch.setattr(proxy_manager, "is_proxy_running", lambda *_: True)
        stop_spy = MagicMock(return_value=True)
        monkeypatch.setattr(proxy_manager, "stop_proxy", stop_spy)

        manager.proxy_stop(str(tmp_path))

        stop_spy.assert_called_once_with(SAMPLE_CONFIG, str(tmp_path.resolve()))
        assert "停止しました" in capsys.readouterr().out

    def test_noop_when_proxy_not_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        manager = _make_manager()
        _hook_common, proxy_manager = manager._load_proxy_modules()

        monkeypatch.setattr(
            _hook_common, "load_package_config", lambda *_args, **_kwargs: SAMPLE_CONFIG
        )
        monkeypatch.setattr(proxy_manager, "is_proxy_running", lambda *_: False)
        stop_spy = MagicMock(return_value=True)
        monkeypatch.setattr(proxy_manager, "stop_proxy", stop_spy)

        manager.proxy_stop(str(tmp_path))

        stop_spy.assert_not_called()
        assert "停止しています" in capsys.readouterr().out

    def test_missing_cocoindex_config_exits_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _make_manager()
        _hook_common, _proxy_manager = manager._load_proxy_modules()
        monkeypatch.setattr(_hook_common, "load_package_config", lambda *_args, **_kwargs: {})

        with pytest.raises(SystemExit):
            manager.proxy_stop(str(tmp_path))

    def test_stop_proxy_failure_exits_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """is_proxy_running=True かつ stop_proxy=False の場合はエラー終了する。"""
        manager = _make_manager()
        _hook_common, proxy_manager = manager._load_proxy_modules()

        monkeypatch.setattr(
            _hook_common, "load_package_config", lambda *_args, **_kwargs: SAMPLE_CONFIG
        )
        monkeypatch.setattr(proxy_manager, "is_proxy_running", lambda *_: True)
        stop_spy = MagicMock(return_value=False)
        monkeypatch.setattr(proxy_manager, "stop_proxy", stop_spy)

        with pytest.raises(SystemExit) as exc_info:
            manager.proxy_stop(str(tmp_path))

        assert exc_info.value.code == 1
        stop_spy.assert_called_once_with(SAMPLE_CONFIG, str(tmp_path.resolve()))
        assert "停止に失敗しました" in capsys.readouterr().err
