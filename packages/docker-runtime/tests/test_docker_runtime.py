"""Shared Docker runtime behavior tests (docker-runtime EV-01 through EV-08)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

cli = load_module(
    "docker_runtime_cli",
    "packages/docker-runtime/lib/docker_runtime_cli.py",
)
lifecycle = load_module(
    "docker_runtime_lifecycle_tests",
    "packages/docker-runtime/lib/docker_runtime_lifecycle.py",
)
profile = load_module(
    "docker_runtime_profile_tests",
    "packages/docker-runtime/lib/docker_runtime_profile.py",
)
broker = load_module(
    "docker_runtime_broker_tests",
    "packages/docker-runtime/docker/broker/broker.py",
)

IMAGE_ID = "sha256:" + "a" * 64


def _completed(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_ensure_image_reuses_process_local_build_cache(tmp_path: Path) -> None:
    context = tmp_path / "scenario"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM example@sha256:" + "b" * 64, encoding="utf-8")
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return _completed(stdout=IMAGE_ID)
        return _completed()

    cache = cli.ImageCache()
    for _ in range(2):
        cli.ensure_image(
            "runtime:test",
            context,
            context_hash_label="ai.orchestra.test.context-sha256",
            auto_build=True,
            build_args=["--build-arg", "VERSION=1"],
            runner=runner,
            cache=cache,
        )

    builds = [command for command in commands if command[:2] == ["docker", "build"]]
    assert len(builds) == 1
    assert "--no-cache" in builds[0]
    assert any(value.startswith("ai.orchestra.test.context-sha256=") for value in builds[0])


def test_prebuilt_image_requires_immutable_digest(tmp_path: Path) -> None:
    with pytest.raises(cli.DockerCliError, match="immutable"):
        cli.ensure_image(
            "runtime:mutable",
            tmp_path,
            context_hash_label="ai.orchestra.test.context-sha256",
            auto_build=False,
            build_args=[],
            runner=lambda *_args, **_kwargs: _completed(),
            cache=cli.ImageCache(),
        )


def test_resource_removal_only_accepts_explicit_missing_response() -> None:
    responses = iter(
        [
            _completed(returncode=1, stderr="permission denied"),
            _completed(returncode=1, stderr="permission denied"),
        ]
    )
    assert cli.remove_container("owned", runner=lambda *_args, **_kwargs: next(responses)) is False

    assert (
        cli.remove_network(
            "gone",
            runner=lambda *_args, **_kwargs: _completed(
                returncode=1,
                stderr="Error response from daemon: network gone not found",
            ),
        )
        is True
    )


def test_profile_builders_keep_hardening_and_validate_mounts(tmp_path: Path) -> None:
    assert profile.tmpfs("/tmp", 501, 20, size="64m").startswith("/tmp:rw,noexec,nosuid,nodev")
    assert profile.bounded_container_command(
        {"max_lifetime_sec": 30}, ["command"], kill_after_seconds=5
    ) == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "30s",
        "command",
    ]
    with pytest.raises(profile.DockerProfileError, match="comma"):
        profile.bind_mount(tmp_path / "invalid,path", "/workspace", read_only=True)


def test_broker_command_is_dual_homed_hardened_and_uses_image_id() -> None:
    spec = lifecycle.BrokerContainerSpec(
        docker_label="ai.orchestra.test",
        broker_alias="test-broker",
        container_name="test-broker-1",
        internal_network="test-internal",
        external_network="test-external",
        broker_image_id=IMAGE_ID,
        broker_env={"TOKEN": "run-token"},
        owner_labels={"ai.orchestra.test.owner": "owner"},
    )

    command = lifecycle.broker_run_command(spec)
    rendered = " ".join(command)

    assert "--network test-internal" in rendered
    assert "--network-alias test-broker" in rendered
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert command[-1] == IMAGE_ID
    assert "/var/run/docker.sock" not in rendered


def test_partial_broker_startup_failure_cleans_owned_resources() -> None:
    spec = lifecycle.BrokerContainerSpec(
        docker_label="ai.orchestra.test",
        broker_alias="test-broker",
        container_name="test-broker-1",
        internal_network="test-internal",
        external_network="test-external",
        broker_image_id=IMAGE_ID,
        broker_env={},
        owner_labels={},
    )
    removed: list[str] = []
    checked_count = 0

    def checked(*_args, **_kwargs) -> subprocess.CompletedProcess:
        nonlocal checked_count
        checked_count += 1
        if checked_count == 3:
            raise RuntimeError("broker failed")
        return _completed()

    with pytest.raises(RuntimeError, match="broker failed"):
        lifecycle.start_broker_container(
            spec,
            runner=lambda *_args, **_kwargs: _completed(),
            checked=checked,
            remove_container=lambda name, **_kwargs: not removed.append(name),
            remove_network=lambda name, **_kwargs: not removed.append(name),
            inject_token=lambda: None,
            wait_ready=lambda: None,
            session_factory=lambda: pytest.fail("session must not be created"),
            error_type=RuntimeError,
        )

    assert removed == ["test-broker-1", "test-external", "test-internal"]


def test_runtime_labels_keep_harness_namespaces_independent() -> None:
    meta = lifecycle.RuntimeLabels("ai.orchestra.meta-harness")
    loop = lifecycle.RuntimeLabels("ai.orchestra.loop-harness")

    assert meta.owner_label == "ai.orchestra.meta-harness.owner"
    assert loop.owner_label == "ai.orchestra.loop-harness.owner"
    assert meta.owner_label != loop.owner_label


def _replace_broker_environment(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    names = {
        "DR_BROKER_NAMESPACE",
        "DR_BROKER_RUN_TOKEN",
        "DR_BROKER_PORT",
        "DR_BROKER_BUDGET_USD",
        "DR_BROKER_IDLE_TIMEOUT_SEC",
        "DR_BROKER_MAX_LIFETIME_SEC",
        "DR_BROKER_STARTUP_TIMEOUT_SEC",
        "DR_BROKER_MAX_REQUESTS",
        "DR_BROKER_MAX_TOTAL_TOKENS",
        "DR_BROKER_MAX_UPSTREAM_BYTES",
        "DR_PRICE_INPUT",
        "DR_PRICE_OUTPUT",
        "DR_PRICE_CACHE_CREATION",
        "DR_PRICE_CACHE_READ",
        "DR_BROKER_MODEL_ALLOWLIST",
        "MH_BROKER_MODEL_ALLOWLIST",
        "MH_BROKER_RUN_TOKEN",
        "MH_BROKER_PORT",
        "MH_BROKER_BUDGET_USD",
        "MH_BROKER_IDLE_TIMEOUT_SEC",
        "MH_BROKER_MAX_LIFETIME_SEC",
        "MH_BROKER_STARTUP_TIMEOUT_SEC",
        "MH_BROKER_MAX_REQUESTS",
        "MH_BROKER_MAX_TOTAL_TOKENS",
        "MH_BROKER_MAX_UPSTREAM_BYTES",
        "MH_PRICE_INPUT",
        "MH_PRICE_OUTPUT",
        "MH_PRICE_CACHE_CREATION",
        "MH_PRICE_CACHE_READ",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_broker_environment_falls_back_to_legacy_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_broker_environment(
        monkeypatch,
        {
            "MH_BROKER_RUN_TOKEN": "legacy-token",
            "MH_BROKER_PORT": "8787",
            "MH_BROKER_BUDGET_USD": "3.0",
            "MH_BROKER_IDLE_TIMEOUT_SEC": "300",
            "MH_BROKER_MAX_LIFETIME_SEC": "660",
            "MH_BROKER_STARTUP_TIMEOUT_SEC": "30",
            "MH_BROKER_MAX_REQUESTS": "64",
            "MH_BROKER_MAX_TOTAL_TOKENS": "500000",
            "MH_BROKER_MAX_UPSTREAM_BYTES": "50000000",
            "MH_PRICE_INPUT": "15.0",
            "MH_PRICE_OUTPUT": "75.0",
            "MH_PRICE_CACHE_CREATION": "18.75",
            "MH_PRICE_CACHE_READ": "1.5",
        },
    )

    settings = broker._broker_settings_from_env()

    assert settings == broker.BrokerSettings(
        port=8787,
        startup_timeout_seconds=30,
        run_token="legacy-token",
        budget_usd=3.0,
        pricing=broker.Pricing(15.0, 75.0, 18.75, 1.5),
        max_requests=64,
        max_total_tokens=500000,
        max_upstream_bytes=50000000,
        idle_timeout_seconds=300,
        max_lifetime_seconds=660,
        identity=broker.BrokerIdentity(
            "meta-harness-broker",
            "ai-orchestra-meta-harness-broker/0.1",
        ),
    )


def test_broker_environment_prefers_generic_names(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = {
        "MH_BROKER_RUN_TOKEN": "legacy-token",
        "MH_BROKER_PORT": "8787",
        "MH_BROKER_BUDGET_USD": "3.0",
        "MH_BROKER_IDLE_TIMEOUT_SEC": "300",
        "MH_BROKER_MAX_LIFETIME_SEC": "660",
        "MH_BROKER_STARTUP_TIMEOUT_SEC": "30",
        "MH_BROKER_MAX_REQUESTS": "64",
        "MH_BROKER_MAX_TOTAL_TOKENS": "500000",
        "MH_BROKER_MAX_UPSTREAM_BYTES": "50000000",
        "MH_PRICE_INPUT": "15.0",
        "MH_PRICE_OUTPUT": "75.0",
        "MH_PRICE_CACHE_CREATION": "18.75",
        "MH_PRICE_CACHE_READ": "1.5",
    }
    generic = {
        "DR_BROKER_NAMESPACE": "loop-harness",
        "DR_BROKER_RUN_TOKEN": "generic-token",
        "DR_BROKER_PORT": "9001",
        "DR_BROKER_BUDGET_USD": "4.5",
        "DR_BROKER_IDLE_TIMEOUT_SEC": "301",
        "DR_BROKER_MAX_LIFETIME_SEC": "661",
        "DR_BROKER_STARTUP_TIMEOUT_SEC": "31",
        "DR_BROKER_MAX_REQUESTS": "65",
        "DR_BROKER_MAX_TOTAL_TOKENS": "500001",
        "DR_BROKER_MAX_UPSTREAM_BYTES": "50000001",
        "DR_PRICE_INPUT": "16.0",
        "DR_PRICE_OUTPUT": "76.0",
        "DR_PRICE_CACHE_CREATION": "19.0",
        "DR_PRICE_CACHE_READ": "2.0",
    }
    _replace_broker_environment(monkeypatch, {**legacy, **generic})

    settings = broker._broker_settings_from_env()

    assert settings == broker.BrokerSettings(
        port=9001,
        startup_timeout_seconds=31,
        run_token="generic-token",
        budget_usd=4.5,
        pricing=broker.Pricing(16.0, 76.0, 19.0, 2.0),
        max_requests=65,
        max_total_tokens=500001,
        max_upstream_bytes=50000001,
        idle_timeout_seconds=301,
        max_lifetime_seconds=661,
        identity=broker.BrokerIdentity(
            "loop-harness-broker",
            "ai-orchestra-loop-harness-broker/0.1",
        ),
    )


def test_broker_environment_falls_back_per_missing_generic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_broker_environment(
        monkeypatch,
        {
            "DR_BROKER_PORT": "9001",
            "MH_BROKER_PORT": "8787",
            "MH_PRICE_INPUT": "15.0",
        },
    )

    assert broker._env_value("DR_BROKER_PORT", "MH_BROKER_PORT") == "9001"
    assert broker._env_value("DR_PRICE_INPUT", "MH_PRICE_INPUT") == "15.0"


def test_env_value_raises_key_error_when_neither_variable_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EV-21: fail-loud (KeyError naming both variables) when neither the generic
    nor the legacy env var is set."""
    _replace_broker_environment(monkeypatch, {})

    with pytest.raises(KeyError, match="DR_BROKER_PORT.*MH_BROKER_PORT"):
        broker._env_value("DR_BROKER_PORT", "MH_BROKER_PORT")


