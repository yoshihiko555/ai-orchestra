#!/usr/bin/env python3
"""Pure Docker command/profile builders for the meta-harness scenario backend."""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Any

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

_LIB_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _LIB_DIR.parent
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
if str(_DOCKER_RUNTIME_LIB) not in sys.path:
    sys.path.insert(0, str(_DOCKER_RUNTIME_LIB))

import docker_runtime_profile as runtime  # noqa: E402
import meta_harness_common as mh  # noqa: E402

# Single source of truth for the broker env fallback prices (Issue #261 PR2 review
# round 4): broker_env() must never hardcode its own price literals, or a
# partial/`.local.yaml`-overridden config that nulls out one pricing key would
# silently fall back to a value that has drifted from mh.DEFAULTS (e.g. the retired
# Opus-tier ceiling) instead of the currently pinned Sonnet-tier default.
_DEFAULT_PRICING_UPPER_BOUND_USD_PER_MILLION = mh.DEFAULTS["evaluate"]["isolation"]["broker"][
    "pricing_upper_bound_usd_per_million"
]

# Fixed, safe request/budget envelope for the dedicated negative-probe broker session
# (Issue #261 PR2 review round 6, High). That session exists purely to prove the
# broker enforces model_allowlist and never spends the user's real run budget, so it
# must not inherit the user's configured max_requests/budget: a tight
# `max_requests: 1` (or an exhausted budget) would let the first probe consume the
# only slot and reject the second (count_tokens) probe via the request envelope
# before it ever reaches the allowlist check, reporting a false "enforced" pass.
ALLOWLIST_PROBE_MAX_REQUESTS = 8
ALLOWLIST_PROBE_BUDGET_USD = mh.DEFAULTS["scenario_run"]["max_budget_usd_default"]

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
        "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
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


def _validate_model_slug(value: Any, *, field: str) -> str:
    """Validate a single model slug used for the broker allowlist/CSV env (CodeRabbit
    High, PR #265): must be a non-empty string, free of commas (the allowlist env is
    CSV-joined -- a comma inside one element would silently expand into multiple
    entries) and free of control characters."""
    if not isinstance(value, str) or not value.strip():
        raise DockerProfileError(f"{field} must be a non-empty model slug string, got {value!r}.")
    if "," in value:
        raise DockerProfileError(
            f"{field} must not contain a comma (the broker allowlist env is CSV-joined "
            f"and a comma would silently expand into multiple entries): {value!r}."
        )
    if _CONTROL_CHAR_RE.search(value):
        raise DockerProfileError(f"{field} must not contain control characters: {value!r}.")
    return value


def effective_broker_model_allowlist(config: dict) -> list[str]:
    """Validate config and return the broker model allowlist to wire into the env
    (Issue #261 PR2 review round 3).

    The broker's fixed `pricing_upper_bound_usd_per_million` is calibrated for
    exactly one price point: the pinned `evaluate.model` / `judge.model`. Every
    prior "escape hatch" in this function's history (omit the restriction when a
    model is unpinned; auto-union a repinned model into the allowlist; wire the
    full human-curated `model_allowlist` menu instead of just the pinned pair)
    turned out to let *some* other model run under that fixed price ceiling and
    under-count real cost. The contract is now a single fail-closed rule:

    1. Both `evaluate.model` and `judge.model` MUST be pinned (non-null). An
       unpinned model runs at the CLI/session default, which the fixed pricing
       ceiling was never calibrated for -- this now raises
       :class:`DockerProfileError` instead of silently omitting the broker's
       allowlist restriction (the previous "unpinned = no restriction"
       backward-compat path is retired).
    2. `evaluate.model` and `judge.model` MUST be the same model slug (Issue #261
       PR2 review round 4). The broker pricing table has exactly one price point
       per run; a dual-model setup (e.g. a cheap evaluate model with a pricier
       judge model added to the allowlist) would under-count whichever model's
       real price exceeds that single ceiling. Running evaluate and judge under
       different models is a deliberate design change (per-role pricing) this
       function does not support -- it fails closed instead.
    3. The (single) pinned model MUST already be listed in the configured
       `evaluate.isolation.broker.model_allowlist` "menu" (human-curated,
       intentionally NOT auto-unioned): this is a deliberate acknowledgement
       step, not an allowlist by itself.
    4. The value actually wired to the broker is the pinned model only, never the
       full configured menu: any additional "menu" entries lack a corresponding
       pricing calibration and must not be admitted just because they were
       listed for step 3's validation.

    `model_allowlist` must be a `list`, not a bare string (CodeRabbit High, PR #265):
    iterating a scalar string produces one entry per character, which would silently
    admit almost any single-character model id. Every element (and the pinned
    model) is further validated by :func:`_validate_model_slug`.
    """
    evaluate_cfg = config.get("evaluate") or {}
    judge_cfg = config.get("judge") or {}
    evaluate_model = evaluate_cfg.get("model")
    judge_model = judge_cfg.get("model")
    unpinned = [
        field
        for field, value in (("evaluate.model", evaluate_model), ("judge.model", judge_model))
        if not value
    ]
    if unpinned:
        raise DockerProfileError(
            "broker model allowlist fail-closed: "
            f"{', '.join(unpinned)} is not pinned (null). An unpinned model would run "
            "at the CLI's session-default price, which the broker's fixed "
            "evaluate.isolation.broker.pricing_upper_bound_usd_per_million ceiling is "
            "not calibrated for. Pin both evaluate.model and judge.model to a specific "
            "model slug and calibrate pricing_upper_bound_usd_per_million and "
            "evaluate.isolation.broker.model_allowlist to that model before re-running."
        )
    if evaluate_model != judge_model:
        raise DockerProfileError(
            "broker model allowlist fail-closed: evaluate.model "
            f"({evaluate_model!r}) and judge.model ({judge_model!r}) differ. The broker "
            "pricing table (evaluate.isolation.broker.pricing_upper_bound_usd_per_million) "
            "has exactly one price point per run; running evaluate and judge under "
            "different models would under-count whichever model's real price exceeds that "
            "single ceiling. Pin both evaluate.model and judge.model to the same model "
            "slug -- a genuine dual-model setup requires a pricing redesign (e.g. "
            "per-role pricing), not just adding the second model to model_allowlist."
        )
    _validate_model_slug(evaluate_model, field="evaluate.model")
    broker = (evaluate_cfg.get("isolation") or {}).get("broker") or {}
    raw_configured = broker.get("model_allowlist")
    if raw_configured is not None and not isinstance(raw_configured, list):
        raise DockerProfileError(
            "evaluate.isolation.broker.model_allowlist must be a list of model slug "
            f"strings, got {type(raw_configured).__name__} ({raw_configured!r}); a bare "
            "string would be silently iterated character-by-character."
        )
    configured = raw_configured or []
    for index, item in enumerate(configured):
        _validate_model_slug(item, field=f"evaluate.isolation.broker.model_allowlist[{index}]")
    configured_set = {str(item) for item in configured}
    pinned_model = str(evaluate_model)
    if pinned_model not in configured_set:
        raise DockerProfileError(
            "broker model allowlist fail-closed: pinned model "
            f"{pinned_model!r} is not in evaluate.isolation.broker.model_allowlist "
            f"({', '.join(sorted(configured_set)) or '(empty)'}). "
            "Update both evaluate.isolation.broker.model_allowlist and "
            "evaluate.isolation.broker.pricing_upper_bound_usd_per_million to match the "
            "repinned model before re-running."
        )
    # Wire only the pinned model -- any additional model_allowlist "menu" entries
    # (step 4 above) lack pricing calibration and must not be admitted.
    return [pinned_model]


