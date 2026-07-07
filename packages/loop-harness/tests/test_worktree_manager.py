"""Unit tests for worktree_manager."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

wm = load_module("worktree_manager", "packages/loop-harness/lib/worktree_manager.py")


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "loop-harness@example.com"], path)
    _git(["config", "user.name", "Loop Harness Test"], path)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "init"], path)


def test_loop_id_branch_and_path_naming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wm, "resolve_repo_identity_hash", lambda _project_dir: "deadbeef")
    monkeypatch.setattr(wm, "resolve_root_worktree", lambda _project_dir: tmp_path)
    assert wm.compute_loop_id(str(tmp_path), 42) == "deadbeef-issue-42"
    assert wm.branch_name_for(42) == "loop/issue-42"
    assert wm.worktree_path_for(str(tmp_path), 42) == str(tmp_path / ".worktrees" / "loop-issue-42")


def test_is_existing_loop_worktree_requires_independent_worktree_and_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_git(args: list[str], _cwd: str) -> str:
        if args == ["rev-parse", "--git-dir"]:
            return ".git/worktrees/loop-issue-1"
        if args == ["rev-parse", "--git-common-dir"]:
            return "../../.git"
        if args == ["branch", "--show-current"]:
            return "loop/issue-1"
        return ""

    monkeypatch.setattr(wm, "_git", fake_git)
    assert wm.is_existing_loop_worktree(str(worktree), "loop/issue-1") is True
    assert wm.is_existing_loop_worktree(str(worktree), "loop/issue-2") is False


def test_create_worktree_reuses_existing_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / ".worktrees" / "loop-issue-1"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(wm, "worktree_path_for", lambda _project_dir, _issue_number: str(worktree))
    monkeypatch.setattr(wm, "resolve_repo_identity_hash", lambda _project_dir: "deadbeef")
    monkeypatch.setattr(wm, "is_existing_loop_worktree", lambda _path, _branch: True)

    called = {"run": False}

    def fake_run_git(_args: list[str], _cwd: str) -> None:
        called["run"] = True

    monkeypatch.setattr(wm, "_run_git", fake_run_git)
    info = wm.create_worktree(str(tmp_path), 1)
    assert info.path == str(worktree)
    assert info.branch == "loop/issue-1"
    assert info.repo_identity_hash == "deadbeef"
    assert called["run"] is False


def test_create_worktree_runs_git_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / ".worktrees" / "loop-issue-1"
    monkeypatch.setattr(wm, "worktree_path_for", lambda _project_dir, _issue_number: str(worktree))
    monkeypatch.setattr(wm, "resolve_repo_identity_hash", lambda _project_dir: "deadbeef")
    monkeypatch.setattr(wm, "is_existing_loop_worktree", lambda _path, _branch: False)
    monkeypatch.setattr(wm, "_default_base_branch", lambda _project_dir: "origin/main")
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], _cwd: str) -> None:
        calls.append(args)

    monkeypatch.setattr(wm, "_run_git", fake_run_git)
    info = wm.create_worktree(str(tmp_path), 1)
    assert calls == [["worktree", "add", "-b", "loop/issue-1", str(worktree), "origin/main"]]
    assert info.repo_identity_hash == "deadbeef"


def test_create_worktree_uses_existing_branch_when_worktree_is_missing(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    _init_repo(main)
    _git(["branch", "loop/issue-1"], main)

    info = wm.create_worktree(str(main), 1)

    assert _git(["branch", "--show-current"], Path(info.path)) == "loop/issue-1"


def test_repo_identity_hash_is_stable_for_linked_worktree_without_remote(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    linked = tmp_path / "linked"
    _init_repo(main)
    _git(["worktree", "add", "-b", "loop/issue-2", str(linked), "HEAD"], main)

    assert wm.resolve_repo_identity_hash(str(main)) == wm.resolve_repo_identity_hash(str(linked))


def test_verify_repo_identity_recomputes_from_worktree_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wm, "resolve_repo_identity_hash", lambda path: "deadbeef" if path == "/wt" else "bad"
    )
    assert wm.verify_repo_identity("/wt", "deadbeef") is True
    assert wm.verify_repo_identity("/wt", "bad") is False


def test_worktree_path_for_fails_when_root_resolution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_project_dir: str) -> Path:
        raise wm.RootResolutionError("no root")

    monkeypatch.setattr(wm, "resolve_root_worktree", fail)
    try:
        wm.worktree_path_for(str(tmp_path), 1)
    except wm.WorktreeError as exc:
        assert "no root" in str(exc)
    else:
        raise AssertionError("expected WorktreeError")
