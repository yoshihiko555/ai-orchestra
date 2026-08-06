#!/usr/bin/env python3
"""OS-level isolation profile for meta-harness scenario runs."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import isolation as iso  # noqa: E402
import scenario_docker as docker  # noqa: E402

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

_ALLOW_READ_KEY = "allow" + "Read"
_DENY_READ_KEY = "deny" + "Read"
_ALLOW_WRITE_KEY = "allow" + "Write"
_DENY_WRITE_KEY = "deny" + "Write"
_RUNTIME_ROOT_ENV = "HO" + "ME"
_RUNTIME_CONFIG_DIR = "." + "claude"
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_SNAPSHOT_DIR = "git-snapshot"
_GIT_WRAPPER_DIR = "bin"
_IGNORED_BASELINE_FILENAME = "ignored-baseline.json"
_IMPLEMENTED_EXECUTION_BACKENDS: frozenset[str] = frozenset({"docker"})
_SYSTEM_TOOL_SEARCH_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
_SYSTEM_READ_ROOTS = (
    Path("/bin"),
    Path("/sbin"),
    Path("/usr"),
    Path("/System"),
    Path("/opt/homebrew"),
    Path("/Library/Apple"),
    Path("/dev"),
    Path("/private/etc"),
    Path("/private/var/db/timezone"),
)


class ScenarioIsolationError(iso.IsolationError):
    """Scenario execution must fail closed when isolation cannot be proven."""


def execution_boundary_available(config: dict) -> bool:
    """Return true only for a backend with credential and descendant-process containment."""
    isolation_config = (config.get("evaluate") or {}).get("isolation") or {}
    isolation_backend = isolation_config.get("backend", "srt")
    backend = isolation_config.get("execution_backend", "none")
    return isolation_backend == backend and backend in _IMPLEMENTED_EXECUTION_BACKENDS


@dataclass(frozen=True)
class ScenarioIsolationLaunch:
    executable: str
    settings_path: Path | None
    settings: dict
    env: dict[str, str]
    metadata: dict
    backend: str = "srt"
    docker_launch: docker.DockerScenarioLaunch | None = None
    owned_settings_dir: Path | None = None
    owned_tmp_dir: Path | None = None
    owned_runtime_state_dir: Path | None = None


def build_scenario_srt_settings(
    *,
    worktree_dir: Path,
    main_root: Path,
    config: dict,
    runtime_state_dir: Path,
    instruction_path: Path,
    run_tmp_dir: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> dict:
    """Build a deny-then-allow SRT profile for one candidate worktree."""
    worktree = _validated_directory(worktree_dir, "scenario worktree")
    runtime_state = _validated_directory(runtime_state_dir, "scenario runtime state")
    instruction = _validated_instruction(instruction_path)
    allowed_read = iso._dedupe_paths(
        [
            worktree,
            runtime_state,
            instruction,
            *([run_tmp_dir] if run_tmp_dir is not None else []),
            *_scenario_runtime_read_roots(),
        ]
    )
    iso._assert_no_forbidden_allow_read(allowed_read, iso.forbidden_read_paths(main_root, config))
    allowed_write = iso._dedupe_paths(
        [
            worktree,
            runtime_state / _RUNTIME_CONFIG_DIR,
            *([run_tmp_dir] if run_tmp_dir is not None else []),
        ]
    )
    return {
        "network": {
            "allowedDomains": list(iso.CLAUDE_BARE_ALLOWED_DOMAINS),
            "deniedDomains": [],
            "strictAllowlist": True,
            "allowUnixSockets": [],
            "allowLocalBinding": False,
        },
        "filesystem": {
            _DENY_READ_KEY: [str(Path("/"))],
            _ALLOW_READ_KEY: [str(path) for path in allowed_read],
            _ALLOW_WRITE_KEY: [str(path) for path in allowed_write],
            _DENY_WRITE_KEY: [],
        },
        "ignoreViolations": {},
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }


def resolve_scenario_isolation(
    *,
    worktree_dir: Path,
    main_root: Path,
    config: dict,
    instruction_path: Path,
    source_commit: str,
    runtime_state_dir: Path | None = None,
    settings_dir: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> ScenarioIsolationLaunch:
    """Resolve a launch only after a complete credential/process containment backend exists."""
    if not execution_boundary_available(config):
        raise ScenarioIsolationError(
            "scenario execution boundary unavailable: credential broker and detached-process "
            "containment are required"
        )
    isolation_config = (config.get("evaluate") or {}).get("isolation") or {}
    backend = isolation_config.get("backend", "srt")
    if backend == "docker":
        try:
            docker_launch = docker.resolve_docker_launch(
                worktree_dir=worktree_dir,
                main_root=main_root,
                config=config,
                instruction_path=instruction_path,
                source_commit=source_commit,
                runtime_state_dir=runtime_state_dir,
                runner=runner,
                prepare_git_snapshot=_prepare_isolated_git,
            )
        except (
            docker.DockerScenarioError,
            docker.dcli.DockerCliError,
            docker.credentials.ClaudeCredentialError,
            iso.IsolationError,
        ) as exc:
            raise ScenarioIsolationError(str(exc)) from exc
        return ScenarioIsolationLaunch(
            executable="docker",
            settings_path=None,
            settings={},
            env=docker_launch.env,
            metadata=docker_launch.metadata,
            backend="docker",
            docker_launch=docker_launch,
        )
    return _resolve_scenario_isolation_profile(
        worktree_dir=worktree_dir,
        main_root=main_root,
        config=config,
        instruction_path=instruction_path,
        source_commit=source_commit,
        runtime_state_dir=runtime_state_dir,
        settings_dir=settings_dir,
        runner=runner,
    )


def _resolve_scenario_isolation_profile(
    *,
    worktree_dir: Path,
    main_root: Path,
    config: dict,
    instruction_path: Path,
    source_commit: str,
    runtime_state_dir: Path | None = None,
    settings_dir: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> ScenarioIsolationLaunch:
    """Build and test the filesystem profile without claiming execution readiness."""
    isolation_config = (config.get("evaluate") or {}).get("isolation") or {}
    backend = isolation_config.get("backend", "srt")
    if backend != "srt":
        raise ScenarioIsolationError(f"unsupported evaluate.isolation.backend: {backend!r}")
    owns_runtime_state = runtime_state_dir is None
    owns_settings = settings_dir is None
    runtime_state = runtime_state_dir
    launch_settings = settings_dir
    run_tmp: Path | None = None
    try:
        runtime_state = runtime_state or _create_private_dir("mh-scenario-state-")
        launch_settings = launch_settings or _create_private_dir("mh-scenario-srt-")
        runtime_state.chmod(0o700)
        (runtime_state / _RUNTIME_CONFIG_DIR).mkdir(mode=0o700, exist_ok=True)
        run_tmp = iso._create_run_tmp_dir()
        executable = iso._require_srt_binary()
        version = iso._get_srt_version(executable, runner=runner)
        _check_version_pin(isolation_config, version)
        git_wrapper_dir = _prepare_isolated_git(
            worktree_dir=worktree_dir,
            runtime_state_dir=runtime_state,
            source_commit=source_commit,
            runner=runner,
        )
        settings = build_scenario_srt_settings(
            worktree_dir=worktree_dir,
            main_root=main_root,
            config=config,
            runtime_state_dir=runtime_state,
            instruction_path=instruction_path,
            run_tmp_dir=run_tmp,
            runner=runner,
        )
        settings_path = iso.write_srt_settings(settings, launch_settings)
        env = _scenario_env(worktree_dir, runtime_state, run_tmp, git_wrapper_dir)
        iso._run_srt_canary_self_test(
            srt_path=executable,
            settings_path=settings_path,
            view_dir=worktree_dir,
            main_root=main_root,
            config=config,
            env=env,
            runner=runner,
        )
        return ScenarioIsolationLaunch(
            executable=executable,
            settings_path=settings_path,
            settings=settings,
            env=env,
            metadata={
                **iso.build_isolation_metadata(
                    backend_name="srt", srt_version=version, settings=settings
                ),
                "git": {"mode": "isolated-snapshot", "source_commit": source_commit},
            },
            owned_settings_dir=launch_settings if owns_settings else None,
            owned_tmp_dir=run_tmp,
            owned_runtime_state_dir=runtime_state if owns_runtime_state else None,
        )
    except Exception as exc:
        if owns_settings and launch_settings is not None:
            shutil.rmtree(launch_settings, ignore_errors=True)
        if owns_runtime_state and runtime_state is not None:
            shutil.rmtree(runtime_state, ignore_errors=True)
        if run_tmp is not None:
            shutil.rmtree(run_tmp, ignore_errors=True)
        if isinstance(exc, ScenarioIsolationError):
            raise
        if isinstance(exc, iso.IsolationError):
            raise ScenarioIsolationError(str(exc)) from exc
        raise


def cleanup_scenario_isolation(launch: ScenarioIsolationLaunch) -> None:
    if launch.backend == "docker" and launch.docker_launch is not None:
        docker.cleanup_docker_launch(launch.docker_launch)
        return
    for path in (
        launch.owned_settings_dir,
        launch.owned_tmp_dir,
        launch.owned_runtime_state_dir,
    ):
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)


def write_oracle_srt_settings(launch: ScenarioIsolationLaunch) -> Path:
    """Derive a no-network, read-only-worktree profile for post-scenario oracles."""
    if launch.backend != "srt" or launch.settings_path is None:
        raise ScenarioIsolationError("SRT oracle settings requested for a non-SRT launch")
    settings = json.loads(json.dumps(launch.settings))
    settings["network"]["allowedDomains"] = []
    settings["filesystem"][_ALLOW_WRITE_KEY] = (
        [str(launch.owned_tmp_dir.resolve())] if launch.owned_tmp_dir is not None else []
    )
    return iso.write_srt_settings(settings, launch.settings_path.parent / "oracle")


def build_scenario_command(
    launch: ScenarioIsolationLaunch, raw_command: list[str]
) -> tuple[list[str], list[str] | None]:
    if launch.backend == "docker" and launch.docker_launch is not None:
        return (
            docker.build_scenario_command(launch.docker_launch, raw_command),
            launch.docker_launch.cleanup_command,
        )
    if launch.settings_path is None:
        raise ScenarioIsolationError("SRT launch has no settings path")
    return [launch.executable, "--settings", str(launch.settings_path), *raw_command], None


def build_oracle_command(
    launch: ScenarioIsolationLaunch,
    command: str,
) -> tuple[list[str], list[str] | None]:
    if launch.backend == "docker" and launch.docker_launch is not None:
        container_name = f"{docker.NAME_PREFIX}oracle-{os.urandom(3).hex()}"
        return (
            docker.build_oracle_command(
                launch.docker_launch,
                command,
                container_name=container_name,
            ),
            ["docker", "rm", "-f", container_name],
        )
    settings_path = write_oracle_srt_settings(launch)
    return (
        [launch.executable, "--settings", str(settings_path), "/bin/sh", "-c", command],
        None,
    )


def build_judge_command(
    launch: ScenarioIsolationLaunch,
    claude_command: list[str],
    *,
    max_output_tokens: int,
) -> tuple[list[str], list[str] | None]:
    if launch.backend != "docker" or launch.docker_launch is None:
        return claude_command, None
    container_name = f"{docker.NAME_PREFIX}judge-{os.urandom(3).hex()}"
    return (
        docker.build_judge_command(
            launch.docker_launch,
            claude_command,
            container_name=container_name,
            max_output_tokens=max_output_tokens,
        ),
        ["docker", "rm", "-f", container_name],
    )


def resolve_max_output_tokens_default(config: dict) -> int:
    return docker.profile.resolve_max_output_tokens_default(config)


def refresh_isolation_metadata(launch: ScenarioIsolationLaunch) -> dict:
    if launch.backend == "docker" and launch.docker_launch is not None:
        updated = docker.refresh_launch_metadata(launch.docker_launch)
        launch.metadata.clear()
        launch.metadata.update(updated)
    return dict(launch.metadata)


def export_scenario_workspace(launch: ScenarioIsolationLaunch) -> None:
    if launch.backend == "docker" and launch.docker_launch is not None:
        docker.export_docker_workspace(launch.docker_launch)


def _scenario_env(
    worktree_dir: Path,
    runtime_state_dir: Path,
    run_tmp_dir: Path,
    git_wrapper_dir: Path,
) -> dict[str, str]:
    runtime_state = runtime_state_dir.resolve()
    inherited_path = iso.build_minimal_env().get("PATH", "/usr/bin:/bin")
    return iso.build_minimal_env(
        {
            _RUNTIME_ROOT_ENV: str(runtime_state),
            "CLAUDE_CONFIG_DIR": str(runtime_state / _RUNTIME_CONFIG_DIR),
            "AI_ORCHESTRA_DIR": str(worktree_dir.resolve()),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_DIR": str(runtime_state / _GIT_SNAPSHOT_DIR),
            "GIT_WORK_TREE": str(worktree_dir.resolve()),
            "PATH": f"{git_wrapper_dir.resolve()}:{inherited_path}",
            **iso._tmp_env(run_tmp_dir),
        }
    )


def _scenario_runtime_read_roots() -> list[Path]:
    roots = [path.resolve() for path in _SYSTEM_READ_ROOTS if path.exists()]
    for tool in ("claude", "srt", "git", "python", "python3", "pytest", "curl"):
        executable = shutil.which(tool, path=_SYSTEM_TOOL_SEARCH_PATH)
        if executable is None:
            continue
        original = Path(executable).absolute()
        resolved = original.resolve()
        if _under_real_home(original) or _under_real_home(resolved):
            continue
        roots.extend([original.parent, resolved.parent])
        if original.parent.name == "bin":
            roots.append(original.parent.parent)
        if resolved.parent.name == "bin":
            roots.append(resolved.parent.parent)
    return iso._dedupe_paths(roots)


def _prepare_isolated_git(
    *,
    worktree_dir: Path,
    runtime_state_dir: Path,
    source_commit: str,
    runner: SubprocessRunner,
    container_paths: bool = False,
) -> Path:
    """Create candidate-visible Git metadata without exposing the linked worktree metadata."""
    if not _SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise ScenarioIsolationError(
            f"invalid source_commit for scenario isolation: {source_commit!r}"
        )
    git_path = shutil.which("git")
    if git_path is None:
        raise ScenarioIsolationError("git executable not found on PATH")
    worktree = _validated_directory(worktree_dir, "scenario worktree")
    runtime_state = _validated_directory(runtime_state_dir, "scenario runtime state")
    snapshot_dir = runtime_state / _GIT_SNAPSHOT_DIR
    wrapper_dir = runtime_state / _GIT_WRAPPER_DIR
    wrapper_mode = 0o711 if container_paths else 0o700
    wrapper_dir.mkdir(mode=wrapper_mode, exist_ok=True)
    wrapper_dir.chmod(wrapper_mode)
    git_env = iso.build_minimal_env(
        {
            _RUNTIME_ROOT_ENV: str(runtime_state),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    commands = [
        [git_path, "init", "--bare", str(snapshot_dir)],
        [git_path, f"--git-dir={snapshot_dir}", f"--work-tree={worktree}", "add", "--all", "--"],
        [
            git_path,
            f"--git-dir={snapshot_dir}",
            f"--work-tree={worktree}",
            "-c",
            "user.name=meta-harness",
            "-c",
            "user.email=meta-harness@invalid",
            "commit",
            "--allow-empty",
            "--no-gpg-sign",
            "-m",
            "scenario baseline",
        ],
    ]
    for command in commands:
        try:
            completed = runner(
                command,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=60,
                env=git_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScenarioIsolationError(f"could not create isolated Git snapshot: {exc}") from exc
        if completed.returncode != 0:
            raise ScenarioIsolationError(
                "could not create isolated Git snapshot: "
                f"{(completed.stderr or completed.stdout).strip()[:500]}"
            )
    ignored_paths = _collect_ignored_baseline_paths(
        runner=runner,
        git_path=git_path,
        snapshot_dir=snapshot_dir,
        worktree=worktree,
        git_env=git_env,
    )
    ignored_baseline_path = runtime_state / _IGNORED_BASELINE_FILENAME
    ignored_baseline_path.write_text(
        json.dumps({"ignored_paths": ignored_paths}, indent=2) + "\n",
        encoding="utf-8",
    )
    # `write_text` leaves the mode at whatever the host umask dictates. The oracle container
    # bind-mounts this file read-only and runs as a non-root `--user {uid}:{gid}` (see
    # `scenario_docker_profile.build_oracle_command`); a host umask of e.g. 077 would leave
    # this world/group-unreadable (0o600, owned by the host user), so that non-root container
    # user could not read it at all -- `_load_ignored_baseline` would then see an `OSError` and
    # every collateral-scope scenario would fail closed with a misleading "invalid ignored
    # baseline file" error instead of the intended ignored-path diffing. Pin the mode
    # explicitly, mirroring `wrapper_path`'s existing container_paths-conditional chmod below.
    ignored_baseline_path.chmod(0o644 if container_paths else 0o600)
    wrapper_path = wrapper_dir / "git"
    # The container path is supplied by docker/scenario/Dockerfile, which installs git.
    wrapper_git = "/usr/bin/git" if container_paths else git_path
    wrapper_snapshot = "/runtime/git-snapshot" if container_paths else str(snapshot_dir)
    wrapper_worktree = "/workspace" if container_paths else str(worktree)
    quoted_git = shlex.quote(wrapper_git)
    quoted_snapshot = shlex.quote(wrapper_snapshot)
    quoted_worktree = shlex.quote(wrapper_worktree)
    # Issue #357: the wrapper used to special-case the exact-argument forms
    # `rev-parse --short HEAD` / `rev-parse HEAD` and fake their output to
    # `source_commit`, while any other equivalent invocation (e.g. with a `-C`
    # global option) fell through to this same `exec` line and returned the
    # snapshot repository's real HEAD instead. That gave two contradictory
    # "truths" for the same question depending on how the caller phrased the
    # command, so an agent and an oracle disagreeing on invocation form could
    # disagree on HEAD too. Always exec the real git against the snapshot so
    # every invocation form resolves identically and consistently.
    wrapper_path.write_text(
        "#!/bin/sh\n"
        f'exec {quoted_git} --git-dir={quoted_snapshot} --work-tree={quoted_worktree} "$@"\n',
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755 if container_paths else 0o700)
    return wrapper_dir.resolve()


def _collect_ignored_baseline_paths(
    *,
    runner: SubprocessRunner,
    git_path: str,
    snapshot_dir: Path,
    worktree: Path,
    git_env: dict[str, str],
) -> list[str]:
    """Record ignored paths hidden when the fresh snapshot applies gitignore rules."""
    command = [
        git_path,
        f"--git-dir={snapshot_dir}",
        f"--work-tree={worktree}",
        "status",
        "--porcelain",
        "-z",
        "--ignored=matching",
        "--untracked-files=all",
    ]
    try:
        completed = runner(
            command,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=60,
            env=git_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScenarioIsolationError(f"could not collect ignored Git baseline: {exc}") from exc
    if completed.returncode != 0:
        raise ScenarioIsolationError(
            "could not collect ignored Git baseline: "
            f"{(completed.stderr or completed.stdout).strip()[:500]}"
        )

    tokens = (completed.stdout or "").split("\0")
    if tokens and not tokens[-1]:
        tokens.pop()
    ignored_paths: set[str] = set()
    for token in tokens:
        if token[:2] != "!!":
            continue
        entry = token[3:]
        if entry.endswith("/"):
            ignored_paths.update(_walk_ignored_directory_files(worktree, entry))
        else:
            ignored_paths.add(entry)
    return sorted(ignored_paths)


def _walk_ignored_directory_files(worktree: Path, relative_directory: str) -> list[str]:
    """Expand a collapsed ignored directory without following symlinked subdirectories.

    `os.walk(..., followlinks=False)` never recurses into a symlinked subdirectory, but it
    still reports that subdirectory's name in `dirnames` -- it does *not* silently drop it.
    The original implementation only inspected `filenames`, so a candidate-created symlink
    pointing at a directory (e.g. `ln -s / .claude/meta-harness/evil-link`) never appeared in
    the returned paths at all, letting it slip past the unconditional symlink rejection in
    every caller. Each symlinked subdirectory name must therefore be recorded here as its own
    path (its target is irrelevant -- only that a symlink exists at that path matters to
    callers), and then pruned from `dirnames` in place so the walk still does not descend into
    it (topdown `os.walk` respects in-place mutation of the `dirnames` list it yields).
    """
    paths: list[str] = []
    for directory, subdirectories, filenames in os.walk(
        worktree / relative_directory, followlinks=False
    ):
        symlinked_subdirectories = [
            name for name in subdirectories if (Path(directory) / name).is_symlink()
        ]
        for name in symlinked_subdirectories:
            paths.append((Path(directory) / name).relative_to(worktree).as_posix())
        subdirectories[:] = [
            name for name in subdirectories if name not in symlinked_subdirectories
        ]
        for filename in filenames:
            path = Path(directory) / filename
            if not path.is_file() and not path.is_symlink():
                continue
            paths.append(path.relative_to(worktree).as_posix())
    return paths


def _under_real_home(path: Path) -> bool:
    try:
        path.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return False
    return True


def _check_version_pin(isolation_config: dict, version: str) -> None:
    expected = isolation_config.get("srt_version_pin")
    if expected is not None and version != expected:
        raise ScenarioIsolationError(
            f"evaluate.isolation.srt_version_pin mismatch: expected {expected!r}, got {version!r}"
        )


def _validated_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ScenarioIsolationError(f"{label} must be a regular directory: {path}")
    return path.resolve()


def _validated_instruction(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ScenarioIsolationError(
            f"scenario instruction must be a regular non-symlink file: {path}"
        )
    return path.resolve()


def _create_private_dir(prefix: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    path.chmod(0o700)
    return path.resolve()
