"""meta-harness namespace adapter tests for the shared Docker image lifecycle.

Covers the persistent image-cache lifecycle EV additions (EV-94/95/96/97):
manifest-based cross-process reuse (delegated to docker-runtime, exercised via
recipe/policy shape), namespace isolation from loop-harness, immutable-digest
fail-closed under `auto_build_images: false`, and post-ensure `image_pin`
reconciliation. Mirrors `packages/loop-harness/tests/test_loop_docker_image.py`.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

docker_image = load_module(
    "meta_harness_scenario_docker_image_tests",
    "packages/meta-harness/lib/scenario_docker_image.py",
)


def _base_config() -> dict:
    return {
        "evaluate": {
            "isolation": {
                "image": "ai-orchestra/meta-harness-scenario:2.1.207",
                "image_pin": None,
                "auto_build_images": True,
                "image_cache": {},
                "broker": {"image": "ai-orchestra/meta-harness-broker:0.1.0"},
            }
        }
    }


def _fail_runner(*_args: object, **_kwargs: object) -> None:
    pytest.fail("Docker CLI must not be invoked in this hermetic test")


class TestConfigDefaults:
    """EV-95: meta-harness image cache defaults form an independent namespace."""

    def test_shipped_config_defines_meta_harness_namespace(self) -> None:
        config = yaml.safe_load(
            (REPO_ROOT / "packages/meta-harness/config/meta-harness.yaml").read_text(
                encoding="utf-8"
            )
        )
        cache = config["evaluate"]["isolation"]["image_cache"]

        assert cache == {
            "manifest_path": ".claude/meta-harness/docker-image-cache.json",
            "keep_generations": 3,
            "lock_path": ".claude/meta-harness/docker-image-build.lock",
            "builder_name": "meta-harness-builder",
            "buildkit_cache_max_age": "168h",
            "buildkit_cache_max_size": "10g",
        }

    def test_defaults_match_loop_harness_shape_but_different_values(self) -> None:
        """The meta-harness and loop-harness namespaces must never collide."""
        loop_config = yaml.safe_load(
            (REPO_ROOT / "packages/loop-harness/config/loop-harness.yaml").read_text(
                encoding="utf-8"
            )
        )
        loop_cache = loop_config["lp2"]["isolation"]["image_cache"]
        meta_config = yaml.safe_load(
            (REPO_ROOT / "packages/meta-harness/config/meta-harness.yaml").read_text(
                encoding="utf-8"
            )
        )
        meta_cache = meta_config["evaluate"]["isolation"]["image_cache"]

        assert loop_cache["manifest_path"] != meta_cache["manifest_path"]
        assert loop_cache["lock_path"] != meta_cache["lock_path"]
        assert loop_cache["builder_name"] != meta_cache["builder_name"]


class TestRecipeAndPolicyNamespace:
    """EV-95: recipe/policy handed to the shared API carry the meta-harness namespace."""

    def test_scenario_recipe_and_policy_use_meta_harness_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def ensure(recipe, policy, **kwargs):
            captured.update(recipe=recipe, policy=policy, kwargs=kwargs)
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)

        docker_image.ensure_scenario_image(_base_config(), tmp_path)

        recipe = captured["recipe"]
        policy = captured["policy"]
        assert recipe.family == "scenario"
        assert recipe.repository == "ai-orchestra/meta-harness-scenario"
        assert recipe.docker_label == docker_image.DOCKER_LABEL
        assert recipe.docker_label == "ai.orchestra.meta-harness"
        assert recipe.context_dir == REPO_ROOT / "packages/meta-harness/docker/scenario"
        assert policy.manifest_path == tmp_path / ".claude/meta-harness/docker-image-cache.json"
        assert policy.lock_path == tmp_path / ".claude/meta-harness/docker-image-build.lock"
        assert policy.builder_name == "meta-harness-builder"
        assert captured["kwargs"]["auto_build"] is True
        assert captured["kwargs"]["immutable_image"] == "ai-orchestra/meta-harness-scenario:2.1.207"

    def test_broker_recipe_and_policy_use_meta_harness_defaults_and_shared_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def ensure(recipe, policy, **kwargs):
            captured.update(recipe=recipe, policy=policy, kwargs=kwargs)
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)

        docker_image.ensure_broker_image(_base_config(), tmp_path)

        recipe = captured["recipe"]
        policy = captured["policy"]
        assert recipe.family == "broker"
        assert recipe.repository == "ai-orchestra/meta-harness-broker"
        assert recipe.docker_label == "ai.orchestra.meta-harness"
        # The broker Docker context is shared with docker-runtime/loop-harness,
        # but the label/manifest/lock/builder namespace stays meta-harness-owned.
        assert recipe.context_dir == REPO_ROOT / "packages/docker-runtime/docker/broker"
        assert policy.manifest_path == tmp_path / ".claude/meta-harness/docker-image-cache.json"
        assert policy.lock_path == tmp_path / ".claude/meta-harness/docker-image-build.lock"
        assert policy.builder_name == "meta-harness-builder"

    def test_namespace_is_independent_from_loop_harness_defaults(self, tmp_path: Path) -> None:
        loop_docker_image = load_module(
            "loop_harness_docker_image_tests_for_meta_harness_comparison",
            "packages/loop-harness/lib/loop_docker_image.py",
        )

        assert docker_image.DOCKER_LABEL != loop_docker_image.DOCKER_LABEL
        assert docker_image.DEFAULT_MANIFEST_PATH != loop_docker_image.DEFAULT_MANIFEST_PATH
        assert docker_image.DEFAULT_LOCK_PATH != loop_docker_image.DEFAULT_LOCK_PATH
        assert docker_image.DEFAULT_BUILDER_NAME != loop_docker_image.DEFAULT_BUILDER_NAME


class TestConfigOverridesAndBackwardCompatibleDefaults:
    """EV-94/95: config overrides reach the policy; unspecified keys keep defaults."""

    def test_image_cache_overrides_are_reflected_in_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def ensure(recipe, policy, **kwargs):
            captured.update(recipe=recipe, policy=policy)
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
        config = _base_config()
        config["evaluate"]["isolation"]["image_cache"] = {
            "manifest_path": ".claude/meta-harness/custom-cache.json",
            "lock_path": ".claude/meta-harness/custom-lock.lock",
            "keep_generations": 7,
            "builder_name": "custom-meta-harness-builder",
            "buildkit_cache_max_age": "24h",
            "buildkit_cache_max_size": "5g",
        }

        docker_image.ensure_scenario_image(config, tmp_path)

        policy = captured["policy"]
        assert policy.manifest_path == tmp_path / ".claude/meta-harness/custom-cache.json"
        assert policy.lock_path == tmp_path / ".claude/meta-harness/custom-lock.lock"
        assert policy.keep_generations == 7
        assert policy.builder_name == "custom-meta-harness-builder"
        assert policy.buildkit_cache_max_age == "24h"
        assert policy.buildkit_cache_max_size == "5g"

    def test_missing_image_cache_config_falls_back_to_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backward compatibility: a config predating `image_cache` (or one that omits
        it) must not crash and must resolve to the documented defaults."""
        captured: dict[str, object] = {}

        def ensure(recipe, policy, **kwargs):
            captured.update(recipe=recipe, policy=policy)
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
        # This test is about the image_cache-config fallback, not image_pin
        # verification (covered separately below); disable the pin check
        # (via an explicit `image_pin: None`, matching `_base_config()`) so
        # this hermetic test doesn't need a real `claude --version` runner.
        config = {"evaluate": {"isolation": {"image_pin": None}}}

        docker_image.ensure_scenario_image(config, tmp_path)

        policy = captured["policy"]
        assert policy.manifest_path == tmp_path / docker_image.DEFAULT_MANIFEST_PATH
        assert policy.lock_path == tmp_path / docker_image.DEFAULT_LOCK_PATH
        assert policy.keep_generations == docker_image.DEFAULT_KEEP_GENERATIONS
        assert policy.builder_name == docker_image.DEFAULT_BUILDER_NAME
        assert policy.buildkit_cache_max_age == docker_image.DEFAULT_BUILDKIT_CACHE_MAX_AGE
        assert policy.buildkit_cache_max_size == docker_image.DEFAULT_BUILDKIT_CACHE_MAX_SIZE
        recipe = captured["recipe"]
        assert recipe.repository == "ai-orchestra/meta-harness-scenario"

    def test_non_positive_keep_generations_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def ensure(recipe, policy, **kwargs):
            captured["policy"] = policy
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
        config = _base_config()
        config["evaluate"]["isolation"]["image_cache"] = {"keep_generations": 0}

        docker_image.ensure_scenario_image(config, tmp_path)

        assert captured["policy"].keep_generations == docker_image.DEFAULT_KEEP_GENERATIONS


