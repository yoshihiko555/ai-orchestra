"""Shared Docker runtime behavior tests (docker-runtime EV-01 through EV-08)."""

from __future__ import annotations

import os
import subprocess
import time
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


def test_prebuilt_image_rejects_digest_with_trailing_newline(tmp_path: Path) -> None:
    """`$` matches just before a trailing newline, so a naive regex would
    accept `...@sha256:<64hex>\\n` as a valid immutable digest. Anchoring to
    `\\Z` closes that gap (Issue #307)."""
    digest = "runtime@sha256:" + "a" * 64
    with pytest.raises(cli.DockerCliError, match="immutable"):
        cli.ensure_image(
            digest + "\n",
            tmp_path,
            context_hash_label="ai.orchestra.test.context-sha256",
            auto_build=False,
            build_args=[],
            runner=lambda *_args, **_kwargs: _completed(),
            cache=cli.ImageCache(),
        )


def test_context_hash_detects_content_changes_within_same_process(tmp_path: Path) -> None:
    """The memoized `context_hash` (Issue #250) must not return a stale
    digest once a context file's content changes mid-process, even without an
    explicit `clear_context_hash_cache()` call (Issue #307 review). The
    content change here also changes file size, so the cache's stat
    signature invalidates deterministically regardless of filesystem mtime
    resolution."""
    context = tmp_path / "scenario"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text("FROM example@sha256:" + "b" * 64, encoding="utf-8")

    first = cli.context_hash(context)
    dockerfile.write_text(
        "FROM example@sha256:" + "b" * 64 + "\nRUN echo changed", encoding="utf-8"
    )
    second = cli.context_hash(context)

    assert first != second


def test_context_hash_detects_added_and_removed_files_within_same_process(
    tmp_path: Path,
) -> None:
    """Adding or removing a file under `context_dir` changes the number of
    hashed files, so the cache's stat signature must invalidate even when
    every pre-existing file is byte-for-byte unchanged (Issue #307
    review)."""
    context = tmp_path / "scenario"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM example@sha256:" + "b" * 64, encoding="utf-8")

    baseline = cli.context_hash(context)
    added_file = context / "extra.txt"
    added_file.write_text("extra", encoding="utf-8")
    after_add = cli.context_hash(context)
    added_file.unlink()
    after_remove = cli.context_hash(context)

    assert baseline != after_add
    assert after_remove == baseline


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


def test_align_mount_ownership_is_noop_for_non_root_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile.os, "getuid", lambda: 1000)
    calls: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(
        profile.os,
        "chown",
        lambda path, uid, gid, **_kwargs: calls.append((Path(path), uid, gid)),
    )
    target = tmp_path / "worktree"
    target.mkdir()
    # Explicit mode: scaffolding dir/file, not owner-only secrets (Issue #301).
    target.chmod(0o755)
    (target / "file.txt").write_text("content", encoding="utf-8")
    (target / "file.txt").chmod(0o644)

    profile.align_mount_ownership(target)

    assert calls == []


def test_align_mount_ownership_reowns_tree_to_forced_non_root_identity_when_host_is_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile.os, "getuid", lambda: 0)
    monkeypatch.setattr(profile.os, "getgid", lambda: 0)
    calls: list[tuple[Path, int, int, bool]] = []
    monkeypatch.setattr(
        profile.os,
        "chown",
        lambda path, uid, gid, follow_symlinks=True: calls.append(
            (Path(path), uid, gid, follow_symlinks)
        ),
    )
    target = tmp_path / "worktree"
    nested = target / "nested"
    nested.mkdir(parents=True)
    # Explicit mode: scaffolding dirs/files, not owner-only secrets (Issue #301).
    target.chmod(0o755)
    nested.chmod(0o755)
    (nested / "file.txt").write_text("content", encoding="utf-8")
    (nested / "file.txt").chmod(0o644)

    profile.align_mount_ownership(target)

    reowned = {(path, uid, gid) for path, uid, gid, _follow in calls}
    assert reowned == {
        (target, 65532, 65532),
        (nested, 65532, 65532),
        (nested / "file.txt", 65532, 65532),
    }
    # The top-level path uses follow_symlinks=True (the default `os.chown` signature); every
    # descendant is chowned with follow_symlinks=False so a malicious symlink planted inside the
    # tree cannot redirect ownership changes to an arbitrary host path outside it.
    descendant_calls = [call for call in calls if call[0] != target]
    assert all(call[3] is False for call in descendant_calls)


