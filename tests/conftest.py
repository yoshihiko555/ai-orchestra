"""E2E テスト共通フィクスチャ。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRA_MANAGER = REPO_ROOT / "scripts" / "orchestra-manager.py"
SYNC_ORCHESTRA = REPO_ROOT / "scripts" / "sync-orchestra.py"


@pytest.fixture()
def orchestra_dir() -> Path:
    """ai-orchestra リポジトリのルートパスを返す。"""
    return REPO_ROOT


@pytest.fixture()
def e2e_project(tmp_path: Path) -> Path:
    """git 初期化済みの一時プロジェクトを作成する。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# e2e test\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    return project


def run_orchex(
    *args: str,
    project: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """orchestra-manager.py をサブプロセスで実行する。"""
    cmd = [sys.executable, str(ORCHESTRA_MANAGER), *args, "--project", str(project)]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)},
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
