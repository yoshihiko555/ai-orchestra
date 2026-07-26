"""Persistent Docker image lifecycle tests (docker-runtime EV-13 through EV-18)."""

from __future__ import annotations

import fcntl
import itertools
import json
import os
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
# Default `docker image inspect --format {{.Created}}` response for any
# tracked image that a test hasn't given an explicit `image_created_at`
# entry -- far enough in the past that the cross-checkout liveness guard
# (`_image_recently_created`) never blocks removal unless a test opts in.
DEFAULT_CREATED_AT = "2000-01-01T00:00:00.000000000Z"


def _completed(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _clock(*moments: datetime) -> Callable[[], datetime]:
    """A deterministic test clock that yields each of `moments` in order,
    then keeps yielding the last one forever instead of raising
    `StopIteration`.

    Since PR #320 review, `ensure_recipe_image()` may read the clock more
    than once per call (once up front, once more immediately before a
    return, so a pin lease's expiry is computed from a fresh timestamp
    rather than one captured before a potentially long family-lock wait or
    build). Tests that care about "call N used moment N" no longer have to
    hand-count exactly how many reads happen inside a single call -- the
    trailing repeat means any extra reads within (or past) the last
    intended moment simply keep returning it.
    """
    values = itertools.chain(moments, itertools.repeat(moments[-1]))
    return lambda: next(values)


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
        self.rm_missing: set[str] = set()
        self.image_created_at: dict[str, str] = {}

    def __call__(self, command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        self.commands.append(command)
        if command[:4] == ["docker", "image", "inspect", "--format"]:
            image_ref = command[-1]
            format_arg = command[4] if len(command) > 4 else ""
            if format_arg == "{{.Created}}":
                return self._inspect_created_at(image_ref)
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
            output = self.image_ls_output or self._image_list()
            if "--all" not in command:
                # Real `docker image ls` excludes dangling (`<none>:<none>`)
                # images unless `--all`/`-a` is passed. Modeling that here
                # turns "someone dropped `--all` from the real command" into
                # a failing assertion instead of a silent no-op (Issue #231
                # E2E finding).
                output = self._strip_dangling(output)
            if "--no-trunc" not in command:
                # Real `docker image ls` truncates the `ID` field to 12 hex
                # chars unless `--no-trunc` is passed. Modeling that here
                # turns "someone dropped `--no-trunc` from the real command"
                # into a failing assertion instead of a silent pin-lease
                # bypass (PR #320 review: the pin ledger stores full
                # `sha256:<64 hex>` IDs, which a truncated `ID` would never
                # match).
                output = self._truncate_ids(output)
            return _completed(stdout=output)
        if command[:3] == ["docker", "image", "rm"]:
            image_ref = command[-1]
            if image_ref in self.rm_missing:
                return _completed(
                    1, stderr=f"Error response from daemon: No such image: {image_ref}"
                )
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

    def _inspect_created_at(self, image_ref: str) -> subprocess.CompletedProcess:
        if image_ref in self.image_created_at:
            return _completed(stdout=f"{self.image_created_at[image_ref]}\n")
        if image_ref in self.images:
            return _completed(stdout=f"{DEFAULT_CREATED_AT}\n")
        return _completed(1, stderr=f"Error response from daemon: No such image: {image_ref}")

    def _strip_dangling(self, output: str) -> str:
        lines = []
        for line in output.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if value.get("Repository") == "<none>" and value.get("Tag") == "<none>":
                continue
            lines.append(line)
        return "\n".join(lines)

    def _truncate_ids(self, output: str) -> str:
        lines = []
        for line in output.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            image_id = value.get("ID")
            if isinstance(image_id, str):
                value["ID"] = image_id.removeprefix("sha256:")[:12]
            lines.append(json.dumps(value))
        return "\n".join(lines)

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
    clock = _clock(
        datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
    )

    first = image.ensure_recipe_image(_recipe(context), policy, runner=fake, clock=clock)
    second = image.ensure_recipe_image(_recipe(context), policy, runner=fake, clock=clock)

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
    clock = _clock(
        datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
    )
    stale = image.ensure_recipe_image(
        _recipe(context, target="stale"), policy, runner=fake, clock=clock
    )
    fresh_recipe = _recipe(context, target="fresh")
    fake.rm_should_fail.add(stale.tag)

    with caplog.at_level("WARNING"):
        ensured = image.ensure_recipe_image(fresh_recipe, policy, runner=fake, clock=clock)

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


def _journal_path(policy: object) -> Path:
    return image._pending_journal_path(policy.manifest_path)


def _seed_pending_entry(
    policy: object,
    *,
    tag: str,
    digest: str,
    recipe: object,
    last_cleanup_at: str | None = None,
    started_at: str = "2026-07-16T00:00:00+00:00",
    family: str | None = None,
) -> None:
    journal = {
        "entries": {
            tag: {
                "digest": digest,
                "family": family if family is not None else recipe.family,
                "repository": recipe.repository,
                "docker_label": recipe.docker_label,
                "started_at": started_at,
            }
        },
        "last_cleanup_at": last_cleanup_at,
    }
    image._write_pending_journal(_journal_path(policy), journal)


def test_cleanup_reclaims_orphaned_tag_left_by_a_pending_build(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #231 scenario 2: a `--load` that succeeded but whose manifest
    write never happened (e.g. the process died in between) leaves a pending
    journal record. The next `ensure_recipe_image` call for the same
    docker_label must reclaim the orphaned tag even though it is absent from
    the manifest."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    _seed_pending_entry(policy, tag=orphan_tag, digest=orphan_digest, recipe=recipe)

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is True
    removed = [command for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert [orphan_tag] == [command[-1] for command in removed if command[-1] == orphan_tag]
    assert orphan_tag not in fake.images
    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag not in journal["entries"]


def test_cleanup_removes_dangling_owner_labelled_image(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #231 scenario 3: a same-tag rebuild makes the previous image
    dangling (`Repository`/`Tag` become `<none>`), which `_family_candidates`
    never revisits. Opportunistic cleanup must remove it unconditionally
    since a dangling, label-owned image cannot be referenced by any tag."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    dangling_id = "sha256:" + "d" * 64
    fake = FakeDocker()
    fake.image_ls_output = json.dumps({"Repository": "<none>", "Tag": "<none>", "ID": dangling_id})

    image.ensure_recipe_image(recipe, policy, runner=fake)

    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert dangling_id in removed
    # Real `docker image ls` hides dangling images unless `--all`/`-a` is
    # passed; FakeDocker mirrors that (see `_strip_dangling`), so this
    # assertion fails if `--all` is ever dropped from the real command
    # (Issue #231 E2E finding: without it, dangling recovery is a silent
    # no-op against a real daemon).
    image_ls = next(
        command for command in fake.commands if command[:3] == ["docker", "image", "ls"]
    )
    assert "--all" in image_ls
    # `docker image ls` truncates `ID` to 12 hex chars unless `--no-trunc`
    # is passed, but the pin ledger stores full `sha256:<64 hex>` IDs (from
    # `docker image inspect`). Without `--no-trunc`, a leased dangling
    # image's ID would never match its lease entry, silently defeating the
    # protection tested by `test_cleanup_protects_dangling_image_with_active_pin_lease`
    # (PR #320 review).
    assert "--no-trunc" in image_ls


def test_fake_docker_truncates_image_ids_without_no_trunc_flag() -> None:
    """Fidelity check for `FakeDocker` itself (PR #320 review): confirms it
    actually mirrors `docker image ls`'s real truncation behavior, so
    dropping `--no-trunc` from the production command would make
    `test_cleanup_removes_dangling_owner_labelled_image`'s `pinned_id in
    removed`-style assertions fail instead of silently passing against IDs
    that could never occur against a real daemon."""
    fake = FakeDocker()
    fake.image_ls_output = json.dumps({"Repository": "<none>", "Tag": "<none>", "ID": IMAGE_ID})

    truncated = fake(["docker", "image", "ls", "--all", "--format", "{{json .}}"])
    full = fake(["docker", "image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"])

    assert json.loads(truncated.stdout)["ID"] == IMAGE_ID.removeprefix("sha256:")[:12]
    assert json.loads(full.stdout)["ID"] == IMAGE_ID


def test_cleanup_leaves_other_projects_tagged_image_alone(
    tmp_path: Path,
    context: Path,
) -> None:
    """A tagged image sharing the owner label but unknown to both the
    pending journal and the manifest (e.g. another project's build sharing
    the same label) must never be removed by opportunistic cleanup."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    other_reference = "someone-else/other-repo:sha-000000000000"
    fake = FakeDocker()
    fake.image_ls_output = json.dumps(
        {
            "Repository": "someone-else/other-repo",
            "Tag": "sha-000000000000",
            "ID": "sha256:" + "9" * 64,
        }
    )

    image.ensure_recipe_image(recipe, policy, runner=fake)

    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert other_reference not in removed
    assert ("sha256:" + "9" * 64) not in removed


def test_build_failure_triggers_best_effort_pending_tag_cleanup(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #231: if `_build_image` itself fails, the pending record it just
    registered must be reclaimed best-effort, and the original
    `DockerImageError` must propagate unchanged (not replaced by a cleanup
    error). PR #320 review (second round): since the tag never existed
    before or after this failed attempt, there is nothing to protect *or*
    to remove -- `docker image rm` is never even called (only the pending
    journal entry, this process's own bookkeeping, is dropped)."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    digest = image.recipe_hash(recipe)
    expected_tag = image.recipe_tag(recipe, digest)

    class FailingBuildDocker(FakeDocker):
        def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
            if command[:3] == ["docker", "buildx", "build"]:
                self.commands.append(command)
                self.build_count += 1
                return _completed(1, stderr="buildx: build failed")
            return super().__call__(command, **kwargs)

    fake = FailingBuildDocker()

    with pytest.raises(image.DockerImageError, match="could not build required Docker image"):
        image.ensure_recipe_image(recipe, policy, runner=fake)

    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert expected_tag not in removed
    journal = image._load_pending_journal(_journal_path(policy))
    assert expected_tag not in journal["entries"]


def test_build_failure_never_deletes_a_tag_owned_by_another_checkout(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (second round, P1): the same content-addressed tag can
    already exist in the shared Docker daemon before this build attempt even
    starts (e.g. built by a different checkout sharing this repository),
    invisible to this checkout's local manifest. If this build fails without
    ever touching that pre-existing tag, the failure handler must not delete
    it -- only a tag this attempt itself created or replaced is fair game."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    digest = image.recipe_hash(recipe)
    expected_tag = image.recipe_tag(recipe, digest)

    class FailingBuildDocker(FakeDocker):
        def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
            if command[:3] == ["docker", "buildx", "build"]:
                self.commands.append(command)
                self.build_count += 1
                return _completed(1, stderr="buildx: build failed")
            return super().__call__(command, **kwargs)

    fake = FailingBuildDocker()
    fake.images[expected_tag] = OTHER_IMAGE_ID  # pre-existing, owned by "another checkout"

    with pytest.raises(image.DockerImageError, match="could not build required Docker image"):
        image.ensure_recipe_image(recipe, policy, runner=fake)

    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert expected_tag not in removed
    assert fake.images[expected_tag] == OTHER_IMAGE_ID
    journal = image._load_pending_journal(_journal_path(policy))
    assert expected_tag not in journal["entries"]


def test_cleanup_is_suppressed_within_ttl_when_nothing_pending(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #231: with no pending records and a recent `last_cleanup_at`,
    the reuse-path fast check must not pay for a `docker image ls`/`docker
    image rm` cleanup sweep on every call."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    fake = FakeDocker()
    first_time = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    second_time = datetime(2026, 7, 16, 0, 30, tzinfo=UTC)
    image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: first_time)
    image._write_pending_journal(
        _journal_path(policy),
        {"entries": {}, "last_cleanup_at": first_time.isoformat()},
    )
    fake.commands = []

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: second_time)

    assert ensured.built is False
    ls_or_rm = [
        command
        for command in fake.commands
        if command[:3] in (["docker", "image", "ls"], ["docker", "image", "rm"])
    ]
    assert ls_or_rm == []


def test_cleanup_skips_pending_entry_while_family_build_lock_is_held(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #231 review (Critical): a pending record whose `family` build
    lock is currently held by another process must never be treated as
    stale, even though it is old enough (`started_at`) to otherwise pass the
    grace-period check -- the family lock is the authoritative liveness
    signal since `ensure_recipe_image` holds it for the entire build."""
    policy = _policy(tmp_path)
    scenario_recipe = _recipe(context)
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(scenario_recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    _seed_pending_entry(
        policy,
        tag=orphan_tag,
        digest=orphan_digest,
        recipe=scenario_recipe,
        family="scenario",
    )
    scenario_lock = image._family_lock_path(policy.lock_path, "scenario")
    scenario_lock.parent.mkdir(parents=True, exist_ok=True)
    held_fd = os.open(scenario_lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX)
    try:
        # A different family (broker) sharing the same docker_label/manifest
        # so the cleanup its `ensure_recipe_image` call triggers considers
        # the held-open scenario entry, without this call itself ever
        # needing the (already externally held) scenario family lock.
        broker_recipe = _recipe(
            context, family="broker", repository="ai-orchestra/loop-harness-broker"
        )
        ensured = image.ensure_recipe_image(broker_recipe, policy, runner=fake)
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)

    assert ensured.built is True
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert orphan_tag not in removed
    assert orphan_tag in fake.images
    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag in journal["entries"]


def test_cleanup_skips_recently_started_pending_entry_even_without_a_lock_holder(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #231 review (Critical, defense in depth): a pending record
    whose `started_at` is still within `BUILD_TIMEOUT_SECONDS +
    PENDING_LIVENESS_GRACE_SECONDS` must be left alone even when its
    family's build lock happens to be free (e.g. the probe raced a build
    that had not yet acquired the lock, or clock skew)."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    now = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    _seed_pending_entry(
        policy,
        tag=orphan_tag,
        digest=orphan_digest,
        recipe=recipe,
        started_at=now.isoformat(),
    )

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: now)

    assert ensured.built is True
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert orphan_tag not in removed
    assert orphan_tag in fake.images
    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag in journal["entries"]


def test_cleanup_issues_no_docker_commands_when_pending_entry_is_live_within_ttl(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #231 review (High): a pending entry alone must not force a full
    `docker image ls`/`buildx du` sweep. Once it is classified as `live`
    (recent `started_at`), the presence of *some* pending record must not
    bypass the TTL the way it used to -- only a genuine stale candidate, or
    the TTL itself elapsing, may trigger one."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    live_digest = "e" * 64
    live_tag = image.recipe_tag(recipe, live_digest)
    first_time = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    second_time = datetime(2026, 7, 16, 0, 5, tzinfo=UTC)
    fake = FakeDocker()
    fake.images[live_tag] = IMAGE_ID
    # Warm the cache first (also records `last_cleanup_at`), so the second
    # call below is a reuse-path hit and never touches `_prune_image_family`
    # (whose own `docker image ls` would otherwise contaminate the
    # assertion below).
    image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: first_time)
    _seed_pending_entry(
        policy,
        tag=live_tag,
        digest=live_digest,
        recipe=recipe,
        started_at=second_time.isoformat(),
        last_cleanup_at=first_time.isoformat(),
    )
    fake.commands = []

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: second_time)

    assert ensured.built is False
    ls_or_rm = [
        command
        for command in fake.commands
        if command[:3] in (["docker", "image", "ls"], ["docker", "image", "rm"])
    ]
    assert ls_or_rm == []
    journal = image._load_pending_journal(_journal_path(policy))
    assert live_tag in journal["entries"]


def test_cleanup_drops_malformed_pending_tag_without_touching_docker(
    tmp_path: Path,
    context: Path,
) -> None:
    """A pending-journal tag that does not match `recipe_tag()`'s exact
    shape (e.g. a corrupted or adversarial entry) must never reach `docker
    image rm`; it is dropped from the journal instead."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    malformed_tag = "not a valid reference; rm -rf /"
    fake = FakeDocker()
    _seed_pending_entry(policy, tag=malformed_tag, digest="e" * 64, recipe=recipe)

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is True
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert malformed_tag not in removed
    journal = image._load_pending_journal(_journal_path(policy))
    assert malformed_tag not in journal["entries"]


def test_cleanup_consumes_pending_entry_when_image_was_never_created(
    tmp_path: Path,
    context: Path,
) -> None:
    """Issue #231 E2E finding: if the build process died before `--load`
    ever ran, the pending tag was never actually created in the daemon.
    `docker image rm` then fails with "No such image", which must be
    treated the same as a successful removal -- otherwise the journal entry
    (and its accompanying warning) would persist forever."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    missing_digest = "e" * 64
    missing_tag = image.recipe_tag(recipe, missing_digest)
    fake = FakeDocker()
    fake.rm_missing.add(missing_tag)
    _seed_pending_entry(policy, tag=missing_tag, digest=missing_digest, recipe=recipe)

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is True
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert missing_tag in removed
    journal = image._load_pending_journal(_journal_path(policy))
    assert missing_tag not in journal["entries"]


def test_cleanup_rm_failure_does_not_fail_ensure_recipe_image(
    tmp_path: Path,
    context: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `docker image rm` failure during opportunistic cleanup is
    best-effort: it must be logged and left for a later attempt, never fail
    the overall `ensure_recipe_image` call."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    orphan_digest = "b" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    fake.rm_should_fail.add(orphan_tag)
    _seed_pending_entry(policy, tag=orphan_tag, digest=orphan_digest, recipe=recipe)

    with caplog.at_level("WARNING"):
        ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is True
    assert orphan_tag in fake.images
    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag in journal["entries"]
    assert any("could not prune stale Docker image" in record.message for record in caplog.records)


def test_cleanup_skips_stale_tag_when_family_lock_is_taken_after_the_judgment(
    tmp_path: Path,
    context: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #320 review (CodeRabbit): the liveness judgment runs under the
    manifest lock, but `docker image rm` itself runs after releasing it,
    leaving a TOCTOU window in which a build for the same family could
    start. The removal loop must re-acquire the family lock immediately
    before removing (see
    `test_cleanup_holds_family_lock_through_ownership_check_and_removal` for
    the "holds it throughout" half of this fix) and skip if it cannot."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    now = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    _seed_pending_entry(
        policy,
        tag=orphan_tag,
        digest=orphan_digest,
        recipe=recipe,
        started_at=(now - timedelta(hours=2)).isoformat(),
    )
    # `_partition_pending`'s own probe (under the manifest lock) still needs
    # to see the family lock as free, so the entry becomes a stale
    # candidate; only the *second* acquisition attempt, in the removal loop
    # itself, should find it taken (simulating a new build racing in during
    # the TOCTOU window between the two).
    real_probe = image._family_build_in_progress
    probe_calls: list[str] = []

    def racing_probe(policy_arg: object, family: str) -> bool:
        probe_calls.append(family)
        return real_probe(policy_arg, family)

    monkeypatch.setattr(image, "_family_build_in_progress", racing_probe)

    @contextmanager
    def racing_hold(policy_arg: object, family: str):
        # A new build "raced in" and took the family lock in between the
        # partition-phase probe and the removal loop's own acquisition
        # attempt -- so this attempt cannot acquire it either.
        yield False

    monkeypatch.setattr(image, "_hold_family_lock_if_free", racing_hold)

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: now)

    assert ensured.built is True
    assert len(probe_calls) == 1
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert orphan_tag not in removed
    assert orphan_tag in fake.images
    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag in journal["entries"]


def test_cleanup_holds_family_lock_through_ownership_check_and_removal(
    tmp_path: Path,
    context: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #320 review (second round): re-acquiring the family lock right
    before removal is not enough on its own -- it must stay *held* through
    the CreatedAt ownership check and the removal itself, or a new build for
    the same family could still start, acquire the lock, and `--load` the
    very tag about to be removed in the gap between the check and the `rm`.
    """
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    now = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    _seed_pending_entry(
        policy,
        tag=orphan_tag,
        digest=orphan_digest,
        recipe=recipe,
        started_at=(now - timedelta(hours=2)).isoformat(),
    )
    family_lock_path = image._family_lock_path(policy.lock_path, recipe.family)
    lock_was_held_during_check: list[bool] = []
    real_check = image._image_recently_created

    def probing_check(reference: str, now_arg: datetime, *, runner) -> bool:
        # From a *separate* file descriptor, verify the family lock is
        # currently held (by `_hold_family_lock_if_free`, still in its
        # `with` block around this very call) -- proving the lock is not
        # released between the liveness judgment and the removal.
        family_lock_path.parent.mkdir(parents=True, exist_ok=True)
        probe_fd = os.open(family_lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_was_held_during_check.append(False)  # acquired -- NOT held, bad
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
        except BlockingIOError:
            lock_was_held_during_check.append(True)  # still held -- good
        finally:
            os.close(probe_fd)
        return real_check(reference, now_arg, runner=runner)

    monkeypatch.setattr(image, "_image_recently_created", probing_check)

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: now)

    assert ensured.built is True
    assert lock_was_held_during_check == [True]
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert orphan_tag in removed


def test_cleanup_skips_stale_tag_created_within_liveness_window(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (P1, chatgpt-codex-connector, partial mitigation): a
    stale-looking pending entry's tag might actually be a *different*
    checkout's very recent rebuild of the same shared repository/tag --
    that checkout's family lock lives under its own `lock_path` and is
    invisible here. Cross-checking the tag's `CreatedAt` against the same
    liveness window used for pending records protects that case."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    now = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    fake.image_created_at[orphan_tag] = (now - timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z"
    )
    _seed_pending_entry(
        policy,
        tag=orphan_tag,
        digest=orphan_digest,
        recipe=recipe,
        started_at=(now - timedelta(hours=2)).isoformat(),
    )

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: now)

    assert ensured.built is True
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert orphan_tag not in removed
    assert orphan_tag in fake.images


def test_repository_validation_accepts_registry_host_with_port() -> None:
    """PR #320 review (P2, chatgpt-codex-connector): a repository with a
    registry host and port (`registry.example:5000/team/scenario`, already
    supported by loop-harness `image` configs -- see
    packages/loop-harness/tests/test_loop_docker_image.py) must not be
    rejected as malformed, or a crash-orphaned pending entry for it becomes
    permanently unrecoverable."""
    tag = "registry.example:5000/team/scenario:sha-" + "a" * 12
    assert image._is_removable_tag_reference(tag) is True
    assert image._is_removable_tag_reference("localhost:5000/foo:sha-" + "a" * 12) is True
    # Still rejects an actually malformed/adversarial reference.
    assert image._is_removable_tag_reference("not a valid reference; rm -rf /") is False
    assert image._is_removable_tag_reference("repo:with:extra:colons:sha-" + "a" * 12) is False


def test_cleanup_reclaims_orphaned_tag_with_registry_port_repository(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (P2): a registry-with-port repository's pending entry
    must be treated as a normal stale candidate, not dropped as malformed
    (which would make the corresponding tag permanently unrecoverable)."""
    policy = _policy(tmp_path)
    recipe = _recipe(context, repository="registry.example:5000/team/scenario")
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    _seed_pending_entry(policy, tag=orphan_tag, digest=orphan_digest, recipe=recipe)

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is True
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert orphan_tag in removed
    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag not in journal["entries"]


def test_build_failure_keeps_pending_entry_when_removal_also_fails(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (P2, chatgpt-codex-connector): if the build fails and
    the best-effort cleanup's `docker image rm` also fails for a reason
    other than "already gone" (e.g. a transient daemon error or in-use
    conflict), the pending journal entry must be kept for a later retry.
    Clearing it unconditionally would permanently orphan a tagged,
    manifest-unregistered image with no other reclaim path (dangling
    cleanup only ever looks at *untagged* images, and generation pruning
    only looks at manifest-registered ones)."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    digest = image.recipe_hash(recipe)
    expected_tag = image.recipe_tag(recipe, digest)

    class FailingTagLatestDocker(FakeDocker):
        """`buildx build` succeeds normally (so `expected_tag` really is
        created, i.e. this attempt genuinely mutates it and removal is
        warranted), but the post-build `docker tag ... :latest` step fails,
        triggering the `except` cleanup path."""

        def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
            if command[:2] == ["docker", "tag"]:
                self.commands.append(command)
                return _completed(1, stderr="tag failed")
            return super().__call__(command, **kwargs)

    fake = FailingTagLatestDocker()
    fake.rm_should_fail.add(expected_tag)

    with pytest.raises(image.DockerImageError, match="could not update Docker image alias"):
        image.ensure_recipe_image(recipe, policy, runner=fake)

    journal = image._load_pending_journal(_journal_path(policy))
    assert expected_tag in journal["entries"]


def test_cleanup_does_not_advance_ttl_when_image_scan_fails(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (P2, chatgpt-codex-connector): if `docker image ls`
    fails (daemon hiccup), `last_cleanup_at` must not advance -- otherwise
    the next dangling-image sweep stays suppressed for up to
    CLEANUP_TTL_SECONDS even after the daemon has recovered, even though
    cached-image inspects (and thus normal `ensure_recipe_image` calls)
    keep succeeding throughout."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    now = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)

    class FailOnceLsDocker(FakeDocker):
        def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
            if command[:3] == ["docker", "image", "ls"] and self.ls_fail_once:
                self.ls_fail_once = False
                self.commands.append(command)
                return _completed(1, stderr="Cannot connect to the Docker daemon")
            return super().__call__(command, **kwargs)

    fake = FailOnceLsDocker()
    fake.ls_fail_once = True

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: now)

    assert ensured.built is True
    journal = image._load_pending_journal(_journal_path(policy))
    assert journal["last_cleanup_at"] is None


def test_cleanup_protects_dangling_image_with_active_pin_lease(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (P1, chatgpt-codex-connector): a same-tag rebuild can
    make an `ensure_recipe_image()`-returned image_id dangling before the
    session that already resolved it gets around to starting a container
    from it. While that image_id's pin lease is unexpired, opportunistic
    dangling cleanup must never remove it."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    now = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    pinned_id = "sha256:" + "f" * 64
    fake = FakeDocker()
    fake.image_ls_output = json.dumps({"Repository": "<none>", "Tag": "<none>", "ID": pinned_id})
    image._lease_image_id(policy, pinned_id, now)

    image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: now)

    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert pinned_id not in removed
    ledger = image._load_pin_ledger(image._pin_ledger_path(policy.manifest_path))
    assert pinned_id in ledger["leases"]


def test_cleanup_removes_dangling_image_after_pin_lease_expires(
    tmp_path: Path,
    context: Path,
) -> None:
    """The pin-lease protection in `test_cleanup_protects_dangling_image_with_active_pin_lease`
    is time-bounded: once `IMAGE_ID_LEASE_TTL_SECONDS` has elapsed, the
    lease is purged and the dangling image becomes a normal cleanup
    candidate again."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    leased_at = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    later = leased_at + timedelta(seconds=image.IMAGE_ID_LEASE_TTL_SECONDS + 1)
    pinned_id = "sha256:" + "f" * 64
    fake = FakeDocker()
    fake.image_ls_output = json.dumps({"Repository": "<none>", "Tag": "<none>", "ID": pinned_id})
    image._lease_image_id(policy, pinned_id, leased_at)

    image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: later)

    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert pinned_id in removed
    ledger = image._load_pin_ledger(image._pin_ledger_path(policy.manifest_path))
    assert pinned_id not in ledger["leases"]


def test_ensure_recipe_image_leases_the_returned_image_id(
    tmp_path: Path,
    context: Path,
) -> None:
    """`ensure_recipe_image()` must record a pin lease for the `image_id` it
    returns (PR #320 review) so a same-tag rebuild's opportunistic dangling
    cleanup never removes an image a caller is actively holding onto."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    now = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    fake = FakeDocker()

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=lambda: now)

    ledger = image._load_pin_ledger(image._pin_ledger_path(policy.manifest_path))
    assert ensured.image_id in ledger["leases"]


def test_lease_expiry_uses_a_fresh_clock_read_not_the_call_start_time(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (P2, second round): `now_dt` is captured once at the
    very top of `ensure_recipe_image`, before cleanup, an unbounded
    family-lock wait, and up to `BUILD_TIMEOUT_SECONDS` of build time. If
    the pin lease were computed from that stale timestamp, a long-running
    call could already have an expired lease by the time it's written. The
    expiry must instead come from a fresh clock read taken immediately
    before the return."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    call_start = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    just_before_return = call_start + timedelta(hours=1)
    clock = _clock(call_start, just_before_return)
    fake = FakeDocker()

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake, clock=clock)

    ledger = image._load_pin_ledger(image._pin_ledger_path(policy.manifest_path))
    expected_expiry = (
        just_before_return + timedelta(seconds=image.IMAGE_ID_LEASE_TTL_SECONDS)
    ).isoformat()
    assert ledger["leases"][ensured.image_id] == expected_expiry


def test_immutable_mode_still_reclaims_stale_auto_build_residue(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (P2): `auto_build=False` (`auto_build_images: false`)
    used to return before ever reaching the opportunistic cleanup call, so
    pending-tag residue left behind by an *earlier* auto-build configuration
    would never be reclaimed once a project switched to pinned immutable
    digests. Cleanup must run at the very top of `ensure_recipe_image`,
    before the auto_build branch, matching the ADR's "cleanup always runs"
    invariant."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    immutable_digest_image = "ai-orchestra/loop-harness-scenario@sha256:" + "9" * 64
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    fake.images[immutable_digest_image] = OTHER_IMAGE_ID
    _seed_pending_entry(policy, tag=orphan_tag, digest=orphan_digest, recipe=recipe)

    ensured = image.ensure_recipe_image(
        recipe,
        policy,
        auto_build=False,
        immutable_image=immutable_digest_image,
        runner=fake,
    )

    assert ensured.built is False
    assert ensured.image_id == OTHER_IMAGE_ID
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert orphan_tag in removed
    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag not in journal["entries"]


def test_repository_validation_accepts_bracketed_ipv6_registry_host() -> None:
    """PR #320 review (P2): Docker's own reference grammar accepts a
    bracketed IPv6 registry host (`distribution/reference`'s domain
    grammar), and `image_repository()`/the build path already accept such
    configs -- only the deletion-time validator used to reject them,
    permanently orphaning any crash-residue pending entry for such a
    repository."""
    tag = "[2001:db8::1]:5000/team/scenario:sha-" + "a" * 12
    assert image._is_removable_tag_reference(tag) is True
    assert image._is_removable_tag_reference("[::1]/team/scenario:sha-" + "a" * 12) is True


def test_cleanup_reclaims_orphaned_tag_with_ipv6_registry_repository(
    tmp_path: Path,
    context: Path,
) -> None:
    """Integration-level companion to
    `test_repository_validation_accepts_bracketed_ipv6_registry_host`: a
    pending entry for an IPv6-registry repository must be treated as a
    normal stale candidate, not dropped as malformed."""
    policy = _policy(tmp_path)
    recipe = _recipe(context, repository="[2001:db8::1]:5000/team/scenario")
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    _seed_pending_entry(policy, tag=orphan_tag, digest=orphan_digest, recipe=recipe)

    ensured = image.ensure_recipe_image(recipe, policy, runner=fake)

    assert ensured.built is True
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert orphan_tag in removed
    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag not in journal["entries"]


def test_cleanup_does_not_resolve_pending_entry_from_stale_manifest_digest(
    tmp_path: Path,
    context: Path,
) -> None:
    """PR #320 review (P2, second round): a manifest entry sharing the
    pending record's digest key is not proof that the pending build is
    "resolved" -- the manifest could still describe an *older* image A
    while a *newer* build of the identical recipe produced image B,
    `--load`ed it (replacing A's tag), and then crashed before the manifest
    write. `resolved` must require the manifest's recorded `image_id` to
    match what the tag *currently* resolves to; otherwise the pending
    record for B is dropped as "resolved" without B ever being reclaimed."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    digest = "e" * 64
    tag = image.recipe_tag(recipe, digest)
    old_image_id = "sha256:" + "1" * 64
    new_image_id = "sha256:" + "2" * 64
    policy.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    policy.manifest_path.write_text(
        json.dumps(
            {
                digest: {
                    "image_id": old_image_id,
                    "built_at": "2026-07-16T00:00:00+00:00",
                    "last_used_at": "2026-07-16T00:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeDocker()
    # The tag was already replaced by a newer, manifest-unregistered build.
    fake.images[tag] = new_image_id
    _seed_pending_entry(policy, tag=tag, digest=digest, recipe=recipe)

    image.ensure_recipe_image(recipe, policy, runner=fake)

    # If the buggy "digest in manifest" check had fired, the entry would
    # have been dropped as resolved without ever calling `docker image rm`.
    removed = [command[-1] for command in fake.commands if command[:3] == ["docker", "image", "rm"]]
    assert tag in removed
    journal = image._load_pending_journal(_journal_path(policy))
    assert tag not in journal["entries"]


def test_validate_policy_rejects_lock_path_colliding_with_pending_journal(
    tmp_path: Path,
) -> None:
    """PR #320 review (P2): if `lock_path` happens to equal the sidecar
    pending-journal path `_pending_journal_path()` derives from
    `manifest_path`'s stem, a journal write's atomic replace would silently
    swap out the very inode `exclusive_file_lock` is holding a lock on,
    breaking mutual exclusion for any concurrent process. Reject this at
    validation time instead."""
    policy = image.ImageCachePolicy(
        manifest_path=tmp_path / "docker-image-cache.json",
        lock_path=tmp_path / "docker-image-cache.pending.json",  # collides
        keep_generations=3,
        builder_name="loop-harness-builder",
        buildkit_cache_max_age="168h",
        buildkit_cache_max_size="10g",
    )

    with pytest.raises(image.DockerImageError, match="path conflict"):
        image._validate_policy(policy)


def test_validate_policy_rejects_lock_path_colliding_with_pin_ledger(
    tmp_path: Path,
) -> None:
    """Same hazard as `test_validate_policy_rejects_lock_path_colliding_with_pending_journal`,
    but for the pin-lease ledger sidecar instead of the pending journal."""
    policy = image.ImageCachePolicy(
        manifest_path=tmp_path / "docker-image-cache.json",
        lock_path=tmp_path / "docker-image-cache.pins.json",  # collides
        keep_generations=3,
        builder_name="loop-harness-builder",
        buildkit_cache_max_age="168h",
        buildkit_cache_max_size="10g",
    )

    with pytest.raises(image.DockerImageError, match="path conflict"):
        image._validate_policy(policy)


def test_validate_policy_accepts_distinct_cache_paths(tmp_path: Path) -> None:
    """Sanity check: the ordinary, non-colliding configuration used by every
    other test in this file must not be rejected."""
    image._validate_policy(_policy(tmp_path))


def test_atomic_write_json_normalizes_mkstemp_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #320 review (P2): a raw `OSError` from `tempfile.mkstemp()` (e.g.
    `ENOSPC`) must be normalized to `DockerImageError`, so it is caught by
    every caller's `except DockerImageError` (best-effort cleanup after a
    build failure, the pin-lease writer, ...) instead of replacing an
    in-flight exception, or failing an otherwise-successful
    `ensure_recipe_image` call outright."""

    def failing_mkstemp(*_args, **_kwargs):
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr(image.tempfile, "mkstemp", failing_mkstemp)

    with pytest.raises(image.DockerImageError, match="boom"):
        image._atomic_write_json(tmp_path / "x.json", {"a": 1}, error_message="boom")


def test_cleanup_does_not_consume_a_new_pending_record_written_during_removal(
    tmp_path: Path,
    context: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #320 review (P2, second round): the manifest lock is released for
    the entire stale-tag removal. If a new build for the same tag
    re-recorded a fresh pending entry (`_record_pending_build`) while that
    removal was in flight, the post-removal journal consumption must not
    destroy that new entry -- only the exact stale snapshot the removal
    decision was based on may be popped."""
    policy = _policy(tmp_path)
    recipe = _recipe(context)
    orphan_digest = "e" * 64
    orphan_tag = image.recipe_tag(recipe, orphan_digest)
    fake = FakeDocker()
    fake.images[orphan_tag] = IMAGE_ID
    _seed_pending_entry(policy, tag=orphan_tag, digest=orphan_digest, recipe=recipe)

    real_remove = image._remove_image_best_effort
    new_pending_digest = "f" * 64
    new_started_at = "2026-07-20T00:00:00+00:00"

    def racing_remove(reference: str, *, runner) -> bool:
        result = real_remove(reference, runner=runner)
        if reference == orphan_tag:
            # Simulate a new build re-recording this exact tag while the
            # manifest lock was released for this removal.
            _seed_pending_entry(
                policy,
                tag=orphan_tag,
                digest=new_pending_digest,
                recipe=recipe,
                started_at=new_started_at,
            )
        return result

    monkeypatch.setattr(image, "_remove_image_best_effort", racing_remove)

    image.ensure_recipe_image(recipe, policy, runner=fake)

    journal = image._load_pending_journal(_journal_path(policy))
    assert orphan_tag in journal["entries"]
    assert journal["entries"][orphan_tag]["digest"] == new_pending_digest
    assert journal["entries"][orphan_tag]["started_at"] == new_started_at
