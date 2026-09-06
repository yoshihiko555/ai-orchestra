#!/usr/bin/env python3
"""Validated Phase-4 Docker configuration for loop-harness."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_DIGEST_IMAGE_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_MEMORY_RE = re.compile(r"^[1-9][0-9]*(?:[bkmg]|[kmgt]i?b)?$", re.IGNORECASE)
DEFAULT_SCENARIO_IMAGE = "ai-orchestra/loop-harness-scenario:2.1.207"
DEFAULT_BROKER_IMAGE = "ai-orchestra/loop-harness-broker:0.1.0"


class DockerConfigError(ValueError):
    """The loop-harness Docker isolation configuration is unsafe or invalid."""


@dataclass(frozen=True)
class DockerResources:
    """Container resource ceilings accepted by ``docker run``."""

    pids_limit: int
    memory: str
    cpus: float


@dataclass(frozen=True)
class BrokerPricing:
    """Fail-closed upper-bound prices used by the broker."""

    input: float
    output: float
    cache_creation: float
    cache_read: float


@dataclass(frozen=True)
class BrokerConfig:
    """Validated broker image, port, timeout, and usage limits."""

    image: str
    port_range: tuple[int, int]
    idle_timeout_sec: int
    startup_timeout_sec: int
    budget_usd: float
    max_requests: int
    max_total_tokens: int
    max_upstream_bytes: int
    pricing: BrokerPricing


@dataclass(frozen=True)
class DockerIsolationConfig:
    """Validated ``lp2.isolation`` settings used by the Docker executor."""

    backend: str
    execution_backend: str
    image: str
    image_pin: str | None
    auto_build_images: bool
    resources: DockerResources
    checker_read_only_worktree: bool
    broker: BrokerConfig

    @property
    def docker_execution_enabled(self) -> bool:
        """Only execution_backend enables Docker; backend alone never does."""
        return self.execution_backend == "docker"


def validate_isolation_config(config: Mapping[str, Any]) -> DockerIsolationConfig:
    """Validate and normalize the Phase-4 ``lp2.isolation`` configuration."""
    lp2 = _mapping(config.get("lp2", {}), "lp2")
    isolation = _mapping(lp2.get("isolation", {}), "lp2.isolation")
    backend, execution_backend = _validated_backends(isolation)

    image = _nonempty_string(
        isolation.get("image", DEFAULT_SCENARIO_IMAGE),
        "image",
    )
    image_pin = _optional_string(isolation.get("image_pin"), "image_pin")
    auto_build_images = _boolean(isolation.get("auto_build_images", True), "auto_build_images")
    resources = _validate_resources(isolation.get("resources", {}))
    checker_read_only = _validate_checker(isolation.get("checker", {}))
    broker = _validate_broker(isolation.get("broker", {}))
    if not auto_build_images:
        _require_digest_image(image, "image")
        _require_digest_image(broker.image, "broker.image")

    return DockerIsolationConfig(
        backend=backend,
        execution_backend=execution_backend,
        image=image,
        image_pin=image_pin,
        auto_build_images=auto_build_images,
        resources=resources,
        checker_read_only_worktree=checker_read_only,
        broker=broker,
    )


def docker_execution_enabled(config: Mapping[str, Any]) -> bool:
    """Validate only the switches and return the sole Docker execution opt-in.

    Docker-only profile fields must not change the existing host path while
    ``execution_backend`` remains ``none``. The full profile is validated only after this
    switch returns true.
    """
    lp2 = _mapping(config.get("lp2", {}), "lp2")
    isolation = _mapping(lp2.get("isolation", {}), "lp2.isolation")
    _, execution_backend = _validated_backends(isolation)
    return execution_backend == "docker"


def _validated_backends(isolation: Mapping[str, Any]) -> tuple[str, str]:
    backend = _choice(isolation.get("backend", "none"), "backend", {"none", "docker"})
    execution_backend = _choice(
        isolation.get("execution_backend", "none"),
        "execution_backend",
        {"none", "docker"},
    )
    if execution_backend == "docker" and backend != "docker":
        raise DockerConfigError("execution_backend docker requires backend docker")
    return backend, execution_backend


def _validate_resources(value: Any) -> DockerResources:
    resources = _mapping(value, "resources")
    memory = _nonempty_string(resources.get("memory", "1g"), "resources.memory")
    if _MEMORY_RE.fullmatch(memory) is None:
        raise DockerConfigError("resources.memory must be a positive Docker memory size")
    return DockerResources(
        pids_limit=_positive_int(resources.get("pids_limit", 64), "resources.pids_limit"),
        memory=memory,
        cpus=_positive_number(resources.get("cpus", 1.0), "resources.cpus"),
    )


def _validate_checker(value: Any) -> bool:
    checker = _mapping(value, "checker")
    read_only = _boolean(checker.get("read_only_worktree", True), "checker.read_only_worktree")
    if not read_only:
        raise DockerConfigError("checker.read_only_worktree must be true in Phase 4")
    return read_only


def _validate_broker(value: Any) -> BrokerConfig:
    broker = _mapping(value, "broker")
    pricing = _mapping(
        broker.get("pricing_upper_bound_usd_per_million", {}),
        "broker.pricing_upper_bound_usd_per_million",
    )
    return BrokerConfig(
        image=_nonempty_string(
            broker.get("image", DEFAULT_BROKER_IMAGE),
            "broker.image",
        ),
        port_range=_port_range(broker.get("port_range", [8790, 8990])),
        idle_timeout_sec=_positive_int(
            broker.get("idle_timeout_sec", 300), "broker.idle_timeout_sec"
        ),
        startup_timeout_sec=_positive_int(
            broker.get("startup_timeout_sec", 30), "broker.startup_timeout_sec"
        ),
        # Issue #405: the previous defaults (budget_usd=3.0, max_requests=64,
        # max_total_tokens=500_000) rejected Claude Code's very first request outright -- its
        # system prompt + tools already push the per-request cost upper bound past a $3 run
        # budget, so every Maker/Checker LLM-review request was budget-rejected before a single
        # token could be spent. Recalibrated from an actual Maker run (31 requests, $6.68,
        # `total_tokens` over 2M once cache reads are counted) with headroom.
        budget_usd=_positive_number(broker.get("budget_usd", 25.0), "broker.budget_usd"),
        max_requests=_positive_int(broker.get("max_requests", 400), "broker.max_requests"),
        max_total_tokens=_positive_int(
            broker.get("max_total_tokens", 30000000), "broker.max_total_tokens"
        ),
        max_upstream_bytes=_positive_int(
            broker.get("max_upstream_bytes", 500000000), "broker.max_upstream_bytes"
        ),
        pricing=BrokerPricing(
            input=_positive_number(pricing.get("input", 15.0), "broker.pricing.input"),
            output=_positive_number(pricing.get("output", 75.0), "broker.pricing.output"),
            cache_creation=_positive_number(
                pricing.get("cache_creation", 18.75), "broker.pricing.cache_creation"
            ),
            cache_read=_positive_number(
                pricing.get("cache_read", 1.5), "broker.pricing.cache_read"
            ),
        ),
    )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DockerConfigError(f"{field} must be a mapping")
    return value


def _choice(value: Any, field: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        allowed = ", ".join(sorted(choices))
        raise DockerConfigError(f"{field} must be one of: {allowed}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DockerConfigError(f"{field} must be a boolean")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DockerConfigError(f"{field} must be a positive integer")
    return value


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DockerConfigError(f"{field} must be a positive finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise DockerConfigError(f"{field} must be a positive finite number")
    return normalized


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DockerConfigError(f"{field} must be a non-empty trimmed string")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise DockerConfigError(f"{field} must not contain whitespace or control characters")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise DockerConfigError(f"{field} must be a non-empty trimmed string or null")
    if any(ord(character) < 32 for character in value):
        raise DockerConfigError(f"{field} must not contain control characters")
    return value


def _port_range(value: Any) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise DockerConfigError("broker.port_range must be [start, end]")
    start = _positive_int(value[0], "broker.port_range start")
    end = _positive_int(value[1], "broker.port_range end")
    if start > end or end > 65535:
        raise DockerConfigError("broker.port_range must be ordered within 1..65535")
    return start, end


def _require_digest_image(image: str, field: str) -> None:
    if _DIGEST_IMAGE_RE.fullmatch(image) is None:
        raise DockerConfigError(
            f"{field} must use an immutable @sha256 digest when auto_build_images is false"
        )
