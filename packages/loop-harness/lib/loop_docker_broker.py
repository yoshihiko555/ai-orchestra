#!/usr/bin/env python3
"""Loop-harness adapter for the shared dual-network credential broker."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
if str(_DOCKER_RUNTIME_LIB) not in sys.path:
    sys.path.insert(0, str(_DOCKER_RUNTIME_LIB))

import docker_runtime_cli as runtime_cli  # noqa: E402
import docker_runtime_credentials as credentials  # noqa: E402
import docker_runtime_lifecycle as lifecycle  # noqa: E402
import docker_runtime_profile as runtime_profile  # noqa: E402

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

DOCKER_LABEL = "ai.orchestra.loop-harness"
BROKER_ALIAS = "lh-broker"
BROKER_NAMESPACE = "loop-harness"
BROKER_SCRIPT = "/app/broker.py"
STALE_MAX_AGE_SECONDS = 24 * 60 * 60
_RUNTIME_LABELS = lifecycle.RuntimeLabels(DOCKER_LABEL, STALE_MAX_AGE_SECONDS)


class LoopDockerBrokerError(RuntimeError):
    """The isolated action broker could not be started, queried, or removed."""


@dataclass
class LoopBrokerSession:
    container_name: str
    internal_network: str
    external_network: str
    run_token: str
    port: int
    scenario_image_id: str
    broker_image_id: str
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
                BROKER_SCRIPT,
                "--print-metrics",
            ],
            runner=self.runner,
            timeout=10,
        )
        if completed.returncode != 0:
            raise LoopDockerBrokerError("could not read credential broker metrics")
        try:
            value = json.loads(completed.stdout)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LoopDockerBrokerError("credential broker metrics are not valid JSON") from exc
        if not isinstance(value, dict):
            raise LoopDockerBrokerError("credential broker metrics must be a JSON object")
        self.metrics = value
        return dict(self.metrics)

    def start_keepalive(self) -> None:
        if self._keepalive_thread is not None:
            return
        interval = max(1, min(30, self.idle_timeout_seconds // 3))
        self._keepalive_thread = threading.Thread(
            target=lifecycle.broker_keepalive_loop,
            args=(self, self._keepalive_stop),
            kwargs={
                "interval_seconds": interval,
                "broker_script": BROKER_SCRIPT,
                "run_command": _run,
            },
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
            error_type=LoopDockerBrokerError,
            remove_container=runtime_cli.remove_container,
            remove_network=runtime_cli.remove_network,
        )


def start_broker(
    config: dict[str, Any],
    *,
    scope: str,
    owner_id: str,
    scenario_image_id: str,
    broker_image_id: str,
    max_lifetime_seconds: int,
    runner: SubprocessRunner = subprocess.run,
) -> LoopBrokerSession:
    """Start one dual-homed broker, cleaning every partial resource on failure."""
    broker = _broker_config(config)
    start_port, end_port = broker.get("port_range", [8790, 8990])
    port = start_port + secrets.randbelow(end_port - start_port + 1)
    nonce = secrets.token_hex(3)
    stem = f"lh-{runtime_profile.safe_name(scope)}-{nonce}"
    container_name = f"{stem}-broker"
    internal_network = f"{stem}-internal"
    external_network = f"{stem}-external"
    run_token = f"lh-{secrets.token_urlsafe(24)}"
    try:
        credential = credentials.load_claude_oauth_credential(
            minimum_ttl_seconds=(max_lifetime_seconds + credentials.TOKEN_TTL_MARGIN_SECONDS),
            runner=runner,
        )
    except credentials.ClaudeCredentialError as exc:
        raise LoopDockerBrokerError(str(exc)) from exc
    owner_labels = lifecycle.resource_labels(_RUNTIME_LABELS, owner_id)
    broker_env = _broker_env(
        broker,
        run_token=run_token,
        port=port,
        max_lifetime_seconds=max_lifetime_seconds,
    )
    spec = lifecycle.BrokerContainerSpec(
        docker_label=DOCKER_LABEL,
        broker_alias=BROKER_ALIAS,
        container_name=container_name,
        internal_network=internal_network,
        external_network=external_network,
        broker_image_id=broker_image_id,
        broker_env=broker_env,
        owner_labels=owner_labels,
    )

    def session_factory() -> LoopBrokerSession:
        return LoopBrokerSession(
            container_name=container_name,
            internal_network=internal_network,
            external_network=external_network,
            run_token=run_token,
            port=port,
            scenario_image_id=scenario_image_id,
            broker_image_id=broker_image_id,
            owner_labels=owner_labels,
            runner=runner,
            idle_timeout_seconds=int(broker["idle_timeout_sec"]),
        )

    try:
        return lifecycle.start_broker_container(
            spec,
            runner=runner,
            checked=_checked,
            remove_container=runtime_cli.remove_container,
            remove_network=runtime_cli.remove_network,
            inject_token=lambda: _inject_token(
                container_name,
                credential.access_token,
                runner=runner,
            ),
            wait_ready=lambda: _wait_ready(container_name, port, broker, runner=runner),
            session_factory=session_factory,
            error_type=LoopDockerBrokerError,
        )
    except runtime_cli.DockerCliError as exc:
        raise LoopDockerBrokerError(str(exc)) from exc


def start_isolated_network(
    *,
    scope: str,
    owner_id: str,
    runner: SubprocessRunner = subprocess.run,
) -> str:
    """Create just the scenario container's dedicated internal network -- no broker container.

    Codex review, PR #262, High: a checker action whose resolved params have only
    `mechanical.commands` and no `llm_review` block never calls `execute_claude()`
    (`build_action_executor()` computes `needs_broker=False` for exactly that case), so starting
    the full credential broker here -- and, more importantly, `start_broker()`'s unconditional
    `credentials.load_claude_oauth_credential()` -- would turn an otherwise-valid Docker
    mechanical check into a hard infrastructure failure on any host without a live Claude OAuth
    credential (Linux/CI, no-token environments). The scenario container still needs *a*
    dedicated internal network at `docker run` time (`loop_docker_profile._validate_spec` forbids
    bridge/default/host/none), so only that network is created here; it carries the same
    owner/run labels a broker-created network would, so `sweep_stale_resources()` below reclaims
    it identically if the driver crashes before cleanup. `DockerActionRuntime._broker_exec_env()`
    still fails closed with `DockerActionError` if `execute_claude()` is ever called against a
    runtime started this way (`self.broker` stays `None`).
    """
    nonce = secrets.token_hex(3)
    internal_network = f"lh-{runtime_profile.safe_name(scope)}-{nonce}-internal"
    owner_labels = lifecycle.resource_labels(_RUNTIME_LABELS, owner_id)
    _checked(
        [
            "docker",
            "network",
            "create",
            "--internal",
            "--label",
            f"{DOCKER_LABEL}=run",
            *lifecycle.label_args(owner_labels),
            internal_network,
        ],
        runner=runner,
        message="could not create internal Docker network",
    )
    return internal_network


def stop_isolated_network(
    internal_network: str,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> bool:
    """Remove the network `start_isolated_network()` created."""
    return runtime_cli.remove_network(internal_network, runner=runner)


def sweep_stale_resources(
    owner_id: str,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> None:
    lifecycle.sweep_stale_resources(
        _RUNTIME_LABELS,
        owner_id,
        runner=runner,
        run_command=_run,
        best_effort=runtime_cli.best_effort,
        container_stale=lambda inspected, owner: lifecycle.container_is_stale(
            inspected, owner, labels=_RUNTIME_LABELS
        ),
        network_stale=lambda inspected, owner: lifecycle.network_is_stale(
            inspected, owner, labels=_RUNTIME_LABELS
        ),
    )


def _broker_config(config: dict[str, Any]) -> dict[str, Any]:
    lp2 = config.get("lp2") if isinstance(config.get("lp2"), dict) else {}
    isolation = lp2.get("isolation") if isinstance(lp2.get("isolation"), dict) else {}
    broker = isolation.get("broker") if isinstance(isolation.get("broker"), dict) else {}
    return dict(broker)


def _broker_env(
    broker: dict[str, Any],
    *,
    run_token: str,
    port: int,
    max_lifetime_seconds: int,
) -> dict[str, str]:
    pricing = broker["pricing_upper_bound_usd_per_million"]
    return {
        "DR_BROKER_NAMESPACE": BROKER_NAMESPACE,
        "DR_BROKER_RUN_TOKEN": run_token,
        "DR_BROKER_PORT": str(port),
        "DR_BROKER_BUDGET_USD": str(broker["budget_usd"]),
        "DR_BROKER_IDLE_TIMEOUT_SEC": str(broker["idle_timeout_sec"]),
        "DR_BROKER_MAX_LIFETIME_SEC": str(max_lifetime_seconds),
        "DR_BROKER_STARTUP_TIMEOUT_SEC": str(broker["startup_timeout_sec"]),
        "DR_BROKER_MAX_REQUESTS": str(broker["max_requests"]),
        "DR_BROKER_MAX_TOTAL_TOKENS": str(broker["max_total_tokens"]),
        "DR_BROKER_MAX_UPSTREAM_BYTES": str(broker["max_upstream_bytes"]),
        "DR_PRICE_INPUT": str(pricing["input"]),
        "DR_PRICE_OUTPUT": str(pricing["output"]),
        "DR_PRICE_CACHE_CREATION": str(pricing["cache_creation"]),
        "DR_PRICE_CACHE_READ": str(pricing["cache_read"]),
    }


def _inject_token(
    container_name: str,
    token: str,
    *,
    runner: SubprocessRunner,
) -> None:
    try:
        completed = runner(
            [
                "docker",
                "exec",
                "-i",
                container_name,
                "/usr/bin/python3",
                BROKER_SCRIPT,
                "--write-token",
            ],
            input=token,
            capture_output=True,
            text=True,
            timeout=10,
            env=runtime_cli.host_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoopDockerBrokerError("could not inject OAuth token into broker tmpfs") from exc
    if completed.returncode != 0:
        raise LoopDockerBrokerError("could not inject OAuth token into broker tmpfs")


def _wait_ready(
    container_name: str,
    port: int,
    broker: dict[str, Any],
    *,
    runner: SubprocessRunner,
) -> None:
    deadline = time.monotonic() + int(broker["startup_timeout_sec"])
    command = [
        "docker",
        "exec",
        container_name,
        "/usr/bin/python3",
        BROKER_SCRIPT,
        "--health",
        "--port",
        str(port),
    ]
    while time.monotonic() < deadline:
        if _run(command, runner=runner, timeout=5).returncode == 0:
            return
        time.sleep(0.1)
    raise LoopDockerBrokerError("credential broker did not become healthy")


def _run(
    command: list[str],
    *,
    runner: SubprocessRunner,
    timeout: int | float,
) -> subprocess.CompletedProcess:
    return runtime_cli.run(command, runner=runner, timeout=timeout)


def _checked(
    command: list[str],
    *,
    runner: SubprocessRunner,
    message: str,
) -> subprocess.CompletedProcess:
    try:
        return runtime_cli.checked(command, runner=runner, message=message)
    except runtime_cli.DockerCliError as exc:
        raise LoopDockerBrokerError(str(exc)) from exc