def test_align_mount_ownership_skips_excluded_leaf_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, PR #262, High (round 4): don't re-own excluded leaf entries.

    A root-run driver must not use this re-own to newly grant the fixed non-root container
    identity access to a caller-excluded file (e.g. a project-local override the caller wants to
    keep at its original, possibly stricter, owner). Ancestor directories stay re-owned so the
    container can still traverse them.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 0)
    monkeypatch.setattr(profile.os, "getgid", lambda: 0)
    calls: list[Path] = []
    monkeypatch.setattr(
        profile.os,
        "chown",
        lambda path, uid, gid, follow_symlinks=True: calls.append(Path(path)),
    )
    target = tmp_path / "worktree"
    nested = target / "nested"
    nested.mkdir(parents=True)
    # Explicit mode: scaffolding dirs/files, not owner-only secrets (Issue #301).
    target.chmod(0o755)
    nested.chmod(0o755)
    excluded_file = nested / "secret.local.yaml"
    excluded_file.write_text("content", encoding="utf-8")
    kept_file = nested / "file.txt"
    kept_file.write_text("content", encoding="utf-8")
    kept_file.chmod(0o644)

    profile.align_mount_ownership(target, exclude=frozenset({excluded_file}))

    assert excluded_file not in calls
    assert kept_file in calls
    assert nested in calls
    assert target in calls


def test_align_mount_ownership_skips_hardlink_alias_of_an_excluded_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, PR #262, P2 (round 8): an `exclude` entry must also cover a hardlink alias
    of that same file, not just its own path.

    `exclude` previously only matched by `Path` equality. A hardlink to the same excluded
    `.local.yaml` inode planted at a different path inside the same worktree is a distinct
    directory entry `rglob()` walks into with its own path but the *same* underlying inode --
    `child in excluded` alone misses it, re-owning the excluded file's inode (through the alias
    path) to the non-root container identity and exposing its contents via that alias.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 0)
    monkeypatch.setattr(profile.os, "getgid", lambda: 0)
    calls: list[Path] = []
    monkeypatch.setattr(
        profile.os,
        "chown",
        lambda path, uid, gid, follow_symlinks=True: calls.append(Path(path)),
    )
    target = tmp_path / "worktree"
    nested = target / "nested"
    nested.mkdir(parents=True)
    # Explicit mode: scaffolding dirs, not owner-only secrets (Issue #301).
    target.chmod(0o755)
    nested.chmod(0o755)
    excluded_file = nested / "secret.local.yaml"
    excluded_file.write_text("content", encoding="utf-8")
    hardlink_alias = nested / "alias-of-secret"
    os.link(excluded_file, hardlink_alias)

    profile.align_mount_ownership(target, exclude=frozenset({excluded_file}))

    assert excluded_file not in calls
    assert hardlink_alias not in calls
    assert nested in calls
    assert target in calls


def test_align_mount_ownership_skips_owner_only_permission_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, PR #262, P1 (round 10): don't re-own root-owned `0600` secrets.

    A root-run driver must not use this re-own to newly grant the fixed non-root container
    identity access to a secret file (e.g. `.env`, `.netrc`) that was deliberately left at a
    restrictive mode with no group/other permission bits at all -- even when the caller never
    enumerated it in `exclude`. Ordinary worktree files at the usual mode stay re-owned so Maker
    can still write them.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 0)
    monkeypatch.setattr(profile.os, "getgid", lambda: 0)
    calls: list[Path] = []
    monkeypatch.setattr(
        profile.os,
        "chown",
        lambda path, uid, gid, follow_symlinks=True: calls.append(Path(path)),
    )
    target = tmp_path / "worktree"
    nested = target / "nested"
    nested.mkdir(parents=True)
    # Explicit mode: scaffolding dirs, not owner-only secrets (Issue #301).
    target.chmod(0o755)
    nested.chmod(0o755)
    secret_file = nested / ".env"
    secret_file.write_text("SECRET=1", encoding="utf-8")
    secret_file.chmod(0o600)
    ordinary_file = nested / "file.txt"
    ordinary_file.write_text("content", encoding="utf-8")
    ordinary_file.chmod(0o644)

    profile.align_mount_ownership(target)

    assert secret_file not in calls
    assert ordinary_file in calls
    assert nested in calls
    assert target in calls


def test_align_mount_ownership_rejects_owner_only_secret_for_non_root_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, PR #262, P1 (round 11): the round-10 owner-only skip only protects a secret
    from *this* re-own -- it does nothing when the host process is already non-root, because
    `non_root_identity()` then maps the scenario container to that same host uid/gid, so the
    secret is readable by the untrusted Maker/Checker regardless of chown. There is no ownership
    change that can fix this; the only fail-closed option is to refuse to start.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 1000)
    calls: list[Path] = []
    monkeypatch.setattr(
        profile.os,
        "chown",
        lambda path, uid, gid, **_kwargs: calls.append(Path(path)),
    )
    target = tmp_path / "worktree"
    nested = target / "nested"
    nested.mkdir(parents=True)
    secret_file = nested / ".env"
    secret_file.write_text("SECRET=1", encoding="utf-8")
    secret_file.chmod(0o600)

    with pytest.raises(profile.DockerProfileError, match="non-root container"):
        profile.align_mount_ownership(target)

    assert calls == []


