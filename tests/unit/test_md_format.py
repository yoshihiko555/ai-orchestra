"""生成 Markdown の prettier 整形（scripts/lib/md_format.py）のテスト。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.module_loader import load_module

md_format = load_module("md_format", "scripts/lib/md_format.py")


class _PrettierStub:
    """prettier 呼び出しを記録するスタブ。

    md_format は候補選別の `--version` と本番の `--write` を分けて実行するため、
    スタブも両者を区別して応答する。
    """

    def __init__(self, *, probe: Any = None, write: Any = None) -> None:
        self.probe = probe if probe is not None else _completed(0, stdout="3.9.6")
        self.write = write if write is not None else _completed(0)
        self.probe_calls: list[list[str]] = []
        self.write_calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        is_probe = "--version" in cmd
        (self.probe_calls if is_probe else self.write_calls).append(cmd)
        result = self.probe if is_probe else self.write
        if isinstance(result, Exception):
            raise result
        if isinstance(result, list):
            index = min(len(self.probe_calls) - 1, len(result) - 1)
            picked = result[index]
            if isinstance(picked, Exception):
                raise picked
            return picked
        return result


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture()
def markdown_file(tmp_path: Path) -> Path:
    path = tmp_path / "doc.md"
    path.write_text("# title\n", encoding="utf-8")
    return path


class TestFormatMarkdownFiles:
    def test_batches_all_paths_into_one_invocation(
        self, tmp_path: Path, markdown_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        second = tmp_path / "other.md"
        second.write_text("# other\n", encoding="utf-8")
        stub = _PrettierStub()
        monkeypatch.setattr(md_format.subprocess, "run", stub)

        assert md_format.format_markdown_files([markdown_file, second], tmp_path) is True
        assert len(stub.write_calls) == 1
        assert str(markdown_file) in stub.write_calls[0]
        assert str(second) in stub.write_calls[0]

    def test_skips_launcher_that_cannot_run_prettier(
        self, tmp_path: Path, markdown_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `pnpm exec` は prettier が無いと ERR_PNPM_RECURSIVE_EXEC_NO_PACKAGE で
        # 非ゼロ終了する。終了コードだけでは「整形失敗」と区別できないため、
        # --version による選別で弾けることを確認する。
        stub = _PrettierStub(
            probe=[
                _completed(1, stderr="ERR_PNPM_RECURSIVE_EXEC_NO_PACKAGE"),
                _completed(0, stdout="3.9.6"),
            ]
        )
        monkeypatch.setattr(md_format.subprocess, "run", stub)

        assert md_format.format_markdown_files([markdown_file], tmp_path) is True
        assert len(stub.probe_calls) == 2
        assert len(stub.write_calls) == 1

    def test_skips_launcher_that_is_not_installed(
        self, tmp_path: Path, markdown_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _PrettierStub(probe=[FileNotFoundError(), _completed(0, stdout="3.9.6")])
        monkeypatch.setattr(md_format.subprocess, "run", stub)

        assert md_format.format_markdown_files([markdown_file], tmp_path) is True
        assert len(stub.write_calls) == 1

    def test_returns_false_and_warns_when_prettier_unavailable(
        self,
        tmp_path: Path,
        markdown_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stub = _PrettierStub(probe=FileNotFoundError())
        monkeypatch.setattr(md_format.subprocess, "run", stub)

        assert md_format.format_markdown_files([markdown_file], tmp_path) is False
        assert stub.write_calls == []
        assert "prettier not found" in capsys.readouterr().err

    def test_stops_without_retrying_when_prettier_itself_fails(
        self,
        tmp_path: Path,
        markdown_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # パースエラーは候補を変えても同じ結果になるため、次の候補は試さない。
        stub = _PrettierStub(write=_completed(2, stderr="SyntaxError: unexpected token"))
        monkeypatch.setattr(md_format.subprocess, "run", stub)

        assert md_format.format_markdown_files([markdown_file], tmp_path) is False
        assert len(stub.write_calls) == 1
        captured = capsys.readouterr().err
        assert "prettier failed" in captured
        assert "SyntaxError" in captured

    def test_stops_without_retrying_on_timeout(
        self,
        tmp_path: Path,
        markdown_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # 別候補が別バージョンで書きかけのファイルを続けて整形しないようにする。
        stub = _PrettierStub(write=subprocess.TimeoutExpired(cmd="prettier", timeout=1))
        monkeypatch.setattr(md_format.subprocess, "run", stub)

        assert md_format.format_markdown_files([markdown_file], tmp_path) is False
        assert len(stub.write_calls) == 1
        assert "timed out" in capsys.readouterr().err

    def test_no_invocation_when_no_existing_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _PrettierStub()
        monkeypatch.setattr(md_format.subprocess, "run", stub)

        assert md_format.format_markdown_files([tmp_path / "missing.md"], tmp_path) is True
        assert stub.probe_calls == []
        assert stub.write_calls == []

    def test_never_installs_prettier_implicitly(self) -> None:
        # 暗黙ダウンロードでバージョンが変わると整形結果＝コミット差分がぶれる。
        for prefix in md_format.PRETTIER_COMMAND_PREFIXES:
            if prefix[0] == "npm":
                assert "--no" in prefix
            if prefix[0] == "npx":
                assert "--no-install" in prefix

    def test_total_budget_fits_in_sync_engine_timeout(self) -> None:
        # sync_engine.py が facet build を timeout=30 で起動するため、
        # 選別から整形までの合計がそれを超えると build ごと打ち切られる。
        assert md_format.FORMAT_BUDGET_SEC < 30


class TestFacetBuilderIntegration:
    """FacetBuilder が整形をバッチ化して呼び出すことを検証する。"""

    def test_build_all_formats_once_for_every_generated_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        facet_build = load_module("test_facet_build_helpers", "tests/unit/test_facet_build.py")
        facet_builder = __import__("lib.facet_builder", fromlist=["FacetBuilder"])

        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        facet_build._setup_facet_sources(orchestra_dir)

        calls: list[list[Path]] = []
        monkeypatch.setattr(
            facet_builder,
            "format_markdown_files",
            lambda paths, cwd: calls.append(list(paths)) or True,
        )

        builder = facet_builder.FacetBuilder(orchestra_dir)
        outputs = builder.build_all("claude", project_dir)

        assert len(calls) == 1
        assert set(calls[0]) == set(outputs)
