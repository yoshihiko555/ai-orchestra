"""実リポジトリやユーザースキルに触れない hook テスト環境。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "packages" / "skill-evolution"


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / ".claude").mkdir()
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(directory))
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO))
    monkeypatch.setenv("HOME", str(home))
    return directory


@pytest.fixture
def write_config(project):
    def write(text, *, local=False):
        directory = project / ".claude" / "config" / "skill-evolution"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("skill-evolution.local.yaml" if local else "skill-evolution.yaml")
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture
def run_hook(project):
    def run(filename, payload, *, raw=None):
        completed = subprocess.run(
            [sys.executable, str(PACKAGE / "hooks" / filename)],
            input=raw if raw is not None else json.dumps(payload).encode(),
            capture_output=True,
            env=os.environ.copy(),
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr.decode()
        assert completed.stdout == b""
        return completed

    return run


@pytest.fixture
def read_events(project):
    def read():
        return [
            json.loads(line)
            for path in sorted(
                (project / ".claude" / "logs" / "skill-evolution").glob("runs-*.jsonl")
            )
            for line in path.read_text().splitlines()
        ]

    return read


@pytest.fixture
def skill_dir(project):
    directory = project / ".claude" / "skills" / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("レビューの手順\n", encoding="utf-8")
    return directory
