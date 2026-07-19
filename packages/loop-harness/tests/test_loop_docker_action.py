"""Phase-4 action executor and one-container lifecycle tests."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

docker_config = load_module(
    "loop_docker_config",
    "packages/loop-harness/lib/loop_docker_config.py",
)
profile = load_module("loop_docker_profile", "packages/loop-harness/lib/loop_docker_profile.py")
git_ephemeral = load_module(
    "loop_git_ephemeral",
    "packages/loop-harness/lib/loop_git_ephemeral.py",
)
docker_settings = load_module(
    "loop_docker_settings",
    "packages/loop-harness/lib/loop_docker_settings.py",
)
broker_runtime = load_module(
    "loop_docker_broker",
    "packages/loop-harness/lib/loop_docker_broker.py",
)
docker_image = load_module(
    "loop_docker_image",
    "packages/loop-harness/lib/loop_docker_image.py",
)
docker_action = load_module(
    "loop_docker_action",
    "packages/loop-harness/lib/loop_docker_action.py",
)
action_executor = load_module(
    "loop_action_executor_tests",
    "packages/loop-harness/lib/loop_action_executor.py",
)

IMAGE_ID = "sha256:" + "a" * 64


def _config(*, execution_backend: str = "docker") -> dict[str, Any]:
    config = yaml.safe_load(
        (REPO_ROOT / "packages/loop-harness/config/loop-harness.yaml").read_text(encoding="utf-8")
    )
    config["lp2"]["isolation"].update({"backend": "docker", "execution_backend": execution_backend})
    return config


def _request(
    tmp_path: Path,
    *,
    kind: str = "maker",
    needs_broker: bool = True,
    lease_lost: Any = None,
) -> object:
    config = _config()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return docker_action.DockerActionRequest(
        config=config,
        isolation=docker_config.validate_isolation_config(config),
        project_dir=tmp_path,
        loop_id="loop-211",
        action_id="action-001",
        worktree_path=worktree,
        branch="issue-211",
        kind=kind,
        remaining_wall_clock_seconds=lambda: 600,
        needs_broker=needs_broker,
        lease_lost=lease_lost,
    )


def test_start_broker_applies_validated_defaults_for_a_minimal_broker_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, P2 (round 11): a config that omits `lp2.isolation.broker` entirely
    (relying on `validate_isolation_config()`'s own defaults for every field) is valid, but the
    old call site passed the raw, un-defaulted config mapping straight into `start_broker()`,
    which re-derived broker settings from it with none of those defaults applied -- crashing with
    a bare `KeyError` (e.g. reading `broker["budget_usd"]`) the moment Docker execution actually
    tried to start the broker. `DockerActionRuntime._start()` now passes the already-validated
    `DockerIsolationConfig.broker` instead, so `start_broker()` never re-parses a raw mapping.
    """
    config = _config()
    del config["lp2"]["isolation"]["broker"]
    validated_broker = docker_config.validate_isolation_config(config).broker

    monkeypatch.setattr(
        broker_runtime.credentials,
        "load_claude_oauth_credential",
        lambda **_kwargs: SimpleNamespace(access_token="fake-token"),
    )
    monkeypatch.setattr(
        broker_runtime.lifecycle,
        "start_broker_container",
        lambda _spec, *, session_factory, **_kwargs: session_factory(),
    )

    session = broker_runtime.start_broker(
        validated_broker,
        scope="loop-broker-defaults",
        owner_id="owner",
        scenario_image_id=IMAGE_ID,
        broker_image_id=IMAGE_ID,
        max_lifetime_seconds=60,
        runner=lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )

    assert session.idle_timeout_seconds == 300


def test_backend_docker_without_execution_backend_uses_host_executor(tmp_path: Path) -> None:
    def host_runner(*args: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, "", "")

    executor = action_executor.build_action_executor(
        _config(execution_backend="none"),
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="run_maker",
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=host_runner,
    )

    assert isinstance(executor, action_executor.HostActionExecutor)