def test_broker_settings_from_env_rejects_empty_run_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_broker_environment(
        monkeypatch,
        {
            "DR_BROKER_NAMESPACE": "loop-harness",
            "DR_BROKER_RUN_TOKEN": "",
            "DR_BROKER_PORT": "9001",
            "DR_BROKER_BUDGET_USD": "4.5",
            "DR_BROKER_IDLE_TIMEOUT_SEC": "301",
            "DR_BROKER_MAX_LIFETIME_SEC": "661",
            "DR_BROKER_STARTUP_TIMEOUT_SEC": "31",
            "DR_BROKER_MAX_REQUESTS": "65",
            "DR_BROKER_MAX_TOTAL_TOKENS": "500001",
            "DR_BROKER_MAX_UPSTREAM_BYTES": "50000001",
            "DR_PRICE_INPUT": "16.0",
            "DR_PRICE_OUTPUT": "76.0",
            "DR_PRICE_CACHE_CREATION": "19.0",
            "DR_PRICE_CACHE_READ": "2.0",
        },
    )

    with pytest.raises(RuntimeError, match="run token must not be empty"):
        broker._broker_settings_from_env()


def test_model_allowlist_env_is_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _replace_broker_environment(monkeypatch, {})

    assert (
        broker._model_allowlist_env("DR_BROKER_MODEL_ALLOWLIST", "MH_BROKER_MODEL_ALLOWLIST")
        is None
    )


