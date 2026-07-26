"""Persistent Docker image lifecycle tests (docker-runtime EV-13 through EV-18)."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.module_loader import load_module

load_module("docker_runtime_cli", "packages/docker-runtime/lib/docker_runtime_cli.py")
image = load_module(
    "docker_runtime_image_tests",
    "packages/docker-runtime/lib/docker_runtime_image.py",
)

IMAGE_ID = "sha256:" + "a" * 64
OTHER_IMAGE_ID = "sha256:" + "c" * 64
DOCKER_LABEL = "ai.orchestra.loop-harness"


def _completed(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class FakeDocker:
    def __init__(self, *, du_total: str = "1GB") -> None:
        self.commands: list[list[str]] = []
        self.images: dict[str, str] = {}
        self.builder_exists = False
        self.builder_driver = "docker-container"
        self.build_count = 0
        self.du_total = du_total
        self.lock_path: Path | None = None
        self.lock_was_held = False
        self.image_ls_output: str | None = None
        self.create_should_fail_once = False
        self.rm_should_fail: set[str] = set()

    def __call__(self, command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        self.commands.append(command)
        if command[:4] == ["docker", "image", "inspect", "--format"]:
            image_ref = command[-1]
            image_id = self.images.get(image_ref)
            return _completed(stdout=f"{image_id}\n") if image_id else _completed(1)
        if command[:3] == ["docker", "buildx", "inspect"]:
            if not self.builder_exists:
                return _completed(1)
            return _completed(
                stdout=f"Name:          loop-harness-builder\nDriver:        {self.builder_driver}\n"
            )
        if command[:3] == ["docker", "buildx", "create"]:
            if self.create_should_fail_once:
                self.create_should_fail_once = False
                # Simulate a racing process winning the create: the builder
                # now exists even though this process's create failed.
                self.builder_exists = True
                return _completed(1, stderr="buildx: instance already exists")
            self.builder_exists = True
            return _completed(stdout="loop-harness-builder\n")
        if command[:3] == ["docker", "buildx", "build"]:
            self._record_lock_state()
            tag = command[command.index("-t") + 1]
            self.images[tag] = IMAGE_ID
            self.images[IMAGE_ID] = IMAGE_ID
            self.build_count += 1
            return _completed()
        if command[:2] == ["docker", "tag"]:
            self.images[command[3]] = self.images[command[2]]
            return _completed()
        if command[:3] == ["docker", "image", "ls"]:
            return _completed(stdout=self.image_ls_output or self._image_list())
        if command[:3] == ["docker", "image", "rm"]:
            image_ref = command[-1]
            if image_ref in self.rm_should_fail:
                return _completed(
                    1, stderr=f"image is being used by a running container: {image_ref}"
                )
            self.images.pop(image_ref, None)
            return _completed()
        if command[:3] == ["docker", "buildx", "prune"]:
            return _completed()
        if command[:3] == ["docker", "buildx", "du"]:
            return _completed(stdout=f"Reclaimable: 0B\nTotal: {self.du_total}\n")
        raise AssertionError(f"unexpected Docker command: {command}")

    def _image_list(self) -> str:
        lines = []
        for reference in sorted(self.images):
            if reference.startswith("sha256:") or reference.endswith(":latest"):
                continue
            repository, tag = reference.rsplit(":", 1)
            lines.append(json.dumps({"Repository": repository, "Tag": tag, "ID": IMAGE_ID}))
        return "\n".join(lines)

    def _record_lock_state(self) -> None:
        if self.lock_path is None:
            return
        fd = os.open(self.lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_was_held = True
        finally:
            os.close(fd)


def _recipe(context: Path, **overrides) -> object:
    values = {
        "family": "scenario",
        "repository": "ai-orchestra/loop-harness-scenario",
        "context_dir": context,
        "docker_label": DOCKER_LABEL,
        "build_args": {"CLAUDE_CODE_VERSION": "2.1.207"},
        "platform": None,
        "target": None,
    }
    values.update(overrides)
    return image.ImageRecipe(**values)


def _policy(tmp_path: Path, **overrides) -> object:
    values = {
        "manifest_path": tmp_path / ".claude" / "loop" / "docker-image-cache.json",
        "lock_path": tmp_path / ".claude" / "loop" / "docker-image-build.lock",
        "keep_generations": 3,
        "builder_name": "loop-harness-builder",
        "buildkit_cache_max_age": "168h",
        "buildkit_cache_max_size": "10g",
    }
    values.update(overrides)
    return image.ImageCachePolicy(**values)


@pytest.fixture
def context(tmp_path: Path) -> Path:
    path = tmp_path / "scenario"
    path.mkdir()
    (path / "Dockerfile").write_text(
        "FROM node:test@sha256:" + "b" * 64 + "\n",
        encoding="utf-8",
    )
    return path


def test_recipe_hash_covers_normalized_args_platform_and_target(context: Path) -> None:
    """EV-13: Every build input affects the immutable recipe tag."""
    first = _recipe(context, build_args={"B": "2", "A": "1"})
    reordered = _recipe(context, build_args={"A": "1", "B": "2"})

    assert image.recipe_hash(first) == image.recipe_hash(reordered)
    assert image.recipe_hash(first) != image.recipe_hash(
        _recipe(context, build_args={"A": "changed", "B": "2"})
    )
    assert image.recipe_hash(first) != image.recipe_hash(
        _recipe(context, build_args={"A": "1", "B": "2"}, platform="linux/arm64")
    )
    assert image.recipe_hash(first) != image.recipe_hash(
        _recipe(context, build_args={"A": "1", "B": "2"}, target="runtime")
    )
    assert image.recipe_hash(first) != image.recipe_hash(
        _recipe(context, build_args={"A": "1", "B": "2"}, docker_label="other.label")
    )


def test_manifest_reuses_image_across_calls_while_build_lock_is_held(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-14/EV-17: Persistent validation skips duplicate builds under flock.

    The build itself is serialized on the per-family build lock (Issue
    #250 Fix B), not the base manifest lock, so the FakeDocker build-time
    lock probe targets the family lock path.
    """
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    fake = FakeDocker()
    fake.lock_path = image._family_lock_path(policy.lock_path, recipe.family)
    times = iter(
        [
            datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        ]
    )

    first = image.ensure_recipe_image(
        _recipe(context), policy, runner=fake, clock=lambda: next(times)
    )
    second = image.ensure_recipe_image(
        _recipe(context), policy, runner=fake, clock=lambda: next(times)
    )

    assert first.built is True
    assert second.built is False
    assert second.image_id == IMAGE_ID
    assert fake.build_count == 1
    assert fake.lock_was_held is True
    manifest = json.loads(policy.manifest_path.read_text(encoding="utf-8"))
    assert manifest[first.recipe_hash]["last_used_at"] == "2026-07-16T01:00:00+00:00"
    assert set(manifest[first.recipe_hash]) == {"image_id", "built_at", "last_used_at"}