def test_host_executor_ignores_unused_docker_profile_fields(tmp_path: Path) -> None:
    config = _config(execution_backend="none")
    config["lp2"]["isolation"]["checker"]["read_only_worktree"] = False
    config["lp2"]["isolation"]["resources"]["pids_limit"] = "invalid"

    executor = action_executor.build_action_executor(
        config,
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="run_checker",
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=lambda *args: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    assert isinstance(executor, action_executor.HostActionExecutor)


def test_host_only_action_skips_docker_validation_when_isolation_config_is_invalid(
    tmp_path: Path,
) -> None:
    """Codex review, PR #262, High: advance_phase/stop/exit_* never dispatch into a container,
    so an unrelated Docker isolation config typo must not stop them from running on the host.
    """
    config = _config(execution_backend="docker")
    config["lp2"]["isolation"]["resources"]["memory"] = "not-a-size"
    with pytest.raises(docker_config.DockerConfigError):
        docker_config.validate_isolation_config(config)

    executor = action_executor.build_action_executor(
        config,
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="advance_phase",
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=lambda *args: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    assert isinstance(executor, action_executor.HostActionExecutor)


def test_run_checker_without_llm_review_disables_broker(tmp_path: Path) -> None:
    """Codex review, PR #262, High: a checker action with no `llm_review` block never calls
    execute_claude(), so build_action_executor() must resolve `needs_broker=False` for it.
    """
    executor = action_executor.build_action_executor(
        _config(),
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="run_checker",
        params={"mechanical": {"commands": ["true"]}},
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=lambda *args: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    assert isinstance(executor, action_executor.DockerActionExecutor)
    assert executor.runtime.request.needs_broker is False


def test_run_checker_with_llm_review_keeps_broker(tmp_path: Path) -> None:
    executor = action_executor.build_action_executor(
        _config(),
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="run_checker",
        params={"llm_review": {"selection": "skill-review-policy"}},
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=lambda *args: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    assert isinstance(executor, action_executor.DockerActionExecutor)
    assert executor.runtime.request.needs_broker is True


def test_run_checker_without_explicit_params_defaults_broker_enabled(tmp_path: Path) -> None:
    """Test call sites that omit `params` entirely must keep today's always-True behavior."""
    executor = action_executor.build_action_executor(
        _config(),
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="run_checker",
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=lambda *args: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    assert isinstance(executor, action_executor.DockerActionExecutor)
    assert executor.runtime.request.needs_broker is True


def test_run_maker_always_keeps_broker_regardless_of_params(tmp_path: Path) -> None:
    executor = action_executor.build_action_executor(
        _config(),
        project_dir=str(tmp_path),
        loop_id="loop-211",
        action_id="action-001",
        action="run_maker",
        params={},
        worktree_path=str(tmp_path),
        branch="issue-211",
        remaining_wall_clock_seconds=lambda: 600,
        host_child_runner=lambda *args: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    assert isinstance(executor, action_executor.DockerActionExecutor)
    assert executor.runtime.request.needs_broker is True


def test_docker_executor_never_falls_back_to_host_after_runtime_failure() -> None:
    class FailedRuntime:
        def execute_claude(self, *_args: Any, **_kwargs: Any) -> Any:
            raise docker_action.DockerActionError("daemon unavailable")

    host_calls: list[object] = []
    executor = action_executor.DockerActionExecutor(FailedRuntime())

    with pytest.raises(docker_action.DockerActionError, match="daemon unavailable"):
        executor.execute_claude(["claude", "-p", "prompt"], "/tmp", 30, {})

    assert host_calls == []


def test_empty_action_result_is_not_treated_as_maker_success() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.success: bool | None = None

        def finish(self, *, action_succeeded: bool) -> None:
            self.success = action_succeeded

    runtime = Runtime()

    action_executor.DockerActionExecutor(runtime).finish({})

    assert runtime.success is False


def test_action_executor_cancel_delegates_only_for_docker() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    runtime = Runtime()
    action_executor.HostActionExecutor(lambda *_args: None).cancel()
    action_executor.DockerActionExecutor(runtime).cancel()

    assert runtime.cancelled is True


def test_maker_worktree_chown_runs_before_local_override_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, High (round 3): move override snapshot after Docker chown.

    Under a root-run driver, `align_mount_ownership(worktree_path)` must run *before*
    `prepare_ephemeral_git()` -- which internally snapshots the worktree's project-local override
    files' uid/gid as the local-override guard's trusted baseline -- so that baseline already
    reflects the container-ready ownership. Reordering the other way would make the guard see the
    driver's own chown as Maker tampering and safe-stop as `maker_partial_worktree`.
    """
    order: list[str] = []
    session = SimpleNamespace(
        runtime_dir=tmp_path / "runtime", ephemeral_dir=tmp_path / "runtime" / "git-ephemeral"
    )
    mount_spec = SimpleNamespace(mounts=(), env={"GIT_DIR": "/git", "GIT_WORK_TREE": "/work"})

    def chown(
        path: Path,
        *,
        exclude: frozenset[Path] | None = None,
        protect_owner_only: bool = True,
    ) -> None:
        del exclude, protect_owner_only
        label = "chown_worktree" if path == tmp_path / "worktree" else "chown_ephemeral"
        order.append(label)

    monkeypatch.setattr(docker_action.profile.runtime, "align_mount_ownership", chown)
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "prepare_ephemeral_git",
        lambda **_kwargs: order.append("git_prepare") or session,
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "create_settings_bundle",
        lambda *_args: (
            order.append("settings") or docker_settings.DockerSettingsBundle(tmp_path / "trusted")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "build_maker_git_mount_spec",
        lambda *_args: order.append("mount_spec") or mount_spec,
    )

    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="maker"),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime._prepare_mounts()

    assert order == ["chown_worktree", "git_prepare", "settings", "mount_spec", "chown_ephemeral"]


def test_maker_worktree_chown_excludes_local_override_leaf_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, High (round 4): don't re-own project-local override files.

    Under a root-run driver, the recursive worktree chown must not itself grant the fixed
    non-root container identity new read/write access to a `.claude/config/**/*.local.{yaml,json}`
    file that was deliberately more restrictively owned/permissioned than the rest of the
    worktree.
    """
    worktree = tmp_path / "worktree"
    override_dir = worktree / ".claude" / "config" / "agent-routing"
    override_dir.mkdir(parents=True)
    override_file = override_dir / "cli-tools.local.yaml"
    override_file.write_text("secret: value\n", encoding="utf-8")
    (override_dir / "cli-tools.yaml").write_text("tracked: value\n", encoding="utf-8")

    session = SimpleNamespace(
        runtime_dir=tmp_path / "runtime", ephemeral_dir=tmp_path / "runtime" / "git-ephemeral"
    )
    mount_spec = SimpleNamespace(mounts=(), env={"GIT_DIR": "/git", "GIT_WORK_TREE": "/work"})
    chown_calls: list[tuple[Path, frozenset[Path] | None]] = []

    def chown(
        path: Path,
        *,
        exclude: frozenset[Path] | None = None,
        protect_owner_only: bool = True,
    ) -> None:
        del protect_owner_only
        chown_calls.append((path, exclude))

    monkeypatch.setattr(docker_action.profile.runtime, "align_mount_ownership", chown)
    monkeypatch.setattr(
        docker_action.git_ephemeral, "prepare_ephemeral_git", lambda **_kwargs: session
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "create_settings_bundle",
        lambda *_args: docker_settings.DockerSettingsBundle(tmp_path / "trusted"),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral, "build_maker_git_mount_spec", lambda *_args: mount_spec
    )

    request = docker_action.DockerActionRequest(
        config=_config(),
        isolation=docker_config.validate_isolation_config(_config()),
        project_dir=tmp_path,
        loop_id="loop-211",
        action_id="action-001",
        worktree_path=worktree,
        branch="issue-211",
        kind="maker",
        remaining_wall_clock_seconds=lambda: 600,
    )
    runtime = docker_action.DockerActionRuntime(
        request,
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime._prepare_mounts()

    worktree_chown = next(call for call in chown_calls if call[0] == worktree)
    assert worktree_chown[1] == frozenset({override_file})


def test_maker_worktree_chown_excludes_symlinked_local_override_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, Critical (round 5): a symlinked local override's resolved,
    in-worktree target must also be excluded from the recursive chown, not just the symlink's
    own path -- ``align_mount_ownership()`` reaches that target through its own real path while
    walking the worktree, not through the symlink, so excluding only the link path would still
    let a root-owned, stricter-than-usual-permission target gain the non-root container identity.
    """
    worktree = tmp_path / "worktree"
    override_dir = worktree / ".claude" / "config" / "agent-routing"
    override_dir.mkdir(parents=True)
    real_target_dir = worktree / "secrets"
    real_target_dir.mkdir()
    real_target = real_target_dir / "cli-tools-real.local.yaml"
    real_target.write_text("secret: value\n", encoding="utf-8")
    override_link = override_dir / "cli-tools.local.yaml"
    (override_link).symlink_to(real_target)

    session = SimpleNamespace(
        runtime_dir=tmp_path / "runtime", ephemeral_dir=tmp_path / "runtime" / "git-ephemeral"
    )
    mount_spec = SimpleNamespace(mounts=(), env={"GIT_DIR": "/git", "GIT_WORK_TREE": "/work"})
    chown_calls: list[tuple[Path, frozenset[Path] | None]] = []

    def chown(
        path: Path,
        *,
        exclude: frozenset[Path] | None = None,
        protect_owner_only: bool = True,
    ) -> None:
        del protect_owner_only
        chown_calls.append((path, exclude))

    monkeypatch.setattr(docker_action.profile.runtime, "align_mount_ownership", chown)
    monkeypatch.setattr(
        docker_action.git_ephemeral, "prepare_ephemeral_git", lambda **_kwargs: session
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "create_settings_bundle",
        lambda *_args: docker_settings.DockerSettingsBundle(tmp_path / "trusted"),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral, "build_maker_git_mount_spec", lambda *_args: mount_spec
    )

    request = docker_action.DockerActionRequest(
        config=_config(),
        isolation=docker_config.validate_isolation_config(_config()),
        project_dir=tmp_path,
        loop_id="loop-211",
        action_id="action-001",
        worktree_path=worktree,
        branch="issue-211",
        kind="maker",
        remaining_wall_clock_seconds=lambda: 600,
    )
    runtime = docker_action.DockerActionRuntime(
        request,
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime._prepare_mounts()

    worktree_chown = next(call for call in chown_calls if call[0] == worktree)
    assert worktree_chown[1] == frozenset({override_link, real_target})


def test_checker_ephemeral_git_dir_is_reowned_before_read_only_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, P1 (round 11): unlike the Maker branch, the checker path never
    re-owned `self.git_session.ephemeral_dir` before mounting it read-only. Under a root-run
    driver, `prepare_ephemeral_git()` creates that ephemeral GIT_DIR as root, but the checker
    scenario container always runs as the fixed non-root 65532:65532 identity -- without this
    re-own, it could never read its own `GIT_DIR`, breaking every `git` invocation inside the
    checker (mechanical checks, the LLM reviewer's own `git diff`/`git log`).

    Codex review, PR #262, P1 (round 12): round 11 placed this re-own *before*
    `build_checker_git_mount_spec()`, but that function's `_harden_ephemeral_git_metadata()` step
    unconditionally recreates `config`/`objects/info/alternates` under the calling process's own
    uid every time it runs -- silently undoing a re-own that ran before it. The original version
    of this test mocked `build_checker_git_mount_spec` as a bare lambda that recorded nothing, so
    it could not catch that ordering bug. This now records both calls into one ordered list and
    asserts `build_checker_git_mount_spec` runs strictly before the re-own.
    """
    session = SimpleNamespace(
        runtime_dir=tmp_path / "runtime", ephemeral_dir=tmp_path / "runtime" / "git-ephemeral"
    )
    mount_spec = SimpleNamespace(mounts=(), env={"GIT_DIR": "/git", "GIT_WORK_TREE": "/work"})
    events: list[str] = []
    chown_calls: list[tuple[Path, bool]] = []

    def chown(
        path: Path,
        *,
        exclude: frozenset[Path] | None = None,
        protect_owner_only: bool = True,
    ) -> None:
        del exclude
        events.append("chown")
        chown_calls.append((path, protect_owner_only))

    monkeypatch.setattr(docker_action.profile.runtime, "align_mount_ownership", chown)
    monkeypatch.setattr(
        docker_action.git_ephemeral, "prepare_ephemeral_git", lambda **_kwargs: session
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "create_settings_bundle",
        lambda *_args: docker_settings.DockerSettingsBundle(tmp_path / "trusted"),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "build_checker_git_mount_spec",
        lambda *_args: events.append("mount_spec") or mount_spec,
    )

    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="checker"),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime._prepare_mounts()

    assert (session.ephemeral_dir, False) in chown_calls
    assert events == ["mount_spec", "chown"]


def test_checker_worktree_owner_only_secret_check_is_wired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, P1 (round 12): unlike the Maker branch (which chowns
    `self.request.worktree_path` and, as a side effect, runs `align_mount_ownership()`'s round-11
    owner-only-secret reject check on a non-root driver), the Checker branch never touched
    `self.request.worktree_path` at all -- an owner-only (e.g. 0600) secret left in the worktree
    was silently readable from inside a non-root-driver Checker. This only verifies the wiring
    (the reject-only check runs, with the right path and exclude args); the real filesystem
    permission behavior of `reject_owner_only_secrets()` itself is covered by
    `packages/docker-runtime/tests/test_docker_runtime.py`.
    """
    session = SimpleNamespace(
        runtime_dir=tmp_path / "runtime", ephemeral_dir=tmp_path / "runtime" / "git-ephemeral"
    )
    mount_spec = SimpleNamespace(mounts=(), env={"GIT_DIR": "/git", "GIT_WORK_TREE": "/work"})
    reject_calls: list[tuple[Path, frozenset[Path] | None]] = []

    def reject(path: Path, *, exclude: frozenset[Path] | None = None) -> None:
        reject_calls.append((path, exclude))

    monkeypatch.setattr(docker_action.profile.runtime, "reject_owner_only_secrets", reject)
    monkeypatch.setattr(
        docker_action.profile.runtime, "align_mount_ownership", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral, "prepare_ephemeral_git", lambda **_kwargs: session
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "create_settings_bundle",
        lambda *_args: docker_settings.DockerSettingsBundle(tmp_path / "trusted"),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral, "build_checker_git_mount_spec", lambda *_args: mount_spec
    )

    request = _request(tmp_path, kind="checker")
    runtime = docker_action.DockerActionRuntime(
        request,
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime._prepare_mounts()

    expected_exclude = docker_action._local_override_leaf_paths(request.worktree_path)
    assert reject_calls == [(request.worktree_path, expected_exclude)]


def test_maker_lifecycle_uses_production_primitives_in_required_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session = SimpleNamespace(
        runtime_dir=tmp_path / "runtime", ephemeral_dir=tmp_path / "runtime" / "git-ephemeral"
    )
    bundle = docker_settings.DockerSettingsBundle(tmp_path / "trusted")
    mount_spec = SimpleNamespace(mounts=(), env={"GIT_DIR": "/git", "GIT_WORK_TREE": "/work"})

    class Broker:
        internal_network = "lh-internal"
        base_url = "http://lh-broker:8790"
        run_token = "run-token"

        def cleanup(self) -> None:
            events.append("broker_cleanup")

    monkeypatch.setattr(
        docker_action.runtime_cli,
        "docker_daemon_available",
        lambda **_kwargs: events.append("daemon") or True,
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "sweep_stale_resources",
        lambda *_args, **_kwargs: events.append("stale_sweep"),
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_scenario_image",
        lambda *_args, **_kwargs: (
            events.append("scenario_image") or SimpleNamespace(image_id=IMAGE_ID)
        ),
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_broker_image",
        lambda *_args, **_kwargs: (
            events.append("broker_image") or SimpleNamespace(image_id=IMAGE_ID)
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "prepare_ephemeral_git",
        lambda **_kwargs: events.append("git_prepare") or session,
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "create_settings_bundle",
        lambda *_args: events.append("settings") or bundle,
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "build_maker_git_mount_spec",
        lambda *_args: events.append("mount_spec") or mount_spec,
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "start_broker",
        lambda *_args, **_kwargs: events.append("broker") or Broker(),
    )
    monkeypatch.setattr(
        docker_action.profile,
        "build_scenario_container_command",
        lambda *_args: events.append("profile") or ["docker", "run"],
    )

    def docker_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "run"]:
            events.append("scenario")
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[:2] == ["docker", "top"]:
            events.append("idle_check")
            return subprocess.CompletedProcess(
                command,
                0,
                "PID COMMAND COMMAND\n"
                "101 docker-init /usr/bin/docker-init -- /usr/bin/timeout 660s "
                "/usr/bin/sleep infinity\n"
                "102 timeout /usr/bin/timeout 660s /usr/bin/sleep infinity\n"
                "103 sleep /usr/bin/sleep infinity\n",
                "",
            )
        raise AssertionError(command)

    monkeypatch.setattr(docker_action.runtime_cli, "run", docker_run)
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda *_args, **_kwargs: events.append("scenario_cleanup") or True,
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: events.append("git_finalize"),
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "cleanup_settings_bundle",
        lambda *_args: events.append("settings_cleanup"),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: events.append("git_cleanup"),
    )

    def host_child(
        command: list[str], _cwd: str, _timeout: float, _env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        events.append("exec")
        return subprocess.CompletedProcess(command, 0, '{"result":"ok"}', "")

    runtime = docker_action.DockerActionRuntime(_request(tmp_path), host_child_runner=host_child)
    runtime.execute_claude(
        ["host-claude", "--settings", "/host/settings", "-p", "prompt"],
        str(tmp_path / "worktree"),
        30,
        {},
    )
    runtime.finish(action_succeeded=True)

    # Codex review, PR #262, High (round 3): `ensure_broker_image`/`start_broker` now run after
    # `_prepare_mounts()` (git_prepare/settings/mount_spec), not before -- `needs_broker` is only
    # known once the request is built, and skipping both entirely for a mechanical-only checker
    # request (see `test_mechanical_only_checker_skips_broker_and_uses_isolated_network` below) is
    # the whole point of this reordering.
    assert events == [
        "daemon",
        "stale_sweep",
        "scenario_image",
        "git_prepare",
        "settings",
        "mount_spec",
        "broker_image",
        "broker",
        "profile",
        "scenario",
        "idle_check",
        "exec",
        "idle_check",
        "scenario_cleanup",
        "broker_cleanup",
        "git_finalize",
        "settings_cleanup",
        "git_cleanup",
    ]


def test_start_fails_before_any_docker_setup_when_budget_already_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, High (round 5): fail fast on an already-exhausted wall-clock
    budget before any Docker setup work (daemon sweep, scenario image ensure) runs, instead of
    only discovering the exhausted budget after `ensure_scenario_image()` has already run.
    """
    events: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "docker_daemon_available",
        lambda **_kwargs: events.append("daemon") or True,
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "sweep_stale_resources",
        lambda *_args, **_kwargs: events.append("stale_sweep"),
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_scenario_image",
        lambda *_args, **_kwargs: events.append("scenario_image") or SimpleNamespace(),
    )

    config = _config()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    request = docker_action.DockerActionRequest(
        config=config,
        isolation=docker_config.validate_isolation_config(config),
        project_dir=tmp_path,
        loop_id="loop-211",
        action_id="action-001",
        worktree_path=worktree,
        branch="issue-211",
        kind="maker",
        remaining_wall_clock_seconds=lambda: 0,
    )
    runtime = docker_action.DockerActionRuntime(
        request,
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )

    with pytest.raises(docker_action.DockerActionError, match="wall-clock budget is exhausted"):
        runtime._ensure_started()

    assert events == ["daemon"]


def test_mechanical_only_checker_skips_broker_and_uses_isolated_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, High: a checker action with no `llm_review` never needs a broker.

    `needs_broker=False` (the request `build_action_executor()` builds for exactly this case)
    must skip `ensure_broker_image`/`start_broker` (and therefore the Claude OAuth credential
    load inside it) entirely, using only a dedicated internal network for the scenario container.
    """
    session = SimpleNamespace(
        runtime_dir=tmp_path / "runtime", ephemeral_dir=tmp_path / "runtime" / "git-ephemeral"
    )
    bundle = docker_settings.DockerSettingsBundle(tmp_path / "trusted")
    mount_spec = SimpleNamespace(mounts=(), env={"GIT_DIR": "/git", "GIT_WORK_TREE": "/work"})
    captured_networks: list[str] = []

    def fail_if_called(name: str) -> Any:
        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(f"{name} must not be called for a mechanical-only checker")

        return _raise

    monkeypatch.setattr(
        docker_action.runtime_cli, "docker_daemon_available", lambda **_kwargs: True
    )
    monkeypatch.setattr(
        docker_action.broker_runtime, "sweep_stale_resources", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_scenario_image",
        lambda *_args, **_kwargs: SimpleNamespace(image_id=IMAGE_ID),
    )
    monkeypatch.setattr(
        docker_action.docker_image, "ensure_broker_image", fail_if_called("ensure_broker_image")
    )
    monkeypatch.setattr(
        docker_action.broker_runtime, "start_broker", fail_if_called("start_broker")
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "prepare_ephemeral_git",
        lambda **_kwargs: session,
    )
    monkeypatch.setattr(
        docker_action.docker_settings, "create_settings_bundle", lambda *_args: bundle
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral, "build_checker_git_mount_spec", lambda *_a, **_k: mount_spec
    )

    def start_isolated_network(*, scope: str, owner_id: str, runner: Any) -> str:
        del scope, owner_id, runner
        network = "lh-isolated-internal"
        captured_networks.append(network)
        return network

    stopped_networks: list[str] = []
    monkeypatch.setattr(
        docker_action.broker_runtime, "start_isolated_network", start_isolated_network
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "stop_isolated_network",
        lambda name, *, runner: stopped_networks.append(name) or True,
    )

    seen_specs: list[Any] = []

    def build_command(spec: Any) -> list[str]:
        seen_specs.append(spec)
        return ["docker", "run"]

    monkeypatch.setattr(docker_action.profile, "build_scenario_container_command", build_command)

    def docker_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[:2] == ["docker", "top"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "PID COMMAND COMMAND\n"
                "101 docker-init /usr/bin/docker-init -- /usr/bin/timeout 660s "
                "/usr/bin/sleep infinity\n"
                "102 timeout /usr/bin/timeout 660s /usr/bin/sleep infinity\n"
                "103 sleep /usr/bin/sleep infinity\n",
                "",
            )
        raise AssertionError(command)

    monkeypatch.setattr(docker_action.runtime_cli, "run", docker_run)
    monkeypatch.setattr(
        docker_action.runtime_cli, "remove_container", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "verify_failed_maker_worktree",
        lambda *_args: (_ for _ in ()).throw(AssertionError("checker must not finalize git")),
    )
    monkeypatch.setattr(docker_action.docker_settings, "cleanup_settings_bundle", lambda *_a: None)
    monkeypatch.setattr(docker_action.git_ephemeral, "cleanup_ephemeral_git", lambda *_a: None)

    def host_child(
        command: list[str], _cwd: str, _timeout: float, _env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "ok", "")

    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="checker", needs_broker=False),
        host_child_runner=host_child,
    )
    output, exit_code = runtime.execute_mechanical("true", "/tmp", 30)
    runtime.finish(action_succeeded=True)

    assert exit_code == 0
    assert output == "ok"
    assert runtime.broker is None
    assert captured_networks == ["lh-isolated-internal"]
    assert seen_specs[0].internal_network == "lh-isolated-internal"
    assert stopped_networks == ["lh-isolated-internal"]


def test_mechanical_exec_env_forwards_overrides_but_not_container_reserved_keys() -> None:
    """Codex review, PR #262, High (round 6 adds `XDG_CONFIG_HOME`); precedence updated round 8.

    `*_CACHE_DIR`-style tool overrides must reach the container so mechanical commands can
    redirect writes off the read-only checker worktree, but `HOME`/`TMPDIR`/`PATH`/`GIT_DIR`/
    `GIT_WORK_TREE`/`XDG_CONFIG_HOME` are container-owned (tmpfs mounts, the image's own
    toolchain, the ephemeral Git wiring, and -- like `HOME` -- a host scratch-home path that is
    never mounted into the container) and must never be clobbered by the host-derived checker
    env. `RUFF_CACHE_DIR` specifically is always pinned to the container-safe default (see the
    dedicated precedence test below); a different `*_CACHE_DIR` key with no built-in default
    still forwards through unmodified.
    """
    checker_env = {
        "RUFF_CACHE_DIR": "/host/ruff-cache",
        "MYPY_CACHE_DIR": "/host/mypy-cache",
        "HOME": "/host/scratch-home",
        "TMPDIR": "/host/tmp",
        "PATH": "/host/bin:/usr/bin",
        "GIT_DIR": "/host/git-dir",
        "GIT_WORK_TREE": "/host/worktree",
        "XDG_CONFIG_HOME": "/host/scratch-home/.config",
    }

    merged = docker_action._mechanical_exec_env(checker_env)

    assert merged == {
        "RUFF_CACHE_DIR": "/tmp/ruff-cache",
        "MYPY_CACHE_DIR": "/host/mypy-cache",
    }


def test_mechanical_exec_env_drops_host_secrets_outside_the_cache_dir_allowlist() -> None:
    """Codex review, PR #262, Critical (round 7): stop forwarding host secrets into checker
    containers.

    `checker_env` is `loop_driver_support.maker_env(os.environ, ...)`, which only strips a
    handful of push-authentication keys -- any other host secret still riding along in the
    driver process's own `os.environ` must never reach a Maker-authored mechanical command
    running inside the isolated container. Only `*_CACHE_DIR`-suffixed keys are forwarded;
    everything else, including real-world credential env var names, is dropped.
    """
    checker_env = {
        "RUFF_CACHE_DIR": "/host/ruff-cache",
        "MYPY_CACHE_DIR": "/host/mypy-cache",
        "AWS_SECRET_ACCESS_KEY": "super-secret",
        "OPENAI_API_KEY": "sk-should-not-leak",
        "ANTHROPIC_API_KEY": "sk-ant-should-not-leak",
        "SOME_OTHER_TOKEN": "also-should-not-leak",
    }

    merged = docker_action._mechanical_exec_env(checker_env)

    assert merged == {
        "RUFF_CACHE_DIR": "/tmp/ruff-cache",
        "MYPY_CACHE_DIR": "/host/mypy-cache",
    }


def test_mechanical_exec_env_ruff_cache_dir_default_wins_over_ambient_host_value() -> None:
    """Codex review, PR #262, P2 (round 8): the container-safe `RUFF_CACHE_DIR` default must
    always win, even when the caller's forwarded env carries an ambient value for the same key.

    `checker_env` is derived from the *host* driver process's own `os.environ`. An operator
    whose shell merely happens to export an ambient `RUFF_CACHE_DIR` pointing at a host path
    (e.g. `~/.cache/ruff`) -- with no intent to override anything Docker-specific -- must not
    have that host-only path silently forwarded into the container in place of the working
    `/tmp` default: that path does not exist inside the container's filesystem namespace and
    breaks the checker with no way for the allowlist to tell an ambient value apart from a
    deliberate override.
    """
    checker_env = {"RUFF_CACHE_DIR": "/Users/example/.cache/ruff"}

    merged = docker_action._mechanical_exec_env(checker_env)

    assert merged == {"RUFF_CACHE_DIR": "/tmp/ruff-cache"}


def test_align_mount_ownership_or_raise_normalizes_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, PR #262, High (round 6): fail-closed, not a raw worker crash.

    `align_mount_ownership()`'s `os.chown()` can raise `PermissionError`/`OSError` on a
    filesystem that rejects `chown` for the driver's identity (root-squash NFS, a disappearing
    bind source). `_ensure_started()` only normalizes a curated exception list that does not
    include raw `OSError`, so this must be normalized to `DockerActionError` at the call site
    instead of escaping unwrapped and crashing the whole driver process.
    """

    def _raise_permission_error(
        _path: Path, *, exclude: Any = None, protect_owner_only: bool = True
    ) -> None:
        del exclude, protect_owner_only
        raise PermissionError("chown not permitted")

    monkeypatch.setattr(
        docker_action.profile.runtime, "align_mount_ownership", _raise_permission_error
    )

    with pytest.raises(docker_action.DockerActionError):
        docker_action._align_mount_ownership_or_raise(tmp_path)


def test_mechanical_exec_env_defaults_ruff_cache_dir_to_a_writable_tmpfs_path() -> None:
    """Codex review, PR #262, High (round 4): default `RUFF_CACHE_DIR` for Docker mechanical runs.

    The bundled issue-loop's default mechanical commands include `ruff check .`; ruff defaults
    its cache directory to `.ruff_cache` under the project root unless `RUFF_CACHE_DIR` is set,
    but the checker worktree (the mechanical command's `cwd`) is mounted read-only under Docker.
    Without an explicit default, this fails before linting even runs unless the operator happens
    to export an override. `/tmp` is the container's own tmpfs mount, always writable regardless
    of `cwd`.
    """
    assert docker_action._mechanical_exec_env(None) == {"RUFF_CACHE_DIR": "/tmp/ruff-cache"}
    assert docker_action._mechanical_exec_env({}) == {"RUFF_CACHE_DIR": "/tmp/ruff-cache"}


def test_execute_mechanical_forwards_env_to_docker_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="checker", needs_broker=False),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.container_name = "lh-action"
    runtime._started = True
    monkeypatch.setattr(docker_action, "enforce_scenario_container_idle", lambda *_a, **_k: None)
    seen_env: list[dict[str, str]] = []

    def host_child(
        command: list[str], _cwd: str, _timeout: float, _env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        seen_env.append(dict(_env))
        # `--env KEY=VALUE` flags are what actually reach `docker exec`; assert on those too.
        rendered = " ".join(command)
        assert "--env RUFF_CACHE_DIR=/tmp/ruff-cache" in rendered
        assert "--env HOME=" not in rendered
        return subprocess.CompletedProcess(command, 0, "ok", "")

    runtime.host_child_runner = host_child
    output, exit_code = runtime.execute_mechanical(
        "true",
        "/tmp",
        30,
        env={"RUFF_CACHE_DIR": "/tmp/ruff-cache", "HOME": "/host/scratch-home"},
    )

    assert exit_code == 0
    assert output == "ok"


def test_execute_mechanical_normalizes_claude_p_timeout_to_exit_124(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, High (round 4): preserve mechanical timeout results in Docker.

    A per-command mechanical timeout must still destroy the scenario container (fail-closed --
    a killed `docker exec` client does not guarantee the exec'd process inside the container
    actually stopped, see `enforce_scenario_container_idle`), but the checker's sealed artifact
    contract expects an ordinary `(output, 124)` result here, the same as the host executor's
    `_run_mechanical_command`, not an opaque Docker infrastructure failure that discards this
    command's output and the sealed result entirely.
    """
    removed: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    def timed_out(*_args: Any) -> subprocess.CompletedProcess[str]:
        raise docker_action.driver_support.ClaudePTimeoutError(
            "claude -p timed out after 30s",
            stdout="partial output",
            stderr="partial error",
        )

    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="checker", needs_broker=False),
        host_child_runner=timed_out,
    )
    runtime.container_name = "lh-action"
    runtime._started = True

    output, exit_code = runtime.execute_mechanical("pytest -q", "/tmp", 30)

    assert exit_code == 124
    assert output == "partial outputpartial error\ncommand timed out"
    assert removed == ["lh-action"]
    assert runtime._scenario_removed is True


def test_mechanical_timeout_skips_subsequent_commands_instead_of_docker_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, High (round 5): a mechanical timeout must make the runtime refuse
    further `docker exec` attempts, not just this one command.

    The default checker runs several mechanical commands in sequence (e.g. `pytest -q` then
    `ruff check .`). Once the first command times out, `_execute()` has already destroyed the
    scenario container; a second call must not try `docker exec` against that removed container
    (which would surface as an opaque Docker infrastructure failure and discard the preserved
    timeout result), it must short-circuit to another `(output, 124)`-shaped result instead.
    """
    calls = {"host_child": 0}

    def timed_out(*_args: Any) -> subprocess.CompletedProcess[str]:
        calls["host_child"] += 1
        raise docker_action.driver_support.ClaudePTimeoutError(
            "claude -p timed out after 30s", stdout="partial output", stderr=""
        )

    monkeypatch.setattr(docker_action.runtime_cli, "remove_container", lambda *_a, **_k: True)
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="checker", needs_broker=False),
        host_child_runner=timed_out,
    )
    runtime.container_name = "lh-action"
    runtime._started = True

    first_output, first_exit_code = runtime.execute_mechanical("pytest -q", "/tmp", 30)
    second_output, second_exit_code = runtime.execute_mechanical("ruff check .", "/tmp", 30)

    assert first_exit_code == 124
    assert second_exit_code == 124
    assert "isolated runtime unusable" in second_output
    # Only the first command actually reached `docker exec`; the second was short-circuited.
    assert calls["host_child"] == 1


def test_mechanical_timeout_latch_also_short_circuits_execute_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, High (round 8): the mechanical-timeout latch (round 5, above) must
    also stop `execute_claude()`, not just later `execute_mechanical()` calls.

    A checker with both `mechanical` and `llm_review` layers calls `execute_claude()` right
    after `execute_mechanical()`. Once the mechanical command times out, `_execute()` has
    already destroyed the scenario container; without this guard, `execute_claude()` would still
    attempt a `docker exec` against the removed container and surface an opaque
    `DockerActionError` (instead of the typed `ClaudePTimeoutError` `_run_one_llm_reviewer()`
    already degrades into an ordinary infrastructure-failure `CheckResult`), discarding the
    perfectly sealed mechanical timeout result.
    """
    calls = {"host_child": 0}

    def timed_out(*_args: Any) -> subprocess.CompletedProcess[str]:
        calls["host_child"] += 1
        raise docker_action.driver_support.ClaudePTimeoutError(
            "claude -p timed out after 30s", stdout="partial output", stderr=""
        )

    monkeypatch.setattr(docker_action.runtime_cli, "remove_container", lambda *_a, **_k: True)
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier", needs_broker=False),
        host_child_runner=timed_out,
    )
    runtime.container_name = "lh-action"
    runtime._started = True

    _output, exit_code = runtime.execute_mechanical("pytest -q", "/tmp", 30)
    assert exit_code == 124

    with pytest.raises(docker_action.driver_support.ClaudePTimeoutError):
        runtime.execute_claude(["claude", "-p", "prompt"], "/tmp", 30, {})

    # Only the mechanical command reached a real host child; execute_claude's short-circuit
    # never attempted a docker exec against the already-removed container.
    assert calls["host_child"] == 1


def test_non_idle_process_forces_container_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.container_name = "lh-action"
    runtime._started = True
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "PID COMMAND COMMAND\n101 sleep /usr/bin/sleep 300\n", ""
        ),
    )
    removed: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    with pytest.raises(docker_action.DockerActionError, match="non-idle"):
        runtime._assert_idle_or_destroy()

    assert removed == ["lh-action"]
    assert runtime._scenario_removed is True


def test_allowed_idle_command_spoof_does_not_match_startup_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_output = (
        "PID COMMAND COMMAND\n"
        "101 docker-init /usr/bin/docker-init -- /usr/bin/timeout 660s "
        "/usr/bin/sleep infinity\n"
        "102 timeout /usr/bin/timeout 660s /usr/bin/sleep infinity\n"
        "103 sleep /usr/bin/sleep infinity\n"
    )
    spoofed_output = baseline_output + "104 sleep /usr/bin/sleep infinity\n"
    outputs = iter((baseline_output, spoofed_output))

    monkeypatch.setattr(
        docker_action.runtime_cli,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, next(outputs), ""),
    )
    removed: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    baseline = docker_action.capture_scenario_idle_baseline("lh-action")
    with pytest.raises(docker_action.DockerActionError, match="non-idle") as caught:
        docker_action.enforce_scenario_container_idle(
            "lh-action",
            expected_snapshot=baseline,
        )

    assert caught.value.container_removed is True
    assert removed == ["lh-action"]


def test_docker_exec_timeout_is_normalized_and_removes_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(*_args: Any) -> subprocess.CompletedProcess[str]:
        raise TimeoutError("docker exec timed out")

    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier"),
        host_child_runner=timed_out,
    )
    runtime.container_name = "lh-action"
    runtime._started = True
    runtime.broker = SimpleNamespace(
        base_url="http://lh-broker:8790",
        run_token="run-token",
    )
    removed: list[str] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    with pytest.raises(docker_action.DockerActionError, match="docker exec did not complete"):
        runtime.execute_claude(["claude", "-p", "prompt"], "/tmp", 30, {})

    assert removed == ["lh-action"]
    assert runtime._scenario_removed is True


def test_cancel_before_start_prevents_any_docker_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier"),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    daemon_checks: list[bool] = []
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "docker_daemon_available",
        lambda **_kwargs: daemon_checks.append(True) or True,
    )

    runtime.cancel()

    with pytest.raises(docker_action.DockerActionError, match="cancelled"):
        runtime.execute_mechanical("true", "/tmp", 30)

    assert daemon_checks == []


def test_cancel_removes_a_running_scenario_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier"),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.container_name = "lh-action"
    runtime._scenario_start_attempted = True
    runtime._started = True
    removed: list[str] = []
    broker_cleanups: list[bool] = []

    class Broker:
        def cleanup(self) -> None:
            broker_cleanups.append(True)

    runtime.broker = Broker()
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )

    runtime.cancel()
    runtime.cancel()

    assert removed == ["lh-action"]
    assert runtime._scenario_removed is True
    assert broker_cleanups == []

    runtime.finish(action_succeeded=False)

    assert broker_cleanups == [True]


def test_cancel_racing_scenario_start_removes_container_before_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind="classifier"),
        host_child_runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("cancelled scenario must never reach docker exec")
        ),
    )
    entered_start = threading.Event()
    release_start = threading.Event()
    removed: list[str] = []

    class Broker:
        internal_network = "lh-internal"

        def cleanup(self) -> None:
            return

    monkeypatch.setattr(
        docker_action.runtime_cli,
        "docker_daemon_available",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "sweep_stale_resources",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_scenario_image",
        lambda *_args, **_kwargs: SimpleNamespace(image_id=IMAGE_ID),
    )
    monkeypatch.setattr(
        docker_action.docker_image,
        "ensure_broker_image",
        lambda *_args, **_kwargs: SimpleNamespace(image_id=IMAGE_ID),
    )
    monkeypatch.setattr(
        docker_action.broker_runtime,
        "start_broker",
        lambda *_args, **_kwargs: Broker(),
    )

    def blocking_start(*_args: Any, **_kwargs: Any) -> None:
        entered_start.set()
        assert release_start.wait(timeout=5)

    monkeypatch.setattr(docker_action, "start_scenario_container", blocking_start)
    monkeypatch.setattr(
        docker_action,
        "capture_scenario_idle_baseline",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda name, **_kwargs: removed.append(name) or True,
    )
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            runtime.execute_mechanical("true", "/tmp", 30)
        except BaseException as exc:
            failures.append(exc)

    execute_thread = threading.Thread(target=execute)
    execute_thread.start()
    assert entered_start.wait(timeout=5)
    cancel_thread = threading.Thread(target=runtime.cancel)
    cancel_thread.start()
    assert runtime._cancel_requested.wait(timeout=5)
    assert cancel_thread.is_alive()
    release_start.set()
    execute_thread.join(timeout=5)
    cancel_thread.join(timeout=5)

    assert not execute_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], docker_action.DockerActionError)
    assert "cancelled" in str(failures[0])
    assert removed == [runtime.container_name]


def test_trusted_settings_bundle_is_readable_by_non_root_container_and_cleanupable(
    tmp_path: Path,
) -> None:
    guard = tmp_path / "maker_bash_guard.py"
    guard.write_text("raise SystemExit(0)\n", encoding="utf-8")

    bundle = docker_settings.create_settings_bundle(tmp_path / "runtime", guard)

    assert bundle.source_dir.stat().st_mode & 0o777 == 0o555
    assert (bundle.source_dir / "settings.json").stat().st_mode & 0o777 == 0o444
    assert (bundle.source_dir / "maker_bash_guard.py").stat().st_mode & 0o777 == 0o555
    docker_settings.cleanup_settings_bundle(bundle)
    assert not bundle.source_dir.exists()


def test_failed_maker_partial_worktree_safe_stop_preserves_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True
    finalized: list[bool] = []
    cleaned: list[bool] = []
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "verify_failed_maker_worktree",
        lambda *_args: (_ for _ in ()).throw(
            git_ephemeral.EphemeralGitSafetyStop("maker_partial_worktree", "partial worktree")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: finalized.append(True),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: cleaned.append(True),
    )

    with pytest.raises(git_ephemeral.EphemeralGitSafetyStop, match="partial worktree") as caught:
        runtime.finish(action_succeeded=False)

    assert caught.value.stop_reason == "maker_partial_worktree"
    assert finalized == []
    assert cleaned == [True]


def test_successful_maker_dirty_finalize_safe_stops_as_partial_worktree_not_infra_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, High (round 7): a Docker Maker that exits 0 but leaves the
    worktree dirty must reach the same documented `maker_partial_worktree` safe-stop as a failed
    Maker, not an opaque, unclassified infrastructure error -- and `finish()` must propagate the
    real `finalize_ephemeral_git()` conversion (see `loop_git_ephemeral._as_partial_worktree_stop`)
    as-is rather than swallowing or re-wrapping it, since `_finished` is already latched by the
    time this runs and a later `abort()` fallback would silently no-op.
    """
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True
    cleaned: list[bool] = []
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            git_ephemeral.EphemeralGitSafetyStop(
                "maker_partial_worktree", "Maker left uncommitted worktree changes"
            )
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: cleaned.append(True),
    )

    with pytest.raises(
        git_ephemeral.EphemeralGitSafetyStop, match="uncommitted worktree changes"
    ) as caught:
        runtime.finish(action_succeeded=True)

    assert caught.value.stop_reason == "maker_partial_worktree"
    assert cleaned == [True]


def test_discard_after_lease_loss_skips_git_finalize_and_verify_but_still_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local pre-push review (round 9, P1): `discard_after_lease_loss()` is the dedicated quiet
    teardown `loop_driver._dispatch()` calls instead of `finish()`/`abort()` in the exact race
    window where a Maker finished cleanly right before the driver's own lease was detected lost
    (see that method's own docstring). Unlike `finish()`/`abort()`, it must never reach
    `finalize_ephemeral_git()` (no CAS publish onto the shared branch) or `verify_failed_maker_
    worktree()` (no baseline-diff check, which always misclassifies a successful Maker's clean
    commit as `maker_partial_worktree` drift against the pre-Maker `baseline_sha`).

    Codex review, PR #262, P1 (round 11): unlike other exit paths, this method must also *not*
    call `cleanup_ephemeral_git()`/`cleanup_settings_bundle()` -- both operate on
    `self.git_session.runtime_dir`, which is deterministic per `(loop_id, action_id)`, not per
    attempt. By the time this quiet teardown runs, `attach(..., recover_orphans=True)` may
    already have handed this same pending action to a replacement worker that has re-run
    `prepare_ephemeral_git()` against that same path; deleting it here would destroy the
    replacement's live runtime instead of this stale worker's own. Only the scenario
    container/broker cleanup (randomly nonced, never reused across workers) is safe to run.
    """
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime.container_name = "lh-action"
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True
    events: list[str] = []

    class Broker:
        def cleanup(self) -> None:
            events.append("broker_cleanup")

    runtime.broker = Broker()
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("discard_after_lease_loss must never CAS-publish")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "verify_failed_maker_worktree",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("discard_after_lease_loss must never verify against baseline_sha")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError(
                "discard_after_lease_loss must never delete the shared "
                "(loop_id, action_id) runtime dir -- a replacement worker may already own it"
            )
        ),
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "cleanup_settings_bundle",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError(
                "discard_after_lease_loss must never delete the shared "
                "(loop_id, action_id) settings bundle -- a replacement worker may already own it"
            )
        ),
    )

    runtime.discard_after_lease_loss()

    assert events == ["broker_cleanup"]
    assert runtime._finished is True

    # Idempotent, like finish()/cancel(): a second call must not repeat any cleanup step.
    runtime.discard_after_lease_loss()

    assert events == ["broker_cleanup"]


def test_finish_skips_git_finalize_when_lease_lost_before_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, P1 (round 8, fence #2): `_cleanup_containers()` inside `finish()`
    can itself spend real wall-clock time destroying the scenario/broker/network, during which
    the driver's heartbeat thread can flip the `request.lease_lost` signal after
    `loop_driver._dispatch()` already decided to call `executor.finish(result)`. This proves
    `finish(action_succeeded=True)` re-checks `request.lease_lost()` right before
    `_finish_git()` and, if it is already true, never reaches `finalize_ephemeral_git()` (no CAS
    publish onto the shared branch).

    Codex review, PR #262, P1 (round 13): the same re-check now also gates `finish()`'s `finally`
    block, so `cleanup_ephemeral_git()`/`cleanup_settings_bundle()` (`_cleanup_local_runtime()`)
    must not run either once the lease is already lost -- a replacement worker may already have
    re-created the same deterministic `(loop_id, action_id)` runtime dir/settings bundle (see
    `discard_after_lease_loss()`'s docstring), so deleting it here would destroy a live run
    instead of this stale worker's own leftover.
    """
    events: list[str] = []
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, lease_lost=lambda: True),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("finish() must never CAS-publish once the lease is already lost")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "verify_failed_maker_worktree",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("finish() must never verify against baseline_sha once lease is lost")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError(
                "finish() must never delete the shared (loop_id, action_id) runtime dir once "
                "the lease is already lost -- a replacement worker may already own it"
            )
        ),
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "cleanup_settings_bundle",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError(
                "finish() must never delete the shared (loop_id, action_id) settings bundle "
                "once the lease is already lost -- a replacement worker may already own it"
            )
        ),
    )

    runtime.finish(action_succeeded=True)

    assert events == []
    assert runtime._finished is True


