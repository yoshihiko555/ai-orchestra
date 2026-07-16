"""Loop-harness Docker image namespace and config tests (loop-harness EV-113)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

docker_image = load_module(
    "loop_docker_image_tests",
    "packages/loop-harness/lib/loop_docker_image.py",
)


def test_default_config_keeps_execution_disabled_and_defines_image_lifecycle() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / "packages/loop-harness/config/loop-harness.yaml").read_text(encoding="utf-8")
    )
    isolation = config["lp2"]["isolation"]

    assert isolation["backend"] == "none"
    assert isolation["execution_backend"] == "none"
    assert isolation["image_cache"] == {
        "manifest_path": ".claude/loop/docker-image-cache.json",
        "keep_generations": 3,
        "lock_path": ".claude/loop/docker-image-build.lock",
        "builder_name": "loop-harness-builder",
        "buildkit_cache_max_age": "168h",
        "buildkit_cache_max_size": "10g",
    }


def test_scenario_dockerfile_is_dedicated_digest_pinned_and_non_root() -> None:
    dockerfile = (REPO_ROOT / "packages/loop-harness/docker/scenario/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert dockerfile.splitlines()[0].startswith("FROM node:22.17.0-bookworm-slim@sha256:")
    assert "ARG CLAUDE_CODE_VERSION=2.1.207" in dockerfile
    assert "USER 65532:65532" in dockerfile


def test_wrapper_injects_loop_namespace_and_main_root_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def ensure(recipe, policy, **kwargs):
        captured.update(recipe=recipe, policy=policy, kwargs=kwargs)
        return docker_image.EnsuredImage("sha256:" + "a" * 64, "managed:tag", "b" * 64, True)

    monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
    config = {
        "lp2": {
            "isolation": {
                "image": "registry.example:5000/team/scenario:configured",
                "image_pin": None,
                "image_cache": {},
            }
        }
    }

    docker_image.ensure_scenario_image(config, tmp_path)

    recipe = captured["recipe"]
    policy = captured["policy"]
    assert recipe.family == "scenario"
    assert recipe.repository == "registry.example:5000/team/scenario"
    assert recipe.docker_label == "ai.orchestra.loop-harness"
    assert policy.manifest_path == tmp_path / ".claude/loop/docker-image-cache.json"
    assert policy.lock_path == tmp_path / ".claude/loop/docker-image-build.lock"


def test_wrapper_rejects_cache_path_outside_main_root(tmp_path: Path) -> None:
    config = {
        "lp2": {
            "isolation": {
                "image_cache": {"manifest_path": "../outside.json"},
            }
        }
    }

    with pytest.raises(docker_image.DockerImageError, match="escapes main root"):
        docker_image.ensure_scenario_image(config, tmp_path)


def test_wrapper_fails_closed_on_image_pin_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def ensure(recipe, _policy, **_kwargs):
        captured["recipe"] = recipe
        return docker_image.EnsuredImage("sha256:" + "a" * 64, "managed:tag", "b" * 64, True)

    monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
    monkeypatch.setattr(
        docker_image.runtime_cli,
        "image_claude_version",
        lambda *_args, **_kwargs: "2.1.206 (Claude Code)",
    )
    config = {"lp2": {"isolation": {"image_pin": "2.1.207 (Claude Code)"}}}

    with pytest.raises(docker_image.DockerImageError, match="image_pin mismatch"):
        docker_image.ensure_scenario_image(config, tmp_path)

    assert captured["recipe"].build_args == {"CLAUDE_CODE_VERSION": "2.1.207"}


def test_image_pin_semver_versions_produce_validated_build_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes = []
    current_pin = {"value": ""}

    def ensure(recipe, _policy, **_kwargs):
        recipes.append(recipe)
        return docker_image.EnsuredImage("sha256:" + "a" * 64, "managed:tag", "b" * 64, True)

    monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
    monkeypatch.setattr(
        docker_image.runtime_cli,
        "image_claude_version",
        lambda *_args, **_kwargs: current_pin["value"],
    )

    for version_pin, expected_version in [
        ("2.1.207", "2.1.207"),
        ("2.1.207 (Claude Code)", "2.1.207"),
        ("2.1.207-beta.1", "2.1.207-beta.1"),
    ]:
        current_pin["value"] = version_pin
        config = {"lp2": {"isolation": {"image_pin": version_pin}}}

        docker_image.ensure_scenario_image(config, tmp_path)

        assert recipes[-1].build_args == {"CLAUDE_CODE_VERSION": expected_version}


def test_image_pin_rejects_invalid_versions_before_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_calls = []
    injection_pin = "".join(['2.1.207";', "cu", "rl${IFS}evil|", "s", 'h;"'])

    def ensure(*args, **kwargs):
        build_calls.append((args, kwargs))
        raise AssertionError("invalid image_pin reached the Docker image build")

    monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)

    for version_pin in [injection_pin, "2.1", "v2.1.207", "2.1.207.9"]:
        config = {"lp2": {"isolation": {"image_pin": version_pin}}}
        with pytest.raises(docker_image.DockerImageError, match="semver"):
            docker_image.ensure_scenario_image(config, tmp_path)

    config = {"lp2": {"isolation": {"image_pin": ""}}}
    with pytest.raises(docker_image.DockerImageError, match="Claude CLI version"):
        docker_image.ensure_scenario_image(config, tmp_path)

    assert build_calls == []


def test_loop_harness_manifest_depends_on_docker_runtime() -> None:
    manifest = json.loads(
        (REPO_ROOT / "packages/loop-harness/manifest.json").read_text(encoding="utf-8")
    )

    assert "docker-runtime" in manifest["depends"]
