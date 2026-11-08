"""EV-28、EV-29、EV-81: v1 CLI の引数と非推奨案内。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "scripts/skill_evolution.py"


@pytest.mark.parametrize(
    "arguments,code",
    [
        (["status", "review"], 0),
        (["check-trigger", "review"], 1),
        (["evaluate", "--history", "missing.json"], 0),
        (["evaluate", "--history", "-"], 0),
        (["provenance", "review"], 0),
        (["lock", "acquire", "review"], 0),
        (["lock", "release", "review"], 0),
    ],
)
def test_ev28_v1_subcommands_are_deprecated_with_compatible_arguments(project, arguments, code):
    completed = subprocess.run(
        [sys.executable, str(CLI), "--project", str(project), *arguments],
        input="壊れた JSON も読み込まない",
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert completed.returncode == code
    payload = json.loads(completed.stdout)
    assert payload["deprecated"] is True
    assert "ADR-20261108-065" in payload["message"]
    assert "非推奨" in payload["message"]
    assert completed.stderr == ""
    assert not (project / ".claude/logs").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["unknown"],
        ["status"],
        ["check-trigger"],
        ["evaluate"],
        ["evaluate", "--from-json", "broken"],
        ["provenance"],
        ["lock"],
        ["lock", "invalid", "review"],
        ["lock", "acquire"],
        ["status", "review", "--unknown"],
    ],
)
def test_ev29_bad_or_missing_arguments_keep_argparse_errors(project, arguments):
    completed = subprocess.run(
        [sys.executable, str(CLI), "--project", str(project), *arguments],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 2
    assert "usage:" in completed.stderr and "error:" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""


def test_ev81_check_trigger_never_recommends_starting_even_with_many_lessons(project):
    directory = project / ".claude/skill-evolution/lessons"
    directory.mkdir(parents=True)
    path = directory / "review.md"
    path.write_text("\n".join(f"- 教訓 {index}" for index in range(100)))
    before = path.read_bytes()
    completed = subprocess.run(
        [sys.executable, str(CLI), "--project", str(project), "check-trigger", "review"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["deprecated"] is True and payload["triggered"] is False
    assert "suggested_command" not in payload
    assert "ADR-20261108-065" in payload["message"]
    assert path.read_bytes() == before