def test_finish_skips_local_runtime_cleanup_when_lease_lost_during_container_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, P1 (round 13): `_cleanup_containers()` can itself spend real
    wall-clock time (destroying the scenario/broker/network), during which the driver's
    heartbeat thread can flip `request.lease_lost()` from `False` to `True` -- exactly the
    scenario `finish()`'s own docstring calls out. This proves the post-cleanup
    `lease_already_lost` re-check gates `finish()`'s `finally`-block `_cleanup_local_runtime()`
    call too (not just `_finish_git()`): when the lease dies during `_cleanup_containers()`,
    neither `cleanup_ephemeral_git()` nor `cleanup_settings_bundle()` must run, because a
    replacement worker may already have re-created the same deterministic `(loop_id, action_id)`
    runtime dir/settings bundle once this worker's lease was confirmed lost.
    """
    lease_state = {"lost": False}
    events: list[str] = []
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, lease_lost=lambda: lease_state["lost"]),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True

    original_cleanup_containers = runtime._cleanup_containers

    def cleanup_containers_flips_lease() -> tuple[Any, list[str]]:
        result = original_cleanup_containers()
        lease_state["lost"] = True
        return result

    monkeypatch.setattr(runtime, "_cleanup_containers", cleanup_containers_flips_lease)
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("finish() must never CAS-publish once the lease is already lost")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: events.append("git_cleanup"),
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "cleanup_settings_bundle",
        lambda *_args: events.append("settings_cleanup"),
    )

    runtime.finish(action_succeeded=True)

    assert events == []
    assert runtime._finished is True


def test_finish_runs_local_runtime_cleanup_when_lease_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review, PR #262, P1 (round 13): the new `lease_already_lost` guard on `finish()`'s
    `finally` block must not regress the ordinary case where the lease is never lost -- local
    runtime cleanup (`cleanup_ephemeral_git()`/`cleanup_settings_bundle()`) still has to run so a
    successfully finished action does not leak its ephemeral GIT_DIR/settings bundle on disk.
    """
    events: list[str] = []
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, lease_lost=lambda: False),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: events.append("git_finalize"),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: events.append("git_cleanup"),
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "cleanup_settings_bundle",
        lambda *_args: events.append("settings_cleanup"),
    )

    runtime.finish(action_succeeded=True)

    assert events == ["git_finalize", "settings_cleanup", "git_cleanup"]
    assert runtime._finished is True


