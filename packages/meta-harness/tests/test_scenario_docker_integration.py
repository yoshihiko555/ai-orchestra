"""Real Docker containment smoke tests (EV-46, EV-47)."""

from __future__ import annotations

import copy
import os
import secrets
import shutil
import subprocess
import time

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_docker_integration_tests",
    "packages/meta-harness/lib/meta_harness_common.py",
)
docker = load_module(
    "meta_harness_scenario_docker_integration_tests",
    "packages/meta-harness/lib/scenario_docker.py",
)
siso = load_module(
    "meta_harness_scenario_isolation_docker_integration_tests",
    "packages/meta-harness/lib/scenario_isolation.py",
)

pytestmark = pytest.mark.docker


def _require_docker() -> None:
    required = os.environ.get("META_HARNESS_REQUIRE_DOCKER") == "1"
    if shutil.which("docker") is None:
        if required:
            pytest.fail("Docker CLI is required for the containment gate")
        pytest.skip("docker CLI is unavailable")
    completed = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=20)
    if completed.returncode != 0:
        if required:
            pytest.fail("Docker daemon is required for the containment gate")
        pytest.skip("Docker daemon is unavailable")


@pytest.fixture(scope="module")
def images() -> tuple[str, str]:
    _require_docker()
    scenario, broker = docker.ensure_images_detailed(copy.deepcopy(mh.DEFAULTS))
    return scenario.tag, broker.tag


