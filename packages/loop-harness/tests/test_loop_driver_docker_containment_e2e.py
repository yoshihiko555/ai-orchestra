"""Opt-in containment E2E for the production loop-harness Docker primitives."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

profile = load_module(
    "loop_docker_profile_containment_e2e",
    "packages/loop-harness/lib/loop_docker_profile.py",
)
docker_action = load_module(
    "loop_docker_action_containment_e2e",
    "packages/loop-harness/lib/loop_docker_action.py",
)
git_ephemeral = load_module(
    "loop_git_ephemeral_containment_e2e",
    "packages/loop-harness/lib/loop_git_ephemeral.py",
)
driver = load_module(
    "loop_driver_containment_e2e",
    "packages/loop-harness/scripts/loop_driver.py",
)

DEFAULT_IMAGE = "ai-orchestra/loop-harness-scenario:2.1.207"
pytestmark = pytest.mark.docker


def _run(
    *args: object,
    cwd: Path | None = None,
    check: bool = True,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _require_real_docker() -> tuple[str, str]:
    if os.environ.get("LOOP_HARNESS_RUN_DOCKER_GIT_E2E") != "1":
        pytest.skip("set LOOP_HARNESS_RUN_DOCKER_GIT_E2E=1 for containment E2E")
    if shutil.which("docker") is None:
        pytest.fail("Docker CLI is required when containment E2E is opted in")
    info = _run("docker", "info", check=False, timeout=20)
    if info.returncode != 0:
        pytest.fail("Docker daemon is required when containment E2E is opted in")
    image = os.environ.get("LOOP_HARNESS_DOCKER_GIT_E2E_IMAGE", DEFAULT_IMAGE)
    inspected = _run(
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        image,
        check=False,
        timeout=20,
    )
    image_id = inspected.stdout.strip()
    if inspected.returncode != 0 or not image_id.startswith("sha256:"):
        pytest.fail(f"build the loop-harness scenario image before containment E2E: {image}")
    return image, image_id


@pytest.fixture
def docker_image_id() -> str:
    return _require_real_docker()[1]


@pytest.fixture
def internal_network(docker_image_id: str) -> str:
    del docker_image_id
    name = f"lh-containment-{secrets.token_hex(5)}"
    _run("docker", "network", "create", "--internal", name)
    try:
        yield name
    finally:
        _run("docker", "network", "rm", name, check=False)


def _git(*args: object, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run("git", *args, cwd=cwd)


def _git_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    project.mkdir()
    _git("init", "--initial-branch=main", cwd=project)
    _git("config", "user.name", "Containment E2E", cwd=project)
    _git("config", "user.email", "containment@example.invalid", cwd=project)
    (project / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=project)
    _git("commit", "-m", "baseline", cwd=project)
    _git("worktree", "add", "-b", "issue-211-containment", worktree, "HEAD", cwd=project)
    baseline = _git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    _make_world_accessible(tmp_path)
    return project, worktree, baseline


def _make_world_accessible(path: Path) -> None:
    path.chmod(path.stat().st_mode | 0o077)
    if not path.is_dir():
        return
    for child in path.rglob("*"):
        child.chmod(child.stat().st_mode | (0o077 if child.is_dir() else 0o066))


def _spec(
    *,
    name: str,
    image_id: str,
    network: str,
    workdir: Path,
    mounts: tuple[Any, ...],
    env: dict[str, str],
) -> object:
    return profile.ScenarioContainerSpec(
        container_name=name,
        image_id=image_id,
        internal_network=network,
        workdir=workdir,
        mounts=mounts,
        env=env,
        resources={"pids_limit": 64, "memory": "512m", "cpus": 1.0},
        max_lifetime_sec=120,
        owner_labels={"ai.orchestra.loop-harness.owner": "containment-e2e"},
    )


def _exec(
    container_name: str,
    command: list[str],
    *,
    workdir: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        *profile.build_exec_command(container_name, command, workdir=workdir),
        check=check,
    )


def _remove_or_fail(container_name: str) -> None:
    if not docker_action.remove_scenario_container(container_name):
        pytest.fail(f"production cleanup could not remove scenario container: {container_name}")


def _docker_config() -> dict[str, Any]:
    config = yaml.safe_load(
        (REPO_ROOT / "packages/loop-harness/config/loop-harness.yaml").read_text(encoding="utf-8")
    )
    config["lp2"]["isolation"].update({"backend": "docker", "execution_backend": "docker"})
    return config


def test_maker_commit_round_trip_uses_production_profile_and_cleanup(
    tmp_path: Path,
    docker_image_id: str,
    internal_network: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, worktree, baseline = _git_fixture(tmp_path)
    config = _docker_config()
    container_names: list[str] = []

    class Broker:
        base_url = "http://lh-broker:8790"
        run_token = "containment-e2e-token"

        def __init__(self) -> None:
            self.internal_network = internal_network
            self.cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    broker = Broker()
    monkeypatch.setattr(driver.ld, "load_config", lambda *_args: config)
    monkeypatch.setattr(
        driver.lda.broker_runtime,
        "sweep_stale_resources",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        driver.lda.docker_image,
        "ensure_scenario_image",
        lambda *_args, **_kwargs: SimpleNamespace(image_id=docker_image_id),
    )
    monkeypatch.setattr(
        driver.lda.docker_image,
        "ensure_broker_image",
        lambda *_args, **_kwargs: SimpleNamespace(image_id=docker_image_id),
    )
    monkeypatch.setattr(
        driver.lda.broker_runtime,
        "start_broker",
        lambda *_args, **_kwargs: broker,
    )
    production_start = driver.lda.start_scenario_container

    def start_and_capture(spec: object, **kwargs: Any) -> None:
        container_names.append(spec.container_name)
        _make_world_accessible(tmp_path)
        production_start(spec, **kwargs)

    monkeypatch.setattr(driver.lda, "start_scenario_container", start_and_capture)
    loop_driver = driver.LoopDriver("loop-211-maker-e2e", str(project), "e2e-lease")
    command_result: dict[str, object] = {}

    def execute_maker_in_selected_executor(*_args: object) -> dict[str, Any]:
        mechanical_runner = loop_driver._action_executor.mechanical_runner
        if mechanical_runner is None:
            raise AssertionError("driver did not select DockerActionExecutor")
        output, returncode = mechanical_runner(
            "printf 'committed in isolated Maker\\n' > tracked.txt && "
            "git add tracked.txt && git commit -m 'isolated Maker commit'",
            str(worktree),
            60,
        )
        command_result.update(output=output, returncode=returncode)
        if returncode != 0:
            raise driver.lda.DockerActionError("isolated Maker command failed")
        return {"maker": {"agent": "containment-e2e"}}

    monkeypatch.setattr(loop_driver, "_dispatch_action", execute_maker_in_selected_executor)
    proposal = SimpleNamespace(
        action=driver.lc.Action.RUN_MAKER.value,
        action_id="maker-action",
        context={"params": {"maker_agent": "containment-e2e"}},
    )
    state = SimpleNamespace(
        worktree_path=str(worktree),
        branch="issue-211-containment",
    )

    try:
        result = loop_driver._dispatch(proposal, state)

        assert result == {"maker": {"agent": "containment-e2e"}}
        assert command_result["returncode"] == 0, command_result.get("output")
        new_sha = _git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        assert new_sha != baseline
        assert (worktree / "tracked.txt").read_text(encoding="utf-8") == (
            "committed in isolated Maker\n"
        )
        assert broker.cleaned is True
        assert len(container_names) == 1
        assert _run("docker", "inspect", container_names[0], check=False).returncode != 0
    finally:
        for container_name in container_names:
            _remove_or_fail(container_name)


def test_checker_is_read_only_has_no_sensitive_mounts_and_no_egress(
    tmp_path: Path,
    docker_image_id: str,
    internal_network: str,
) -> None:
    project, worktree, _baseline = _git_fixture(tmp_path)
    session = git_ephemeral.prepare_ephemeral_git(
        project_dir=project,
        loop_id="loop-211-checker-e2e",
        action_id="checker-action",
        worktree_path=worktree,
        branch="issue-211-containment",
    )
    _make_world_accessible(tmp_path)
    mount_spec = git_ephemeral.build_checker_git_mount_spec(session)
    driver_state_canary = project / ".claude" / "loop" / "driver-state-canary.json"
    driver_state_canary.write_text('{"secret":"host-only"}\n', encoding="utf-8")
    name = f"lh-checker-e2e-{secrets.token_hex(4)}"
    docker_action.start_scenario_container(
        _spec(
            name=name,
            image_id=docker_image_id,
            network=internal_network,
            workdir=worktree,
            mounts=mount_spec.mounts,
            env=dict(mount_spec.env),
        )
    )
    idle_baseline = docker_action.capture_scenario_idle_baseline(name)
    try:
        _exec(name, ["git", "rev-parse", "HEAD"], workdir=worktree)
        docker_action.assert_scenario_container_idle(name, expected_snapshot=idle_baseline)
        ruff = _exec(name, ["ruff", "check", "."], workdir=worktree, check=False)
        docker_action.assert_scenario_container_idle(name, expected_snapshot=idle_baseline)
        pytest_version = _exec(
            name,
            ["pytest", "--version"],
            workdir=worktree,
            check=False,
        )
        docker_action.assert_scenario_container_idle(name, expected_snapshot=idle_baseline)
        write_worktree = _exec(
            name,
            ["/bin/bash", "-lc", "printf x >> tracked.txt"],
            workdir=worktree,
            check=False,
        )
        docker_action.assert_scenario_container_idle(name, expected_snapshot=idle_baseline)
        write_git_pointer = _exec(
            name,
            ["/bin/bash", "-lc", "printf x >> .git"],
            workdir=worktree,
            check=False,
        )
        docker_action.assert_scenario_container_idle(name, expected_snapshot=idle_baseline)
        boundaries = _exec(
            name,
            [
                "/bin/bash",
                "-lc",
                f"test ! -e {session.common_dir / 'config'} && "
                f"test ! -e {driver_state_canary} && "
                "test ! -S /var/run/docker.sock && test ! -S /run/docker.sock",
            ],
            workdir=worktree,
            check=False,
        )
        docker_action.assert_scenario_container_idle(name, expected_snapshot=idle_baseline)
        egress = _exec(
            name,
            [
                "python3",
                "-c",
                "import socket; socket.create_connection(('1.1.1.1', 443), 2)",
            ],
            workdir=worktree,
            check=False,
        )
        docker_action.assert_scenario_container_idle(name, expected_snapshot=idle_baseline)
        inspected = _run("docker", "inspect", name).stdout

        assert ruff.returncode == 0, ruff.stdout + ruff.stderr
        assert pytest_version.returncode == 0, pytest_version.stdout + pytest_version.stderr
        assert write_worktree.returncode != 0
        assert write_git_pointer.returncode != 0
        assert boundaries.returncode == 0
        assert egress.returncode != 0
        assert '"ReadonlyRootfs": true' in inspected
        assert '"NetworkMode": "' + internal_network + '"' in inspected
        assert "docker.sock" not in inspected
    finally:
        _remove_or_fail(name)
        git_ephemeral.cleanup_ephemeral_git(session)


def test_non_idle_exec_reclaims_container_cgroup(
    tmp_path: Path,
    docker_image_id: str,
    internal_network: str,
) -> None:
    name = f"lh-cgroup-e2e-{secrets.token_hex(4)}"
    docker_action.start_scenario_container(
        _spec(
            name=name,
            image_id=docker_image_id,
            network=internal_network,
            workdir=Path("/tmp"),
            mounts=(),
            env={},
        )
    )
    idle_baseline = docker_action.capture_scenario_idle_baseline(name)
    _exec(
        name,
        [
            "python3",
            "-c",
            "import subprocess; subprocess.Popen(['sleep','300'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL, start_new_session=True)",
        ],
        workdir=Path("/tmp"),
    )
    top = _run("docker", "top", name, "-eo", "pid,comm,args").stdout
    host_pids = [
        int(line.split(maxsplit=1)[0])
        for line in top.splitlines()[1:]
        if line.strip() and line.rstrip().endswith("sleep 300")
    ]
    assert host_pids, top

    with pytest.raises(docker_action.DockerActionError, match="non-idle"):
        docker_action.enforce_scenario_container_idle(
            name,
            expected_snapshot=idle_baseline,
        )

    inspect = _run("docker", "inspect", name, check=False)
    stats = _run(
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{.Name}}",
        name,
        check=False,
        timeout=20,
    )
    assert inspect.returncode != 0
    assert name not in stats.stdout.splitlines()
    for pid in host_pids:
        proc = Path(f"/proc/{pid}")
        if proc.parent.is_dir():
            deadline = time.monotonic() + 5
            while proc.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not proc.exists()