def test_abort_skips_baseline_verify_when_lease_lost_before_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same fence as `test_finish_skips_git_finalize_when_lease_lost_before_finish`, exercised via
    `abort()` (`finish(action_succeeded=False)`): once the lease is already lost, `abort()` must
    not diff the Maker's worktree against the stale `baseline_sha` via `verify_failed_maker_
    worktree()`, which would misclassify a Maker that committed cleanly before losing the lease
    as `maker_partial_worktree` drift.

    Codex review, PR #262, P1 (round 13): the same re-check now also gates `finish()`'s `finally`
    block, so `cleanup_ephemeral_git()`/`cleanup_settings_bundle()` (`_cleanup_local_runtime()`)
    must not run either once the lease is already lost -- see
    `test_finish_skips_git_finalize_when_lease_lost_before_finish` for the full rationale.
    """
    events: list[str] = []
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, lease_lost=lambda: True),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "verify_failed_maker_worktree",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("abort() must never verify against baseline_sha once lease is lost")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("abort() must never CAS-publish once the lease is already lost")
        ),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError(
                "abort() must never delete the shared (loop_id, action_id) runtime dir once "
                "the lease is already lost -- a replacement worker may already own it"
            )
        ),
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "cleanup_settings_bundle",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError(
                "abort() must never delete the shared (loop_id, action_id) settings bundle "
                "once the lease is already lost -- a replacement worker may already own it"
            )
        ),
    )

    runtime.finish(action_succeeded=False)

    assert events == []
    assert runtime._finished is True


def test_discard_after_lease_loss_never_raises_even_when_cleanup_steps_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`discard_after_lease_loss()` has no safe-stop channel left to persist a failure into once
    the lease is already gone (see its own docstring), so a container/broker cleanup failure must
    not escape as a raised exception, and must not stop `_finished` from being latched.

    Codex review, PR #262, P1 (round 11): `git_ephemeral.cleanup_ephemeral_git()` and
    `docker_settings.cleanup_settings_bundle()` must not be called at all here (see this method's
    own docstring) -- both are asserted unreachable rather than exercised, since this same test
    used to double as coverage for their best-effort error handling before that call was removed.
    """
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime.container_name = "lh-action"
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True

    class BrokenBroker:
        def cleanup(self) -> None:
            raise docker_action.broker_runtime.LoopDockerBrokerError("broker cleanup failed")

    runtime.broker = BrokenBroker()
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("discard_after_lease_loss must never call cleanup_ephemeral_git()")
        ),
    )
    monkeypatch.setattr(
        docker_action.docker_settings,
        "cleanup_settings_bundle",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("discard_after_lease_loss must never call cleanup_settings_bundle()")
        ),
    )

    runtime.discard_after_lease_loss()

    assert runtime._finished is True


def test_maker_finalizes_after_scenario_removal_when_broker_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.git_session = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    runtime._scenario_start_attempted = True
    runtime._scenario_removed = True

    class FailedBroker:
        def cleanup(self) -> None:
            raise broker_runtime.LoopDockerBrokerError("broker cleanup failed")

    runtime.broker = FailedBroker()
    finalized: list[bool] = []
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "finalize_ephemeral_git",
        lambda *_args: finalized.append(True),
    )
    monkeypatch.setattr(
        docker_action.git_ephemeral,
        "cleanup_ephemeral_git",
        lambda *_args: None,
    )

    with pytest.raises(
        docker_action.DockerActionSafetyStop,
        match="isolated action cleanup failed",
    ) as caught:
        runtime.finish(action_succeeded=True)

    assert caught.value.stop_reason == "action_cleanup_failed"
    assert caught.value.details == {"cleanup_errors": ["broker cleanup failed"]}
    assert finalized == [True]


@pytest.mark.parametrize(
    ("kind", "expected_reason"),
    [
        ("maker", "maker_container_cleanup_unconfirmed"),
        ("checker", "container_cleanup_unconfirmed"),
        ("classifier", "container_cleanup_unconfirmed"),
    ],
)
def test_unconfirmed_container_removal_safe_stops_every_action_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_reason: str,
) -> None:
    runtime = docker_action.DockerActionRuntime(
        _request(tmp_path, kind=kind),
        host_child_runner=lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    runtime.container_name = "lh-action"
    runtime._scenario_start_attempted = True
    runtime._started = True
    monkeypatch.setattr(
        docker_action.runtime_cli,
        "remove_container",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(docker_action.DockerActionSafetyStop) as caught:
        runtime.finish(action_succeeded=False)

    assert caught.value.stop_reason == expected_reason
