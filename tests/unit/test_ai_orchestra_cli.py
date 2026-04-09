"""ai_orchestra/cli.py のユニットテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.module_loader import load_module

cli_mod = load_module("ai_orchestra_cli_test", "ai_orchestra/cli.py")


class TestGetOrchestraDir:
    """get_orchestra_dir のテスト。"""

    def test_prefers_packaged_install_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ai_orchestra/packages があればそのディレクトリを返す。"""
        pkg_dir = tmp_path / "site-packages" / "ai_orchestra"
        (pkg_dir / "packages").mkdir(parents=True)
        monkeypatch.setattr(cli_mod, "__file__", str(pkg_dir / "cli.py"))

        assert cli_mod.get_orchestra_dir() == pkg_dir

    def test_falls_back_to_repo_root_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """親ディレクトリ直下の packages を開発モードとして解決する。"""
        repo_root = tmp_path / "repo"
        (repo_root / "packages").mkdir(parents=True)
        monkeypatch.setattr(cli_mod, "__file__", str(repo_root / "ai_orchestra" / "cli.py"))

        assert cli_mod.get_orchestra_dir() == repo_root

    def test_uses_env_var_when_filesystem_layout_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """レイアウトで解決できない場合は環境変数を使う。"""
        env_root = tmp_path / "env-root"
        env_root.mkdir()
        monkeypatch.setattr(cli_mod, "__file__", str(tmp_path / "other" / "ai_orchestra" / "cli.py"))
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(env_root))

        assert cli_mod.get_orchestra_dir() == env_root

    def test_exits_when_no_resolution_path_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """どの解決経路も使えない場合は exit(1) する。"""
        monkeypatch.setattr(cli_mod, "__file__", str(tmp_path / "missing" / "ai_orchestra" / "cli.py"))
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

        with pytest.raises(SystemExit, match="1"):
            cli_mod.get_orchestra_dir()

        captured = capsys.readouterr()
        assert "見つかりません" in captured.err
