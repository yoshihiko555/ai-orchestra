"""orchestra-manager.py の `proxy stop` / `proxy status` サブコマンドテスト。

対象観点（docs/evaluation/cocoindex.md, docs/evaluation/orchex-cli.md）:
- EV-10（should）: proxy の手動停止は `orchestra-manager.py proxy stop --project .`
  の実行によってのみ行われる（hook からは停止されない）。
- EV-29（should, orchex-cli）: `proxy stop`/`proxy status` の cocoindex 未導入判定は
  `.claude/orchestra.json` の `installed_packages` を基準に行う（Issue #236）。
  config ファイルの発見可否だけに頼ると、AI_ORCHESTRA_DIR が ai-orchestra
  リポジトリ自体を指す通常構成ではベース設定が常に発見され、未導入プロジェクト
  でもエラー分岐が発火しない既知のギャップがあった。
"""

from __future__ import annotations

import json
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


def _seed_cocoindex_installed(project_dir: Path) -> None:
    """cocoindex が installed_packages に記録済みのプロジェクト状態を seed する。"""
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "orchestra.json").write_text(
        json.dumps({"installed_packages": ["cocoindex"]}), encoding="utf-8"
    )


class TestProxyStop:
    def test_stops_running_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _seed_cocoindex_installed(tmp_path)
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
        _seed_cocoindex_installed(tmp_path)
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
        """cocoindex はインストール済みだが config が発見できない場合もエラー終了する。"""
        _seed_cocoindex_installed(tmp_path)
        manager = _make_manager()
        _hook_common, _proxy_manager = manager._load_proxy_modules()
        monkeypatch.setattr(_hook_common, "load_package_config", lambda *_args, **_kwargs: {})

        with pytest.raises(SystemExit):
            manager.proxy_stop(str(tmp_path))

    def test_stop_proxy_failure_exits_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """is_proxy_running=True かつ stop_proxy=False の場合はエラー終了する。"""
        _seed_cocoindex_installed(tmp_path)
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

    def test_not_installed_exits_with_error_even_if_config_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Issue #236: installed_packages に無ければ config が発見できてもエラー終了する。"""
        manager = _make_manager()
        _hook_common, _proxy_manager = manager._load_proxy_modules()
        # config は発見できる状態でも、installed_packages に cocoindex が無ければエラー
        monkeypatch.setattr(
            _hook_common, "load_package_config", lambda *_args, **_kwargs: SAMPLE_CONFIG
        )

        with pytest.raises(SystemExit) as exc_info:
            manager.proxy_stop(str(tmp_path))

        assert exc_info.value.code == 1
        assert "インストールされていません" in capsys.readouterr().err


class TestProxyStatus:
    def test_not_installed_exits_with_error_even_if_config_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Issue #236: proxy_status も installed_packages を基準に判定する。"""
        manager = _make_manager()
        _hook_common, _proxy_manager = manager._load_proxy_modules()
        monkeypatch.setattr(
            _hook_common, "load_package_config", lambda *_args, **_kwargs: SAMPLE_CONFIG
        )

        with pytest.raises(SystemExit) as exc_info:
            manager.proxy_status(str(tmp_path))

        assert exc_info.value.code == 1
        assert "インストールされていません" in capsys.readouterr().err

    def test_shows_status_when_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _seed_cocoindex_installed(tmp_path)
        manager = _make_manager()
        _hook_common, proxy_manager = manager._load_proxy_modules()

        monkeypatch.setattr(
            _hook_common, "load_package_config", lambda *_args, **_kwargs: SAMPLE_CONFIG
        )
        monkeypatch.setattr(
            proxy_manager,
            "get_proxy_config",
            lambda *_: {"host": "127.0.0.1", "port": 8792},
        )
        monkeypatch.setattr(
            proxy_manager,
            "get_proxy_state",
            lambda *_: {"proxy_state": "ready", "pid": 123, "child_pid": None, "inner_port": None},
        )
        monkeypatch.setattr(proxy_manager, "resolve_pid_path", lambda *_: tmp_path / "pid")
        monkeypatch.setattr(proxy_manager, "_read_pid", lambda *_: None)

        manager.proxy_status(str(tmp_path))

        out = capsys.readouterr().out
        assert "稼働中" in out
        assert "123" in out
