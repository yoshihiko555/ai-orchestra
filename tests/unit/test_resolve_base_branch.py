"""`resolve_base_branch.py` の解決ロジックのテスト。

解決優先順位:
    1. --base 明示指定
    2. 環境変数 AI_ORCHESTRA_BASE_BRANCH
    3. 自動推定（merge-base 距離で親を選択）
    4. Fallback: "main"
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "packages" / "git-workflow" / "scripts" / "resolve_base_branch.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("resolve_base_branch", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = _load_module()


def _git(args: list[str], cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env=env,
    )


def _write_commit(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(["add", filename], repo)
    _git(["commit", "-q", "-m", message], repo)


@pytest.fixture(autouse=True)
def _unset_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """実行環境に AI_ORCHESTRA_BASE_BRANCH が設定されていると auto-detect テストが
    環境変数の値で上書きされるため、毎テスト開始時に必ず削除する。"""
    monkeypatch.delenv(resolver.ENV_VAR, raising=False)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """main ブランチ + 初回コミット済みの一時リポジトリを作成する。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "--initial-branch=main", "--template="], repo)
    _write_commit(repo, "README.md", "# test\n", "init")
    return repo


# ---- 明示指定 ----


def test_explicit_base_has_highest_priority(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv(resolver.ENV_VAR, "stage")
    assert resolver.resolve(explicit="develop", cwd=git_repo) == "develop"


def test_explicit_base_strips_origin_prefix(git_repo: Path) -> None:
    assert resolver.resolve(explicit="origin/staging", cwd=git_repo) == "staging"


# ---- 環境変数 ----


def test_env_var_used_when_no_explicit(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv(resolver.ENV_VAR, "stage")
    assert resolver.resolve(cwd=git_repo) == "stage"


def test_env_var_strips_origin_prefix(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv(resolver.ENV_VAR, "origin/stage")
    assert resolver.resolve(cwd=git_repo) == "stage"


def test_empty_env_var_is_ignored(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv(resolver.ENV_VAR, "   ")
    # main のみのリポジトリなので自動推定 or fallback で main になる
    assert resolver.resolve(cwd=git_repo) == "main"


# ---- 自動推定 ----


def test_auto_detect_main_only(git_repo: Path) -> None:
    """main から切った feature branch は main を選ぶ。"""
    _git(["checkout", "-q", "-b", "feat/foo"], git_repo)
    _write_commit(git_repo, "foo.txt", "foo\n", "add foo")
    assert resolver.resolve(cwd=git_repo) == "main"


def test_auto_detect_main_plus_stage_branched_from_stage(git_repo: Path) -> None:
    """stage から切った feature branch は stage を選ぶ。"""
    # main にもう 1 コミット（stage と main が diverge するのを模す）
    _write_commit(git_repo, "m.txt", "main only\n", "main-side")
    # stage を作り、別コミットを乗せる
    _git(["checkout", "-q", "-b", "stage", "HEAD~1"], git_repo)
    _write_commit(git_repo, "s.txt", "stage only\n", "stage-side")
    # stage から feature branch
    _git(["checkout", "-q", "-b", "feat/foo"], git_repo)
    _write_commit(git_repo, "foo.txt", "foo\n", "feat")

    assert resolver.resolve(cwd=git_repo) == "stage"


def test_auto_detect_main_plus_stage_branched_from_main(git_repo: Path) -> None:
    """main+stage がある環境でも main から切ったブランチは main を選ぶ。"""
    # stage を main から作る
    _git(["checkout", "-q", "-b", "stage"], git_repo)
    _write_commit(git_repo, "s.txt", "stage only\n", "stage-side")
    # main に戻して main 先端から feature branch を作る
    _git(["checkout", "-q", "main"], git_repo)
    _write_commit(git_repo, "m.txt", "main only\n", "main-side")
    _git(["checkout", "-q", "-b", "feat/foo"], git_repo)
    _write_commit(git_repo, "foo.txt", "foo\n", "feat")

    assert resolver.resolve(cwd=git_repo) == "main"


def test_auto_detect_prefers_stage_on_tie(git_repo: Path) -> None:
    """main と stage が同一コミットを指す場合、候補リストの先頭優先で stage を選ぶ。"""
    # main から stage を作る（同一コミット、divergence なし）
    _git(["checkout", "-q", "-b", "stage"], git_repo)
    # stage から feature branch を作る（これも同一コミット）
    _git(["checkout", "-q", "-b", "feat/foo"], git_repo)
    _write_commit(git_repo, "foo.txt", "foo\n", "feat")

    # main と stage は同じコミットを指す（distance 0 で同値）
    # CANDIDATES 先頭優先で stage が選ばれる
    assert resolver.resolve(cwd=git_repo) == "stage"


def test_auto_detect_excludes_current_branch(git_repo: Path) -> None:
    """現在ブランチが候補名と一致していても除外され、別の候補が選ばれる。"""
    # main から develop / stage を順に作成する（全て同一コミット）
    _git(["checkout", "-q", "-b", "develop"], git_repo)
    _git(["checkout", "-q", "-b", "stage"], git_repo)
    # stage にいる状態で resolve
    # 除外ロジックが効かないと tie-break で stage (CANDIDATES 先頭近く) が返る。
    # 除外ロジックが効くと stage が候補から外れ、次に近い develop が選ばれる。
    assert resolver.resolve(cwd=git_repo) == "develop"


# ---- Fallback ----


def test_fallback_when_no_candidates(tmp_path: Path) -> None:
    """候補ブランチが 1 つも存在しない場合は fallback "main" を返す。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "--initial-branch=feat/alone", "--template="], repo)
    _write_commit(repo, "README.md", "# test\n", "init")

    assert resolver.resolve(cwd=repo) == "main"


# ---- CLI ----


def test_cli_prints_resolved_branch(git_repo: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--base", "release"],
        cwd=str(git_repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "release"


def test_cli_cwd_argument(git_repo: Path, tmp_path: Path) -> None:
    _git(["checkout", "-q", "-b", "feat/foo"], git_repo)
    _write_commit(git_repo, "foo.txt", "foo\n", "feat")
    # 別ディレクトリから --cwd で対象 repo を指定
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--cwd", str(git_repo)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "main"