def test_cache_hit_skips_redundant_latest_tag_when_already_current(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #307 review: a cache hit must not re-issue `docker tag` for
    `:latest` when it already resolves to the cached image (the common case
    on repeated cache hits) -- only the manifest's `last_used_at` refreshes."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    fake = FakeDocker()
    fake.lock_path = image._family_lock_path(policy.lock_path, recipe.family)

    first = image.ensure_recipe_image(_recipe(context), policy, runner=fake)
    tag_commands_after_build = [c for c in fake.commands if c[:2] == ["docker", "tag"]]
    assert len(tag_commands_after_build) == 1

    second = image.ensure_recipe_image(_recipe(context), policy, runner=fake)

    assert first.built is True
    assert second.built is False
    assert second.image_id == IMAGE_ID
    tag_commands_after_cache_hit = [c for c in fake.commands if c[:2] == ["docker", "tag"]]
    assert tag_commands_after_cache_hit == tag_commands_after_build


def test_cache_hit_retags_latest_when_it_points_elsewhere(
    tmp_path: Path,
    context: Path,
) -> None:
    """A cache hit must still retag `:latest` when it currently points at a
    different (or missing) image, preserving the pre-Issue-#307 behavior."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    fake = FakeDocker()
    fake.lock_path = image._family_lock_path(policy.lock_path, recipe.family)

    first = image.ensure_recipe_image(_recipe(context), policy, runner=fake)
    # Simulate `:latest` having drifted (e.g. another family's build clobbered
    # the shared alias) between the build and the next cache-hit call.
    fake.images[f"{recipe.repository}:latest"] = OTHER_IMAGE_ID

    second = image.ensure_recipe_image(_recipe(context), policy, runner=fake)

    assert first.built is True
    assert second.built is False
    tag_commands = [c for c in fake.commands if c[:2] == ["docker", "tag"]]
    assert len(tag_commands) == 2
    assert fake.images[f"{recipe.repository}:latest"] == IMAGE_ID


@pytest.mark.parametrize("drift", ["missing", "repointed"])
def test_manifest_docker_drift_is_rebuilt(
    tmp_path: Path,
    context: Path,
    drift: str,
) -> None:
    """EV-15: A missing or repointed image invalidates its manifest entry."""
    policy = _policy(tmp_path)
    fake = FakeDocker()
    first = image.ensure_recipe_image(_recipe(context), policy, runner=fake)
    if drift == "missing":
        fake.images.clear()
    else:
        fake.images[first.tag] = OTHER_IMAGE_ID
        fake.images[OTHER_IMAGE_ID] = OTHER_IMAGE_ID

    rebuilt = image.ensure_recipe_image(_recipe(context), policy, runner=fake)

    assert rebuilt.built is True
    assert fake.build_count == 2


def test_manifest_partial_corruption_skips_invalid_entry_and_reuses_valid_entry(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-19: One invalid record does not invalidate a reusable manifest entry."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    digest = image.recipe_hash(recipe)
    broken_digest = "f" * 64
    policy.manifest_path.parent.mkdir(parents=True)
    policy.manifest_path.write_text(
        json.dumps(
            {
                digest: {
                    "image_id": IMAGE_ID,
                    "built_at": "2026-07-16T00:00:00+00:00",
                    "last_used_at": "2026-07-16T00:00:00+00:00",
                },
                broken_digest: {"image_id": IMAGE_ID},
            }
        ),
        encoding="utf-8",
    )
    fake = FakeDocker()
    fake.images[IMAGE_ID] = IMAGE_ID
    fake.images[image.recipe_tag(recipe, digest)] = IMAGE_ID

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is False
    assert ensured.recipe_hash == digest
    assert fake.build_count == 0
    manifest = json.loads(policy.manifest_path.read_text(encoding="utf-8"))
    assert digest in manifest
    assert broken_digest not in manifest


@pytest.mark.parametrize(
    "malformed_last_used_at",
    ["zzzz", "2026-07-16", "2026-07-16T00:00:00"],
    ids=["non-timestamp", "date-only", "timezone-naive"],
)
def test_manifest_malformed_timestamp_skips_invalid_entry_and_reuses_valid_entry(
    tmp_path: Path,
    context: Path,
    malformed_last_used_at: str,
) -> None:
    """EV-19: A malformed or timezone-naive last_used_at is dropped as invalid.

    Pruning sorts last_used_at as text, so an unparsable value like "zzzz"
    could otherwise outrank valid entries and cause a fresh image to be
    pruned. The valid entry must remain a cache hit.
    """
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    digest = image.recipe_hash(recipe)
    broken_digest = "f" * 64
    policy.manifest_path.parent.mkdir(parents=True)
    policy.manifest_path.write_text(
        json.dumps(
            {
                digest: {
                    "image_id": IMAGE_ID,
                    "built_at": "2026-07-16T00:00:00+00:00",
                    "last_used_at": "2026-07-16T00:00:00+00:00",
                },
                broken_digest: {
                    "image_id": IMAGE_ID,
                    "built_at": "2026-07-16T00:00:00+00:00",
                    "last_used_at": malformed_last_used_at,
                },
            }
        ),
        encoding="utf-8",
    )
    fake = FakeDocker()
    fake.images[IMAGE_ID] = IMAGE_ID
    fake.images[image.recipe_tag(recipe, digest)] = IMAGE_ID

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is False
    assert ensured.recipe_hash == digest
    assert fake.build_count == 0
    manifest = json.loads(policy.manifest_path.read_text(encoding="utf-8"))
    assert digest in manifest
    assert broken_digest not in manifest


def test_exclusive_file_lock_preserves_critical_section_oserror(tmp_path: Path) -> None:
    """EV-20: Caller OSError values are not mislabeled as lock failures."""
    lock_path = tmp_path / "docker-image-build.lock"

    with pytest.raises(OSError, match="boom - not a lock failure") as exc_info:
        with image.exclusive_file_lock(lock_path):
            raise OSError("boom - not a lock failure")

    assert type(exc_info.value) is OSError


def test_exclusive_file_lock_wraps_acquisition_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EV-20: Actual lock acquisition failures retain the lock error contract."""
    lock_path = tmp_path / "docker-image-build.lock"

    def fail_open(*_args, **_kwargs):
        raise OSError("acquisition failed")

    monkeypatch.setattr(image.os, "open", fail_open)

    with pytest.raises(image.DockerImageError, match="could not lock Docker image build"):
        with image.exclusive_file_lock(lock_path):
            pass


def test_prune_keeps_recent_family_generations_and_label_scope(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-16: Only hash tags recorded in this manifest and beyond the
    retained generation count are removed. A tag matching the label/
    repository but absent from this manifest (e.g. another project's build)
    is preserved even though it looks "unused"."""
    digests = [str(index) * 64 for index in range(1, 4)]
    manifest = {
        digest: image.ManifestEntry(IMAGE_ID, f"2026-07-1{index}T00:00:00+00:00", used)
        for index, (digest, used) in enumerate(
            zip(
                digests,
                [
                    "2026-07-13T00:00:00+00:00",
                    "2026-07-14T00:00:00+00:00",
                    "2026-07-15T00:00:00+00:00",
                ],
                strict=True,
            ),
            start=1,
        )
    }
    repository = "ai-orchestra/loop-harness-scenario"
    fake = FakeDocker()
    fake.image_ls_output = "\n".join(
        [json.dumps({"Repository": repository, "Tag": f"sha-{digest[:12]}"}) for digest in digests]
        + [
            json.dumps({"Repository": repository, "Tag": "sha-" + "f" * 12}),
            json.dumps({"Repository": repository, "Tag": "latest"}),
            json.dumps(
                {
                    "Repository": "ai-orchestra/loop-harness-broker",
                    "Tag": f"sha-{digests[0][:12]}",
                }
            ),
        ]
    )

    updated = image._prune_image_family(
        _recipe(context),
        _policy(tmp_path, keep_generations=2),
        manifest,
        runner=fake,
    )

    removed = [command for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert removed == [
        ["docker", "image", "rm", f"{repository}:sha-{digests[0][:12]}"],
    ]
    assert set(updated) == set(digests[1:])
    image_ls = next(
        command for command in fake.commands if command[:3] == ["docker", "image", "ls"]
    )
    assert f"label={DOCKER_LABEL}=image" in image_ls


def test_prune_conflict_is_best_effort_and_does_not_abort_ensure_recipe_image(
    tmp_path: Path,
    context: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `docker image rm` conflict (e.g. an old generation still in use by a
    running container) must not fail a build that already succeeded and was
    recorded in the manifest; it is downgraded to a best-effort warning."""
    policy = _policy(tmp_path, keep_generations=1)
    fake = FakeDocker()
    times = iter(
        [
            datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        ]
    )
    stale = image.ensure_recipe_image(
        _recipe(context, target="stale"), policy, runner=fake, clock=lambda: next(times)
    )
    fresh_recipe = _recipe(context, target="fresh")
    fake.rm_should_fail.add(stale.tag)

    with caplog.at_level("WARNING"):
        ensured = image.ensure_recipe_image(
            fresh_recipe, policy, runner=fake, clock=lambda: next(times)
        )

    assert ensured.built is True
    assert fake.build_count == 2
    assert any(
        "could not prune managed Docker image" in record.message for record in caplog.records
    )
    manifest = json.loads(policy.manifest_path.read_text(encoding="utf-8"))
    assert stale.recipe_hash in manifest
    assert ensured.recipe_hash in manifest


def test_build_uses_dedicated_builder_and_scoped_cache_gc(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-18: BuildKit GC never targets the developer's default builder."""
    fake = FakeDocker(du_total="11GB")
    image.ensure_recipe_image(_recipe(context), _policy(tmp_path), runner=fake)

    build = next(
        command for command in fake.commands if command[:3] == ["docker", "buildx", "build"]
    )
    assert build[build.index("--builder") + 1] == "loop-harness-builder"
    assert "--load" in build
    prunes = [command for command in fake.commands if command[:3] == ["docker", "buildx", "prune"]]
    assert [command[command.index("--filter") + 1] for command in prunes] == [
        "until=168h",
        "until=0",
    ]
    assert all(
        command[command.index("--builder") + 1] == "loop-harness-builder" for command in prunes
    )


def test_build_rejects_existing_builder_with_incompatible_driver(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-18: A pre-existing builder/context sharing the name but not using
    the docker-container driver is rejected rather than silently reused."""
    fake = FakeDocker()
    fake.builder_exists = True
    fake.builder_driver = "docker"

    with pytest.raises(image.DockerImageError, match="docker-container"):
        image.ensure_recipe_image(_recipe(context), _policy(tmp_path), runner=fake)

    assert fake.build_count == 0


def test_ensure_builder_retries_inspect_after_raced_create_failure(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-18: When two projects (different lock files) race to bootstrap the
    same global builder name, a `buildx create` conflict must not abort the
    loser if the builder the winner created passes the driver check."""
    fake = FakeDocker()
    fake.create_should_fail_once = True

    ensured = image.ensure_recipe_image(_recipe(context), _policy(tmp_path), runner=fake)

    assert ensured.built is True
    assert fake.build_count == 1
    creates = [
        command for command in fake.commands if command[:3] == ["docker", "buildx", "create"]
    ]
    assert len(creates) == 1
    inspects = [
        command for command in fake.commands if command[:3] == ["docker", "buildx", "inspect"]
    ]
    assert len(inspects) == 2


def test_ensure_builder_raises_when_raced_builder_has_incompatible_driver(
    tmp_path: Path,
    context: Path,
) -> None:
    """A raced create failure only rescues the caller when the builder that
    now exists actually satisfies the docker-container driver contract."""
    fake = FakeDocker()
    fake.create_should_fail_once = True
    fake.builder_driver = "docker"

    with pytest.raises(image.DockerImageError, match="docker-container"):
        image.ensure_recipe_image(_recipe(context), _policy(tmp_path), runner=fake)

    assert fake.build_count == 0


def test_auto_build_disabled_requires_immutable_digest(tmp_path: Path, context: Path) -> None:
    with pytest.raises(image.DockerImageError, match="immutable"):
        image.ensure_recipe_image(
            _recipe(context),
            _policy(tmp_path),
            auto_build=False,
            immutable_image="ai-orchestra/loop-harness-scenario:latest",
            runner=FakeDocker(),
        )


def test_auto_build_disabled_rejects_digest_with_trailing_newline(
    tmp_path: Path, context: Path
) -> None:
    """`$` matches just before a trailing newline, so a naive regex would
    accept `...@sha256:<64hex>\\n` as a valid immutable digest. Anchoring to
    `\\Z` closes that gap (Issue #307)."""
    digest = "ai-orchestra/loop-harness-scenario@sha256:" + "a" * 64
    with pytest.raises(image.DockerImageError, match="immutable"):
        image.ensure_recipe_image(
            _recipe(context),
            _policy(tmp_path),
            auto_build=False,
            immutable_image=digest + "\n",
            runner=FakeDocker(),
        )


def test_family_name_must_be_lock_file_safe(tmp_path: Path, context: Path) -> None:
    """EV-28: `family` is interpolated into the per-family lock filename, so
    it must be validated before use."""
    with pytest.raises(image.DockerImageError, match="family"):
        image.ensure_recipe_image(
            _recipe(context, family="../../etc"),
            _policy(tmp_path),
            runner=FakeDocker(),
        )


def test_family_build_lock_does_not_block_a_different_familys_build(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-28: An in-flight build lock for one family must not serialize a
    concurrent ensure for a different family sharing the same policy
    (Issue #250 Fix B)."""
    policy = _policy(tmp_path)
    scenario_lock = image._family_lock_path(policy.lock_path, "scenario")
    scenario_lock.parent.mkdir(parents=True, exist_ok=True)
    held_fd = os.open(scenario_lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX)
    try:
        fake = FakeDocker()
        ensured = image.ensure_recipe_image(
            _recipe(context, family="broker", repository="ai-orchestra/loop-harness-broker"),
            policy,
            runner=fake,
        )
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)

    assert ensured.built is True
    assert fake.build_count == 1


def test_concurrent_manifest_writes_across_families_do_not_lose_entries(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-29: A manifest write for one family must not clobber an entry
    written by a concurrent process for a different family (Issue #250 Fix
    B: the post-build manifest write re-reads from disk instead of reusing
    an in-memory snapshot taken before the build started)."""
    policy = _policy(tmp_path)
    other_digest = "9" * 64
    other_entry = {
        "image_id": OTHER_IMAGE_ID,
        "built_at": "2026-07-16T00:00:00+00:00",
        "last_used_at": "2026-07-16T00:00:00+00:00",
    }

    class RacingDocker(FakeDocker):
        def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
            if command[:3] == ["docker", "buildx", "build"]:
                # Simulate a different family's process finishing its own
                # build and writing its manifest entry while this family's
                # build is still in flight.
                policy.manifest_path.parent.mkdir(parents=True, exist_ok=True)
                policy.manifest_path.write_text(
                    json.dumps({other_digest: other_entry}), encoding="utf-8"
                )
            return super().__call__(command, **kwargs)

    recipe = _recipe(context)
    fake = RacingDocker()

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    manifest = json.loads(policy.manifest_path.read_text(encoding="utf-8"))
    assert other_digest in manifest
    assert ensured.recipe_hash in manifest


def test_cache_hit_does_not_inspect_unrelated_manifest_entries(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-30: Only the requested recipe's digest triggers `docker image
    inspect`; unrelated (but schema-valid) manifest entries are trusted
    as-is and are not dropped even if their image no longer exists (Issue
    #250 Fix C)."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    digest = image.recipe_hash(recipe)
    stale_digest = "7" * 64
    stale_entry = {
        "image_id": OTHER_IMAGE_ID,  # never present in fake.images
        "built_at": "2026-07-16T00:00:00+00:00",
        "last_used_at": "2026-07-16T00:00:00+00:00",
    }
    policy.manifest_path.parent.mkdir(parents=True)
    policy.manifest_path.write_text(
        json.dumps(
            {
                digest: {
                    "image_id": IMAGE_ID,
                    "built_at": "2026-07-16T00:00:00+00:00",
                    "last_used_at": "2026-07-16T00:00:00+00:00",
                },
                stale_digest: stale_entry,
            }
        ),
        encoding="utf-8",
    )
    fake = FakeDocker()
    fake.images[IMAGE_ID] = IMAGE_ID
    fake.images[image.recipe_tag(recipe, digest)] = IMAGE_ID

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is False
    inspected_refs = [
        command[-1]
        for command in fake.commands
        if command[:4] == ["docker", "image", "inspect", "--format"]
    ]
    assert OTHER_IMAGE_ID not in inspected_refs
    manifest = json.loads(policy.manifest_path.read_text(encoding="utf-8"))
    assert stale_digest in manifest


def test_exclusive_file_lock_rejects_symlinked_lock_path(tmp_path: Path) -> None:
    """EV-31: A symlinked lock path must fail closed (O_NOFOLLOW) instead of
    following the symlink into locking/chmod-ing an attacker-controlled
    target (Issue #250 Fix D, TOCTOU)."""
    target = tmp_path / "victim.txt"
    target.write_text("do-not-touch", encoding="utf-8")
    lock_path = tmp_path / "docker-image-build.lock"
    lock_path.symlink_to(target)

    with pytest.raises(image.DockerImageError, match="could not lock Docker image build"):
        with image.exclusive_file_lock(lock_path):
            pass

    assert target.read_text(encoding="utf-8") == "do-not-touch"