def _pricing_value(pricing: dict, key: str) -> float:
    """Resolve one pricing_upper_bound_usd_per_million field with a null-safe
    fallback to mh.DEFAULTS (local review round 5, High).

    `dict.get(key, default)` only falls back when the key is absent -- a
    `.local.yaml` override that nulls out a single pricing key (e.g.
    `pricing_upper_bound_usd_per_million: {input: null}`) leaves the key
    *present* with value `None` after `_deep_merge`, so `.get` would return
    `None` verbatim. That `None` used to reach `str(None)` -> `"None"` in the
    broker env, which crashes the broker's `_float_env` (`float("None")`)
    uncaught. This must be an explicit None-check, not `.get(key, default)`.
    """
    value = pricing.get(key)
    return value if value is not None else _DEFAULT_PRICING_UPPER_BOUND_USD_PER_MILLION[key]


def allowlist_probe_config_overrides(config: dict) -> dict:
    """Return `config` with the negative-probe broker session's max_requests/budget
    pinned to fixed, safe values (Issue #261 PR2 review round 6, High).

    Everything else (image, pinned model, pricing, model_allowlist, port_range,
    resources, ...) is left exactly as configured, since the probe must still
    exercise the real pricing/model setup to prove enforcement. Only the request
    envelope and budget are overridden, because a tight user `max_requests` (e.g.
    `1`) or an already-spent budget would reject the second (count_tokens) probe via
    `begin_request()` before it ever reaches the allowlist check -- a false
    "enforced" pass that proves nothing.
    """
    evaluate_cfg = config.get("evaluate") or {}
    isolation_cfg = evaluate_cfg.get("isolation") or {}
    broker_cfg = isolation_cfg.get("broker") or {}
    scenario_run_cfg = config.get("scenario_run") or {}
    return {
        **config,
        "evaluate": {
            **evaluate_cfg,
            "isolation": {
                **isolation_cfg,
                "broker": {**broker_cfg, "max_requests": ALLOWLIST_PROBE_MAX_REQUESTS},
            },
        },
        "scenario_run": {
            **scenario_run_cfg,
            "max_budget_usd_default": ALLOWLIST_PROBE_BUDGET_USD,
        },
    }


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
        "DR_PRICE_INPUT": str(_pricing_value(pricing, "input")),
        "DR_PRICE_OUTPUT": str(_pricing_value(pricing, "output")),
        "DR_PRICE_CACHE_CREATION": str(_pricing_value(pricing, "cache_creation")),
        "DR_PRICE_CACHE_READ": str(_pricing_value(pricing, "cache_read")),
        "MH_BROKER_RUN_TOKEN": run_token,
        "MH_BROKER_PORT": str(port),
        "MH_BROKER_BUDGET_USD": str(scenario_run.get("max_budget_usd_default", 3.0)),
        "MH_BROKER_IDLE_TIMEOUT_SEC": str(idle_timeout),
        "MH_BROKER_MAX_LIFETIME_SEC": str(container_max_lifetime_seconds(config)),
        "MH_BROKER_STARTUP_TIMEOUT_SEC": str(broker.get("startup_timeout_sec", 30)),
        "MH_BROKER_MAX_REQUESTS": str(broker.get("max_requests", 64)),
        "MH_BROKER_MAX_TOTAL_TOKENS": str(broker.get("max_total_tokens", 500000)),
        "MH_BROKER_MAX_UPSTREAM_BYTES": str(broker.get("max_upstream_bytes", 50000000)),
        "MH_PRICE_INPUT": str(_pricing_value(pricing, "input")),
        "MH_PRICE_OUTPUT": str(_pricing_value(pricing, "output")),
        "MH_PRICE_CACHE_CREATION": str(_pricing_value(pricing, "cache_creation")),
        "MH_PRICE_CACHE_READ": str(_pricing_value(pricing, "cache_read")),
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
