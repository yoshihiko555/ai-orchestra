"""dashboard-html.py の出力パス生成テスト。"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


class TestDashboardHtmlDefaultOutput:
    SCRIPT = "packages/audit/scripts/dashboard-html.py"

    def test_default_output_writes_to_claude_dir(self, tmp_path: Path) -> None:
        """-o 未指定時に .claude/YYYYMMDD-dashboard.html へ保存される。"""
        # Arrange
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".claude").mkdir()
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        expected = project_dir / ".claude" / f"{date_str}-dashboard.html"

        # Act
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--project", str(project_dir)],
            capture_output=True,
            text=True,
        )

        # Assert
        assert result.returncode == 0
        assert expected.exists()
        assert "Dashboard written to" in result.stderr

    def test_explicit_output_path(self, tmp_path: Path) -> None:
        """-o 指定時にそのパスへ保存される。"""
        # Arrange
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        out_file = tmp_path / "custom.html"

        # Act
        result = subprocess.run(
            [
                sys.executable,
                self.SCRIPT,
                "--project",
                str(project_dir),
                "-o",
                str(out_file),
            ],
            capture_output=True,
            text=True,
        )

        # Assert
        assert result.returncode == 0
        assert out_file.exists()
        assert "Dashboard written to" in result.stderr

    def test_stdout_output_with_dash(self, tmp_path: Path) -> None:
        """-o - 指定時に stdout へ出力される。"""
        # Arrange
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Act
        result = subprocess.run(
            [
                sys.executable,
                self.SCRIPT,
                "--project",
                str(project_dir),
                "-o",
                "-",
            ],
            capture_output=True,
            text=True,
        )

        # Assert
        assert result.returncode == 0
        assert "<!DOCTYPE html>" in result.stdout
        assert "Dashboard written to" not in result.stderr
