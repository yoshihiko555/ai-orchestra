"""Real-Git tests for Maker ephemeral GIT_DIR preparation and CAS write-back."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.module_loader import load_module

git_ephemeral = load_module(
    "loop_git_ephemeral_tests",
    "packages/loop-harness/lib/loop_git_ephemeral.py",
)

BRANCH = "issue-211"
LOOP_ID = "loop-211"
ACTION_ID = "action-001"


def _git(
    *args: object,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *(str(arg) for arg in args)],
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _rev_parse(cwd: Path, revision: str) -> str:
    return _git("rev-parse", revision, cwd=cwd).stdout.strip()


@dataclass(frozen=True)
class GitFixture:
    project_dir: Path
    worktree_path: Path
    baseline_sha: str


@pytest.fixture
def linked_worktree(tmp_path: Path) -> GitFixture:
    project_dir = tmp_path / "project"
    worktree_path = tmp_path / "worktree"
    project_dir.mkdir()
    _git("init", "--initial-branch=main", cwd=project_dir)
    _git("config", "user.name", "Phase Two Test", cwd=project_dir)
    _git("config", "user.email", "phase-two@example.invalid", cwd=project_dir)
    (project_dir / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (project_dir / "unchanged.txt").write_text("must survive\n", encoding="utf-8")
    _git("add", "tracked.txt", "unchanged.txt", cwd=project_dir)
    _git("commit", "-m", "baseline", cwd=project_dir)
    _git("worktree", "add", "-b", BRANCH, worktree_path, "HEAD", cwd=project_dir)
    return GitFixture(project_dir, worktree_path, _rev_parse(worktree_path, "HEAD"))


def _prepare(fixture: GitFixture, **kwargs: Any):
    return git_ephemeral.prepare_ephemeral_git(
        project_dir=fixture.project_dir,
        loop_id=LOOP_ID,
        action_id=ACTION_ID,
        worktree_path=fixture.worktree_path,
        branch=BRANCH,
        **kwargs,
    )


def _ephemeral_env(session: Any) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_DIR": str(session.ephemeral_dir),
        "GIT_WORK_TREE": str(session.worktree_path),
    }


@dataclass(frozen=True)
class RecordedCall:
    args: tuple[str, ...]
    env: dict[str, str]


class RecordingRunner:
    """subprocess.run-compatible recorder with deterministic failure injection."""

    def __init__(
        self,
        *,
        before: Callable[[tuple[str, ...]], None] | None = None,
        fail: Callable[[tuple[str, ...]], int | None] | None = None,
        fail_after_run: Callable[[tuple[str, ...]], int | None] | None = None,
    ) -> None:
        self.calls: list[RecordedCall] = []
        self._before = before
        self._fail = fail
        self._fail_after_run = fail_after_run

    def __call__(self, args: list[object], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        normalized = tuple(str(arg) for arg in args)
        env = {str(key): str(value) for key, value in (kwargs.get("env") or {}).items()}
        self.calls.append(RecordedCall(normalized, env))
        if self._before is not None:
            self._before(normalized)
        injected_returncode = self._fail(normalized) if self._fail is not None else None
        if injected_returncode is None:
            run_kwargs = {**kwargs, "check": False}
            completed = subprocess.run(args, **run_kwargs)
            injected_returncode = (
                self._fail_after_run(normalized) if self._fail_after_run is not None else None
            )
            if injected_returncode is None:
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            else:
                returncode = injected_returncode
                stdout = completed.stdout
                stderr = "injected failure after command execution"
        else:
            returncode = injected_returncode
            stdout = ""
            stderr = "injected command failure"
        if kwargs.get("check") and returncode != 0:
            raise subprocess.CalledProcessError(returncode, args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _maker_commit(session: Any, content: str = "maker commit\n") -> str:
    Path(session.worktree_path, "tracked.txt").write_text(content, encoding="utf-8")
    env = _ephemeral_env(session)
    _git("add", "tracked.txt", env=env)
    _git("commit", "-m", "maker commit", env=env)
    return _git("rev-parse", session.branch_ref, env=env).stdout.strip()


def _shared_ref(session: Any, ref: str | None = None) -> str:
    return _git(
        "--git-dir", session.common_dir, "rev-parse", ref or session.branch_ref
    ).stdout.strip()


def _assert_import_ref_missing(session: Any) -> None:
    completed = _git(
        "--git-dir",
        session.common_dir,
        "show-ref",
        "--verify",
        "--quiet",
        session.import_ref,
        check=False,
    )
    assert completed.returncode == 1


def _create_stale_import_ref(session: Any, sha: str | None = None) -> None:
    _git(
        "--git-dir",
        session.common_dir,
        "update-ref",
        session.import_ref,
        sha or session.baseline_sha,
    )


def _command_index(args: tuple[str, ...], command: str) -> int | None:
    try:
        return args.index(command)
    except ValueError:
        return None


def _has_command(args: tuple[str, ...], command: str) -> bool:
    return _command_index(args, command) is not None


def _assert_hardened(args: tuple[str, ...]) -> None:
    pairs = list(zip(args, args[1:], strict=False))
    assert ("-c", "credential.helper=") in pairs
    assert ("-c", "core.hooksPath=/dev/null") in pairs
    assert ("-c", "core.fsmonitor=") in pairs
    assert ("-c", "uploadpack.packObjectsHook=") in pairs


def test_prepare_ephemeral_git_initializes_trusted_maker_repository(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)

    expected_runtime = (
        linked_worktree.project_dir
        / ".claude"
        / "loop"
        / LOOP_ID
        / "docker-runtime"
        / ACTION_ID
        / "git-ephemeral"
    )
    assert Path(session.ephemeral_dir) == expected_runtime
    assert session.baseline_sha == linked_worktree.baseline_sha
    assert session.branch_ref == f"refs/heads/{BRANCH}"
    assert session.import_ref == f"refs/loop-import/{ACTION_ID}"
    assert _git("rev-parse", session.branch_ref, env=_ephemeral_env(session)).stdout.strip() == (
        linked_worktree.baseline_sha
    )
    assert _git("status", "--porcelain", env=_ephemeral_env(session)).stdout == ""
    assert (
        _git("config", "user.name", env=_ephemeral_env(session)).stdout.strip()
        == "loop-harness-maker"
    )
    assert (
        _git("config", "user.email", env=_ephemeral_env(session)).stdout.strip()
        == "loop-harness-maker@invalid"
    )
    assert (
        Path(session.ephemeral_dir, "objects/info/alternates").read_text(encoding="utf-8")
        == f"{session.common_dir}/objects\n"
    )
    assert (
        Path(session.pinned_git_pointer).read_bytes()
        == Path(linked_worktree.worktree_path, ".git").read_bytes()
    )


def test_prepare_removes_stale_action_runtime_and_import_ref(
    linked_worktree: GitFixture,
) -> None:
    runtime_dir = (
        linked_worktree.project_dir / ".claude" / "loop" / LOOP_ID / "docker-runtime" / ACTION_ID
    )
    stale_ephemeral = runtime_dir / "git-ephemeral"
    stale_ephemeral.mkdir(parents=True)
    sentinel = stale_ephemeral / "must-not-survive"
    sentinel.write_text("stale", encoding="utf-8")
    stale_ref = f"refs/loop-import/{ACTION_ID}"
    common_dir = _git(
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        cwd=linked_worktree.worktree_path,
    ).stdout.strip()
    _git("--git-dir", common_dir, "update-ref", stale_ref, linked_worktree.baseline_sha)

    session = _prepare(linked_worktree)

    assert not sentinel.exists()
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_prepare_rejects_worktree_checked_out_on_another_branch(
    linked_worktree: GitFixture,
) -> None:
    _git("switch", "-c", "unexpected-branch", cwd=linked_worktree.worktree_path)

    with pytest.raises(git_ephemeral.EphemeralGitInfrastructureError, match="branch"):
        _prepare(linked_worktree)


def test_prepare_normalizes_filesystem_cleanup_failure_to_typed_error(
    linked_worktree: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = (
        linked_worktree.project_dir / ".claude" / "loop" / LOOP_ID / "docker-runtime" / ACTION_ID
    )
    runtime_dir.mkdir(parents=True)

    def fail_rmtree(_path: Path) -> None:
        raise PermissionError("injected runtime cleanup failure")

    monkeypatch.setattr(git_ephemeral.shutil, "rmtree", fail_rmtree)

    with pytest.raises(git_ephemeral.EphemeralGitInfrastructureError, match="runtime"):
        _prepare(linked_worktree)


def test_build_maker_git_mount_spec_preserves_overlay_order_and_one_to_one_paths(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)

    spec = git_ephemeral.build_maker_git_mount_spec(session)

    assert [(Path(mount.source), Path(mount.target), mount.read_only) for mount in spec.mounts] == [
        (Path(session.worktree_path), Path(session.worktree_path), False),
        (
            Path(session.pinned_git_pointer),
            Path(session.worktree_path, ".git"),
            True,
        ),
        (Path(session.ephemeral_dir), Path(session.ephemeral_dir), False),
        (
            Path(session.common_dir, "objects"),
            Path(session.common_dir, "objects"),
            True,
        ),
    ]
    assert spec.env["GIT_DIR"] == str(session.ephemeral_dir)
    assert spec.env["GIT_WORK_TREE"] == str(session.worktree_path)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_prepare_commit_finalize_fast_forwards_branch_and_preserves_working_tree(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    runner = RecordingRunner()
    candidate_sha = _maker_commit(session)
    working_tree_bytes = Path(session.worktree_path, "tracked.txt").read_bytes()

    result = git_ephemeral.finalize_ephemeral_git(session, runner=runner)

    assert result.status == "updated"
    assert result.baseline_sha == session.baseline_sha
    assert result.new_sha == candidate_sha
    assert _shared_ref(session) == candidate_sha
    assert _rev_parse(Path(session.worktree_path), "HEAD") == candidate_sha
    assert (
        _git("show", f"{candidate_sha}:unchanged.txt", cwd=Path(session.worktree_path)).stdout
        == "must survive\n"
    )
    assert Path(session.worktree_path, "tracked.txt").read_bytes() == working_tree_bytes
    assert _git("status", "--porcelain", cwd=Path(session.worktree_path)).stdout == ""
    _assert_import_ref_missing(session)
    fetch_args = next(call.args for call in runner.calls if _has_command(call.args, "fetch"))
    assert "--no-tags" in fetch_args
    assert "--no-write-fetch-head" in fetch_args
    assert f"{session.branch_ref}:{session.import_ref}" in fetch_args
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_without_commit_skips_writeback_and_removes_stale_import_ref(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    _create_stale_import_ref(session)

    result = git_ephemeral.finalize_ephemeral_git(session)

    assert result.status == "no_commit"
    assert result.baseline_sha == session.baseline_sha
    assert result.new_sha == session.baseline_sha
    assert _shared_ref(session) == session.baseline_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)
    git_ephemeral.cleanup_ephemeral_git(session)
    assert not Path(session.ephemeral_dir).exists()
    assert not Path(session.pinned_git_pointer).exists()


def test_finalize_rejects_dirty_status_without_shared_ref_writeback(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    _maker_commit(session)
    Path(session.worktree_path, "tracked.txt").write_text("uncommitted\n", encoding="utf-8")
    _create_stale_import_ref(session)

    with pytest.raises(git_ephemeral.EphemeralGitInfrastructureError, match="status|dirty"):
        git_ephemeral.finalize_ephemeral_git(session)

    assert _shared_ref(session) == session.baseline_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_fetch_failure_safe_stops_and_cleans_partially_created_import_ref(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    _maker_commit(session)
    runner = RecordingRunner(fail_after_run=lambda args: 1 if _has_command(args, "fetch") else None)

    with pytest.raises(git_ephemeral.EphemeralGitSafetyStop) as caught:
        git_ephemeral.finalize_ephemeral_git(session, runner=runner)

    assert caught.value.stop_reason == "git_ref_import_failed"
    assert _shared_ref(session) == session.baseline_sha
    _assert_import_ref_missing(session)
    fetch_args = next(call.args for call in runner.calls if _has_command(call.args, "fetch"))
    assert "--no-tags" in fetch_args
    assert "--no-write-fetch-head" in fetch_args
    assert f"{session.branch_ref}:{session.import_ref}" in fetch_args
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_non_fast_forward_safe_stops_without_changing_shared_ref(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    _maker_commit(session)
    env = _ephemeral_env(session)
    tree_sha = _git("write-tree", env=env).stdout.strip()
    divergent_sha = _git("commit-tree", tree_sha, "-m", "divergent root", env=env).stdout.strip()
    _git("update-ref", session.branch_ref, divergent_sha, env=env)

    with pytest.raises(git_ephemeral.EphemeralGitSafetyStop) as caught:
        git_ephemeral.finalize_ephemeral_git(session)

    assert caught.value.stop_reason == "git_ref_not_fast_forward"
    assert _shared_ref(session) == session.baseline_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_merge_base_execution_failure_is_infrastructure_not_non_ff(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    _maker_commit(session)
    runner = RecordingRunner(fail=lambda args: 2 if _has_command(args, "merge-base") else None)

    with pytest.raises(git_ephemeral.EphemeralGitInfrastructureError):
        git_ephemeral.finalize_ephemeral_git(session, runner=runner)

    assert _shared_ref(session) == session.baseline_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_cas_race_safe_stops_without_overwriting_competing_commit(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    candidate_sha = _maker_commit(session)
    tree_sha = _git(
        "--git-dir", session.common_dir, "rev-parse", f"{session.baseline_sha}^{{tree}}"
    )
    rival_sha = _git(
        "--git-dir",
        session.common_dir,
        "commit-tree",
        tree_sha.stdout.strip(),
        "-p",
        session.baseline_sha,
        "-m",
        "competing commit",
    ).stdout.strip()
    moved = False

    def move_shared_ref_before_cas(args: tuple[str, ...]) -> None:
        nonlocal moved
        command = _command_index(args, "update-ref")
        if moved or command is None or len(args) <= command + 1:
            return
        if args[command + 1] != session.branch_ref:
            return
        _git(
            "--git-dir",
            session.common_dir,
            "update-ref",
            session.branch_ref,
            rival_sha,
            session.baseline_sha,
        )
        moved = True

    runner = RecordingRunner(before=move_shared_ref_before_cas)

    with pytest.raises(git_ephemeral.EphemeralGitSafetyStop) as caught:
        git_ephemeral.finalize_ephemeral_git(session, runner=runner)

    assert caught.value.stop_reason == "git_ref_cas_rejected"
    assert _shared_ref(session) == rival_sha
    assert _shared_ref(session) != candidate_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_rejects_import_ref_that_does_not_match_ephemeral_tip(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    candidate_sha = _maker_commit(session)
    tree_sha = _git(
        "--git-dir", session.common_dir, "rev-parse", f"{session.baseline_sha}^{{tree}}"
    ).stdout.strip()
    rival_sha = _git(
        "--git-dir",
        session.common_dir,
        "commit-tree",
        tree_sha,
        "-p",
        session.baseline_sha,
        "-m",
        "unexpected imported commit",
    ).stdout.strip()
    moved = False

    def replace_import_ref_before_resolve(args: tuple[str, ...]) -> None:
        nonlocal moved
        if moved or not _has_command(args, "rev-parse") or session.import_ref not in args:
            return
        _git("--git-dir", session.common_dir, "update-ref", session.import_ref, rival_sha)
        moved = True

    runner = RecordingRunner(before=replace_import_ref_before_resolve)

    with pytest.raises(git_ephemeral.EphemeralGitSafetyStop) as caught:
        git_ephemeral.finalize_ephemeral_git(session, runner=runner)

    assert caught.value.stop_reason == "git_ref_import_failed"
    assert _shared_ref(session) == session.baseline_sha
    assert _shared_ref(session) != candidate_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_neutralizes_poisoned_ephemeral_git_config(
    linked_worktree: GitFixture,
    tmp_path: Path,
) -> None:
    session = _prepare(linked_worktree)
    _maker_commit(session)
    fsmonitor_marker = tmp_path / "fsmonitor-ran"
    upload_marker = tmp_path / "upload-hook-ran"
    fsmonitor = tmp_path / "fsmonitor.sh"
    upload_hook = tmp_path / "upload-hook.sh"
    fsmonitor.write_text(f"#!/bin/sh\ntouch '{fsmonitor_marker}'\nexit 0\n", encoding="utf-8")
    upload_hook.write_text(
        f"#!/bin/sh\ntouch '{upload_marker}'\nexec git pack-objects \"$@\"\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o755)
    upload_hook.chmod(0o755)
    _git("--git-dir", session.ephemeral_dir, "config", "core.fsmonitor", str(fsmonitor))
    _git(
        "--git-dir",
        session.ephemeral_dir,
        "config",
        "uploadpack.packObjectsHook",
        str(upload_hook),
    )
    runner = RecordingRunner()

    result = git_ephemeral.finalize_ephemeral_git(session, runner=runner)

    assert result.status == "updated"
    assert not fsmonitor_marker.exists()
    assert not upload_marker.exists()
    ephemeral_calls = [
        call.args
        for call in runner.calls
        if str(session.ephemeral_dir) in call.args
        or call.env.get("GIT_DIR") == str(session.ephemeral_dir)
    ]
    assert ephemeral_calls
    for args in ephemeral_calls:
        _assert_hardened(args)
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_restores_trusted_config_before_status_filter_can_execute(
    linked_worktree: GitFixture,
    tmp_path: Path,
) -> None:
    session = _prepare(linked_worktree)
    marker = tmp_path / "clean-filter-ran"
    clean_filter = tmp_path / "clean-filter.sh"
    clean_filter.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n", encoding="utf-8")
    clean_filter.chmod(0o755)
    Path(session.worktree_path, ".gitattributes").write_text(
        "tracked.txt filter=host-rce\n", encoding="utf-8"
    )
    _git(
        "--git-dir",
        session.ephemeral_dir,
        "config",
        "filter.host-rce.clean",
        str(clean_filter),
    )
    env = _ephemeral_env(session)
    Path(session.worktree_path, "tracked.txt").write_text("maker commit\n", encoding="utf-8")
    _git("add", ".gitattributes", "tracked.txt", env=env)
    _git("commit", "-m", "maker commit with attributes", env=env)
    marker.unlink(missing_ok=True)

    result = git_ephemeral.finalize_ephemeral_git(session)

    assert result.status == "updated"
    assert not marker.exists()
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_ignores_host_global_filter_config(
    linked_worktree: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _prepare(linked_worktree)
    marker = tmp_path / "global-clean-filter-ran"
    clean_filter = tmp_path / "global-clean-filter.sh"
    global_config = tmp_path / "host-global.gitconfig"
    clean_filter.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n", encoding="utf-8")
    clean_filter.chmod(0o755)
    _git("config", "--file", global_config, "filter.host-rce.clean", str(clean_filter))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    Path(session.worktree_path, ".gitattributes").write_text(
        "tracked.txt filter=host-rce\n", encoding="utf-8"
    )
    env = _ephemeral_env(session)
    Path(session.worktree_path, "tracked.txt").write_text("maker commit\n", encoding="utf-8")
    _git("add", ".gitattributes", "tracked.txt", env=env)
    _git("commit", "-m", "maker commit with global filter", env=env)
    marker.unlink(missing_ok=True)

    result = git_ephemeral.finalize_ephemeral_git(session)

    assert result.status == "updated"
    assert not marker.exists()
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_rejects_checkout_branch_change_before_cas(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    candidate_sha = _maker_commit(session)
    _git("switch", "-c", "unexpected-branch", cwd=linked_worktree.worktree_path)

    with pytest.raises(git_ephemeral.EphemeralGitInfrastructureError, match="branch"):
        git_ephemeral.finalize_ephemeral_git(session)

    assert _shared_ref(session) == session.baseline_sha
    assert _shared_ref(session, "refs/heads/unexpected-branch") == session.baseline_sha
    assert _shared_ref(session) != candidate_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_retries_temp_ref_cleanup_when_initial_delete_fails(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    _create_stale_import_ref(session)
    delete_attempts = 0

    def fail_first_delete(args: tuple[str, ...]) -> int | None:
        nonlocal delete_attempts
        command = _command_index(args, "update-ref")
        if command is None or args[command + 1 : command + 3] != ("-d", session.import_ref):
            return None
        delete_attempts += 1
        return 1 if delete_attempts == 1 else None

    runner = RecordingRunner(fail=fail_first_delete)

    with pytest.raises(git_ephemeral.EphemeralGitInfrastructureError, match="temporary import"):
        git_ephemeral.finalize_ephemeral_git(session, runner=runner)

    assert delete_attempts == 2
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_finalize_detects_dot_git_pointer_tampering_before_writeback(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    candidate_sha = _maker_commit(session)
    Path(session.worktree_path, ".git").write_text(
        "gitdir: /untrusted/location\n", encoding="utf-8"
    )
    _create_stale_import_ref(session)

    with pytest.raises(git_ephemeral.EphemeralGitInfrastructureError, match=r"git.*pointer|\.git"):
        git_ephemeral.finalize_ephemeral_git(session)

    assert _shared_ref(session) == session.baseline_sha
    assert _shared_ref(session) != candidate_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)


def test_post_cas_reset_failure_is_infrastructure_failure_without_rollback(
    linked_worktree: GitFixture,
) -> None:
    session = _prepare(linked_worktree)
    candidate_sha = _maker_commit(session)
    runner = RecordingRunner(fail=lambda args: 1 if _has_command(args, "reset") else None)

    with pytest.raises(git_ephemeral.EphemeralGitInfrastructureError, match="reset"):
        git_ephemeral.finalize_ephemeral_git(session, runner=runner)

    assert _shared_ref(session) == candidate_sha
    _assert_import_ref_missing(session)
    git_ephemeral.cleanup_ephemeral_git(session)