def test_model_allowlist_env_is_none_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _replace_broker_environment(monkeypatch, {"DR_BROKER_MODEL_ALLOWLIST": "  , ,"})

    assert (
        broker._model_allowlist_env("DR_BROKER_MODEL_ALLOWLIST", "MH_BROKER_MODEL_ALLOWLIST")
        is None
    )


def test_model_allowlist_env_prefers_generic_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_broker_environment(
        monkeypatch,
        {
            "DR_BROKER_MODEL_ALLOWLIST": "claude-cheap-model",
            "MH_BROKER_MODEL_ALLOWLIST": "claude-legacy-model",
        },
    )

    result = broker._model_allowlist_env("DR_BROKER_MODEL_ALLOWLIST", "MH_BROKER_MODEL_ALLOWLIST")

    assert result == frozenset({"claude-cheap-model"})


def test_model_allowlist_env_falls_back_to_legacy_and_strips_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_broker_environment(
        monkeypatch,
        {"MH_BROKER_MODEL_ALLOWLIST": " claude-cheap-model , claude-cheaper-model ,,"},
    )

    result = broker._model_allowlist_env("DR_BROKER_MODEL_ALLOWLIST", "MH_BROKER_MODEL_ALLOWLIST")

    assert result == frozenset({"claude-cheap-model", "claude-cheaper-model"})