def test_align_mount_ownership_reject_honors_exclude_for_non_root_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, PR #262, P2 (round 12): the round-11 non-root reject check ignored the
    caller's `exclude` set entirely, an asymmetry with the root branch's chown-skip `exclude`
    handling. A `.local.*` override deliberately left at a restrictive mode (the same file the
    root path's `exclude` already tolerates) must not refuse to start a non-root driver either.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 1000)
    target = tmp_path / "worktree"
    nested = target / "nested"
    nested.mkdir(parents=True)
    # Explicit mode: scaffolding dirs, not owner-only secrets (Issue #301).
    target.chmod(0o755)
    nested.chmod(0o755)
    excluded_file = nested / "secret.local.yaml"
    excluded_file.write_text("content", encoding="utf-8")
    excluded_file.chmod(0o600)

    profile.align_mount_ownership(target, exclude=frozenset({excluded_file}))


def test_align_mount_ownership_reject_still_rejects_non_excluded_secret_for_non_root_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the round-12 exclude fix above: excluding one file must not
    accidentally suppress the reject check for every other owner-only-permission entry.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 1000)
    target = tmp_path / "worktree"
    nested = target / "nested"
    nested.mkdir(parents=True)
    excluded_file = nested / "secret.local.yaml"
    excluded_file.write_text("content", encoding="utf-8")
    secret_file = nested / ".env"
    secret_file.write_text("SECRET=1", encoding="utf-8")
    secret_file.chmod(0o600)

    with pytest.raises(profile.DockerProfileError, match="non-root container"):
        profile.align_mount_ownership(target, exclude=frozenset({excluded_file}))


def test_reject_owner_only_secrets_rejects_for_non_root_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, PR #262, P1 (round 12): `reject_owner_only_secrets()` is the standalone
    entry point a caller uses to run only the round-11 reject check without also chowning (e.g.
    a read-only Checker worktree mount, which never needs re-owning). It must reject the same
    owner-only secret `align_mount_ownership()` would.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 1000)
    target = tmp_path / "worktree"
    target.mkdir()
    secret_file = target / ".env"
    secret_file.write_text("SECRET=1", encoding="utf-8")
    secret_file.chmod(0o600)

    with pytest.raises(profile.DockerProfileError, match="non-root container"):
        profile.reject_owner_only_secrets(target)


def test_reject_owner_only_secrets_honors_exclude_for_non_root_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same round-12 exclude semantics as `align_mount_ownership()`'s reject branch."""
    monkeypatch.setattr(profile.os, "getuid", lambda: 1000)
    target = tmp_path / "worktree"
    target.mkdir()
    target.chmod(0o755)  # Explicit mode: scaffolding dir, not an owner-only secret (Issue #301).
    excluded_file = target / "secret.local.yaml"
    excluded_file.write_text("content", encoding="utf-8")
    excluded_file.chmod(0o600)

    profile.reject_owner_only_secrets(target, exclude=frozenset({excluded_file}))


def test_reject_owner_only_secrets_is_noop_for_root_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, PR #262, P1 (round 12): on a root-run driver, `reject_owner_only_secrets()`
    is intentionally a no-op -- `docs/design/loop-harness-isolation.md` section 9.2 already
    documents a root-run driver's Checker worktree as an accepted availability trade-off, not
    something this reject check also needs to enforce.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 0)
    target = tmp_path / "worktree"
    target.mkdir()
    secret_file = target / ".env"
    secret_file.write_text("SECRET=1", encoding="utf-8")
    secret_file.chmod(0o600)

    profile.reject_owner_only_secrets(target)


def test_align_mount_ownership_protect_owner_only_false_bypasses_reject_for_non_root_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``protect_owner_only=False`` opts a driver-generated path (e.g. the ephemeral Git runtime
    directory) out of the round-11 reject above -- nothing there is a human-placed secret, so a
    restrictive mode from the process umask must not block starting a non-root container.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 1000)
    target = tmp_path / "runtime"
    nested = target / "nested"
    nested.mkdir(parents=True)
    restrictive_file = nested / "config"
    restrictive_file.write_text("data", encoding="utf-8")
    restrictive_file.chmod(0o600)

    profile.align_mount_ownership(target, protect_owner_only=False)