def test_internal_network_blocks_direct_egress_and_has_no_docker_socket(images) -> None:
    scenario_image, _ = images
    nonce = secrets.token_hex(3)
    network = f"mh-run-it-{nonce}-internal"
    subprocess.run(
        ["docker", "network", "create", "--internal", network],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                scenario_image,
                "python3",
                "-c",
                (
                    "import os,socket; "
                    "assert not os.path.exists('/var/run/docker.sock'); "
                    "socket.create_connection(('api.anthropic.com',443),2)"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode != 0
    finally:
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def test_scenario_image_defaults_to_non_root_user(images) -> None:
    scenario_image, _ = images

    completed = subprocess.run(
        ["docker", "run", "--rm", scenario_image, "id", "-u"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.stdout.strip() == "65532"


def test_force_remove_collects_setsid_descendant(images) -> None:
    scenario_image, _ = images
    name = f"mh-run-it-{secrets.token_hex(3)}-setsid"
    code = (
        "import os,time; pid=os.fork(); (os.setsid(),time.sleep(60)) if pid==0 else time.sleep(60)"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--pids-limit",
            "16",
            scenario_image,
            "python3",
            "-c",
            code,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        top = subprocess.run(["docker", "top", name], capture_output=True, text=True, timeout=20)
        assert top.returncode == 0
        assert "python3" in top.stdout
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
    inspected = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, timeout=20
    )
    assert inspected.returncode != 0


def test_preparation_container_self_terminates_at_absolute_lifetime(images, tmp_path) -> None:
    scenario_image, _ = images
    name = f"mh-run-it-{secrets.token_hex(3)}-watchdog"
    resources = docker.profile.resources_config(copy.deepcopy(mh.DEFAULTS))
    resources["max_lifetime_sec"] = 1
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    siso._prepare_isolated_git(
        worktree_dir=tmp_path,
        runtime_state_dir=runtime,
        source_commit="a" * 40,
        runner=subprocess.run,
        container_paths=True,
    )
    (runtime / "git-link-mask").write_text("")
    command = docker.profile.build_preparation_command(
        container_name=name,
        image_id=scenario_image,
        worktree=tmp_path,
        runtime_state_dir=runtime,
        owner_labels=docker._resource_labels("integration-test"),
        resources=resources,
    )
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            inspected = subprocess.run(
                ["docker", "inspect", name], capture_output=True, text=True, timeout=10
            )
            if inspected.returncode != 0:
                break
            time.sleep(0.1)
        assert inspected.returncode != 0
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)


def test_broker_final_image_has_no_shell(images) -> None:
    _, broker_image = images
    completed = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/bin/sh", broker_image, "-c", "true"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode != 0


def test_broker_is_dual_homed_and_unlinks_tmpfs_token(images, monkeypatch) -> None:
    monkeypatch.setattr(
        docker.credentials,
        "load_claude_oauth_credential",
        lambda **_kwargs: docker.credentials.ClaudeOAuthCredential(
            access_token="fake-token-for-no-upstream-smoke",
            expires_at_epoch=time.time() + 3600,
        ),
    )
    session = docker._start_broker(
        copy.deepcopy(mh.DEFAULTS),
        "integration",
        owner_id="integration-test",
        runner=subprocess.run,
    )
    try:
        inspection = subprocess.run(
            ["docker", "inspect", session.container_name],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert session.internal_network in inspection.stdout
        assert session.external_network in inspection.stdout
        token_check = subprocess.run(
            [
                "docker",
                "exec",
                session.container_name,
                "/usr/bin/python3",
                "-c",
                "import os; raise SystemExit(os.path.exists('/run/secrets/oauth-token'))",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert token_check.returncode == 0
    finally:
        session.cleanup()
    assert session.cleaned is True


def test_production_preparation_is_contained_and_exports_bounded_workspace(
    images, tmp_path, monkeypatch
) -> None:
    del images
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "input.txt").write_text("input\n")
    host_secret = tmp_path / "parent-secret.txt"
    host_secret.write_text("must-not-read\n")
    monkeypatch.setenv("PARENT_SECRET", "must-not-cross")
    command = (
        'test "$(id -u)" -ne 0; '
        'test ! -e /var/run/docker.sock; test -z "${PARENT_SECRET:-}"; '
        f"test ! -e {host_secret}; ! touch /rootfs-write-denied; "
        "if python3 -c \"import socket; socket.create_connection(('api.anthropic.com',443),1)\"; "
        "then exit 9; fi; printf prepared > generated.txt"
    )

    completed = docker.run_preparation_command(
        config=copy.deepcopy(mh.DEFAULTS),
        main_root=tmp_path,
        worktree_dir=worktree,
        source_commit="a" * 40,
        prepare_git_snapshot=siso._prepare_isolated_git,
        raw_command=["/bin/sh", "-c", command],
        timeout_seconds=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert (worktree / "generated.txt").read_text() == "prepared"
    names = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "name=mh-run-prepare-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert names.strip() == ""


def test_preparation_command_uses_isolated_git_snapshot(images, tmp_path) -> None:
    del images
    worktree = tmp_path / "git-preparation-worktree"
    worktree.mkdir()
    (worktree / "README.md").write_text("input\n")
    source_commit = "a" * 40

    completed = docker.run_preparation_command(
        config=copy.deepcopy(mh.DEFAULTS),
        main_root=tmp_path,
        worktree_dir=worktree,
        source_commit=source_commit,
        prepare_git_snapshot=siso._prepare_isolated_git,
        raw_command=["/bin/sh", "-c", "git rev-parse --short HEAD > git-head.txt"],
        timeout_seconds=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert (worktree / "git-head.txt").read_text().strip() == source_commit[:7]


def test_preparation_workspace_quota_fails_closed(images, tmp_path) -> None:
    del images
    worktree = tmp_path / "quota-worktree"
    worktree.mkdir()
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"]["resources"]["workspace_size"] = "8m"

    completed = docker.run_preparation_command(
        config=config,
        main_root=tmp_path,
        worktree_dir=worktree,
        source_commit="a" * 40,
        prepare_git_snapshot=siso._prepare_isolated_git,
        raw_command=[
            "/bin/sh",
            "-c",
            "dd if=/dev/zero of=too-big.bin bs=1m count=16 status=none",
        ],
        timeout_seconds=20,
    )

    assert completed.returncode != 0
    assert not (worktree / "too-big.bin").exists()


def test_linked_worktree_git_is_masked_and_snapshot_wrapper_works(
    images, tmp_path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "linked-worktree"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), source_commit],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    instruction = tmp_path / "instruction.md"
    instruction.write_text("report\n")
    monkeypatch.setattr(
        docker.credentials,
        "load_claude_oauth_credential",
        lambda **_kwargs: docker.credentials.ClaudeOAuthCredential(
            access_token="fake-token-for-git-smoke",
            expires_at_epoch=time.time() + 3600,
        ),
    )
    launch = docker.resolve_docker_launch(
        worktree_dir=worktree,
        main_root=repo,
        config=copy.deepcopy(mh.DEFAULTS),
        instruction_path=instruction,
        source_commit=source_commit,
        prepare_git_snapshot=siso._prepare_isolated_git,
    )
    try:
        command = docker.build_scenario_command(
            launch,
            [
                "/bin/sh",
                "-c",
                'test "$(id -u)" -ne 0; test ! -e .git; '
                'test ! -e /var/run/docker.sock; test -z "${PARENT_SECRET:-}"; '
                "! touch /rootfs-write-denied; git rev-parse --short HEAD; "
                "printf exported > result.txt",
            ],
        )
        completed = docker.sproc.run_bounded_capture(
            command,
            cwd=worktree,
            timeout=30,
            env={**docker._docker_host_env(), "PARENT_SECRET": "must-not-cross"},
            cleanup_args=["docker", "rm", "-f", launch.scenario_container_name],
            success_callback=lambda: docker.export_docker_workspace(launch),
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == source_commit[:7]
        assert (worktree / "result.txt").read_text() == "exported"
        assert (worktree / ".git").is_file()

        oracle_name = f"mh-run-it-{secrets.token_hex(3)}-oracle"
        oracle_command = docker.build_oracle_command(
            launch,
            "git rev-parse --short HEAD",
            container_name=oracle_name,
        )
        oracle = docker.sproc.run_bounded_capture(
            oracle_command,
            cwd=worktree,
            timeout=30,
            env=docker._docker_host_env(),
            cleanup_args=["docker", "rm", "-f", oracle_name],
        )
        assert oracle.returncode == 0, oracle.stderr
        assert oracle.stdout.strip() == source_commit[:7]
    finally:
        docker.cleanup_docker_launch(launch)