def test_broker_settings_from_env_defaults_model_allowlist_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_broker_environment(
        monkeypatch,
        {
            "MH_BROKER_RUN_TOKEN": "legacy-token",
            "MH_BROKER_PORT": "8787",
            "MH_BROKER_BUDGET_USD": "3.0",
            "MH_BROKER_IDLE_TIMEOUT_SEC": "300",
            "MH_BROKER_MAX_LIFETIME_SEC": "660",
            "MH_BROKER_STARTUP_TIMEOUT_SEC": "30",
            "MH_BROKER_MAX_REQUESTS": "64",
            "MH_BROKER_MAX_TOTAL_TOKENS": "500000",
            "MH_BROKER_MAX_UPSTREAM_BYTES": "50000000",
            "MH_PRICE_INPUT": "15.0",
            "MH_PRICE_OUTPUT": "75.0",
            "MH_PRICE_CACHE_CREATION": "18.75",
            "MH_PRICE_CACHE_READ": "1.5",
        },
    )

    settings = broker._broker_settings_from_env()

    assert settings.model_allowlist is None


def test_broker_settings_from_env_reads_model_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_broker_environment(
        monkeypatch,
        {
            "MH_BROKER_RUN_TOKEN": "legacy-token",
            "MH_BROKER_PORT": "8787",
            "MH_BROKER_BUDGET_USD": "3.0",
            "MH_BROKER_IDLE_TIMEOUT_SEC": "300",
            "MH_BROKER_MAX_LIFETIME_SEC": "660",
            "MH_BROKER_STARTUP_TIMEOUT_SEC": "30",
            "MH_BROKER_MAX_REQUESTS": "64",
            "MH_BROKER_MAX_TOTAL_TOKENS": "500000",
            "MH_BROKER_MAX_UPSTREAM_BYTES": "50000000",
            "MH_PRICE_INPUT": "15.0",
            "MH_PRICE_OUTPUT": "75.0",
            "MH_PRICE_CACHE_CREATION": "18.75",
            "MH_PRICE_CACHE_READ": "1.5",
            "DR_BROKER_MODEL_ALLOWLIST": "claude-cheap-model,claude-cheaper-model",
        },
    )

    settings = broker._broker_settings_from_env()

    assert settings.model_allowlist == frozenset({"claude-cheap-model", "claude-cheaper-model"})


@pytest.mark.parametrize("namespace", ["", "Loop-Harness", "../loop", "x" * 64])
def test_broker_identity_rejects_invalid_namespace(namespace: str) -> None:
    with pytest.raises(ValueError, match="broker namespace"):
        broker._broker_identity(namespace)


