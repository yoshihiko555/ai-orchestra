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
        self.build_count = 0
        self.du_total = du_total
        self.lock_path: Path | None = None
        self.lock_was_held = False
        self.image_ls_output: str | None = None

    def __call__(self, command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        self.commands.append(command)
        if command[:4] == ["docker", "image", "inspect", "--format"]:
            image_ref = command[-1]
            image_id = self.images.get(image_ref)
            return _completed(stdout=f"{image_id}\n") if image_id else _completed(1)
        if command[:3] == ["docker", "buildx", "inspect"]:
            return _completed() if self.builder_exists else _completed(1)
        if command[:3] == ["docker", "buildx", "create"]:
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
            self.images.pop(command[-1], None)
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


def test_manifest_reuses_image_across_calls_while_build_lock_is_held(
    tmp_path: Path,
    context: Path,
) -> None:
    """EV-14/EV-17: Persistent validation skips duplicate builds under flock."""
    policy = _policy(tmp_path)
    fake = FakeDocker()
    fake.lock_path = policy.lock_path
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
    """EV-16: Only old hash tags from the selected labeled family are removed."""
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
        ["docker", "image", "rm", f"{repository}:sha-" + "f" * 12],
    ]
    assert set(updated) == set(digests[1:])
    image_ls = next(
        command for command in fake.commands if command[:3] == ["docker", "image", "ls"]
    )
    assert f"label={DOCKER_LABEL}=image" in image_ls


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


def test_auto_build_disabled_requires_immutable_digest(tmp_path: Path, context: Path) -> None:
    with pytest.raises(image.DockerImageError, match="immutable"):
        image.ensure_recipe_image(
            _recipe(context),
            _policy(tmp_path),
            auto_build=False,
            immutable_image="ai-orchestra/loop-harness-scenario:latest",
            runner=FakeDocker(),
        )
