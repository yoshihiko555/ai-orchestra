"""orchestra-manager.py の command registry と help の回帰テスト。"""

from __future__ import annotations

import os
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


def test_registry_entries_have_summary_and_examples() -> None:
    for command_name, entry in manager_mod.COMMAND_REGISTRY.items():
        assert entry["summary"], f"{command_name} summary is empty"
        assert entry["examples"], f"{command_name} examples is empty"


@pytest.mark.parametrize("command_name", sorted(manager_mod.COMMAND_REGISTRY))
def test_command_help_exits_zero(command_name: str) -> None:
    result = _run_manager(command_name, "--help")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("command_name", sorted(manager_mod.COMMAND_REGISTRY))
def test_command_help_shows_first_example(command_name: str) -> None:
    result = _run_manager(command_name, "--help")
    first_example = manager_mod.COMMAND_REGISTRY[command_name]["examples"][0]
    assert first_example in result.stdout


def test_top_level_help_lists_all_commands_and_group_headings() -> None:
    result = _run_manager("--help")
    for command_name in manager_mod.COMMAND_REGISTRY:
        assert command_name in result.stdout, command_name
    for heading in manager_mod.GROUP_HEADINGS.values():
        assert heading in result.stdout, heading


def test_top_level_help_has_scenario_blocks_and_see_next_pointer() -> None:
    result = _run_manager("--help")
    for heading in ("初回導入", "日常運用", "テンプレート更新", "次に見る"):
        assert heading in result.stdout, heading


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


def test_version_prefers_checkout_over_installed_package(tmp_path: Path) -> None:
    """site-packages 相当の ai_orchestra が居てもチェックアウト版を優先する。"""
    shadow_pkg = tmp_path / "ai_orchestra"
    shadow_pkg.mkdir()
    (shadow_pkg / "__init__.py").write_text('__version__ = "9.9.9-shadow"\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(ORCHESTRA_MANAGER), "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == f"orchex {ai_orchestra.__version__}"


def test_no_args_prints_top_level_help_and_exits_1() -> None:
    result = _run_manager()

    assert result.returncode == 1
    assert "usage" in result.stdout.lower()
