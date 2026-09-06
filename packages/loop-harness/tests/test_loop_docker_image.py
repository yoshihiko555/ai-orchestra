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
    from_lines = [
        line for line in dockerfile.splitlines() if line.strip().upper().startswith("FROM ")
    ]

    # Both the Python donor stage and the Node base stage must be digest-pinned
    # (loop-harness EV-113 requires reproducible, non-floating base images).
    assert any(line.startswith("FROM python:3.12-slim-bookworm@sha256:") for line in from_lines), (
        "python donor stage must be pinned to a python:3.12-slim-bookworm digest"
    )
    assert any(line.startswith("FROM node:22.17.0-bookworm-slim@sha256:") for line in from_lines), (
        "node base stage must be pinned to a node:22.17.0-bookworm-slim digest"
    )

    # apt must not install its own python3 (bookworm ships 3.11, which cannot
    # satisfy `requires-python = ">=3.12"`); python3 must resolve to the 3.12
    # binary copied in from the donor stage instead (Issue #402).
    apt_package_lines = {
        line.strip().rstrip("\\").strip()
        for line in dockerfile.splitlines()
        if line.strip().rstrip("\\").strip() and "apt-get install" not in line
    }
    assert "python3" not in apt_package_lines
    assert "python3-pip" not in apt_package_lines

    assert "ARG CLAUDE_CODE_VERSION=2.1.207" in dockerfile
    assert "ruff==0.15.1" in dockerfile
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


def test_broker_wrapper_uses_shared_runtime_context_and_cache_namespace(
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
                "broker": {"image": "registry.example/team/broker:configured"},
                "image_cache": {},
            }
        }
    }

    docker_image.ensure_broker_image(config, tmp_path)

    recipe = captured["recipe"]
    policy = captured["policy"]
    assert recipe.family == "broker"
    assert recipe.repository == "registry.example/team/broker"
    assert recipe.context_dir == REPO_ROOT / "packages/docker-runtime/docker/broker"
    assert recipe.docker_label == "ai.orchestra.loop-harness"
    assert policy.manifest_path == tmp_path / ".claude/loop/docker-image-cache.json"
    assert captured["kwargs"]["immutable_image"] == "registry.example/team/broker:configured"


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("registry.example/team/scenario", "registry.example/team/scenario"),
        ("registry.example/team/scenario:latest", "registry.example/team/scenario"),
        (
            "registry.example/team/scenario@sha256:" + "a" * 64,
            "registry.example/team/scenario",
        ),
        (
            "registry.example/team/scenario:latest@sha256:" + "a" * 64,
            "registry.example/team/scenario",
        ),
    ],
    ids=["bare", "tag-only", "digest-only", "tag-and-digest"],
)
def test_image_repository_strips_tag_and_digest(image: str, expected: str) -> None:
    assert docker_image._image_repository(image) == expected


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


@pytest.mark.parametrize("cache_key", ["manifest_path", "lock_path"])
def test_wrapper_rejects_symlinked_cache_file(tmp_path: Path, cache_key: str) -> None:
    """A pre-existing symlink at the cache file path must be rejected, even
    if its target stays under main_root (e.g. pointing at .git/config),
    because _write_manifest would otherwise overwrite the symlink target."""
    target = tmp_path / "victim.txt"
    target.write_text("do-not-overwrite", encoding="utf-8")
    cache_relative = ".claude/loop/cache-link.json"
    cache_path = tmp_path / cache_relative
    cache_path.parent.mkdir(parents=True)
    cache_path.symlink_to(target)

    config = {
        "lp2": {
            "isolation": {
                "image_cache": {cache_key: cache_relative},
            }
        }
    }

    with pytest.raises(docker_image.DockerImageError, match="symlink"):
        docker_image.ensure_scenario_image(config, tmp_path)

    assert target.read_text(encoding="utf-8") == "do-not-overwrite"


def test_wrapper_rejects_symlinked_cache_path_ancestor(tmp_path: Path) -> None:
    """A symlinked ancestor directory (e.g. `.claude/loop` itself, not just
    the leaf cache file) must be rejected before `resolve()` ever follows
    it. Otherwise `.claude/loop/config` could resolve outside main_root
    (e.g. into `.git`) while still passing `is_relative_to(root)`, since
    `resolve()` already followed the symlink -- and later be overwritten by
    the manifest/lock writer."""
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "loop").symlink_to(victim_dir)

    config = {"lp2": {"isolation": {"image_cache": {}}}}

    with pytest.raises(docker_image.DockerImageError, match="symlink"):
        docker_image.ensure_scenario_image(config, tmp_path)

    assert list(victim_dir.iterdir()) == []


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


def test_bare_semver_image_pin_matches_full_claude_version_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare semver pin (e.g. "2.1.207") must pass verification against the
    full `claude --version` output (e.g. "2.1.207 (Claude Code)"), since only
    the bare version token is used as the CLAUDE_CODE_VERSION build arg."""

    def ensure(_recipe, _policy, **_kwargs):
        return docker_image.EnsuredImage("sha256:" + "a" * 64, "managed:tag", "b" * 64, True)

    monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
    monkeypatch.setattr(
        docker_image.runtime_cli,
        "image_claude_version",
        lambda *_args, **_kwargs: "2.1.207 (Claude Code)",
    )
    config = {"lp2": {"isolation": {"image_pin": "2.1.207"}}}

    ensured = docker_image.ensure_scenario_image(config, tmp_path)

    assert ensured.built is True


def test_full_image_pin_rejects_matching_version_with_unexpected_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full-format pin (e.g. "2.1.207 (Claude Code)") must keep the exact
    Docker capability contract: an image reporting the same bare version but
    a different wrapper/suffix must still fail closed, not pass via
    bare-token comparison."""

    def ensure(_recipe, _policy, **_kwargs):
        return docker_image.EnsuredImage("sha256:" + "a" * 64, "managed:tag", "b" * 64, True)

    monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
    monkeypatch.setattr(
        docker_image.runtime_cli,
        "image_claude_version",
        lambda *_args, **_kwargs: "2.1.207 (unexpected wrapper)",
    )
    config = {"lp2": {"isolation": {"image_pin": "2.1.207 (Claude Code)"}}}

    with pytest.raises(docker_image.DockerImageError, match="image_pin mismatch"):
        docker_image.ensure_scenario_image(config, tmp_path)


def test_bare_semver_image_pin_still_rejects_genuine_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ensure(_recipe, _policy, **_kwargs):
        return docker_image.EnsuredImage("sha256:" + "a" * 64, "managed:tag", "b" * 64, True)

    monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
    monkeypatch.setattr(
        docker_image.runtime_cli,
        "image_claude_version",
        lambda *_args, **_kwargs: "9.9.9 (Claude Code)",
    )
    config = {"lp2": {"isolation": {"image_pin": "2.1.207"}}}

    with pytest.raises(docker_image.DockerImageError, match="image_pin mismatch"):
        docker_image.ensure_scenario_image(config, tmp_path)


def test_loop_harness_manifest_depends_on_docker_runtime() -> None:
    manifest = json.loads(
        (REPO_ROOT / "packages/loop-harness/manifest.json").read_text(encoding="utf-8")
    )

    assert "docker-runtime" in manifest["depends"]
