"""orchestra-manager.py の command registry と help の回帰テスト。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import ai_orchestra
from tests.module_loader import REPO_ROOT, load_module

manager_mod = load_module("orchestra_manager_help", "scripts/orchestra-manager.py")
ORCHESTRA_MANAGER = REPO_ROOT / "scripts" / "orchestra-manager.py"
REGISTRY_FIELDS = {"name", "group", "summary", "examples", "build_parser"}


def _run_manager(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ORCHESTRA_MANAGER), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_registry_keys_match_subparser_choices() -> None:
    _parser, subparsers = manager_mod.create_parser()

    assert set(manager_mod.COMMAND_REGISTRY) == set(subparsers.choices)
    assert len(manager_mod.COMMAND_REGISTRY) == 14
    for command_name, entry in manager_mod.COMMAND_REGISTRY.items():
        assert set(entry) == REGISTRY_FIELDS
        assert entry["name"] == command_name


def test_init_help_works() -> None:
    result = _run_manager("init", "--help")

    assert result.returncode == 0
    assert "--project" in result.stdout
    assert "--dry-run" in result.stdout


def test_init_dispatches_to_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_init(self: object, project: str | None, dry_run: bool = False) -> None:
        captured["project"] = project
        captured["dry_run"] = dry_run

    monkeypatch.setattr(manager_mod.OrchestraManager, "init", fake_init)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orchestra-manager.py",
            "--orchestra-dir",
            str(tmp_path),
            "init",
            "--project",
            "example-project",
            "--dry-run",
        ],
    )

    manager_mod.main()

    assert captured == {"project": "example-project", "dry_run": True}


def test_top_level_help_lists_version_option() -> None:
    result = _run_manager("--help")

    assert result.returncode == 0
    assert "--version" in result.stdout


def test_version_prints_real_version_on_direct_execution() -> None:
    result = _run_manager("--version")

    assert result.returncode == 0
    assert result.stdout.strip() == f"orchex {ai_orchestra.__version__}"


def test_no_args_prints_top_level_help_and_exits_1() -> None:
    result = _run_manager()

    assert result.returncode == 1
    assert "usage" in result.stdout.lower()
