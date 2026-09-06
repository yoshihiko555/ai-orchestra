#!/usr/bin/env python3
"""One-action Docker lifecycle for isolated loop-harness execution."""

from __future__ import annotations

import json
import math
import secrets
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_LIB_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _LIB_DIR.parent
_DOCKER_RUNTIME_LIB = _PACKAGE_DIR.parent / "docker-runtime" / "lib"
for _path in (_LIB_DIR, _DOCKER_RUNTIME_LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import docker_runtime_cli as runtime_cli  # noqa: E402
import docker_runtime_lifecycle as runtime_lifecycle  # noqa: E402
import loop_common as lc  # noqa: E402
import loop_docker_broker as broker_runtime  # noqa: E402
import loop_docker_config as docker_config  # noqa: E402
import loop_docker_image as docker_image  # noqa: E402
import loop_docker_profile as profile  # noqa: E402
import loop_docker_settings as docker_settings  # noqa: E402
import loop_driver_support as driver_support  # noqa: E402
import loop_git_ephemeral as git_ephemeral  # noqa: E402
import loop_local_override_guard as local_override_guard  # noqa: E402


def _local_override_leaf_paths(worktree_path: Path) -> frozenset[Path]:
    """Return absolute paths of every project-local override *file* (not ancestor directory).

    Used by `align_mount_ownership()`'s `exclude` to keep these leaf entries at their original
    owner even under a root-run driver; see the round 4 comment at that call site.

    Codex review, PR #262, Critical (round 5): a `.local.yaml`/`.local.json` entry that is itself
    a symlink only contributes its own link path here by default. `align_mount_ownership()`
    reaches a symlink's resolved target through the target's own real path while walking the
    worktree (not through the symlink), so a root-owned, stricter-than-usual-permission target
    file that merely happens to live elsewhere inside the same worktree would still get
    re-chowned to the non-root container identity even though the symlink path is excluded --
    handing the untrusted Maker container read access the original permissions intentionally
    withheld. Also excluding the symlink's resolved target -- but only when that target resolves
    to somewhere inside this worktree, since anything outside it is never reached by
    `align_mount_ownership()`'s own `rglob()` over `worktree_path` in the first place -- closes
    that gap without excluding unrelated files.
    """
    worktree_root = worktree_path.resolve()
    leaves: set[Path] = set()
    for entry in local_override_guard.snapshot_local_overrides(worktree_path):
        if entry.kind == "directory":
            continue
        leaf = worktree_path / entry.path
        leaves.add(leaf)
        if entry.kind != "symlink":
            continue
        resolved = leaf.resolve(strict=False)
        if resolved != leaf and resolved.is_relative_to(worktree_root):
            leaves.add(resolved)
    return frozenset(leaves)


def _align_mount_ownership_or_raise(
    path: Path,
    *,
    exclude: frozenset[Path] | None = None,
    protect_owner_only: bool = True,
) -> None:
    """Run `align_mount_ownership()`, normalizing any raw OS failure to `DockerActionError`.

    Codex review, PR #262, High (round 6): `align_mount_ownership()`'s top-level `os.chown()`
    is unguarded, so a filesystem that rejects `chown` for the caller's identity (root-squash
    NFS, a bind source that disappeared between mount and prep) raises a raw `PermissionError`/
    `OSError`. `_ensure_started()`'s except clause only normalizes a curated list of
    Docker/config/git exception types -- neither is in it -- so letting this escape unwrapped
    would crash the whole driver process instead of producing the fail-closed Docker
    infrastructure result every other mount/container setup failure already gets.

    ``protect_owner_only`` is forwarded to `align_mount_ownership()`; callers pass ``False`` for
    paths this driver fully generates itself (the ephemeral Git runtime directory), where a
    restrictive mode is an artifact of the process umask rather than a human-placed secret -- see
    that function's round 11 docstring note.
    """
    try:
        profile.runtime.align_mount_ownership(
            path, exclude=exclude, protect_owner_only=protect_owner_only
        )
    except OSError as exc:
        raise DockerActionError(f"could not align mount ownership for {path}") from exc


def _reject_owner_only_secrets_or_raise(
    path: Path,
    *,
    exclude: frozenset[Path] | None = None,
) -> None:
    """Fail closed if a non-root driver's Checker worktree mount holds an owner-only secret.

    Codex review, PR #262, P1 (round 12): the Maker branch's chown (`_align_mount_ownership_
    or_raise()` on `self.request.worktree_path`) already runs `align_mount_ownership()`'s
    round-11 `_reject_owner_only_secrets()` check as a side effect on a non-root driver (the
    chown itself is a no-op there, but the reject check inside still runs). The Checker branch
    never chowns its read-only worktree mount at all -- there is nothing to gain from chowning
    a mount the container can only read -- so it never got that same reject check, even though
    an owner-only (e.g. 0600) secret left in the worktree (`.env`, `.netrc`, a project-local
    override) is exactly as readable from inside a non-root-driver Checker as from inside a
    Maker. This calls the same reject-only check directly, without a chown, using the same
    `exclude` semantics `_local_override_leaf_paths()` already provides for the Maker branch.
    No-op on a root-run driver -- see `docker_runtime_profile.reject_owner_only_secrets()`'s own
    docstring.
    """
    try:
        profile.runtime.reject_owner_only_secrets(path, exclude=exclude)
    except OSError as exc:
        raise DockerActionError(f"could not verify mount ownership for {path}") from exc


ActionKind = Literal["maker", "checker", "classifier"]
IdleProcessSnapshot = tuple[tuple[int, str, str], ...]
SubprocessRunner = Callable[..., subprocess.CompletedProcess]
HostChildRunner = Callable[
    [list[str], str, float, dict[str, str]], subprocess.CompletedProcess[str]
]

DOCKER_LABEL = "ai.orchestra.loop-harness"
CONTAINER_LIFETIME_MARGIN_SECONDS = 60
DOCKER_EXEC_CLIENT_FAILURE_EXIT_CODE = 125
_ALLOWED_IDLE_COMMANDS = frozenset({"docker-init", "tini", "timeout", "sleep"})
_RUNTIME_LABELS = runtime_lifecycle.RuntimeLabels(DOCKER_LABEL)
# Codex review, PR #262, High: these are set by the container's own `docker run` invocation
# (build_scenario_container_command's HOME/TMPDIR tmpfs mounts and the container image's own
# PATH), or -- for GIT_DIR/GIT_WORK_TREE, see the Issue #409 note below -- resolved entirely
# through the `.git` pointer overlay, never as an env var anywhere. The host-derived `checker_env`
# passed into execute_mechanical() must never override any of these -- e.g. `checker_env["HOME"]`
# is a *host* scratch-home path (see loop_driver_support.maker_env's `scratch_home`) that does not
# exist inside the container's filesystem namespace, and the host's own `PATH` almost never
# resolves to this hardened image's toolchain.
#
# Codex review, PR #262, High (round 6): `XDG_CONFIG_HOME` joins this reserved set for the same
# reason as `HOME`. `loop_driver_support.maker_env()` always derives it from the same host
# `scratch_home` as `HOME` (`checker_scratch_home()/.config`), but the scenario only mounts the
# action worktree/Git plus `/home/loop` and `/tmp` -- that host `.config` path is never mounted
# into the container. Forwarding it would point any tool that honors `XDG_CONFIG_HOME` at a
# missing (or, if the path happened to collide, unintended) location inside the container's own
# filesystem namespace, failing only under Docker. Treating it as container-owned means tools
# fall back to the container's own `$HOME/.config` under the writable `/home/loop` tmpfs instead.
#
# Issue #409 (design pivot, belt-and-braces removed): GIT_DIR/GIT_WORK_TREE were previously part
# of every process's env inside the scenario container (via `ScenarioContainerSpec.env`, which
# `docker exec` inherits unless overridden), so a Checker's mechanical `pytest -q` run could never
# `git init`/`git init --bare` a fixture repo under its own `tmp_path` -- every such invocation
# resolved `$GIT_DIR` to this action's own ephemeral Git plumbing instead. The fix is on the `.git`
# pointer overlay itself: `loop_git_ephemeral.prepare_ephemeral_git()` rewrites the worktree's
# pinned `.git` pointer to `gitdir: <ephemeral_dir>` (a path mounted 1:1 into every container) and
# sets `core.bare false` on the ephemeral repo, so a container process resolves the *action's own*
# repository correctly through the `.git` file alone, with no env needed at all. `GIT_DIR`/
# `GIT_WORK_TREE` env was originally also attached to `execute_claude()`'s own `docker exec` as
# belt-and-braces on top of that overlay, but Docker E2E proved this env var is actively harmful,
# not just redundant: a Maker's own Bash tool call that itself runs `git init <dir>` then
# `git -C <dir> commit` (e.g. inside a `pytest` run) has that inner `-C <dir>` resolution
# overridden by the ambient `GIT_DIR`/`GIT_WORK_TREE` env, redirecting it onto this action's own
# ephemeral repository instead of the Maker's own fixture -- the exact same failure class this
# same Issue already fixed for the Checker's mechanical exec. GIT_DIR/GIT_WORK_TREE therefore stay
# reserved here (never forwarded from a host-derived env into a mechanical exec) purely as
# defense-in-depth, and are never attached to any exec at all, `execute_claude()` included -- the
# `.git` pointer overlay is now the sole resolution mechanism for every process in every container.
_MECHANICAL_ENV_RESERVED_KEYS = frozenset(
    {"HOME", "TMPDIR", "PATH", "GIT_DIR", "GIT_WORK_TREE", "XDG_CONFIG_HOME"}
)
# Codex review, PR #262, Critical (round 7): `_mechanical_exec_env()` used to forward the entire
# host-derived `checker_env` (minus only the container-reserved keys above) into every mechanical
# `docker exec`. `checker_env` is `loop_driver_support.maker_env(os.environ, ...)`, which only
# strips a handful of push-authentication keys, so any other host secret still riding along in
# `os.environ` -- `AWS_SECRET_ACCESS_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc. -- was
# injected verbatim into Maker-authored mechanical commands, which can trivially print it into
# `mechanical_*.log`, defeating the whole point of Docker isolation for every non-GitHub
# credential. Mirroring `_broker_exec_env()`'s own minimal, explicit container env, forwarding is
# now name-pattern allowlisted instead of deny-listed: only keys matching one of these suffixes
# pass through at all. Every currently known legitimate override (`RUFF_CACHE_DIR`, see
# `_MECHANICAL_ENV_DEFAULTS` below) is a filesystem path a tool redirects its cache to off the
# read-only checker worktree -- never a credential -- so this suffix, not an ever-growing
# per-tool name list, is the allowlist itself.
_MECHANICAL_ENV_ALLOWED_SUFFIXES: tuple[str, ...] = ("_CACHE_DIR",)
# Codex review, PR #262, High (round 4); precedence flipped in round 8: this container-safe
# default is layered *over* the forwarded checker env in `_mechanical_exec_env()`, so it always
# wins for this key regardless of what an ambient host env forwards; see that function's
# docstring for why this is needed at all.
_MECHANICAL_ENV_DEFAULTS: Mapping[str, str] = {
    "RUFF_CACHE_DIR": f"{profile.CONTAINER_TMP}/ruff-cache"
}


class DockerActionError(RuntimeError):
    """An isolated action could not execute or clean up safely."""

    def __init__(self, message: str, *, container_removed: bool = False) -> None:
        super().__init__(message)
        self.container_removed = container_removed


class DockerActionSafetyStop(DockerActionError):
    """A Docker action detected state that requires a durable loop safe-stop."""

    def __init__(
        self,
        stop_reason: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason
        self.details = dict(details or {})


@dataclass(frozen=True)
class DockerActionRequest:
    config: dict[str, Any]
    isolation: docker_config.DockerIsolationConfig
    project_dir: Path
    loop_id: str
    action_id: str
    worktree_path: Path
    branch: str
    kind: ActionKind
    remaining_wall_clock_seconds: Callable[[], float]
    # Codex review, PR #262, High: defaults to True so every existing caller that does not pass
    # this explicitly keeps today's behavior unchanged. Only `build_action_executor()` sets this
    # to False, and only for a "checker" action whose resolved params have no `llm_review` block.
    needs_broker: bool = True
    # Codex review, PR #262, P1 (round 8): defaults to `None` (never lost) so every existing
    # caller that does not pass this explicitly keeps today's behavior unchanged. Only
    # `loop_driver._dispatch()` wires this to `self._lease_lost.is_set`, letting `finish()` (see
    # below) re-check the driver's own lease loss signal immediately before Maker git finalize --
    # not just once before `finish()` is called (the driver's own pre-call check), but again after
    # `_cleanup_containers()` has already spent real wall-clock time, closing the window
    # `finish()`'s own docstring documents.
    lease_lost: Callable[[], bool] | None = None


class DockerActionRuntime:
    """Lazily starts, executes in, and destroys one hardened action container."""

    def __init__(
        self,
        request: DockerActionRequest,
        *,
        host_child_runner: HostChildRunner,
        runner: SubprocessRunner = subprocess.run,
    ) -> None:
        self.request = request
        self.host_child_runner = host_child_runner
        self.runner = runner
        self.owner_id = runtime_lifecycle.owner_id(request.project_dir)
        self.owner_labels = runtime_lifecycle.resource_labels(_RUNTIME_LABELS, self.owner_id)
        self.container_name = ""
        self.broker: broker_runtime.LoopBrokerSession | None = None
        self._isolated_network: str | None = None
        self.git_session: git_ephemeral.EphemeralGitSession | None = None
        self.settings_bundle: docker_settings.DockerSettingsBundle | None = None
        self._started = False
        self._scenario_start_attempted = False
        self._scenario_removed = False
        self._idle_process_baseline: IdleProcessSnapshot | None = None
        self._finished = False
        self._cancel_requested = threading.Event()
        self._lifecycle_lock = threading.RLock()
        # Codex review, PR #262, High (round 5): set once a mechanical command times out (see
        # `execute_mechanical()`). `_execute()` has already destroyed the scenario container by
        # then (fail-closed), but the default checker runs several mechanical commands in
        # sequence (e.g. `pytest -q` then `ruff check .`); without this latch, the next command
        # would still call `_ensure_started()`/`_execute()` against the now-removed container,
        # turning an ordinary, already-preserved `(output, 124)` timeout result into an opaque
        # Docker infrastructure failure that discards it.
        self._mechanical_unusable = False

    @property
    def started(self) -> bool:
        return self._started

    def execute_claude(
        self,
        command: list[str],
        cwd: str,
        timeout_seconds: float,
        _env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        self._ensure_started()
        if self._mechanical_unusable:
            # Codex review, PR #262, High (round 8): mirrors `execute_mechanical()`'s own
            # short-circuit above. A checker with both `mechanical` and `llm_review` layers
            # calls `execute_mechanical()` first; once an earlier mechanical command times out,
            # `_execute()` has already destroyed the scenario container (fail-closed). Without
            # this check, the subsequent `llm_review` layer's `execute_claude()` call would still
            # attempt a `docker exec` against the removed container and raise an opaque
            # `DockerActionError` (returncode 125) that `_dispatch()` treats as an infra failure,
            # discarding the perfectly ordinary, already-sealed mechanical timeout result.
            # Raising `ClaudePTimeoutError` here instead reuses the exact typed error
            # `_run_one_llm_reviewer()`'s except clause already degrades into an
            # `infrastructure_failure=True` `CheckResult`, keeping the checker's sealed-result
            # contract intact instead of surfacing a crash-shaped Docker error.
            raise driver_support.ClaudePTimeoutError(
                "isolated runtime unusable after an earlier mechanical timeout"
            )
        if self.request.kind == "classifier":
            command = _without_settings(command)
        else:
            if self.settings_bundle is None:
                raise DockerActionError("trusted Docker settings bundle is unavailable")
            command = docker_settings.rewrite_claude_settings(command, self.settings_bundle)
        # Issue #409 (design pivot, belt-and-braces removed): no GIT_DIR/GIT_WORK_TREE env is
        # attached here (or anywhere else) any more -- a Maker's own Bash tool call that itself
        # runs `git init <dir>` then `git -C <dir> commit` (e.g. inside a `pytest` run) had that
        # inner `-C <dir>` resolution overridden by an ambient GIT_DIR/GIT_WORK_TREE env,
        # redirecting it onto this action's own ephemeral repository instead of the Maker's own
        # fixture. The `.git` pointer overlay (`loop_git_ephemeral.prepare_ephemeral_git()`'s
        # `gitdir: <ephemeral_dir>` pinned pointer + `core.bare false`) is the sole mechanism this
        # exec -- like every other exec into this container -- resolves the action's own
        # repository through.
        return self._execute(
            command,
            cwd="/tmp" if self.request.kind == "classifier" else cwd,
            timeout_seconds=timeout_seconds,
            env=self._broker_exec_env(),
        )

    def execute_mechanical(
        self,
        command: str,
        cwd: str,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> tuple[str, int]:
        self._ensure_started()
        if self._mechanical_unusable:
            # Codex review, PR #262, High (round 5): an earlier mechanical command in this same
            # action already timed out and destroyed the scenario container (see below). The
            # default checker runs several mechanical commands in sequence, so without this
            # short-circuit the next command would still attempt a `docker exec` against the
            # already-removed container and turn into an opaque Docker infrastructure failure
            # instead of the ordinary skip-shaped timeout result `run_mechanical_checks()`'s own
            # `remaining_budget` exhaustion path already produces for the same "nothing left to
            # run safely" situation.
            return "\ncommand skipped: isolated runtime unusable after an earlier timeout", 124
        try:
            completed = self._execute(
                ["/bin/bash", "-lc", command],
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=_mechanical_exec_env(env),
            )
        except DockerActionError as exc:
            timeout_error = exc.__cause__
            if not isinstance(timeout_error, driver_support.ClaudePTimeoutError):
                raise
            # Codex review, PR #262, High (round 4): `_execute()` already destroyed this
            # scenario container (fail-closed -- a killed `docker exec` client does not
            # guarantee the exec'd process inside the container actually stopped, so the
            # container can never be trusted as idle again), but the checker's sealed artifact
            # contract expects an ordinary `(output, 124)` mechanical timeout result here, the
            # same as the host executor's `_run_mechanical_command`, not an opaque Docker
            # infrastructure failure that discards this command's output and the sealed result
            # entirely.
            self._mechanical_unusable = True
            output = f"{timeout_error.stdout}{timeout_error.stderr}\ncommand timed out"
            return output, 124
        output = completed.stdout
        if completed.stderr:
            output += ("\n" if output else "") + completed.stderr
        return output, completed.returncode

    def finish(self, *, action_succeeded: bool) -> None:
        """Finish a Maker/Checker action, fencing Maker git finalize against a lost lease.

        Codex review, PR #262, P1 (round 8): `abort()` also routes through this method (as
        `finish(action_succeeded=False)`), so this same gate protects both the success path
        (`finish()`) and the failure path (`abort()`) with one check. `_cleanup_containers()`
        above can itself spend real wall-clock time (destroying the scenario/broker/network),
        during which the driver's heartbeat thread can flip `request.lease_lost()` after
        `loop_driver._dispatch()` already decided to call this method. Re-checking here, right
        before `_finish_git()` would otherwise CAS-publish a Maker's commit chain
        (`action_succeeded=True`) or diff its worktree against a now-meaningless stale
        `baseline_sha` (`action_succeeded=False`, which always misclassifies a Maker that
        committed cleanly before losing the lease as `maker_partial_worktree` drift), closes
        that window without needing the caller to hold any lock across the call. `_finish_git()`
        itself still runs its own git subprocess calls without a further re-check -- that
        narrower, sub-second residual TOCTOU window is accepted and documented in
        `docs/design/loop-harness-isolation.md`'s residual-risk section.

        Codex review, PR #262, P1 (round 13): the same `lease_already_lost` re-check also gates
        the `finally` block's `_cleanup_local_runtime()` call, not just `_finish_git()`.
        `_cleanup_local_runtime()` calls `cleanup_ephemeral_git()`/`cleanup_settings_bundle()`
        against `self.git_session.runtime_dir`, which is deterministic per `(loop_id, action_id)`
        -- exactly the path `discard_after_lease_loss()`'s own docstring explains a replacement
        worker may already have re-created via `prepare_ephemeral_git()` once this worker's lease
        is confirmed lost. Without this guard, a stale worker whose lease died during
        `_cleanup_containers()` above (that real wall-clock window is exactly what this method's
        own re-check exists for) would still reach `finally` and delete that shared runtime
        dir/settings bundle out from under the replacement worker's live run. Skipping it here
        leaves at most the same harmless leftover local directory `discard_after_lease_loss()`
        already accepts: `prepare_ephemeral_git()` unconditionally wipes-and-recreates that path
        the next time this action_id is actually retried.
        """
        with self._lifecycle_lock:
            if self._finished:
                return
            self._finished = True
            self._persist_broker_metrics()
            scenario_error, cleanup_errors = self._cleanup_containers()
            primary_error: BaseException | None = scenario_error
            lease_already_lost = self.request.lease_lost is not None and self.request.lease_lost()
            try:
                if primary_error is None and not lease_already_lost:
                    self._finish_git(action_succeeded=action_succeeded)
            except BaseException as exc:
                primary_error = exc
            finally:
                if not lease_already_lost:
                    self._cleanup_local_runtime(cleanup_errors)
            if primary_error is not None:
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(f"action cleanup also failed: {cleanup_error}")
                raise primary_error
            if cleanup_errors:
                raise DockerActionSafetyStop(
                    "action_cleanup_failed",
                    "isolated action cleanup failed",
                    details={"cleanup_errors": cleanup_errors},
                )

    def _persist_broker_metrics(self) -> None:
        """Persist the broker's final `--print-metrics` snapshot before cleanup discards it.

        Issue #405: `LoopBrokerSession.metrics` only ever updates in-memory via
        `refresh_metrics()` (unused in production before this) -- once `_cleanup_containers()`
        below removes the broker container, its metrics (request/rejection counts, anomaly
        reasons, estimated cost) become unrecoverable, leaving a budget-rejection failure
        undiagnosable without an interactive `docker exec` into the still-running broker.
        Deliberately never called from `discard_after_lease_loss()` (EV-50): once the lease is
        already known lost, no artifact write is safe -- see that method's own docstring.
        Best-effort: any failure to read or persist metrics is logged and never blocks finish/
        abort, since a missing metrics artifact is strictly less useful, not incorrect. The
        `hasattr` guard below skips silently (no stderr line at all) rather than logging an
        error for a `self.broker` that structurally has no `refresh_metrics` at all -- real
        production `LoopBrokerSession` always has it; only test/e2e stubs (a handful of
        `class Broker:` fakes across this package's test suite) omit it, and that is not a
        genuine failure worth logging. Still catches a broad `Exception` around the call itself
        (not just `LoopDockerBrokerError`) -- a real `refresh_metrics()` call is a `docker exec`
        that can also raise `subprocess.TimeoutExpired`/`OSError` -- so a broker that does
        support metrics but genuinely fails to report them is still logged.
        """
        if self.broker is None or not hasattr(self.broker, "refresh_metrics"):
            return
        try:
            metrics = self.broker.refresh_metrics()
        except Exception as exc:  # noqa: BLE001 - best-effort diagnostics must never raise
            print(f"loop_docker_action: could not read broker metrics: {exc}", file=sys.stderr)
            return
        try:
            lc.save_artifact(
                self.request.loop_id,
                str(self.request.project_dir),
                self.request.action_id,
                "broker_metrics.json",
                json.dumps(metrics, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001 - best-effort diagnostics must never raise
            print(f"loop_docker_action: could not persist broker metrics: {exc}", file=sys.stderr)

    def cancel(self) -> None:
        """Latch cancellation and destroy a started scenario without leaking thread errors."""
        self._cancel_requested.set()
        # Cancellation owns the untrusted scenario cgroup only. The dispatch thread remains
        # the sole owner of broker/network/settings/Git cleanup through finish()/abort(); doing
        # that work from the heartbeat thread would race result finalization and state fencing.
        with self._lifecycle_lock:
            if not self._scenario_start_attempted or self._scenario_removed:
                return
            try:
                self._destroy_scenario_locked()
            except DockerActionSafetyStop:
                # The dispatch thread retries cleanup and turns persistent failure into a
                # typed safety-stop. Heartbeat threads must never mutate loop state directly.
                return

    def discard_after_lease_loss(self) -> None:
        """Tear down containers/broker/local runtime with zero git reads or writes.

        Local pre-push review (round 9): `loop_driver._dispatch()` calls this -- instead of
        `finish()`/`abort()` -- only in the exact race window where a Maker finished cleanly
        right before this driver's own lease was detected lost. Both `finish()` and `abort()`
        end in `_finish_git()`, which either CAS-publishes the Maker's commits
        (`action_succeeded=True`) or diffs the worktree against `baseline_sha` via
        `verify_failed_maker_worktree()` (`action_succeeded=False`) -- and a Maker that
        committed cleanly before losing the lease always reads as drift against that stale
        baseline, safety-stopping as `maker_partial_worktree`. That safety stop then tries to
        persist through `lds.persist_safe_stop()`, but another worker already holds the lease,
        so the CAS write is rejected -- with no caller left to catch it, crashing the driver
        process instead of returning `EXIT_FOREIGN_LEASE`.

        This method never calls `finalize_ephemeral_git()` or `verify_failed_maker_worktree()`
        -- neither reads nor writes the shared branch or worktree tree at all. It only removes
        this action's own scenario container, broker, and isolated network -- all named with a
        random per-instance nonce (see `container_name`/`internal_network`/`external_network`
        construction), so tearing them down here can never collide with any other worker's own,
        differently-named resources.

        Codex review, PR #262, P1 (round 11): this method deliberately does *not* also call
        `cleanup_ephemeral_git()`/`cleanup_settings_bundle()` the way `_cleanup_local_runtime()`
        (used by `finish()`/`abort()`) does. Both operate on `self.git_session.runtime_dir` (and
        the `trusted-settings` subdirectory `docker_settings.create_settings_bundle()` creates
        under it), which is deterministic per `(loop_id, action_id)` -- not per attempt. This
        method only runs once the lease is *already* known lost, which is exactly the case where
        `attach(..., recover_orphans=True)` may have already handed the same pending action to a
        replacement worker that has since re-run `prepare_ephemeral_git()` against that same
        path. Deleting it here, after that replacement has already recreated it, would destroy
        its live ephemeral GIT_DIR/settings bundle mid-run instead of this stale worker's own.
        `finish()`/`abort()` no longer have this problem either (round 13 fix): both re-check
        `request.lease_lost()` right before running, and `finish()`'s `finally` block now gates
        its own `_cleanup_local_runtime()` call on that same re-checked value, so a lease that
        dies mid-`_cleanup_containers()` skips local runtime cleanup there too -- the same
        replacement-worker race this method exists to avoid. Skipping this cleanup
        here leaves at most a harmless leftover local directory: `prepare_ephemeral_git()`
        unconditionally wipes-and-recreates that same path the next time this action_id is
        actually retried, and if it never is, the debris is a bounded, non-shared-state cost --
        far cheaper than corrupting a live replacement's runtime. It must never raise: once the
        lease is gone there is no safe-stop channel left to persist a failure into, so every
        cleanup step is best-effort and any errors are only logged.
        """
        with self._lifecycle_lock:
            if self._finished:
                return
            self._finished = True
            errors: list[str] = []
            try:
                scenario_error, cleanup_errors = self._cleanup_containers()
                if scenario_error is not None:
                    # Codex review, PR #262, P2 (round 9): `_cleanup_containers()` returns an
                    # unconfirmed scenario-removal failure (e.g. `maker_container_cleanup_
                    # unconfirmed`) separately from `cleanup_errors` -- dropping it here left a
                    # Maker/Checker container that failed `docker rm -f` after the lease was lost
                    # both running and unlogged, breaking the quiet-teardown contract that
                    # cleanup failures are always recorded for operators. Fold it into `errors` so
                    # it reaches the stderr log below alongside the broker/network failures;
                    # untrusted-container sweep still owns actual removal once this is surfaced.
                    errors.append(str(scenario_error))
                errors.extend(cleanup_errors)
            except Exception as exc:  # noqa: BLE001 - quiet teardown must never raise
                errors.append(str(exc))
            if errors:
                print(
                    "loop_docker_action: lease-lost quiet teardown had cleanup errors "
                    f"(ignored, no safe-stop channel left once the lease is gone): {errors}",
                    file=sys.stderr,
                )

    def _ensure_started(self) -> None:
        if self._started:
            return
        try:
            self._start()
        except (DockerActionError, DockerActionSafetyStop):
            raise
        except (
            docker_config.DockerConfigError,
            docker_image.DockerImageError,
            broker_runtime.LoopDockerBrokerError,
            profile.DockerProfileError,
            docker_settings.DockerSettingsError,
            git_ephemeral.EphemeralGitInfrastructureError,
            git_ephemeral.EphemeralGitSafetyStop,
            local_override_guard.LocalOverrideSnapshotError,
        ) as exc:
            self._raise_normalized(exc)

    def _start(self) -> None:
        self._raise_if_cancelled()
        if not runtime_cli.docker_daemon_available(runner=self.runner):
            raise DockerActionError("Docker daemon unavailable")
        # Codex review, PR #262, High (round 5): fail fast, before any Docker setup work runs,
        # when the wall-clock budget is already exhausted -- rather than only discovering that
        # after `ensure_scenario_image()` below has already run. This narrows, but does not
        # fully close, the round-5 gap: capping `ensure_scenario_image()`/`ensure_broker_image()`
        # 's own build/pull work to the *remaining* budget when it is small-but-nonzero would
        # need a new timeout knob threaded through `docker_runtime_image.ensure_recipe_image()`
        # (a shared docker-runtime primitive with none today); left as a documented residual
        # risk rather than a same-diff architectural change.
        _max_lifetime_seconds(self.request.remaining_wall_clock_seconds())
        broker_runtime.sweep_stale_resources(self.owner_id, runner=self.runner)
        scenario_image = docker_image.ensure_scenario_image(
            self.request.config, self.request.project_dir, runner=self.runner
        )
        self._raise_if_cancelled()
        mounts, container_env, workdir = self._prepare_mounts()
        # Issue #409 (design pivot, belt-and-braces removed): `container_env` (from
        # `build_maker_git_mount_spec()`/`build_checker_git_mount_spec()`) never carries
        # GIT_DIR/GIT_WORK_TREE at all any more -- those mount specs' `env` is now always empty.
        # No process anywhere (the scenario container's own `docker run` startup env, any
        # `docker exec` including `execute_claude()`'s) is ever given a GIT_DIR/GIT_WORK_TREE env
        # var; the `.git` pointer overlay (`loop_git_ephemeral.prepare_ephemeral_git()`'s
        # `gitdir: <ephemeral_dir>` pinned pointer + `core.bare false`) is the sole resolution
        # mechanism. See `_MECHANICAL_ENV_RESERVED_KEYS`'s own comment for why an ambient
        # GIT_DIR/GIT_WORK_TREE env, even scoped only to `execute_claude()`'s exec, turned out to
        # be actively harmful rather than merely redundant.
        # Issue #407: the scenario container's *startup* env (this `container_env`, later handed
        # to `ScenarioContainerSpec(env=...)` below) previously carried no `RUFF_CACHE_DIR`
        # default for any `kind` -- only `_mechanical_exec_env()` (used by the Checker's mechanical
        # `docker exec` calls) applied `_MECHANICAL_ENV_DEFAULTS`. A Maker-authored Bash command
        # running `ruff check` inside the scenario container -- via `execute_claude()`'s `docker
        # exec`, whose env is `_broker_exec_env()`, not `_mechanical_exec_env()` -- therefore fell
        # back to ruff's own default of `.ruff_cache` under `cwd` (the mounted, read-write Maker
        # worktree), leaving a `mode 0600` cache directory on the host worktree. The next action's
        # mount-safety check (`_reject_owner_only_secrets_or_raise()`) then refuses to mount that
        # owner-only path into a non-root container, safe-stopping the loop. Merging the same
        # container-tmpfs default in here -- regardless of `kind` (maker/checker/classifier) --
        # means every scenario container starts with a working `RUFF_CACHE_DIR` regardless of
        # which exec path (`execute_claude()` or `execute_mechanical()`) a tool run happens through.
        container_env = {**_MECHANICAL_ENV_DEFAULTS, **container_env}
        max_lifetime = _max_lifetime_seconds(self.request.remaining_wall_clock_seconds())
        scope = f"{self.request.loop_id}-{self.request.action_id}"
        if self.request.needs_broker:
            broker_image = docker_image.ensure_broker_image(
                self.request.config, self.request.project_dir, runner=self.runner
            )
            self.broker = broker_runtime.start_broker(
                self.request.isolation.broker,
                scope=scope,
                owner_id=self.owner_id,
                scenario_image_id=scenario_image.image_id,
                broker_image_id=broker_image.image_id,
                max_lifetime_seconds=max_lifetime,
                runner=self.runner,
            )
            internal_network = self.broker.internal_network
        else:
            # Codex review, PR #262, High: this action's checker phase has no `llm_review` layer
            # and will never call execute_claude(), so the full credential broker (and the Claude
            # OAuth credential its startup requires) is pure overhead here -- and a hard failure
            # on hosts without one. Only the dedicated internal network the scenario container
            # still needs is created; see start_isolated_network()'s docstring.
            internal_network = broker_runtime.start_isolated_network(
                scope=scope,
                owner_id=self.owner_id,
                runner=self.runner,
            )
            self._isolated_network = internal_network
        self._raise_if_cancelled()
        self.container_name = (
            f"lh-{profile.runtime.safe_name(self.request.loop_id)}-"
            f"{profile.runtime.safe_name(self.request.action_id)}-{secrets.token_hex(3)}"
        )
        spec = profile.ScenarioContainerSpec(
            container_name=self.container_name,
            image_id=scenario_image.image_id,
            internal_network=internal_network,
            workdir=workdir,
            mounts=mounts,
            env=container_env,
            resources=profile.resources_config(self.request.isolation.resources),
            max_lifetime_sec=max_lifetime,
            owner_labels=self.owner_labels,
        )
        # Local pre-push review (round 9, P2): build (and thereby validate) the scenario
        # container command *before* acquiring `_lifecycle_lock` below. `build_scenario_
        # container_command()` -> `_validate_mount()` walks every directory mount source with
        # `os.walk()` to reject any Unix socket hidden underneath it (see loop_docker_profile.py's
        # `_reject_socket_descendants()`), which for a large Maker/Checker worktree can be a
        # non-trivial filesystem scan. `cancel()` also needs this same lock to destroy an
        # already-started container (see its own docstring); running that scan while holding the
        # lock would let it delay `cancel()` far longer than the "atomic start section" below is
        # meant to -- that section is scoped to the actual `docker run`/idle-baseline Docker calls
        # only, not to mount validation.
        scenario_command = profile.build_scenario_container_command(spec)
        # docker run plus the trusted idle-baseline capture is one atomic start section.
        # cancel() latches its Event immediately but may wait for this bounded section's Docker
        # calls; the post-start check removes the container before any docker exec can begin.
        with self._lifecycle_lock:
            self._raise_if_cancelled_locked()
            self._scenario_start_attempted = True
            start_scenario_container(spec, runner=self.runner, command=scenario_command)
            self._idle_process_baseline = capture_scenario_idle_baseline(
                self.container_name,
                runner=self.runner,
            )
            self._started = True
            self._raise_if_cancelled_locked()

    def _prepare_mounts(self) -> tuple[tuple[Any, ...], dict[str, str], Path]:
        if self.request.kind == "classifier":
            return (), {}, Path("/tmp")
        if self.request.kind == "maker":
            # Codex review, PR #262, High (round 3): this chown must run *before*
            # prepare_ephemeral_git() below, not after build_maker_git_mount_spec(). The
            # local-override guard's baseline snapshot (loop_git_ephemeral._prepare_ephemeral_git)
            # is captured from the worktree's on-disk state as of that call, including each
            # override file's uid/gid (loop_local_override_guard.LocalOverrideSnapshot). Under a
            # root-run driver, chowning the worktree to the fixed non-root 65532:65532 identity
            # *after* that snapshot was already taken changes every tracked override's uid/gid,
            # so the later `_verify_local_override_snapshot()` call (verify_failed_maker_worktree /
            # finalize_ephemeral_git) sees that intentional, driver-initiated ownership change as
            # Maker tampering and safe-stops as `maker_partial_worktree` even though the Maker
            # never touched those files. Running the chown first means the snapshot already
            # reflects the container-ready ownership, so no spurious drift is ever recorded.
            #
            # Codex review, PR #262, High (round 4): exclude the override *files* themselves
            # (not their ancestor directories) from this chown. Under a root-run driver, a
            # project-local override that was deliberately root-owned with stricter-than-usual
            # permission bits (e.g. mode 600) would otherwise gain the fixed non-root 65532:65532
            # container identity as its new owner -- newly granting the untrusted Maker read
            # access the original permissions never allowed. Ancestor directories stay re-owned so
            # the Maker can still traverse them and create unrelated sibling entries.
            _align_mount_ownership_or_raise(
                self.request.worktree_path,
                exclude=_local_override_leaf_paths(self.request.worktree_path),
            )
        self.git_session = git_ephemeral.prepare_ephemeral_git(
            project_dir=self.request.project_dir,
            loop_id=self.request.loop_id,
            action_id=self.request.action_id,
            worktree_path=self.request.worktree_path,
            branch=self.request.branch,
        )
        runtime_dir = self.git_session.runtime_dir
        self.settings_bundle = docker_settings.create_settings_bundle(
            runtime_dir,
            _LIB_DIR / "maker_bash_guard.py",
        )
        if self.request.kind == "maker":
            git_mounts = git_ephemeral.build_maker_git_mount_spec(self.git_session)
            # Codex review, PR #262, Critical: when the driver process itself runs as root
            # (root-run CI/dev-container environments), non_root_identity() still forces the
            # scenario container to the fixed non-root 65532:65532 identity, but the ephemeral
            # GIT_DIR this same root process just created (inside prepare_ephemeral_git above,
            # after the worktree was already re-owned) stays root-owned. This read-write Maker
            # mount must be re-owned to that same identity or the container's rw mount becomes
            # unwritable to the non-root Maker process. No-op when the driver is not root, since
            # ownership already matches.
            #
            # Codex review, PR #262, P1 (round 11): `protect_owner_only=False` -- this directory
            # is entirely driver-generated Git plumbing (config, refs, alternates), never
            # human-placed secret content, so a restrictive mode picked up from the process
            # umask (e.g. root running under `umask 077`) must not be treated as an
            # `align_mount_ownership()`-protected secret. See that function's own docstring.
            _align_mount_ownership_or_raise(
                self.git_session.ephemeral_dir, protect_owner_only=False
            )
        else:
            # validate_isolation_config() already enforces this for config-built requests.
            # Keep the runtime assertion as defense-in-depth for directly constructed requests.
            if not self.request.isolation.checker_read_only_worktree:
                raise DockerActionError("Checker worktree must be read-only")
            # Codex review, PR #262, P1 (round 12): unlike the Maker branch above, this Checker
            # branch never chowns `self.request.worktree_path` at all (there is nothing to gain
            # from chowning a read-only mount), so it never got the round-11 owner-only-secret
            # reject check that chown runs as a side effect for Maker. Run the same reject-only
            # check directly here, using the same `.local.*` exclude set the Maker branch uses.
            _reject_owner_only_secrets_or_raise(
                self.request.worktree_path,
                exclude=_local_override_leaf_paths(self.request.worktree_path),
            )
            git_mounts = git_ephemeral.build_checker_git_mount_spec(self.git_session)
            # Codex review, PR #262, P1 (round 12): this re-own MUST run *after*
            # build_checker_git_mount_spec(), not before (round 11's order, which this replaces).
            # That function's _harden_ephemeral_git_metadata() step atomically rewrites
            # config/objects/info/alternates inside self.git_session.ephemeral_dir unconditionally,
            # every time it runs, under the calling (host driver) process's own uid. Re-owning
            # before that call gets silently undone by it: under a root-run driver the freshly
            # recreated files come back root:root 0600 and the checker container (always the
            # fixed non-root 65532:65532 identity) can once again never read its own `GIT_DIR` --
            # the exact bug round 11 was supposed to fix. Running the re-own after this call
            # re-owns the directory as it will actually be mounted, including any files that call
            # just rewrote. `protect_owner_only=False` for the same reason as the Maker branch:
            # this directory holds no human secrets, only driver-generated git plumbing.
            _align_mount_ownership_or_raise(
                self.git_session.ephemeral_dir, protect_owner_only=False
            )
        trusted_mount = git_ephemeral.BindMountSpec(
            self.settings_bundle.source_dir,
            Path(self.settings_bundle.container_dir),
            True,
        )
        return (
            (*git_mounts.mounts, trusted_mount),
            dict(git_mounts.env),
            self.request.worktree_path,
        )

    def _execute(
        self,
        command: list[str],
        *,
        cwd: str,
        timeout_seconds: float,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        self._raise_if_cancelled()
        docker_command = profile.build_exec_command(
            self.container_name,
            command,
            workdir=cwd,
            env=env,
        )
        try:
            completed = self.host_child_runner(
                docker_command,
                str(self.request.project_dir),
                timeout_seconds,
                runtime_cli.host_env(),
            )
        except Exception as exc:
            self._destroy_scenario_or_raise()
            raise DockerActionError("docker exec did not complete") from exc
        except BaseException:
            self._destroy_scenario_or_raise()
            raise
        self._raise_if_cancelled()
        if completed.returncode == DOCKER_EXEC_CLIENT_FAILURE_EXIT_CODE:
            self._destroy_scenario_or_raise()
            raise DockerActionError("docker exec failed before the action command ran")
        self._assert_idle_or_destroy()
        return completed

    def _broker_exec_env(self) -> dict[str, str]:
        if self.broker is None:
            raise DockerActionError("credential broker is unavailable")
        # Issue #407: layer the same `RUFF_CACHE_DIR` default `_mechanical_exec_env()` uses
        # underneath this exec's explicit `-e` flags too. `container_env` (set in `_start()`
        # above) already makes the container's own startup env carry this default, and `docker
        # exec` normally inherits it -- but making it explicit here as well means a Maker-authored
        # `ruff check` run through `execute_claude()` never depends on that inheritance holding,
        # the same belt-and-suspenders posture `_mechanical_exec_env()` already takes for the
        # Checker's mechanical exec path. The broker-derived keys below share no name with this
        # default, so there is no real collision, but they are still listed last to win were one
        # ever introduced.
        return {
            **_MECHANICAL_ENV_DEFAULTS,
            "ANTHROPIC_BASE_URL": self.broker.base_url,
            "ANTHROPIC_API_KEY": self.broker.run_token,
            "CLAUDE_CONFIG_DIR": f"{profile.CONTAINER_HOME}/.claude",
            "NO_PROXY": broker_runtime.BROKER_ALIAS,
        }

    def _assert_idle_or_destroy(self) -> None:
        try:
            enforce_scenario_container_idle(
                self.container_name,
                expected_snapshot=self._idle_process_baseline,
                runner=self.runner,
            )
        except DockerActionError as exc:
            self._scenario_removed = exc.container_removed
            raise

    def _destroy_scenario_or_raise(self) -> None:
        with self._lifecycle_lock:
            self._destroy_scenario_locked()

    def _destroy_scenario_locked(self) -> None:
        if self._scenario_removed:
            return
        if not self.container_name:
            return
        if not remove_scenario_container(self.container_name, runner=self.runner):
            raise DockerActionSafetyStop(
                "maker_container_cleanup_unconfirmed"
                if self.request.kind == "maker"
                else "container_cleanup_unconfirmed",
                "could not confirm action container removal",
                details={"container_name": self.container_name},
            )
        self._scenario_removed = True

    def _raise_if_cancelled(self) -> None:
        if not self._cancel_requested.is_set():
            return
        with self._lifecycle_lock:
            self._raise_if_cancelled_locked()

    def _raise_if_cancelled_locked(self) -> None:
        if not self._cancel_requested.is_set():
            return
        if self._scenario_start_attempted and not self._scenario_removed:
            self._destroy_scenario_locked()
        raise DockerActionError("Docker action was cancelled")

    def _cleanup_containers(self) -> tuple[DockerActionSafetyStop | None, list[str]]:
        scenario_error: DockerActionSafetyStop | None = None
        errors: list[str] = []
        try:
            self._destroy_scenario_or_raise()
        except DockerActionSafetyStop as exc:
            scenario_error = exc
        if self.broker is not None:
            try:
                self.broker.cleanup()
            except broker_runtime.LoopDockerBrokerError as exc:
                errors.append(str(exc))
        elif self._isolated_network is not None:
            if not broker_runtime.stop_isolated_network(self._isolated_network, runner=self.runner):
                errors.append(f"could not remove Docker network: {self._isolated_network}")
        return scenario_error, errors

    def _finish_git(self, *, action_succeeded: bool) -> None:
        if self.git_session is None or self.request.kind != "maker":
            return
        if self._scenario_start_attempted and not self._scenario_removed:
            raise DockerActionSafetyStop(
                "maker_container_cleanup_unconfirmed",
                "Maker finalize forbidden because container cleanup was not confirmed",
            )
        if action_succeeded:
            git_ephemeral.finalize_ephemeral_git(self.git_session)
            return
        git_ephemeral.verify_failed_maker_worktree(self.git_session)

    def _cleanup_local_runtime(self, errors: list[str]) -> None:
        try:
            docker_settings.cleanup_settings_bundle(self.settings_bundle)
        except docker_settings.DockerSettingsError as exc:
            errors.append(str(exc))
        if self.git_session is not None:
            try:
                git_ephemeral.cleanup_ephemeral_git(self.git_session)
            except git_ephemeral.EphemeralGitInfrastructureError as exc:
                errors.append(str(exc))

    @staticmethod
    def _raise_normalized(exc: BaseException) -> None:
        if isinstance(exc, git_ephemeral.EphemeralGitSafetyStop):
            raise DockerActionSafetyStop(
                exc.stop_reason,
                str(exc),
                details=exc.details,
            ) from exc
        raise DockerActionError(str(exc)) from exc


def _max_lifetime_seconds(remaining_seconds: float) -> int:
    if not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
        raise DockerActionError("action wall-clock budget is exhausted")
    return math.ceil(remaining_seconds) + CONTAINER_LIFETIME_MARGIN_SECONDS


def start_scenario_container(
    spec: profile.ScenarioContainerSpec,
    *,
    runner: SubprocessRunner = subprocess.run,
    command: list[str] | None = None,
) -> None:
    """Start one scenario using the production hardened profile builder.

    `command`, when given, must be `profile.build_scenario_container_command(spec)`'s own output
    for this same `spec` -- `_start()` passes it precomputed so this function does not rebuild
    (and thereby re-run `_validate_mount()`'s socket-descendant `os.walk()` a second time) while
    holding `_lifecycle_lock` (local pre-push review, round 9, P2). Defaults to `None` so direct
    callers (e.g. the containment e2e tests) keep building it here exactly as before.
    """
    completed = runtime_cli.run(
        command if command is not None else profile.build_scenario_container_command(spec),
        runner=runner,
        timeout=30,
    )
    if completed.returncode != 0:
        raise DockerActionError("could not start hardened scenario container")


def capture_scenario_idle_baseline(
    container_name: str,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> IdleProcessSnapshot:
    """Capture the trusted supervisor identity immediately after container startup."""
    completed = runtime_cli.run(
        ["docker", "top", container_name, "-eo", "pid,comm,args"],
        runner=runner,
        timeout=10,
    )
    snapshot = _process_snapshot(completed.stdout) if completed.returncode == 0 else None
    if snapshot is None or not _only_idle_snapshot(snapshot):
        raise DockerActionError("docker exec left non-idle processes in the action container")
    return snapshot


def assert_scenario_container_idle(
    container_name: str,
    *,
    expected_snapshot: IdleProcessSnapshot | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> None:
    """Fail unless current processes exactly match the trusted startup supervisor."""
    current = capture_scenario_idle_baseline(container_name, runner=runner)
    if expected_snapshot is not None and current != expected_snapshot:
        raise DockerActionError("docker exec left non-idle processes in the action container")


def enforce_scenario_container_idle(
    container_name: str,
    *,
    expected_snapshot: IdleProcessSnapshot | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> None:
    """Destroy the action cgroup fail-closed when an exec leaves residual processes."""
    try:
        assert_scenario_container_idle(
            container_name,
            expected_snapshot=expected_snapshot,
            runner=runner,
        )
    except DockerActionError as exc:
        if not remove_scenario_container(container_name, runner=runner):
            raise DockerActionError("non-idle action container could not be removed") from exc
        raise DockerActionError(str(exc), container_removed=True) from exc


def remove_scenario_container(
    container_name: str,
    *,
    runner: SubprocessRunner = subprocess.run,
) -> bool:
    """Remove and confirm absence using the shared production cleanup primitive."""
    return runtime_cli.remove_container(container_name, runner=runner)


def _mechanical_exec_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Forward only an allowlisted subset of the caller's checker env, with a container-writable
    cache default layered underneath.

    Codex review, PR #262, Critical (round 7): this previously forwarded the caller's *entire*
    sanitized checker env -- effectively the host process's own `os.environ`, minus a handful of
    container-reserved keys and the few push-authentication keys `maker_env()` strips -- into
    every mechanical `docker exec`. Any other host secret (`AWS_SECRET_ACCESS_KEY`,
    `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) rode along unchanged, and Maker-authored
    mechanical commands running inside that same container can trivially print it into
    `mechanical_*.log`, defeating Docker isolation for every non-GitHub credential. Only keys
    matching `_MECHANICAL_ENV_ALLOWED_SUFFIXES` (currently `*_CACHE_DIR`, see that constant's own
    docstring for why a name-pattern allowlist rather than a deny-list) are forwarded at all;
    everything else in the caller's env is silently dropped, not forwarded.

    Codex review, PR #262, High (round 4): the checker worktree (this exec's `cwd`) is mounted
    read-only, but ruff defaults its cache directory to `.ruff_cache` under the project root
    unless `RUFF_CACHE_DIR` is set (https://docs.astral.sh/ruff/settings/#cache-dir), and the
    bundled issue-loop's default mechanical commands include `ruff check .`. `/tmp` is the
    container's own tmpfs mount (`loop_docker_profile.CONTAINER_TMP`), always writable by the
    non-root exec identity regardless of `cwd`, and this value never leaks any host path.

    Codex review, PR #262, P2 (round 8): this container-safe default now always wins over a
    forwarded value for the same key, rather than the other way around. `env` here is derived
    from the *host* driver process's `os.environ` (`loop_driver_support.maker_env`), so an
    operator whose shell merely happens to export an ambient `RUFF_CACHE_DIR` pointing at a host
    path (e.g. `~/.cache/ruff`) -- with no intent to override anything Docker-specific -- used to
    have that host-only path silently forwarded into the container in place of the working
    `/tmp` default, breaking the checker (the path does not exist inside the container's
    filesystem namespace) with no way for the allowlist to tell an ambient host value apart from
    a deliberate override. There is currently no supported way to override this default from the
    host env; a future explicit escape hatch (e.g. a distinct `LOOP_CONTAINER_*` key namespace)
    can be added if a real need for one arises. Allowlisted `*_CACHE_DIR` keys with no built-in
    default (i.e. not `RUFF_CACHE_DIR`) are unaffected and still forward through as before.
    """
    forwarded = (
        {}
        if not env
        else {
            key: value
            for key, value in env.items()
            if key not in _MECHANICAL_ENV_RESERVED_KEYS
            and any(key.endswith(suffix) for suffix in _MECHANICAL_ENV_ALLOWED_SUFFIXES)
        }
    )
    return {**forwarded, **_MECHANICAL_ENV_DEFAULTS}


def _without_settings(command: list[str]) -> list[str]:
    rewritten = list(command)
    try:
        index = rewritten.index("--settings")
    except ValueError:
        return rewritten
    if index + 1 >= len(rewritten):
        raise DockerActionError("claude command has an invalid --settings argument")
    del rewritten[index : index + 2]
    if rewritten:
        rewritten[0] = "claude"
    return rewritten


def _only_idle_processes(output: str) -> bool:
    snapshot = _process_snapshot(output)
    return snapshot is not None and _only_idle_snapshot(snapshot)


def _process_snapshot(output: str) -> IdleProcessSnapshot | None:
    lines = [line.strip() for line in output.splitlines()[1:] if line.strip()]
    if not lines:
        return None
    processes: list[tuple[int, str, str]] = []
    for line in lines:
        fields = line.split(maxsplit=2)
        if len(fields) < 2 or not fields[0].isdigit():
            return None
        processes.append(
            (
                int(fields[0]),
                fields[1],
                fields[2] if len(fields) == 3 else "",
            )
        )
    return tuple(sorted(processes))


def _only_idle_snapshot(snapshot: IdleProcessSnapshot) -> bool:
    if not snapshot:
        return False
    for _pid, command, arguments in snapshot:
        if command not in _ALLOWED_IDLE_COMMANDS or not _is_idle_process(command, arguments):
            return False
    return any(command == "sleep" for _pid, command, _arguments in snapshot)


def _is_idle_process(command: str, arguments: str) -> bool:
    if command == "sleep":
        return arguments.split()[-2:] == ["/usr/bin/sleep", "infinity"]
    if command == "timeout":
        return "/usr/bin/timeout " in f" {arguments}" and arguments.endswith(
            "/usr/bin/sleep infinity"
        )
    if command in {"docker-init", "tini"}:
        return "docker-init" in arguments or "/usr/bin/timeout" in arguments
    return False