class TestAutoBuildImagesFalseFailsClosed:
    """EV-96: auto_build_images: false keeps the immutable-digest fail-closed contract."""

    def test_scenario_rejects_tag_only_image_when_auto_build_disabled(self, tmp_path: Path) -> None:
        config = _base_config()
        config["evaluate"]["isolation"]["auto_build_images"] = False
        config["evaluate"]["isolation"]["image"] = "ai-orchestra/meta-harness-scenario:latest"

        with pytest.raises(docker_image.DockerImageError, match="immutable"):
            docker_image.ensure_scenario_image(config, tmp_path, runner=_fail_runner)

    def test_broker_rejects_tag_only_image_when_auto_build_disabled(self, tmp_path: Path) -> None:
        config = _base_config()
        config["evaluate"]["isolation"]["auto_build_images"] = False
        config["evaluate"]["isolation"]["broker"]["image"] = (
            "ai-orchestra/meta-harness-broker:latest"
        )

        with pytest.raises(docker_image.DockerImageError, match="immutable"):
            docker_image.ensure_broker_image(config, tmp_path, runner=_fail_runner)

    def test_scenario_accepts_digest_pinned_image_when_auto_build_disabled(
        self, tmp_path: Path
    ) -> None:
        config = _base_config()
        config["evaluate"]["isolation"]["auto_build_images"] = False
        config["evaluate"]["isolation"]["image"] = (
            "ai-orchestra/meta-harness-scenario@sha256:" + "1" * 64
        )
        config["evaluate"]["isolation"]["image_pin"] = None

        def runner(*_args: object, **_kwargs: object):
            import subprocess

            return subprocess.CompletedProcess([], 0, stdout="sha256:" + "2" * 64, stderr="")

        ensured = docker_image.ensure_scenario_image(config, tmp_path, runner=runner)

        assert ensured.built is False
        assert ensured.tag == "ai-orchestra/meta-harness-scenario@sha256:" + "1" * 64