def test_broker_identity_accepts_namespace_at_max_length() -> None:
    """Pairs with the 64-char rejection case above: 63 is the boundary that must pass."""
    namespace = "x" * 63

    identity = broker._broker_identity(namespace)

    assert identity.server_version == f"{namespace}-broker"
    assert identity.user_agent == f"ai-orchestra-{namespace}-broker/0.1"


def test_sweep_stale_resources_removes_only_stale_containers_and_networks() -> None:
    """EV-11: Only resources selected by the injected stale checks are removed."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    stale_container = "container-stale"
    active_container = "container-active"
    stale_network = "network-stale"
    active_network = "network-active"
    removed: list[list[str]] = []

    def run_command(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        if command[:3] == ["docker", "ps", "-aq"]:
            return _completed(stdout=f"{stale_container} {active_container}\n")
        if command[:4] == ["docker", "network", "ls", "-q"]:
            return _completed(stdout=f"{stale_network} {active_network}\n")
        return _completed(stdout='[{"Id": "' + command[-1] + '"}]')

    def container_stale(inspected: dict, _owner: str) -> bool:
        return inspected["Id"] == stale_container

    def network_stale(inspected: dict, _owner: str) -> bool:
        return inspected["Id"] == stale_network

    def best_effort(command: list[str], **_kwargs) -> None:
        removed.append(command)

    lifecycle.sweep_stale_resources(
        labels,
        "owner-test",
        runner=subprocess.run,
        run_command=run_command,
        best_effort=best_effort,
        container_stale=container_stale,
        network_stale=network_stale,
    )

    assert removed == [
        ["docker", "rm", "-f", stale_container],
        ["docker", "network", "rm", stale_network],
    ]
    assert ["docker", "rm", "-f", active_container] not in removed
    assert ["docker", "network", "rm", active_network] not in removed


def test_container_is_stale_returns_false_for_owner_mismatch() -> None:
    """EV-12: A container owned by another caller is never stale."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    inspected = {"Config": {"Labels": {labels.owner_label: "other-owner"}}}

    assert lifecycle.container_is_stale(inspected, "owner-test", labels=labels) is False


def test_network_is_stale_returns_false_for_owner_mismatch() -> None:
    """EV-12: A network owned by another caller is never stale."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    inspected = {"Labels": {labels.owner_label: "other-owner"}}

    assert lifecycle.network_is_stale(inspected, "owner-test", labels=labels) is False


def _price_modifier_test_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setattr(broker, "METRICS_PATH", tmp_path / "metrics.json")
    return broker.BrokerState(
        run_token="run-token",
        oauth_token="real-oauth-token",
        budget_usd=3.0,
        pricing=broker.Pricing(3.0, 15.0, 6.0, 0.30),
        max_requests=4,
        max_total_tokens=100_000,
        max_upstream_bytes=100_000,
    )


def test_request_budget_error_allows_body_without_price_modifier_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #261 PR2 review round 6 (High): a normal request body must pass through
    unaffected by the new pricing-modifier rejection."""
    state = _price_modifier_test_state(tmp_path, monkeypatch)
    body = b'{"model": "claude-sonnet-5", "max_tokens": 1, "messages": []}'

    assert state.request_budget_error("/v1/messages", body) is None


@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("inference_geo", "/v1/messages"),
        ("service_tier", "/v1/messages"),
        ("speed", "/v1/messages"),
        ("inference_geo", "/v1/messages/count_tokens"),
        ("service_tier", "/v1/messages/count_tokens"),
        ("speed", "/v1/messages/count_tokens"),
    ],
)
def test_request_budget_error_rejects_price_modifier_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, path: str
) -> None:
    """Issue #261 PR2 review round 6/7 (High): a body carrying a known pricing-modifier
    field (e.g. a non-default inference region, a priority service tier, or a
    premium-priced fast `speed`) can attach a price multiplier the broker's fixed
    pricing_upper_bound_usd_per_million ceiling is not calibrated for, so it is
    rejected fail-closed on both billable paths."""
    state = _price_modifier_test_state(tmp_path, monkeypatch)
    body = (
        '{"model": "claude-sonnet-5", "max_tokens": 1, "messages": [], "' + field + '": "us"}'
    ).encode()

    result = state.request_budget_error(path, body)

    assert result is not None
    status, message = result
    assert status == 400
    assert field in message
