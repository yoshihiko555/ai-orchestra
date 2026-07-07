"""orchestra-manager.py の meta / run passthrough 回帰テスト。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCHESTRA_MANAGER_SCRIPT = REPO_ROOT / "scripts" / "orchestra-manager.py"


def _run_orchestra_manager(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ORCHESTRA_MANAGER_SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)},
        timeout=30,
    )


class TestOrchestraManagerMetaPassthrough:
    def test_meta_passthrough_preserves_arguments_after_literal_double_dash(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)

        result = _run_orchestra_manager(
            "meta", "status", "--project", str(git_project), "--", "--json"
        )

        assert result.returncode == 2
        assert "unrecognized arguments" in result.stderr
        assert "--json" in result.stderr

    def test_run_subcommand_still_passes_arguments_after_double_dash(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)

        result = _run_orchestra_manager(
            "run",
            "meta-harness",
            "meta_harness",
            "--project",
            str(git_project),
            "--",
            "status",
            "--json",
        )

        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["count"] == 0
