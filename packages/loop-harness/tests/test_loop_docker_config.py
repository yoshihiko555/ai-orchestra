"""Phase-4 loop-harness Docker configuration tests."""

from __future__ import annotations

import copy

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

docker_config = load_module(
    "loop_docker_config_tests",
    "packages/loop-harness/lib/loop_docker_config.py",
)


def _config() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "packages/loop-harness/config/loop-harness.yaml").read_text(encoding="utf-8")
    )


def test_default_config_is_disabled_and_has_required_broker_limits() -> None:
    config = _config()
    validated = docker_config.validate_isolation_config(config)

    assert validated.backend == "none"
    assert validated.execution_backend == "none"
    assert validated.docker_execution_enabled is False
    assert validated.broker.budget_usd == 25.0
    assert validated.broker.max_requests == 400
    assert validated.broker.max_total_tokens == 30000000
    assert validated.broker.max_upstream_bytes == 500000000
    assert validated.broker.pricing.output == 75.0


@pytest.mark.parametrize(
    ("backend", "execution_backend", "expected"),
    [
        ("none", "none", False),
        ("docker", "none", False),
        ("docker", "docker", True),
    ],
)
def test_execution_backend_is_the_only_docker_switch(
    backend: str,
    execution_backend: str,
    expected: bool,
) -> None:
    config = _config()
    config["lp2"]["isolation"].update({"backend": backend, "execution_backend": execution_backend})

    assert docker_config.docker_execution_enabled(config) is expected


def test_execution_backend_docker_requires_backend_docker() -> None:
    config = _config()
    config["lp2"]["isolation"]["execution_backend"] = "docker"

    with pytest.raises(docker_config.DockerConfigError, match="requires backend docker"):
        docker_config.validate_isolation_config(config)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("backend",), "podman", "backend must be one of"),
        (("execution_backend",), "host", "execution_backend must be one of"),
        (("auto_build_images",), "false", "must be a boolean"),
        (("resources", "pids_limit"), True, "positive integer"),
        (("resources", "pids_limit"), 0, "positive integer"),
        (("resources", "memory"), "unlimited", "positive Docker memory size"),
        (("resources", "memory"), "1.5g", "positive Docker memory size"),
        (("resources", "cpus"), float("inf"), "positive finite number"),
        (("resources", "cpus"), 0, "positive finite number"),
        (("checker", "read_only_worktree"), False, "must be true"),
        (("broker", "port_range"), [8990, 8790], "ordered within"),
        (("broker", "port_range"), [1, 65536], "ordered within"),
        (("broker", "idle_timeout_sec"), 0, "positive integer"),
        (("broker", "startup_timeout_sec"), 0.5, "positive integer"),
        (("broker", "budget_usd"), "3.0", "positive finite number"),
        (("broker", "max_requests"), 0, "positive integer"),
        (("broker", "max_total_tokens"), True, "positive integer"),
        (("broker", "max_upstream_bytes"), -1, "positive integer"),
        (
            ("broker", "pricing_upper_bound_usd_per_million", "input"),
            float("nan"),
            "positive finite number",
        ),
    ],
)
def test_invalid_types_and_ranges_fail_closed(
    path: tuple[str, ...],
    value: object,
    message: str,
) -> None:
    config = _config()
    target = config["lp2"]["isolation"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(docker_config.DockerConfigError, match=message):
        docker_config.validate_isolation_config(config)


@pytest.mark.parametrize("mutable_field", ["image", "broker.image"])
def test_auto_build_disabled_requires_both_digest_pins(mutable_field: str) -> None:
    digest = "@sha256:" + "a" * 64
    config = _config()
    isolation = config["lp2"]["isolation"]
    isolation["auto_build_images"] = False
    isolation["image"] = "scenario" + digest
    isolation["broker"]["image"] = "broker" + digest
    if mutable_field == "image":
        isolation["image"] = "scenario:latest"
    else:
        isolation["broker"]["image"] = "broker:latest"

    with pytest.raises(docker_config.DockerConfigError, match="immutable @sha256 digest"):
        docker_config.validate_isolation_config(config)


def test_auto_build_disabled_accepts_digest_pins_for_both_images() -> None:
    digest = "@sha256:" + "a" * 64
    config = _config()
    isolation = config["lp2"]["isolation"]
    isolation.update({"auto_build_images": False, "image": "scenario" + digest})
    isolation["broker"]["image"] = "broker" + digest

    assert docker_config.validate_isolation_config(config).auto_build_images is False


def test_image_pin_accepts_claude_version_output_with_spaces() -> None:
    config = _config()
    config["lp2"]["isolation"]["image_pin"] = "2.1.207 (Claude Code)"

    assert docker_config.validate_isolation_config(config).image_pin == "2.1.207 (Claude Code)"


def test_synced_config_matches_source_broker_defaults() -> None:
    source = _config()["lp2"]["isolation"]["broker"]
    synced = yaml.safe_load(
        (REPO_ROOT / ".claude/config/loop-harness/loop-harness.yaml").read_text(encoding="utf-8")
    )["lp2"]["isolation"]["broker"]

    assert copy.deepcopy(synced) == source
