"""meta-harness unit test の共通フィクスチャ・ヘルパー。

CLI レベルのテストは `packages/meta-harness/scripts/meta_harness.py` を
サブプロセスとして起動する。lib レベルのテストは `tests.module_loader.load_module`
経由で `meta_harness_common` / `redaction` を動的ロードする（パッケージディレクトリ名に
ハイフンを含むため通常の import ができない、既存 codex-harness テストと同じ事情）。

このディレクトリには `__init__.py` を置かない（`packages/codex-harness/tests` /
`packages/skill-evolution/tests` と同じ規約）。置いてしまうと pytest の import-mode
（prepend）が本ディレクトリを "tests" パッケージとして解決しようとし、リポジトリ直下の
`tests/`（`tests.module_loader` を提供する本物の "tests" パッケージ）と名前が衝突し、
`ModuleNotFoundError: No module named 'tests.module_loader'` を引き起こす。ヘルパー関数を
モジュールレベル関数ではなく fixture として公開しているのも、テストファイル側から本ファイルを
（パッケージ化なしに）import せずに済ませるため。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
META_HARNESS_SCRIPT = REPO_ROOT / "packages" / "meta-harness" / "scripts" / "meta_harness.py"

_DOCKER_RUNTIME_CLI_MODULE_NAME = "docker_runtime_cli"


def _clear_context_hash_cache() -> None:
    """`context_hash()`（docker-runtime）はプロセス内メモ化される（Issue #307
    review）。meta-harness の Docker lifecycle テスト（`test_scenario_docker.py`
    等）が同じビルドコンテキストパスを跨って古いハッシュを観測しないよう、各
    テストの前後で明示的にクリアする。`sys.modules` を都度検索するのは、
    `docker_runtime_cli` がどのテストファイル経由で（どの順で）読み込まれても
    現在有効なインスタンスに届かせるため。"""
    module = sys.modules.get(_DOCKER_RUNTIME_CLI_MODULE_NAME)
    if module is not None and hasattr(module, "clear_context_hash_cache"):
        module.clear_context_hash_cache()


@pytest.fixture(autouse=True)
def _reset_context_hash_cache() -> Iterator[None]:
    _clear_context_hash_cache()
    yield
    _clear_context_hash_cache()


# Claude Code sandbox では tmp_path（プロジェクト配下扱いされるパス）への `git init` /
# `git worktree add` が Operation not permitted になることがある。sandbox 許可パスを
# 優先して使う（tests/conftest.py の e2e_project フィクスチャと同じ回避策）。
_SANDBOX_TMP = Path("/private/tmp/claude-501")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test",
    "GIT_COMMITTER_EMAIL": "test@test",
}


def _sandbox_safe_base(tmp_path: Path) -> Path:
    """sandbox 許可パスが使えるならそちらを、無ければ pytest の tmp_path を返す。"""
    if _SANDBOX_TMP.is_dir():
        return Path(tempfile.mkdtemp(dir=_SANDBOX_TMP))
    return tmp_path


@pytest.fixture()
def git_project(tmp_path: Path) -> Generator[Path, None, None]:
    """git 初期化済み・1 コミット済みの一時プロジェクトを作る（main root として使う）。"""
    base = _sandbox_safe_base(tmp_path)
    owns_base = base != tmp_path
    try:
        project = base / "project"
        project.mkdir(parents=True, exist_ok=True)
        (project / "README.md").write_text("# meta-harness test project\n", encoding="utf-8")
        # 実リポジトリの .gitignore（`.claude/meta-harness/` を無視）を再現する。無いと `init` 直後の
        # store 作成だけで working tree が dirty 扱いになり、dirty-repo 系テストが意味を失う。
        (project / ".gitignore").write_text(".claude/meta-harness/\n", encoding="utf-8")
        env = {**os.environ, **_GIT_ENV}
        subprocess.run(
            ["git", "init", "-q", "--template="],
            cwd=project,
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"],
            cwd=project,
            check=True,
            capture_output=True,
            env=env,
        )
        yield project
    finally:
        if owns_base:
            shutil.rmtree(base, ignore_errors=True)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """`_GIT_ENV`（決定論的な author/committer identity）を注入して git を実行する。

    グローバル `user.name` / `user.email` が設定されていない CI/sandbox 環境でも
    `git commit` 等が失敗しないようにする（PR #162 レビュー指摘）。
    """
    env = {**os.environ, **_GIT_ENV}
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env
    )


@pytest.fixture()
def git_run() -> Callable[..., subprocess.CompletedProcess[str]]:
    """`_git` ヘルパー（`_GIT_ENV` 注入済み git 実行）を返す fixture。

    test_register.py / test_main_root.py で重複していたローカル `_git` 定義を統合した
    共通ヘルパー（`_GIT_ENV` 未注入だった箇所での `git commit` 失敗を防ぐ）。
    """
    return _git


def _add_feature_worktree(git_project: Path, name: str = "feat-x") -> Path:
    """`git_project` に対する detached HEAD の feature worktree を `.worktrees/<name>/` に作る。"""
    worktrees_root = git_project / ".worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = worktrees_root / name
    _git("worktree", "add", "--detach", str(worktree_dir), "HEAD", cwd=git_project)
    return worktree_dir


@pytest.fixture()
def add_feature_worktree() -> Callable[..., Path]:
    """`_add_feature_worktree` ヘルパーを返す fixture。"""
    return _add_feature_worktree


def _run_meta(
    *args: str,
    project: Path,
    check: bool = False,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """`meta_harness.py` をサブプロセスとして実行する。"""
    cmd = [sys.executable, str(META_HARNESS_SCRIPT), *args, "--project", str(project)]
    env = {**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT), **(env_extra or {})}
    return subprocess.run(
        cmd,
        cwd=str(cwd or project),
        capture_output=True,
        text=True,
        check=check,
        env=env,
        timeout=30,
    )


@pytest.fixture()
def run_meta() -> Callable[..., subprocess.CompletedProcess[str]]:
    """`meta_harness.py` サブプロセス起動ヘルパーを返す fixture。"""
    return _run_meta


def _make_overlay(base_dir: Path, files: dict[str, str]) -> Path:
    overlay_dir = base_dir / "overlay"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = overlay_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return overlay_dir


@pytest.fixture()
def make_overlay() -> Callable[[Path, dict[str, str]], Path]:
    """`facets/**` 配下のファイル群からなる overlay ディレクトリを作るヘルパー fixture。

    引数 `files` は overlay ルート相対パス（例: `"facets/foo/SKILL.md"`）をキーとし、値は
    書き込む内容。
    """
    return _make_overlay


@pytest.fixture()
def default_overlay() -> Callable[[Path], Path]:
    """`register` テストで使い回す最小の有効 overlay を作るヘルパー fixture。"""

    def _default(base_dir: Path) -> Path:
        return _make_overlay(
            base_dir, {"facets/example-facet/SKILL.md": "# example facet\n\ncontent\n"}
        )

    return _default