class TestImagePinReconciliation:
    """EV-97: ensure-time image_pin reconciliation fails closed independently of
    whether the image was freshly built or reused from the manifest."""

    def test_scenario_image_pin_mismatch_fails_closed_after_ensure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def ensure(_recipe, _policy, **_kwargs):
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
        monkeypatch.setattr(
            docker_image.runtime_cli,
            "image_claude_version",
            lambda *_args, **_kwargs: "9.9.9 (Claude Code)",
        )
        config = _base_config()
        config["evaluate"]["isolation"]["image_pin"] = "2.1.207 (Claude Code)"

        with pytest.raises(docker_image.DockerImageError, match="image_pin mismatch"):
            docker_image.ensure_scenario_image(config, tmp_path)

    def test_scenario_image_pin_match_succeeds_after_ensure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def ensure(_recipe, _policy, **_kwargs):
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, False
            )

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
        monkeypatch.setattr(
            docker_image.runtime_cli,
            "image_claude_version",
            lambda *_args, **_kwargs: "2.1.207 (Claude Code)",
        )
        config = _base_config()
        config["evaluate"]["isolation"]["image_pin"] = "2.1.207 (Claude Code)"

        ensured = docker_image.ensure_scenario_image(config, tmp_path)

        assert ensured.built is False
        # Issue #307 review: the verified version must be surfaced on the
        # returned EnsuredImage so callers can reuse it instead of launching
        # another container to look it up again.
        assert ensured.claude_version == "2.1.207 (Claude Code)"

    def test_scenario_no_image_pin_configured_skips_reconciliation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When `image_pin` is unset, ensure must not call the version-check helper
        at all (there is nothing to reconcile against)."""

        def ensure(_recipe, _policy, **_kwargs):
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        def _fail_version_check(*_args: object, **_kwargs: object) -> str:
            pytest.fail("image_claude_version must not be called when image_pin is unset")

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
        monkeypatch.setattr(docker_image.runtime_cli, "image_claude_version", _fail_version_check)
        config = _base_config()
        config["evaluate"]["isolation"]["image_pin"] = None

        ensured = docker_image.ensure_scenario_image(config, tmp_path)

        assert ensured.built is True
        # Issue #307 review: with no pin configured, ensure never resolves a
        # version, so `claude_version` must stay unset (None) rather than a
        # stale/guessed value.
        assert ensured.claude_version is None

    def test_scenario_missing_image_pin_key_applies_default_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config whose `isolation` mapping omits `image_pin` entirely must fall
        back to `DEFAULT_CLAUDE_VERSION_PIN` (pre-Docker-lifecycle-migration
        default), unlike an explicit `image_pin: null` which skips reconciliation
        (see `test_scenario_no_image_pin_configured_skips_reconciliation`).
        Issue #250 review: `.get("image_pin")` without a default previously
        conflated "key absent" with "key explicitly null"."""
        captured: dict[str, object] = {}

        def ensure(recipe, _policy, **_kwargs):
            captured["build_args"] = dict(recipe.build_args)
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
        monkeypatch.setattr(
            docker_image.runtime_cli,
            "image_claude_version",
            lambda *_args, **_kwargs: docker_image.DEFAULT_CLAUDE_VERSION_PIN,
        )
        config = {"evaluate": {"isolation": {}}}

        ensured = docker_image.ensure_scenario_image(config, tmp_path)

        assert ensured.built is True
        assert captured["build_args"] == {"CLAUDE_CODE_VERSION": "2.1.207"}

    def test_broker_ensure_does_not_perform_image_pin_reconciliation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Claude CLI version pin is a scenario-image concept; broker ensure
        must not attempt to reconcile it even if `image_pin` is configured."""

        def ensure(_recipe, _policy, **_kwargs):
            return docker_image.runtime_image.EnsuredImage(
                "sha256:" + "a" * 64, "managed:tag", "b" * 64, True
            )

        def _fail_version_check(*_args: object, **_kwargs: object) -> str:
            pytest.fail("image_claude_version must not be called for the broker image")

        monkeypatch.setattr(docker_image.runtime_image, "ensure_recipe_image", ensure)
        monkeypatch.setattr(docker_image.runtime_cli, "image_claude_version", _fail_version_check)
        config = _base_config()
        config["evaluate"]["isolation"]["image_pin"] = "2.1.207 (Claude Code)"

        ensured = docker_image.ensure_broker_image(config, tmp_path)

        assert ensured.built is True


class TestMainRootPathEnforcement:
    """Manifest/lock paths stay confined under `main_root` (shared main-root
    enforcement, mirrors `packages/loop-harness/tests/test_loop_docker_image.py`)."""

    def test_manifest_path_escaping_main_root_is_rejected(self, tmp_path: Path) -> None:
        config = _base_config()
        config["evaluate"]["isolation"]["image_cache"] = {"manifest_path": "../outside.json"}

        with pytest.raises(docker_image.DockerImageError, match="escapes main root"):
            docker_image.ensure_scenario_image(config, tmp_path, runner=_fail_runner)

    def test_absolute_manifest_path_is_rejected(self, tmp_path: Path) -> None:
        config = _base_config()
        config["evaluate"]["isolation"]["image_cache"] = {
            "manifest_path": str(tmp_path / "absolute-cache.json")
        }

        with pytest.raises(docker_image.DockerImageError, match="relative to main root"):
            docker_image.ensure_scenario_image(config, tmp_path, runner=_fail_runner)

    @pytest.mark.parametrize("cache_key", ["manifest_path", "lock_path"])
    def test_symlinked_cache_file_is_rejected(self, tmp_path: Path, cache_key: str) -> None:
        target = tmp_path / "victim.txt"
        target.write_text("do-not-overwrite", encoding="utf-8")
        cache_relative = ".claude/meta-harness/cache-link.json"
        cache_path = tmp_path / cache_relative
        cache_path.parent.mkdir(parents=True)
        cache_path.symlink_to(target)

        config = _base_config()
        config["evaluate"]["isolation"]["image_cache"] = {cache_key: cache_relative}

        with pytest.raises(docker_image.DockerImageError, match="symlink"):
            docker_image.ensure_scenario_image(config, tmp_path, runner=_fail_runner)

        assert target.read_text(encoding="utf-8") == "do-not-overwrite"

    def test_symlinked_cache_path_ancestor_is_rejected(self, tmp_path: Path) -> None:
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "meta-harness").symlink_to(victim_dir)

        config = _base_config()
        config["evaluate"]["isolation"]["image_cache"] = {}

        with pytest.raises(docker_image.DockerImageError, match="symlink"):
            docker_image.ensure_scenario_image(config, tmp_path, runner=_fail_runner)

        assert list(victim_dir.iterdir()) == []


class TestImageRepositoryHelper:
    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            ("ai-orchestra/meta-harness-scenario", "ai-orchestra/meta-harness-scenario"),
            ("ai-orchestra/meta-harness-scenario:2.1.207", "ai-orchestra/meta-harness-scenario"),
            (
                "ai-orchestra/meta-harness-scenario@sha256:" + "a" * 64,
                "ai-orchestra/meta-harness-scenario",
            ),
            (
                "ai-orchestra/meta-harness-scenario:2.1.207@sha256:" + "a" * 64,
                "ai-orchestra/meta-harness-scenario",
            ),
        ],
        ids=["bare", "tag-only", "digest-only", "tag-and-digest"],
    )
    def test_strips_tag_and_digest(self, image: str, expected: str) -> None:
        assert docker_image._image_repository(image) == expected


def test_config_shipped_default_registry_labels_match_module_constants() -> None:
    config = copy.deepcopy(_base_config())
    assert config["evaluate"]["isolation"]["image"] == docker_image.DEFAULT_SCENARIO_IMAGE
    assert config["evaluate"]["isolation"]["broker"]["image"] == docker_image.DEFAULT_BROKER_IMAGE