def test_align_mount_ownership_reowns_owner_only_permission_directory_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restrictive-mode *directory* is itself skipped, but `rglob()` still walks into it so
    any child that later becomes reachable (e.g. after a permission change) is evaluated too --
    the directory skip alone already keeps the container from traversing in via its own mode.
    """
    monkeypatch.setattr(profile.os, "getuid", lambda: 0)
    monkeypatch.setattr(profile.os, "getgid", lambda: 0)
    calls: list[Path] = []
    monkeypatch.setattr(
        profile.os,
        "chown",
        lambda path, uid, gid, follow_symlinks=True: calls.append(Path(path)),
    )
    target = tmp_path / "worktree"
    restricted_dir = target / ".ssh"
    restricted_dir.mkdir(parents=True)
    target.chmod(0o755)  # Explicit mode: scaffolding dir, not an owner-only secret (Issue #301).
    restricted_dir.chmod(0o700)
    key_file = restricted_dir / "id_rsa"
    key_file.write_text("private-key", encoding="utf-8")
    key_file.chmod(0o600)

    profile.align_mount_ownership(target)

    assert restricted_dir not in calls
    assert key_file not in calls
    assert target in calls


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


def test_network_is_stale_returns_false_for_a_live_startup_network() -> None:
    """Codex review, PR #262, High (round 6): don't sweep a concurrent worker's live network.

    Concurrent same-project workers share one owner id, so a network another worker just
    created -- but has not yet attached a broker/scenario container to -- looks identical to a
    truly orphaned network: same owner label, no containers. As long as its creating process
    (`parent_pid_label`) is still alive, it must not be reclaimed out from under that worker.
    """
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    inspected = {"Labels": {labels.owner_label: "owner-test", labels.parent_pid_label: "4242"}}

    is_stale = lifecycle.network_is_stale(
        inspected, "owner-test", labels=labels, pid_checker=lambda _pid: True
    )

    assert is_stale is False


def test_network_is_stale_returns_true_when_creating_process_is_dead() -> None:
    """A same-owner, container-less network whose creating process died is a genuine leak."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    inspected = {"Labels": {labels.owner_label: "owner-test", labels.parent_pid_label: "4242"}}

    is_stale = lifecycle.network_is_stale(
        inspected, "owner-test", labels=labels, pid_checker=lambda _pid: False
    )

    assert is_stale is True


def test_network_is_stale_returns_true_for_missing_parent_pid_label() -> None:
    """A network predating the parent-pid label (or with a corrupt one) still reaps immediately,
    matching `container_is_stale()`'s own fallback for the same case."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    inspected = {"Labels": {labels.owner_label: "owner-test"}}

    assert lifecycle.network_is_stale(inspected, "owner-test", labels=labels) is True


def test_network_is_stale_returns_true_past_age_cap_despite_pid_reuse() -> None:
    """Codex review, PR #262, High (round 7): PID reuse must not leak networks forever.

    A same-owner, container-less network whose `created_at_label` is already past
    `stale_max_age_seconds` is reclaimed even when `pid_checker` reports the (possibly reused)
    `parent_pid_label` as alive -- mirroring `container_is_stale()`'s own absolute age cap so a
    driver crash followed by OS PID reuse cannot suspend reclamation indefinitely.
    """
    labels = lifecycle.RuntimeLabels("ai.orchestra.test", stale_max_age_seconds=60)
    inspected = {
        "Labels": {
            labels.owner_label: "owner-test",
            labels.parent_pid_label: "4242",
            labels.created_at_label: str(int(time.time()) - 120),
        }
    }

    is_stale = lifecycle.network_is_stale(
        inspected, "owner-test", labels=labels, pid_checker=lambda _pid: True
    )

    assert is_stale is True


def test_network_is_stale_returns_false_within_age_cap_and_live_pid() -> None:
    """A network within its age cap and whose creating process is alive is never stale."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test", stale_max_age_seconds=3600)
    inspected = {
        "Labels": {
            labels.owner_label: "owner-test",
            labels.parent_pid_label: "4242",
            labels.created_at_label: str(int(time.time())),
        }
    }

    is_stale = lifecycle.network_is_stale(
        inspected, "owner-test", labels=labels, pid_checker=lambda _pid: True
    )

    assert is_stale is False


def test_network_is_stale_returns_false_when_containers_are_attached() -> None:
    """A same-owner network with attached containers is never stale regardless of pid liveness."""
    labels = lifecycle.RuntimeLabels("ai.orchestra.test")
    inspected = {
        "Labels": {labels.owner_label: "owner-test", labels.parent_pid_label: "4242"},
        "Containers": {"abc123": {}},
    }

    is_stale = lifecycle.network_is_stale(
        inspected, "owner-test", labels=labels, pid_checker=lambda _pid: False
    )

    assert is_stale is False


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
