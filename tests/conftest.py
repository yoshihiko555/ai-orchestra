"""E2E テスト共通フィクスチャ。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRA_MANAGER = REPO_ROOT / "scripts" / "orchestra-manager.py"
SYNC_ORCHESTRA = REPO_ROOT / "scripts" / "sync-orchestra.py"

# Claude Code sandbox では tmp_path (プロジェクト配下) への git init が
# Operation not permitted になる。sandbox 許可パスを優先して使う。
_SANDBOX_TMP = Path("/private/tmp/claude-501")


@pytest.fixture()
def orchestra_dir() -> Path:
    """ai-orchestra リポジトリのルートパスを返す。"""
    return REPO_ROOT


@pytest.fixture()
def e2e_project(tmp_path: Path) -> Path:
    """git 初期化済みの一時プロジェクトを作成する。"""
    # sandbox 環境では tmp_path で git init が失敗するため、
    # sandbox 許可パスにフォールバックする
    if _SANDBOX_TMP.is_dir():
        base = Path(tempfile.mkdtemp(dir=_SANDBOX_TMP))
    else:
        base = tmp_path
    project = base / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text("# e2e test\n", encoding="utf-8")
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(
        ["git", "init", "-q", "--template="],
        cwd=project,
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=project,
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=project,
        check=True,
        capture_output=True,
        env=git_env,
    )
    return project


def run_orchex(
    *args: str,
    project: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """orchestra-manager.py をサブプロセスで実行する。"""
    cmd = [sys.executable, str(ORCHESTRA_MANAGER), *args, "--project", str(project)]
    env = {
        **os.environ,
        "AI_ORCHESTRA_DIR": str(REPO_ROOT),
        # setup_env_var() が ~/.claude/settings.json に書き込むため、
        # テスト中は HOME をプロジェクト内に向けてグローバル設定を汚さない
        "HOME": str(project),
    }
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def run_session_start(
    project: Path,
    session_id: str = "test",
) -> subprocess.CompletedProcess[str]:
    """sync-orchestra.py を SessionStart として実行する。"""
    payload = json.dumps({"session_id": session_id, "cwd": str(project)})
    return subprocess.run(
        [sys.executable, str(SYNC_ORCHESTRA)],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)},
        timeout=30,
    )
