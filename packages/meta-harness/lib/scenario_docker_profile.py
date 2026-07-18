#!/usr/bin/env python3
"""Pure Docker command/profile builders for the meta-harness scenario backend."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
if str(_DOCKER_RUNTIME_LIB) not in sys.path:
    sys.path.insert(0, str(_DOCKER_RUNTIME_LIB))

import docker_runtime_profile as runtime

NAME_PREFIX = "mh-run-"
BROKER_ALIAS = "mh-broker"
BROKER_NAMESPACE = "meta-harness"
CONTAINER_WORKTREE = "/workspace"
CONTAINER_INPUT = "/input"
CONTAINER_RUNTIME = "/runtime"
CONTAINER_INSTRUCTION = "/meta/self-report-instruction.md"
CONTAINER_GIT_LINK = f"{CONTAINER_WORKTREE}/.git"
CONTAINER_HOME = "/home/meta"
CONTAINER_TMP = "/tmp"
CONTAINER_LIFETIME_MARGIN_SECONDS = 60
CONTAINER_TIMEOUT_KILL_AFTER_SECONDS = 5
DockerProfileError = runtime.DockerProfileError
DEFAULT_MAX_OUTPUT_TOKENS = 4096


def resolve_max_output_tokens_default(config: dict) -> int:
    """Resolve scenario_run.max_output_tokens_default with null-safe fallback.

    An explicit YAML ``max_output_tokens_default: null`` must fall back to the same default
    as an absent key, not propagate ``None`` into ``str()``/``int()`` conversions downstream.
    """
    scenario_run_cfg = config.get("scenario_run") or {}
    value = scenario_run_cfg.get("max_output_tokens_default")
    return int(value) if value is not None else DEFAULT_MAX_OUTPUT_TOKENS


def build_scenario_container_command(launch: Any) -> list[str]:
    uid, gid = _non_root_identity()
    resources = _resource_args(launch.metadata["resources"])
    return [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        launch.scenario_container_name,
        *_run_label_args(launch),
        "--network",
        launch.broker.internal_network,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--init",
        "--user",
        f"{uid}:{gid}",
        *resources,
        "--mount",
        _bind_mount(launch.worktree_dir, CONTAINER_INPUT, read_only=True),
        "--mount",
        _bind_mount(launch.runtime_state_dir, CONTAINER_RUNTIME, read_only=True),
        "--mount",
        _bind_mount(launch.instruction_path, CONTAINER_INSTRUCTION, read_only=True),
        "--tmpfs",
        _tmpfs(
            CONTAINER_WORKTREE,
            uid,
            gid,
            size=str(launch.metadata["resources"]["workspace_size"]),
        ),
        "--tmpfs",
        _tmpfs(CONTAINER_HOME, uid, gid, size="256m"),
        "--tmpfs",
        _tmpfs(CONTAINER_TMP, uid, gid, size="256m"),
        "--workdir",
        CONTAINER_WORKTREE,
        *_container_env_args(_candidate_env(launch)),
        launch.broker.image_id,
        *_bounded_container_command(launch.metadata["resources"], ["/usr/bin/sleep", "infinity"]),
    ]


def build_oracle_command(launch: Any, command: str, *, container_name: str) -> list[str]:
    uid, gid = _non_root_identity()
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        *_run_label_args(launch),
        "--network",
        "none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{uid}:{gid}",
        *_resource_args(launch.metadata["resources"]),
        "--mount",
        _bind_mount(launch.worktree_dir, CONTAINER_WORKTREE, read_only=True),
        "--mount",
        _bind_mount(
            launch.runtime_state_dir / "git-snapshot",
            f"{CONTAINER_RUNTIME}/git-snapshot",
            read_only=True,
        ),
        "--mount",
        _bind_mount(
            launch.runtime_state_dir / "bin",
            f"{CONTAINER_RUNTIME}/bin",
            read_only=True,
        ),
        "--mount",
        _bind_mount(
            launch.runtime_state_dir / "git-link-mask",
            CONTAINER_GIT_LINK,
            read_only=True,
        ),
        "--tmpfs",
        _tmpfs(CONTAINER_TMP, uid, gid, size="64m"),
        "--workdir",
        CONTAINER_WORKTREE,
        *_container_env_args(
            {
                "HOME": CONTAINER_TMP,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_DIR": f"{CONTAINER_RUNTIME}/git-snapshot",
                "GIT_WORK_TREE": CONTAINER_WORKTREE,
                "PATH": f"{CONTAINER_RUNTIME}/bin:/usr/local/bin:/usr/bin:/bin",
            }
        ),
        launch.broker.image_id,
        *_bounded_container_command(launch.metadata["resources"], ["/bin/sh", "-c", command]),
    ]


def build_judge_command(
    launch: Any,
    claude_command: list[str],
    *,
    container_name: str,
    max_output_tokens: int,
) -> list[str]:
    uid, gid = _non_root_identity()
    env = {
        "HOME": CONTAINER_HOME,
        "CLAUDE_CONFIG_DIR": f"{CONTAINER_HOME}/.claude",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens),
        "ANTHROPIC_BASE_URL": launch.broker.base_url,
        "ANTHROPIC_API_KEY": launch.broker.run_token,
        "NO_PROXY": BROKER_ALIAS,
    }
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        *_run_label_args(launch),
        "--network",
        launch.broker.internal_network,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{uid}:{gid}",
        *_resource_args(launch.metadata["resources"]),
        "--tmpfs",
        _tmpfs(CONTAINER_HOME, uid, gid, size="128m"),
        "--tmpfs",
        _tmpfs(CONTAINER_TMP, uid, gid, size="64m"),
        "--workdir",
        CONTAINER_TMP,
        *_container_env_args(env),
        launch.broker.image_id,
        *_bounded_container_command(launch.metadata["resources"], claude_command),
    ]


def build_preparation_command(
    *,
    container_name: str,
    image_id: str,
    worktree: Path,
    runtime_state_dir: Path,
    owner_labels: dict[str, str],
    resources: dict[str, Any],
) -> list[str]:
    uid, gid = _non_root_identity()
    labels = {"ai.orchestra.meta-harness": "run", **owner_labels}
    label_args: list[str] = []
    for key, value in sorted(labels.items()):
        label_args.extend(["--label", f"{key}={value}"])
    return [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        container_name,
        *label_args,
        "--network",
        "none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{uid}:{gid}",
        *_resource_args(resources),
        "--mount",
        _bind_mount(worktree, CONTAINER_INPUT, read_only=True),
        "--mount",
        _bind_mount(
            runtime_state_dir / "git-snapshot",
            f"{CONTAINER_RUNTIME}/git-snapshot",
            read_only=True,
        ),
        "--mount",
        _bind_mount(
            runtime_state_dir / "bin",
            f"{CONTAINER_RUNTIME}/bin",
            read_only=True,
        ),
        "--mount",
        _bind_mount(
            runtime_state_dir / "git-link-mask",
            CONTAINER_GIT_LINK,
            read_only=True,
        ),
        "--tmpfs",
        _tmpfs(
            CONTAINER_WORKTREE,
            uid,
            gid,
            size=str(resources["workspace_size"]),
        ),
        "--tmpfs",
        _tmpfs(CONTAINER_HOME, uid, gid, size="128m"),
        "--tmpfs",
        _tmpfs(CONTAINER_TMP, uid, gid, size="128m"),
        "--workdir",
        CONTAINER_WORKTREE,
        *_container_env_args(
            {
                "HOME": CONTAINER_HOME,
                "AI_ORCHESTRA_DIR": CONTAINER_WORKTREE,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_DIR": f"{CONTAINER_RUNTIME}/git-snapshot",
                "GIT_WORK_TREE": CONTAINER_WORKTREE,
                "TMPDIR": CONTAINER_TMP,
                "PATH": f"{CONTAINER_RUNTIME}/bin:/usr/local/bin:/usr/bin:/bin",
            }
        ),
        image_id,
        *_bounded_container_command(resources, ["/usr/bin/sleep", "infinity"]),
    ]


def build_workspace_init_command(container_name: str) -> list[str]:
    uid, gid = _non_root_identity()
    return [
        "docker",
        "exec",
        "--user",
        f"{uid}:{gid}",
        container_name,
        "/bin/sh",
        "-c",
        "set -eu; (cd /input && tar --exclude=.git -cf - .) | (cd /workspace && tar -xf -)",
    ]


def build_workspace_exec_command(container_name: str, raw_command: list[str]) -> list[str]:
    uid, gid = _non_root_identity()
    return [
        "docker",
        "exec",
        "--user",
        f"{uid}:{gid}",
        "--workdir",
        CONTAINER_WORKTREE,
        container_name,
        *raw_command,
    ]


def candidate_env(launch: Any) -> dict[str, str]:
    return _candidate_env(launch)


def launch_metadata(
    *,
    config: dict,
    broker: Any,
    runtime: Path,
    worktree: Path,
    instruction: Path,
    source_commit: str,
) -> dict[str, Any]:
    resources = resources_config(config)
    profile = {
        "image_id": broker.image_id,
        "broker_image_id": broker.broker_image_id,
        "mounts": [
            [str(worktree), CONTAINER_INPUT, "ro"],
            ["tmpfs", CONTAINER_WORKTREE, resources["workspace_size"]],
            [str(runtime), CONTAINER_RUNTIME, "ro"],
            [str(instruction), CONTAINER_INSTRUCTION, "ro"],
        ],
        "resources": resources,
        "network": "internal-only-via-broker",
    }
    return {
        "backend": "docker",
        "image": broker.scenario_image,
        "image_id": broker.image_id,
        "broker_image": broker.broker_image,
        "broker_image_id": broker.broker_image_id,
        "broker_settings_sha256": broker.broker_settings_sha256,
        "scenario_context_sha256": broker.scenario_context_sha256,
        "broker_context_sha256": broker.broker_context_sha256,
        "scenario_base_image": broker.scenario_base_image,
        "broker_base_image": broker.broker_base_image,
        "platform_profile_input_sha256": _sha256_json(profile),
        "resources": resources,
        "git": {"mode": "isolated-snapshot", "source_commit": source_commit},
        "broker": {"metrics": _empty_broker_metrics()},
    }


def resources_config(config: dict) -> dict[str, Any]:
    resources = ((config.get("evaluate") or {}).get("isolation") or {}).get("resources") or {}
    return {
        "pids_limit": int(resources.get("pids_limit", 128)),
        "memory": str(resources.get("memory", "2g")),
        "cpus": float(resources.get("cpus", 2.0)),
        "workspace_size": str(resources.get("workspace_size", "512m")),
        "workspace_max_files": int(resources.get("workspace_max_files", 10000)),
        "max_lifetime_sec": container_max_lifetime_seconds(config),
    }


def effective_broker_model_allowlist(config: dict) -> list[str]:
    """Derive the effective broker model allowlist (Issue #261 PR2 review follow-up).

    Returns `[]` (meaning: no restriction) whenever either `evaluate.model` or
    `judge.model` is unset. Restricting the broker to the configured
    `model_allowlist` in that case would reject the CLI's session-default
    model and break existing projects that have not pinned a model yet
    (backward compatibility takes priority over the stricter allowlist).

    When both are pinned, the effective allowlist is the union of the
    configured `evaluate.isolation.broker.model_allowlist` and the two pinned
    model values, so repinning `evaluate.model` / `judge.model` without also
    updating `model_allowlist` does not lock the candidate out with a 400.
    """
    evaluate_cfg = config.get("evaluate") or {}
    judge_cfg = config.get("judge") or {}
    evaluate_model = evaluate_cfg.get("model")
    judge_model = judge_cfg.get("model")
    if not evaluate_model or not judge_model:
        return []
    broker = (evaluate_cfg.get("isolation") or {}).get("broker") or {}
    configured = broker.get("model_allowlist") or []
    effective = {str(item) for item in configured}
    effective.add(str(evaluate_model))
    effective.add(str(judge_model))
    return sorted(effective)


def broker_env(config: dict, run_token: str, port: int) -> dict[str, str]:
    broker = ((config.get("evaluate") or {}).get("isolation") or {}).get("broker") or {}
    pricing = broker.get("pricing_upper_bound_usd_per_million") or {}
    scenario_run = config.get("scenario_run") or {}
    idle_timeout = int(broker.get("idle_timeout_sec", 300))
    model_allowlist = effective_broker_model_allowlist(config)
    model_allowlist_env: dict[str, str] = {}
    if model_allowlist:
        joined = ",".join(model_allowlist)
        model_allowlist_env = {
            "DR_BROKER_MODEL_ALLOWLIST": joined,
            "MH_BROKER_MODEL_ALLOWLIST": joined,
        }
    return {
        "DR_BROKER_NAMESPACE": BROKER_NAMESPACE,
        "DR_BROKER_RUN_TOKEN": run_token,
        "DR_BROKER_PORT": str(port),
        "DR_BROKER_BUDGET_USD": str(scenario_run.get("max_budget_usd_default", 3.0)),
        "DR_BROKER_IDLE_TIMEOUT_SEC": str(idle_timeout),
        "DR_BROKER_MAX_LIFETIME_SEC": str(container_max_lifetime_seconds(config)),
        "DR_BROKER_STARTUP_TIMEOUT_SEC": str(broker.get("startup_timeout_sec", 30)),
        "DR_BROKER_MAX_REQUESTS": str(broker.get("max_requests", 64)),
        "DR_BROKER_MAX_TOTAL_TOKENS": str(broker.get("max_total_tokens", 500000)),
        "DR_BROKER_MAX_UPSTREAM_BYTES": str(broker.get("max_upstream_bytes", 50000000)),
        "DR_PRICE_INPUT": str(pricing.get("input", 15.0)),
        "DR_PRICE_OUTPUT": str(pricing.get("output", 75.0)),
        "DR_PRICE_CACHE_CREATION": str(pricing.get("cache_creation", 18.75)),
        "DR_PRICE_CACHE_READ": str(pricing.get("cache_read", 1.5)),
        "MH_BROKER_RUN_TOKEN": run_token,
        "MH_BROKER_PORT": str(port),
        "MH_BROKER_BUDGET_USD": str(scenario_run.get("max_budget_usd_default", 3.0)),
        "MH_BROKER_IDLE_TIMEOUT_SEC": str(idle_timeout),
        "MH_BROKER_MAX_LIFETIME_SEC": str(container_max_lifetime_seconds(config)),
        "MH_BROKER_STARTUP_TIMEOUT_SEC": str(broker.get("startup_timeout_sec", 30)),
        "MH_BROKER_MAX_REQUESTS": str(broker.get("max_requests", 64)),
        "MH_BROKER_MAX_TOTAL_TOKENS": str(broker.get("max_total_tokens", 500000)),
        "MH_BROKER_MAX_UPSTREAM_BYTES": str(broker.get("max_upstream_bytes", 50000000)),
        "MH_PRICE_INPUT": str(pricing.get("input", 15.0)),
        "MH_PRICE_OUTPUT": str(pricing.get("output", 75.0)),
        "MH_PRICE_CACHE_CREATION": str(pricing.get("cache_creation", 18.75)),
        "MH_PRICE_CACHE_READ": str(pricing.get("cache_read", 1.5)),
        **model_allowlist_env,
    }


def container_max_lifetime_seconds(
    config: dict, *, timeout_seconds: int | float | None = None
) -> int:
    """Return the container-internal cap; longer oracle/check timeouts end here first."""
    evaluate = config.get("evaluate") or {}
    broker = (evaluate.get("isolation") or {}).get("broker") or {}
    configured_timeout = evaluate.get("timeout_ms_default", 300000)
    try:
        effective_timeout = (
            math.ceil(float(configured_timeout) / 1000)
            if timeout_seconds is None
            else math.ceil(float(timeout_seconds))
        )
        idle_timeout = int(broker.get("idle_timeout_sec", 300))
    except (TypeError, ValueError, OverflowError) as exc:
        raise DockerProfileError("container lifetime settings must be finite numbers") from exc
    if effective_timeout <= 0 or idle_timeout <= 0:
        raise DockerProfileError("container lifetime settings must be positive")
    return effective_timeout + idle_timeout + CONTAINER_LIFETIME_MARGIN_SECONDS


def safe_name(value: str) -> str:
    return runtime.safe_name(value, max_length=40, strip_chars="-.")


def container_env_args(env: dict[str, str]) -> list[str]:
    return _container_env_args(env)


def tmpfs(target: str, uid: int, gid: int, *, size: str) -> str:
    return _tmpfs(target, uid, gid, size=size)


def non_root_identity() -> tuple[int, int]:
    return _non_root_identity()


def _candidate_env(launch: Any) -> dict[str, str]:
    return {
        "HOME": CONTAINER_HOME,
        "CLAUDE_CONFIG_DIR": f"{CONTAINER_HOME}/.claude",
        "AI_ORCHESTRA_DIR": CONTAINER_WORKTREE,
        "ANTHROPIC_BASE_URL": launch.broker.base_url,
        "ANTHROPIC_API_KEY": launch.broker.run_token,
        "NO_PROXY": BROKER_ALIAS,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_DIR": f"{CONTAINER_RUNTIME}/git-snapshot",
        "GIT_WORK_TREE": CONTAINER_WORKTREE,
        "PATH": f"{CONTAINER_RUNTIME}/bin:/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": CONTAINER_TMP,
    }


def _resource_args(resources: dict[str, Any]) -> list[str]:
    return runtime.resource_args(resources)


def _bounded_container_command(resources: dict[str, Any], command: list[str]) -> list[str]:
    return runtime.bounded_container_command(
        resources,
        command,
        kill_after_seconds=CONTAINER_TIMEOUT_KILL_AFTER_SECONDS,
    )


def _container_env_args(env: dict[str, str]) -> list[str]:
    return runtime.container_env_args(env)


def _run_label_args(launch: Any) -> list[str]:
    labels = {"ai.orchestra.meta-harness": "run", **launch.broker.owner_labels}
    args: list[str] = []
    for key, value in sorted(labels.items()):
        args.extend(["--label", f"{key}={value}"])
    return args


def _empty_broker_metrics() -> dict[str, Any]:
    return {
        "request_count": 0,
        "rejected_count": 0,
        "upstream_request_bytes": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_tokens": 0,
        },
        "estimated_cost_usd": 0.0,
        "budget_exceeded": False,
        "anomaly": False,
        "anomaly_reasons": [],
    }


def _bind_mount(source: Path, target: str, *, read_only: bool) -> str:
    return runtime.bind_mount(source, target, read_only=read_only)


def _tmpfs(target: str, uid: int, gid: int, *, size: str) -> str:
    return runtime.tmpfs(target, uid, gid, size=size)


def _non_root_identity() -> tuple[int, int]:
    return runtime.non_root_identity()


def _sha256_json(value: Any) -> str:
    return runtime.sha256_json(value)
