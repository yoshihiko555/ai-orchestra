#!/usr/bin/env python3
"""Docker + ephemeral OAuth broker backend for meta-harness scenario runs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _LIB_DIR.parent
_DOCKER_DIR = _PACKAGE_DIR / "docker"
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
if str(_DOCKER_RUNTIME_LIB) not in sys.path:
    sys.path.insert(0, str(_DOCKER_RUNTIME_LIB))

import claude_credentials as credentials
import docker_runtime_lifecycle as lifecycle
import scenario_docker_cli as dcli
import scenario_docker_profile as profile
import scenario_process as sproc

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

DOCKER_LABEL = dcli.DOCKER_LABEL
OWNER_LABEL = f"{DOCKER_LABEL}.owner"
PARENT_PID_LABEL = f"{DOCKER_LABEL}.parent-pid"
CREATED_AT_LABEL = f"{DOCKER_LABEL}.created-at"
NAME_PREFIX, BROKER_ALIAS = profile.NAME_PREFIX, profile.BROKER_ALIAS
CONTAINER_WORKTREE = profile.CONTAINER_WORKTREE
CONTAINER_RUNTIME = profile.CONTAINER_RUNTIME
CONTAINER_INSTRUCTION = profile.CONTAINER_INSTRUCTION
CONTAINER_HOME = profile.CONTAINER_HOME
CONTAINER_TMP = profile.CONTAINER_TMP
CONTAINER_BROKER_SCRIPT = "/app/broker.py"
DEFAULT_SCENARIO_IMAGE = dcli.DEFAULT_SCENARIO_IMAGE
DEFAULT_BROKER_IMAGE = dcli.DEFAULT_BROKER_IMAGE
DEFAULT_CLAUDE_VERSION_PIN = dcli.DEFAULT_CLAUDE_VERSION_PIN
CAPABILITY_TIMEOUT_SECONDS = 90
STALE_MAX_AGE_SECONDS = 24 * 60 * 60
WORKSPACE_EXPORT_TIMEOUT_SECONDS = 60
_LOGGER = logging.getLogger(__name__)
_RUNTIME_LABELS = lifecycle.RuntimeLabels(DOCKER_LABEL, STALE_MAX_AGE_SECONDS)
_SEMVER_PIN_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$")


class DockerScenarioError(RuntimeError):
    """The Docker execution boundary could not be constructed or verified."""


@dataclass(frozen=True)
class DockerCapabilityResult:
    claude_version: str | None
    version_pin: str | None
    version_pin_match: bool | None
    checks: dict[str, bool]
    reason: str | None

    @property
    def ok(self) -> bool:
        return (
            self.claude_version is not None
            and self.version_pin_match is not False
            and all(self.checks.values())
        )


@dataclass
class DockerBrokerSession:
    container_name: str
    internal_network: str
    external_network: str
    run_token: str
    port: int
    scenario_image: str
    broker_image: str
    image_id: str
    broker_image_id: str
    broker_settings_sha256: str
    scenario_context_sha256: str
    broker_context_sha256: str
    scenario_base_image: str
    broker_base_image: str
    owner_labels: dict[str, str]
    runner: SubprocessRunner = field(repr=False)
    idle_timeout_seconds: int = 300
    metrics: dict[str, Any] = field(default_factory=dict)
    cleaned: bool = False
    _keepalive_stop: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _keepalive_thread: threading.Thread | None = field(default=None, init=False, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{BROKER_ALIAS}:{self.port}"

    def refresh_metrics(self) -> dict[str, Any]:
        if self.cleaned:
            return dict(self.metrics)
        completed = _run(
            [
                "docker",
                "exec",
                self.container_name,
                "/usr/bin/python3",
                CONTAINER_BROKER_SCRIPT,
                "--print-metrics",
            ],
            runner=self.runner,
            timeout=10,
        )
        if completed.returncode != 0:
            raise DockerScenarioError("could not read credential broker metrics")
        try:
            value = json.loads(completed.stdout)
        except (ValueError, json.JSONDecodeError) as exc:
            raise DockerScenarioError("credential broker metrics are not valid JSON") from exc
        if not isinstance(value, dict):
            raise DockerScenarioError("credential broker metrics must be a JSON object")
        self.metrics = value
        return dict(self.metrics)

    def start_keepalive(self) -> None:
        if self._keepalive_thread is not None:
            return
        interval = max(1, min(30, self.idle_timeout_seconds // 3))
        self._keepalive_thread = threading.Thread(
            target=_broker_keepalive_loop,
            args=(self, self._keepalive_stop),
            kwargs={"interval_seconds": interval},
            daemon=True,
        )
        self._keepalive_thread.start()

    def stop_keepalive(self) -> None:
        self._keepalive_stop.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=11)
            self._keepalive_thread = None

    def cleanup(self) -> None:
        lifecycle.cleanup_broker_session(
            self,
            error_type=DockerScenarioError,
            remove_container=_remove_container,
            remove_network=_remove_network,
        )


@dataclass
class DockerScenarioLaunch:
    backend: str
    env: dict[str, str]
    metadata: dict[str, Any]
    broker: DockerBrokerSession
    runtime_state_dir: Path
    worktree_dir: Path
    instruction_path: Path
    scenario_container_name: str
    owned_runtime_state_dir: Path | None
    cleaned: bool = False

    @property
    def cleanup_command(self) -> list[str]:
        return ["docker", "rm", "-f", self.scenario_container_name]


def check_docker_capabilities(
    config: dict,
    *,
    main_root: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> DockerCapabilityResult:
    """Validate the exact Docker image and broker-backed CLI path before worktree creation."""
    checks: dict[str, bool] = {}
    version_pin = _isolation_config(config).get("image_pin", DEFAULT_CLAUDE_VERSION_PIN)
    # The capability gate must use the same output-token budget as real scenario and judge runs.
    max_output_tokens = profile.resolve_max_output_tokens_default(config)
    try:
        checks["docker_daemon"] = dcli.docker_daemon_available(runner=runner)
        if not checks["docker_daemon"]:
            return _capability_failure(None, version_pin, checks, "Docker daemon unavailable")
        owner_id = _owner_id(main_root or Path.cwd())
        sweep_stale_resources(owner_id, runner=runner)
        with docker_broker_session(
            config, "capability", owner_id=owner_id, runner=runner
        ) as broker:
            checks["scenario_image"] = True
            checks["broker_image"] = True
            version = _image_claude_version(broker.image_id, runner=runner)
            version_match = (
                None if version_pin is None else _version_matches(version, str(version_pin))
            )
            if version_match is False:
                return DockerCapabilityResult(
                    version,
                    version_pin,
                    False,
                    checks,
                    f"image_pin mismatch: expected {version_pin!r}, got {version!r}",
                )
            checks["broker"] = True
            model = (config.get("evaluate") or {}).get("model")
            model_args = ["--model", model] if model else []
            stream = _run_smoke_container(
                broker,
                [
                    "claude",
                    "-p",
                    "Reply OK",
                    "--max-turns",
                    "1",
                    "--no-session-persistence",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    *model_args,
                ],
                max_output_tokens=max_output_tokens,
                runner=runner,
            )
            checks["stream_json"] = stream.returncode == 0 and '"type":"result"' in stream.stdout
            budget = _run_smoke_container(
                broker,
                [
                    "claude",
                    "-p",
                    "Reply OK",
                    "--max-turns",
                    "1",
                    "--no-session-persistence",
                    "--output-format",
                    "json",
                    "--max-budget-usd",
                    "0.02",
                    *model_args,
                ],
                max_output_tokens=max_output_tokens,
                runner=runner,
            )
            checks["max_budget_usd"] = _has_result_json(budget.stdout)
            judge_tool = (config.get("judge") or {}).get("tool", "claude-bare")
            if judge_tool == "claude-bare":
                judge_model = (config.get("judge") or {}).get("model")
                judge_model_args = ["--model", judge_model] if judge_model else []
                bare = _run_smoke_container(
                    broker,
                    [
                        "claude",
                        "-p",
                        'Reply with JSON: {"ok": true}',
                        "--bare",
                        "--no-session-persistence",
                        "--output-format",
                        "json",
                        "--json-schema",
                        json.dumps(
                            {
                                "type": "object",
                                "required": ["ok"],
                                "properties": {"ok": {"type": "boolean"}},
                            }
                        ),
                        "--max-turns",
                        "1",
                        *judge_model_args,
                    ],
                    max_output_tokens=max_output_tokens,
                    runner=runner,
                )
                checks["bare"] = bare.returncode == 0
                checks["json_schema"] = bare.returncode == 0
                checks["broker_auth"] = bare.returncode == 0
            elif judge_tool == "codex":
                # False is intentional: Docker cannot prove host Codex's worktree read deny.
                checks["codex_judge_read_deny"] = False
            else:
                # False records an unsupported configured backend in the capability report.
                checks["known_judge_backend"] = False
        reason = _failed_checks_reason(checks)
        return DockerCapabilityResult(version, version_pin, version_match, checks, reason)
    except (
        DockerScenarioError,
        dcli.DockerCliError,
        credentials.ClaudeCredentialError,
    ) as exc:
        checks.setdefault("docker_backend", False)
        return _capability_failure(None, version_pin, checks, str(exc))


def resolve_docker_launch(
    *,
    worktree_dir: Path,
    main_root: Path,
    config: dict,
    instruction_path: Path,
    source_commit: str,
    runtime_state_dir: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
    prepare_git_snapshot: Callable[..., Path],
) -> DockerScenarioLaunch:
    worktree = _regular_directory(worktree_dir, "scenario worktree")
    instruction = _regular_file(instruction_path, "scenario instruction")
    owns_runtime = runtime_state_dir is None
    runtime = runtime_state_dir or Path(tempfile.mkdtemp(prefix="mh-scenario-docker-"))
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o755)
    broker: DockerBrokerSession | None = None
    try:
        prepare_git_snapshot(
            worktree_dir=worktree,
            runtime_state_dir=runtime,
            source_commit=source_commit,
            runner=runner,
            container_paths=True,
        )
        git_link_mask = runtime / "git-link-mask"
        git_link_mask.write_text("", encoding="utf-8")
        git_link_mask.chmod(0o444)
        run_name = profile.safe_name(source_commit[:8] + "-" + secrets.token_hex(3))
        broker = _start_broker(config, run_name, owner_id=_owner_id(main_root), runner=runner)
        scenario_name = f"{NAME_PREFIX}{run_name}-scenario"
        env = _docker_host_env()
        metadata = profile.launch_metadata(
            config=config,
            broker=broker,
            runtime=runtime,
            worktree=worktree,
            instruction=instruction,
            source_commit=source_commit,
        )
        return DockerScenarioLaunch(
            backend="docker",
            env=env,
            metadata=metadata,
            broker=broker,
            runtime_state_dir=runtime,
            worktree_dir=worktree,
            instruction_path=instruction,
            scenario_container_name=scenario_name,
            owned_runtime_state_dir=runtime if owns_runtime else None,
        )
    except Exception:
        if broker is not None:
            broker.cleanup()
        if owns_runtime:
            shutil.rmtree(runtime, ignore_errors=True)
        raise


def build_scenario_command(launch: DockerScenarioLaunch, raw_command: list[str]) -> list[str]:
    try:
        _checked(
            profile.build_scenario_container_command(launch),
            runner=launch.broker.runner,
            message="could not start scenario Docker container",
        )
        _checked(
            profile.build_workspace_init_command(launch.scenario_container_name),
            runner=launch.broker.runner,
            message="could not initialize scenario Docker workspace",
        )
        return profile.build_workspace_exec_command(launch.scenario_container_name, raw_command)
    except profile.DockerProfileError as exc:
        _remove_container(launch.scenario_container_name, runner=launch.broker.runner)
        raise DockerScenarioError(str(exc)) from exc
    except DockerScenarioError:
        _remove_container(launch.scenario_container_name, runner=launch.broker.runner)
        raise


def build_oracle_command(
    launch: DockerScenarioLaunch,
    command: str,
    *,
    container_name: str,
) -> list[str]:
    try:
        return profile.build_oracle_command(launch, command, container_name=container_name)
    except profile.DockerProfileError as exc:
        raise DockerScenarioError(str(exc)) from exc


def build_judge_command(
    launch: DockerScenarioLaunch,
    claude_command: list[str],
    *,
    container_name: str,
    max_output_tokens: int,
) -> list[str]:
    try:
        return profile.build_judge_command(
            launch,
            claude_command,
            container_name=container_name,
            max_output_tokens=max_output_tokens,
        )
    except profile.DockerProfileError as exc:
        raise DockerScenarioError(str(exc)) from exc


def run_preparation_command(
    *,
    config: dict,
    main_root: Path,
    worktree_dir: Path,
    source_commit: str,
    prepare_git_snapshot: Callable[..., Path],
    raw_command: list[str],
    timeout_seconds: float,
    runner: SubprocessRunner = subprocess.run,
) -> subprocess.CompletedProcess:
    scenario_image, _broker_image = ensure_images(config, runner=runner)
    image_id = _image_id(scenario_image, runner=runner)
    expected = _isolation_config(config).get("image_pin", DEFAULT_CLAUDE_VERSION_PIN)
    actual = _image_claude_version(image_id, runner=runner)
    if expected is not None and not _version_matches(actual, str(expected)):
        raise DockerScenarioError(f"image_pin mismatch: expected {expected!r}, got {actual!r}")
    container_name = f"{NAME_PREFIX}prepare-{secrets.token_hex(4)}"
    worktree = _regular_directory(worktree_dir, "scenario worktree")
    runtime = Path(tempfile.mkdtemp(prefix="mh-prepare-docker-"))
    runtime.chmod(0o755)
    try:
        resources = {
            **profile.resources_config(config),
            "max_lifetime_sec": profile.container_max_lifetime_seconds(
                config, timeout_seconds=timeout_seconds
            ),
        }
        prepare_git_snapshot(
            worktree_dir=worktree,
            runtime_state_dir=runtime,
            source_commit=source_commit,
            runner=runner,
            container_paths=True,
        )
        git_link_mask = runtime / "git-link-mask"
        git_link_mask.write_text("", encoding="utf-8")
        git_link_mask.chmod(0o444)
        start_command = profile.build_preparation_command(
            container_name=container_name,
            image_id=image_id,
            worktree=worktree,
            runtime_state_dir=runtime,
            owner_labels=_resource_labels(_owner_id(main_root)),
            resources=resources,
        )
        _checked(
            start_command,
            runner=runner,
            message="could not start Docker preparation container",
        )
        _checked(
            profile.build_workspace_init_command(container_name),
            runner=runner,
            message="could not initialize Docker preparation workspace",
        )
        completed = sproc.run_bounded_capture(
            profile.build_workspace_exec_command(container_name, raw_command),
            cwd=worktree_dir,
            timeout=timeout_seconds,
            env=_docker_host_env(),
            max_output_bytes=10_000_000,
            cleanup_args=["docker", "rm", "-f", container_name],
            success_callback=lambda: _export_container_workspace(
                container_name,
                worktree_dir,
                resources=resources,
            ),
        )
        return completed
    except (
        OSError,
        subprocess.TimeoutExpired,
        sproc.ScenarioOutputLimitError,
        sproc.ScenarioContainmentUnavailable,
    ) as exc:
        raise DockerScenarioError(f"Docker preparation command failed: {exc}") from exc
    finally:
        removed = _remove_container(container_name, runner=runner)
        if removed:
            shutil.rmtree(runtime, ignore_errors=True)
        if not removed:
            message = "could not remove Docker preparation container"
            if sys.exc_info()[0] is not None:
                _LOGGER.error("%s while preserving the in-flight preparation error", message)
            else:
                raise DockerScenarioError(message)


def export_docker_workspace(launch: DockerScenarioLaunch) -> None:
    _export_container_workspace(
        launch.scenario_container_name,
        launch.worktree_dir,
        resources=launch.metadata["resources"],
    )


def _export_container_workspace(
    container_name: str, worktree_dir: Path, *, resources: dict[str, Any]
) -> None:
    max_bytes = _docker_size_bytes(str(resources.get("workspace_size", "512m")))
    max_files = int(resources.get("workspace_max_files", 10000))
    staging = Path(tempfile.mkdtemp(prefix="mh-workspace-export-", dir=worktree_dir.parent))
    process: subprocess.Popen[bytes] | None = None
    export_timed_out = threading.Event()
    watchdog: threading.Timer | None = None
    try:
        process = subprocess.Popen(
            [
                "docker",
                "exec",
                container_name,
                "/usr/bin/tar",
                "-C",
                CONTAINER_WORKTREE,
                "--exclude=./.git",
                "-cf",
                "-",
                ".",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_docker_host_env(),
            start_new_session=True,
        )

        def stop_stalled_export() -> None:
            export_timed_out.set()
            if process is not None and process.poll() is None:
                process.kill()

        watchdog = threading.Timer(WORKSPACE_EXPORT_TIMEOUT_SECONDS, stop_stalled_export)
        watchdog.daemon = True
        watchdog.start()
        if process.stdout is None:
            raise DockerScenarioError("Docker workspace export stream is unavailable")
        total_bytes = 0
        file_count = 0
        with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
            for member in archive:
                relative = _safe_archive_path(member.name)
                if relative is None:
                    continue
                file_count += 1
                total_bytes += max(0, member.size)
                if file_count > max_files or total_bytes > max_bytes:
                    raise DockerScenarioError("Docker workspace export exceeded configured limits")
                target = staging / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise DockerScenarioError(
                        f"Docker workspace export rejected non-regular entry: {relative}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise DockerScenarioError(f"could not read exported file: {relative}")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(target, flags, member.mode & 0o777)
                with os.fdopen(descriptor, "wb") as destination:
                    shutil.copyfileobj(source, destination, length=64 * 1024)
        try:
            return_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise DockerScenarioError("Docker workspace export timed out") from exc
        if return_code != 0:
            if export_timed_out.is_set():
                raise DockerScenarioError("Docker workspace export timed out")
            stderr = process.stderr.read(4096) if process.stderr is not None else b""
            raise DockerScenarioError(
                "Docker workspace export failed: "
                + stderr.decode("utf-8", errors="replace").strip()
            )
        _replace_worktree_contents(worktree_dir, staging)
    except (OSError, tarfile.TarError) as exc:
        raise DockerScenarioError(f"Docker workspace export failed: {exc}") from exc
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if process is not None and process.poll() is None:
            process.kill()
        shutil.rmtree(staging, ignore_errors=True)


def _safe_archive_path(name: str) -> Path | None:
    while name.startswith("./"):
        name = name[2:]
    if not name or name == ".":
        return None
    value = PurePosixPath(name)
    # Any nested .git entry is an intentional fail-closed export failure: silently skipping
    # candidate-controlled repository metadata would make the exported result incomplete.
    if value.is_absolute() or ".." in value.parts or ".git" in value.parts:
        raise DockerScenarioError(f"unsafe Docker workspace export path: {name}")
    return Path(*value.parts)


def _replace_worktree_contents(worktree: Path, staging: Path) -> None:
    # The evaluation worktree is disposable and is removed when export fails, so a failed
    # delete/copy cannot expose a partially replaced tree to later oracle or promotion stages.
    for existing in worktree.iterdir():
        if existing.name == ".git":
            continue
        if existing.is_dir() and not existing.is_symlink():
            shutil.rmtree(existing)
        else:
            existing.unlink()
    for source in staging.iterdir():
        target = worktree / source.name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def _docker_size_bytes(value: str) -> int:
    suffixes = {"k": 1024, "m": 1024**2, "g": 1024**3}
    normalized = value.strip().lower()
    if len(normalized) < 2 or normalized[-1] not in suffixes:
        raise DockerScenarioError("workspace_size must use a k, m, or g suffix")
    try:
        amount = int(normalized[:-1])
    except ValueError as exc:
        raise DockerScenarioError("workspace_size must be an integer size") from exc
    if amount <= 0:
        raise DockerScenarioError("workspace_size must be positive")
    return amount * suffixes[normalized[-1]]


def refresh_launch_metadata(launch: DockerScenarioLaunch) -> dict[str, Any]:
    metrics = launch.broker.refresh_metrics()
    launch.metadata["broker"] = {**launch.metadata.get("broker", {}), "metrics": metrics}
    return dict(launch.metadata)


def cleanup_docker_launch(launch: DockerScenarioLaunch) -> None:
    if launch.cleaned:
        return
    errors: list[str] = []
    if not _remove_container(launch.scenario_container_name, runner=launch.broker.runner):
        errors.append("could not remove scenario container")
    try:
        launch.broker.cleanup()
    except DockerScenarioError as exc:
        errors.append(str(exc))
    if launch.owned_runtime_state_dir is not None:
        shutil.rmtree(launch.owned_runtime_state_dir, ignore_errors=True)
    launch.cleaned = not errors
    if errors:
        raise DockerScenarioError("; ".join(errors))


class _BrokerContext:
    def __init__(self, session: DockerBrokerSession) -> None:
        self.session = session

    def __enter__(self) -> DockerBrokerSession:
        return self.session

    def __exit__(self, *_args: Any) -> None:
        self.session.cleanup()


def docker_broker_session(
    config: dict,
    scope: str,
    *,
    owner_id: str,
    runner: SubprocessRunner = subprocess.run,
) -> _BrokerContext:
    return _BrokerContext(
        _start_broker(config, profile.safe_name(scope), owner_id=owner_id, runner=runner)
    )


def _start_broker(
    config: dict,
    scope: str,
    *,
    owner_id: str,
    runner: SubprocessRunner,
) -> DockerBrokerSession:
    scenario_image, broker_image = ensure_images(config, runner=runner)
    scenario_image_id = _image_id(scenario_image, runner=runner)
    broker_image_id = _image_id(broker_image, runner=runner)
    isolation = _isolation_config(config)
    broker_cfg = isolation.get("broker") or {}
    port_range = broker_cfg.get("port_range", [8790, 8990])
    if (
        not isinstance(port_range, list)
        or len(port_range) != 2
        or not all(isinstance(value, int) for value in port_range)
        or port_range[0] <= 0
        or port_range[1] < port_range[0]
        or port_range[1] > 65535
    ):
        raise DockerScenarioError("broker.port_range must be [start, end] within 1..65535")
    port = port_range[0] + secrets.randbelow(port_range[1] - port_range[0] + 1)
    name_nonce = secrets.token_hex(3)
    stem = f"{NAME_PREFIX}{profile.safe_name(scope)}-{name_nonce}"
    internal_network = f"{stem}-internal"
    external_network = f"{stem}-external"
    container_name = f"{stem}-broker"
    run_token = f"mh-{secrets.token_urlsafe(24)}"
    credential = credentials.load_claude_oauth_credential(
        minimum_ttl_seconds=credentials.minimum_broker_token_ttl_seconds(config),
        runner=runner,
    )
    version_pin = isolation.get("image_pin", DEFAULT_CLAUDE_VERSION_PIN)
    actual_version = _image_claude_version(scenario_image_id, runner=runner)
    if version_pin is not None and not _version_matches(actual_version, str(version_pin)):
        raise DockerScenarioError(
            f"image_pin mismatch: expected {version_pin!r}, got {actual_version!r}"
        )
    effective_broker_env = profile.broker_env(config, "hash-placeholder", 1)
    settings_hash = _sha256_json(
        {
            "port_range": port_range,
            "environment": {
                key: value
                for key, value in effective_broker_env.items()
                if key
                not in {
                    "DR_BROKER_RUN_TOKEN",
                    "DR_BROKER_PORT",
                    "MH_BROKER_RUN_TOKEN",
                    "MH_BROKER_PORT",
                }
            },
        }
    )
    owner_labels = _resource_labels(owner_id)
    spec = lifecycle.BrokerContainerSpec(
        docker_label=DOCKER_LABEL,
        broker_alias=BROKER_ALIAS,
        container_name=container_name,
        internal_network=internal_network,
        external_network=external_network,
        broker_image_id=broker_image_id,
        broker_env=profile.broker_env(config, run_token, port),
        owner_labels=owner_labels,
    )

    def session_factory() -> DockerBrokerSession:
        return DockerBrokerSession(
            container_name=container_name,
            internal_network=internal_network,
            external_network=external_network,
            run_token=run_token,
            port=port,
            scenario_image=scenario_image,
            broker_image=broker_image,
            image_id=scenario_image_id,
            broker_image_id=broker_image_id,
            broker_settings_sha256=settings_hash,
            scenario_context_sha256=dcli.context_hash("scenario"),
            broker_context_sha256=dcli.context_hash("broker"),
            scenario_base_image=dcli.base_image_reference("scenario"),
            broker_base_image=dcli.base_image_reference("broker"),
            owner_labels=owner_labels,
            runner=runner,
            idle_timeout_seconds=int(broker_cfg.get("idle_timeout_sec", 300)),
        )

    return lifecycle.start_broker_container(
        spec,
        runner=runner,
        checked=_checked,
        remove_container=_remove_container,
        remove_network=_remove_network,
        inject_token=lambda: _inject_token(
            container_name,
            credential.access_token,
            runner=runner,
        ),
        wait_ready=lambda: _wait_for_broker(
            container_name,
            port,
            broker_cfg,
            runner=runner,
        ),
        session_factory=session_factory,
        error_type=DockerScenarioError,
    )


def ensure_images(
    config: dict,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> tuple[str, str]:
    try:
        return dcli.ensure_images(config, runner=runner)
    except dcli.DockerCliError as exc:
        raise DockerScenarioError(str(exc)) from exc


def sweep_stale_resources(owner_id: str, *, runner: SubprocessRunner = subprocess.run) -> None:
    lifecycle.sweep_stale_resources(
        _RUNTIME_LABELS,
        owner_id,
        runner=runner,
        run_command=_run,
        best_effort=_best_effort,
        container_stale=_container_is_stale,
        network_stale=_network_is_stale,
    )


def _run_smoke_container(
    broker: DockerBrokerSession,
    claude_args: list[str],
    *,
    max_output_tokens: int,
    runner: SubprocessRunner,
) -> subprocess.CompletedProcess:
    uid, gid = profile.non_root_identity()
    name = f"{NAME_PREFIX}cap-{secrets.token_hex(3)}"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        broker.internal_network,
        *_label_args({DOCKER_LABEL: "run", **broker.owner_labels}),
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{uid}:{gid}",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--cpus",
        "1.0",
        "--tmpfs",
        profile.tmpfs(CONTAINER_HOME, uid, gid, size="128m"),
        "--tmpfs",
        profile.tmpfs(CONTAINER_TMP, uid, gid, size="64m"),
        "--workdir",
        CONTAINER_TMP,
        *profile.container_env_args(
            {
                "HOME": CONTAINER_HOME,
                "CLAUDE_CONFIG_DIR": f"{CONTAINER_HOME}/.claude",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens),
                "ANTHROPIC_BASE_URL": broker.base_url,
                "ANTHROPIC_API_KEY": broker.run_token,
                "NO_PROXY": BROKER_ALIAS,
            }
        ),
        broker.image_id,
        *claude_args,
    ]
    try:
        return _run(command, runner=runner, timeout=CAPABILITY_TIMEOUT_SECONDS)
    finally:
        _best_effort(["docker", "rm", "-f", name], runner=runner)


def _inject_token(container_name: str, token: str, *, runner: SubprocessRunner) -> None:
    try:
        completed = runner(
            [
                "docker",
                "exec",
                "-i",
                container_name,
                "/usr/bin/python3",
                CONTAINER_BROKER_SCRIPT,
                "--write-token",
            ],
            input=token,
            capture_output=True,
            text=True,
            timeout=10,
            env=_docker_host_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerScenarioError("could not inject OAuth token into broker tmpfs") from exc
    if completed.returncode != 0:
        raise DockerScenarioError("could not inject OAuth token into broker tmpfs")


def _wait_for_broker(
    container_name: str,
    port: int,
    broker_cfg: dict,
    *,
    runner: SubprocessRunner,
) -> None:
    deadline = time.monotonic() + int(broker_cfg.get("startup_timeout_sec", 30))
    command = [
        "docker",
        "exec",
        container_name,
        "/usr/bin/python3",
        CONTAINER_BROKER_SCRIPT,
        "--health",
        "--port",
        str(port),
    ]
    while time.monotonic() < deadline:
        if _run(command, runner=runner, timeout=5).returncode == 0:
            return
        time.sleep(0.1)
    raise DockerScenarioError("credential broker did not become healthy")


def _broker_keepalive_loop(
    session: DockerBrokerSession,
    stop: threading.Event,
    *,
    interval_seconds: int,
) -> None:
    lifecycle.broker_keepalive_loop(
        session,
        stop,
        interval_seconds=interval_seconds,
        broker_script=CONTAINER_BROKER_SCRIPT,
        run_command=_run,
    )


def _isolation_config(config: dict) -> dict:
    return (config.get("evaluate") or {}).get("isolation") or {}


def _version_token(value: str | None) -> str:
    """Extract the leading version token so bare semver pins (e.g. "2.1.207")
    compare equal to full `claude --version` output (e.g. "2.1.207 (Claude Code)").
    """
    if value is None:
        return ""
    stripped = value.strip()
    return stripped.split(maxsplit=1)[0] if stripped else ""


def _is_bare_semver_pin(pin: str) -> bool:
    return _SEMVER_PIN_RE.fullmatch(pin.strip()) is not None


def _version_matches(actual: str | None, pin: str) -> bool:
    """Compare a reported `claude --version` string against a configured
    image_pin.

    A bare semver pin (e.g. "2.1.207") matches via leading-token comparison
    so it accepts the fuller `claude --version` output (e.g.
    "2.1.207 (Claude Code)"). Any other pin format -- including the default
    full form "2.1.207 (Claude Code)" -- must match the reported version
    exactly, preserving the strict Docker capability contract: a prebuilt
    image reporting an unexpected wrapper (e.g. "2.1.207 (unexpected
    wrapper)") must fail closed rather than pass on a token match.
    """
    if actual is None:
        return False
    if _is_bare_semver_pin(pin):
        return _version_token(actual) == _version_token(pin)
    return actual == pin


def _image_claude_version(image: str, *, runner: SubprocessRunner) -> str | None:
    return dcli.image_claude_version(image, runner=runner)


def _image_id(image: str, *, runner: SubprocessRunner) -> str:
    try:
        return dcli.image_id(image, runner=runner)
    except dcli.DockerCliError as exc:
        raise DockerScenarioError(str(exc)) from exc


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _regular_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise DockerScenarioError(f"{label} must be a regular directory: {path}")
    return path.resolve()


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise DockerScenarioError(f"{label} must be a regular non-symlink file: {path}")
    return path.resolve()


def _docker_host_env() -> dict[str, str]:
    return dcli.host_env()


def _run(
    command: list[str],
    *,
    runner: SubprocessRunner,
    timeout: int | float,
) -> subprocess.CompletedProcess:
    return dcli.run(command, runner=runner, timeout=timeout)


def _checked(
    command: list[str],
    *,
    runner: SubprocessRunner,
    message: str,
) -> subprocess.CompletedProcess:
    try:
        return dcli.checked(command, runner=runner, message=message)
    except dcli.DockerCliError as exc:
        raise DockerScenarioError(str(exc)) from exc


def _best_effort(command: list[str], *, runner: SubprocessRunner) -> None:
    dcli.best_effort(command, runner=runner)


def _remove_container(name: str, *, runner: SubprocessRunner) -> bool:
    return dcli.remove_container(name, runner=runner)


def _remove_network(name: str, *, runner: SubprocessRunner) -> bool:
    return dcli.remove_network(name, runner=runner)


def _owner_id(main_root: Path) -> str:
    return lifecycle.owner_id(main_root)


def _resource_labels(owner_id: str) -> dict[str, str]:
    return lifecycle.resource_labels(_RUNTIME_LABELS, owner_id)


def _label_args(labels: dict[str, str]) -> list[str]:
    return lifecycle.label_args(labels)


def _inspect_resource(
    resource: str, *, network: bool = False, runner: SubprocessRunner
) -> dict[str, Any] | None:
    return lifecycle.inspect_resource(
        resource,
        network=network,
        runner=runner,
        run_command=_run,
    )


def _container_is_stale(inspected: dict[str, Any], owner_id: str) -> bool:
    return lifecycle.container_is_stale(
        inspected,
        owner_id,
        labels=_RUNTIME_LABELS,
        pid_checker=_pid_alive,
    )


def _network_is_stale(inspected: dict[str, Any], owner_id: str) -> bool:
    return lifecycle.network_is_stale(inspected, owner_id, labels=_RUNTIME_LABELS)


def _pid_alive(pid: int) -> bool:
    return lifecycle.pid_alive(pid)


def _has_result_json(stdout: str) -> bool:
    try:
        value = json.loads(stdout)
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("type") == "result"


def _failed_checks_reason(checks: dict[str, bool]) -> str | None:
    failed = [name for name, passed in checks.items() if not passed]
    return f"Docker capability check(s) failed: {', '.join(failed)}" if failed else None


def _capability_failure(
    version: str | None,
    version_pin: str | None,
    checks: dict[str, bool],
    reason: str,
) -> DockerCapabilityResult:
    match = None if version_pin is None or version is None else version == version_pin
    return DockerCapabilityResult(version, version_pin, match, checks, reason)
