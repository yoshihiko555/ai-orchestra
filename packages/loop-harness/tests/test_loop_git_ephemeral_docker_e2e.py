"""Opt-in real-Docker round trip for the Maker ephemeral GIT_DIR."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

git_ephemeral = load_module(
    "loop_git_ephemeral_docker_e2e_tests",
    "packages/loop-harness/lib/loop_git_ephemeral.py",
)

DEFAULT_IMAGE = "ai-orchestra/loop-harness-scenario:2.1.207"
pytestmark = pytest.mark.docker


def _run(*args: object, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _require_real_docker(image: str) -> None:
    if os.environ.get("LOOP_HARNESS_RUN_DOCKER_GIT_E2E") != "1":
        pytest.skip("set LOOP_HARNESS_RUN_DOCKER_GIT_E2E=1 for the real-Docker Git E2E")
    if shutil.which("docker") is None:
        pytest.fail("Docker CLI is required for the loop-harness Git E2E")
    info = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if info.returncode != 0:
        pytest.fail("Docker daemon is required for the loop-harness Git E2E")
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if inspect.returncode != 0:
        pytest.fail(f"build the loop-harness scenario image before running this E2E: {image}")


def _make_world_accessible(path: Path) -> None:
    path.chmod(path.stat().st_mode | 0o077)
    if not path.is_dir():
        return
    for child in path.rglob("*"):
        child.chmod(child.stat().st_mode | (0o077 if child.is_dir() else 0o066))


def _maker_identity() -> tuple[int, int]:
    """Resolve the container UID/GID the same way docker_runtime_profile.non_root_identity() does.

    On Linux, bind-mounted files keep their host UID/GID inside the container. This test
    previously always ran the Maker container as the image's baked-in `65532:65532`
    (`packages/loop-harness/docker/scenario/Dockerfile`'s `USER`), which only matches the host
    user when that host user happens to be UID/GID 65532 -- on a real (non-root) Linux CI runner
    it does not, so files the container writes into the bind-mounted worktree/ephemeral dirs come
    back owned by a UID the host process cannot write to, and `finalize_ephemeral_git`'s
    subsequent host-side Git calls fail with permission errors despite the `_make_world_accessible`
    pre-loosening above. Matching the host UID/GID (falling back to 65532:65532 only when host is
    root, mirroring `docker_runtime_profile.non_root_identity()`) avoids that mismatch at the
    source instead of relying solely on permission bits.
    """
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        return 65532, 65532
    return uid, gid


def test_container_commit_round_trips_through_driver_cas(tmp_path: Path) -> None:
    image = os.environ.get("LOOP_HARNESS_DOCKER_GIT_E2E_IMAGE", DEFAULT_IMAGE)
    _require_real_docker(image)
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    project.mkdir()
    _run("git", "init", "--initial-branch=main", cwd=project)
    _run("git", "config", "user.name", "Docker E2E", cwd=project)
    _run("git", "config", "user.email", "docker-e2e@example.invalid", cwd=project)
    (project / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _run("git", "add", "tracked.txt", cwd=project)
    _run("git", "commit", "-m", "baseline", cwd=project)
    _run("git", "worktree", "add", "-b", "issue-211-e2e", worktree, "HEAD", cwd=project)
    baseline_sha = _run("git", "rev-parse", "HEAD", cwd=worktree).stdout.strip()
    session = git_ephemeral.prepare_ephemeral_git(
        project_dir=project,
        loop_id="loop-211-e2e",
        action_id="action-docker-e2e",
        worktree_path=worktree,
        branch="issue-211-e2e",
    )
    spec = git_ephemeral.build_maker_git_mount_spec(session)
    try:
        (worktree / "tracked.txt").write_text("committed in Docker\n", encoding="utf-8")
        _make_world_accessible(tmp_path)
        _make_world_accessible(Path(session.ephemeral_dir))
        uid, gid = _maker_identity()
        docker_args = [
            "docker",
            "run",
            "--rm",
            "--user",
            f"{uid}:{gid}",
            "--workdir",
            str(worktree),
        ]
        for mount in spec.mounts:
            mount_arg = f"type=bind,source={mount.source},target={mount.target}"
            if mount.read_only:
                mount_arg += ",readonly"
            docker_args.extend(("--mount", mount_arg))
        for key, value in spec.env.items():
            docker_args.extend(("--env", f"{key}={value}"))
        docker_args.extend(
            (
                image,
                "sh",
                "-lc",
                "git -c safe.directory='*' add tracked.txt && "
                "git -c safe.directory='*' commit -m 'Docker Maker commit'",
            )
        )

        _run(*docker_args)
        result = git_ephemeral.finalize_ephemeral_git(session)

        assert result.status == "updated"
        assert result.baseline_sha == baseline_sha
        assert result.new_sha != baseline_sha
        assert _run("git", "rev-parse", "HEAD", cwd=worktree).stdout.strip() == result.new_sha
        assert _run("git", "status", "--porcelain", cwd=worktree).stdout == ""
        assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "committed in Docker\n"
    finally:
        git_ephemeral.cleanup_ephemeral_git(session)
