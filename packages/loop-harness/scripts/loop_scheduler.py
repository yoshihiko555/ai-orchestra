#!/usr/bin/env python3
"""LP-2 loop-harness scheduler: resident poller that discovers, spawns, and monitors workers.

One resident process per project. Each poll cycle: reap finished `loop_driver.py` workers
(restarting abnormal exits unless the loop is `failed`/`stopped`), then spawn new workers
for labeled Issues up to the concurrency cap. At startup, verifies repo-identity for any
existing loop runs and safety-stops mismatches before touching them.

See `docs/design/loop-harness-cli.md` 3 節 (authoritative design) for the contract this
module must not deviate from.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import loop_common as lc  # noqa: E402
import loop_definition as ld  # noqa: E402
import loop_driver_support as lds  # noqa: E402
import worktree_manager as wm  # noqa: E402

DEFAULT_DEFINITION_ID = "issue-loop"
DEFAULT_CONCURRENCY_LIMIT = 2
DEFAULT_POLL_INTERVAL_SECONDS = 300

_ACTIVE_STATUSES = frozenset({"running", "waiting_external"})
_NON_RESTARTABLE_STATUSES = frozenset({"failed", "stopped"})


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `loop_scheduler.py [--project <path>] [print-launchd|print-cron]`."""
    args = _parse_args(argv)
    project = _project_dir(args.project)
    if args.command == "print-launchd":
        print(render_launchd_plist(project))
        return 0
    if args.command == "print-cron":
        print(render_cron_entry(project))
        return 0
    run_scheduler(project, args.definition, max_cycles=args.max_cycles)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse loop_scheduler.py CLI arguments."""
    parser = argparse.ArgumentParser(prog="loop_scheduler.py", description=__doc__)
    parser.add_argument("--project")
    parser.add_argument("--definition", default=DEFAULT_DEFINITION_ID)
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="exit after N poll cycles instead of running forever (test/debug use)",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("print-launchd", help="print a launchd plist template to stdout")
    subparsers.add_parser("print-cron", help="print a cron entry template to stdout")
    return parser.parse_args(argv)


def _project_dir(value: str | None) -> str:
    """Resolve project dir from --project or cwd's git root."""
    if value:
        return str(Path(value).resolve())
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    raise lc.RootResolutionError("could not find git repository root")


# -- config resolution (packages/loop-harness/config/loop-harness.yaml) ------------------------


def concurrency_limit(project_dir: str) -> int:
    """Return the configured `lp2.concurrency_limit` (default 2)."""
    config = ld.load_config(project_dir)
    return int(_nested(config, ("lp2", "concurrency_limit"), DEFAULT_CONCURRENCY_LIMIT))


def priority_labels(project_dir: str) -> list[str]:
    """Return the priority-label vocabulary, highest priority first (default: none).

    docs/design/loop-harness-cli.md 3.1 節 mentions a `priority:high`-style label as the
    top sort key "when present" and says the vocabulary is config-defined, but 5.1 節's
    config key tree does not actually enumerate such a key. This implements it as an
    optional `lp2.priority_labels` list so the base behavior without the key stays plain
    `created_at` ascending FIFO (see final report for this design decision).
    """
    config = ld.load_config(project_dir)
    value = _nested(config, ("lp2", "priority_labels"), [])
    return [str(item) for item in value] if isinstance(value, list) else []


def _nested(source: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    """Read a nested mapping value, falling back to default on any miss."""
    current: Any = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


# -- loop-definition-derived discovery parameters (3.1 節: never hardcode the label) ------------


def _trigger_lp2(definition: ld.LoopDefinition) -> dict[str, Any]:
    """Return the `trigger.lp2` mapping, or raise if the definition has none."""
    lp2 = definition.trigger.get("lp2")
    if not isinstance(lp2, dict):
        raise ld.DefinitionValidationError(
            f"{definition.id}: trigger.lp2 is required for scheduler"
        )
    return lp2


def resolve_label(definition: ld.LoopDefinition) -> str:
    """Return the label to poll for, resolved from the loop definition (never hardcoded)."""
    label = _trigger_lp2(definition).get("label")
    if not label:
        raise ld.DefinitionValidationError(f"{definition.id}: trigger.lp2.label is required")
    return str(label)


def resolve_poll_interval(definition: ld.LoopDefinition) -> int:
    """Return the discovery poll interval in seconds for this loop definition."""
    return int(_trigger_lp2(definition).get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS))


# -- discovery (3.1 節) --------------------------------------------------------------------------


def _repo_name_with_owner(project_dir: str) -> str:
    """Return `owner/repo` for the repository at project_dir."""
    completed = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return completed.stdout.strip()


def _gh_list_issues(repo: str, label: str) -> str:
    """Run `gh api .../issues` and return its raw stdout (isolated for test monkeypatching)."""
    completed = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/issues",
            "--method",
            "GET",
            "-f",
            f"labels={label}",
            "-f",
            "state=open",
            "-f",
            "sort=created",
            "-f",
            "direction=asc",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return completed.stdout


def list_labeled_issues(project_dir: str, label: str) -> list[dict[str, Any]]:
    """Return the open Issues carrying label, as parsed `gh api` JSON objects."""
    repo = _repo_name_with_owner(project_dir)
    raw = _gh_list_issues(repo, label)
    data = json.loads(raw) if raw.strip() else []
    return data if isinstance(data, list) else []


def sort_candidates(
    issues: list[dict[str, Any]], priority_order: list[str]
) -> list[dict[str, Any]]:
    """Sort by priority-label rank (lower index = higher priority), then `created_at` ascending."""

    def rank(issue: dict[str, Any]) -> int:
        names = {item.get("name") for item in issue.get("labels", []) if isinstance(item, dict)}
        for index, candidate_label in enumerate(priority_order):
            if candidate_label in names:
                return index
        return len(priority_order)

    return sorted(issues, key=lambda issue: (rank(issue), str(issue.get("created_at") or "")))


def active_loop_ids(project_dir: str) -> set[str]:
    """Return loop_ids whose state.json status is running/waiting_external."""
    root = lc.loop_root(project_dir)
    if not root.is_dir():
        return set()
    active: set[str] = set()
    for entry in root.iterdir():
        state = _try_load_state(entry, project_dir)
        if state is not None and state.status in _ACTIVE_STATUSES:
            active.add(entry.name)
    return active


def _try_load_state(entry: Path, project_dir: str) -> lc.LoopState | None:
    """Load state.json under a `.claude/loop/<loop_id>/` entry, or None if absent/invalid."""
    if not entry.is_dir() or not (entry / "state.json").is_file():
        return None
    try:
        return lc.load_state(entry.name, project_dir)
    except lc.LoopHarnessError:
        return None


def discover_loop_ids(
    project_dir: str,
    definition: ld.LoopDefinition,
    *,
    excluded: frozenset[str] = frozenset(),
) -> list[str]:
    """Discover new loop_ids: labeled open Issues, priority/created_at ordered, minus active ones."""
    label = resolve_label(definition)
    issues = list_labeled_issues(project_dir, label)
    ordered = sort_candidates(issues, priority_labels(project_dir))
    skip = active_loop_ids(project_dir) | excluded
    loop_ids: list[str] = []
    for issue in ordered:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        loop_id = wm.compute_loop_id(project_dir, number)
        if loop_id in skip:
            continue
        loop_ids.append(loop_id)
    return loop_ids


# -- spawn / monitor / restart (3.2, 3.3 節) ------------------------------------------------------


def spawn_worker(loop_id: str, project_dir: str) -> subprocess.Popen[bytes]:
    """Spawn one `loop_driver.py --loop-id <id> --project <path>` child (detached session)."""
    return subprocess.Popen(
        [
            "python3",
            str(_SCRIPT_DIR / "loop_driver.py"),
            "--loop-id",
            loop_id,
            "--project",
            project_dir,
        ],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def should_restart(status: str) -> bool:
    """Return True unless status is a terminal state a human must investigate first (3.3 節)."""
    return status not in _NON_RESTARTABLE_STATUSES


# Mirrors `loop_driver.EXIT_FOREIGN_LEASE`; kept as a local literal instead of importing
# `loop_driver.py` from this sibling script to avoid a script-to-script import dependency.
_EXIT_FOREIGN_LEASE = 3


def lp2_lease_ttl_seconds(project_dir: str) -> int:
    """Return the configured LP-2 lease TTL (also used as the foreign-lease restart cooldown)."""
    config = ld.load_config(project_dir)
    return int(_nested(config, ("lock", "ttl_seconds", "lp2"), 300))


@dataclass
class SchedulerRuntime:
    """In-process worker bookkeeping across poll cycles (not persisted to disk)."""

    workers: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)
    stopped_loop_ids: set[str] = field(default_factory=set)
    # code H4: loop_id -> monotonic deadline before which a foreign-lease-rejected worker must
    # not be respawned (anti restart-storm guard; in-memory/per-scheduler-process only).
    foreign_lease_cooldown_until: dict[str, float] = field(default_factory=dict)


def _respawn_expired_cooldowns(runtime: SchedulerRuntime, project_dir: str) -> list[str]:
    """Respawn any `loop_id` whose foreign-lease cooldown has elapsed (code H4)."""
    now = time.monotonic()
    expired = [
        loop_id
        for loop_id, deadline in runtime.foreign_lease_cooldown_until.items()
        if now >= deadline
    ]
    respawned: list[str] = []
    for loop_id in expired:
        del runtime.foreign_lease_cooldown_until[loop_id]
        if loop_id in runtime.workers or loop_id in runtime.stopped_loop_ids:
            continue
        state = _try_load_state(lc.loop_dir(loop_id, project_dir), project_dir)
        if state is None or not should_restart(state.status):
            continue
        runtime.workers[loop_id] = spawn_worker(loop_id, project_dir)
        respawned.append(loop_id)
    return respawned


def reap_finished_workers(runtime: SchedulerRuntime, project_dir: str) -> list[str]:
    """Poll tracked workers; restart abnormal exits unless failed/stopped. Return respawned ids."""
    respawned = _respawn_expired_cooldowns(runtime, project_dir)
    finished = [loop_id for loop_id, proc in runtime.workers.items() if proc.poll() is not None]
    for loop_id in finished:
        proc = runtime.workers.pop(loop_id)
        if proc.returncode == 0 or loop_id in runtime.stopped_loop_ids:
            continue
        if proc.returncode == _EXIT_FOREIGN_LEASE:
            # The worker never reached `LoopDriver` (rejected at lease acquisition), so
            # `state.json.status` is still "running" (owned by whoever holds the live
            # lease). Respawning immediately every cycle would restart-storm until that
            # foreign lease's own TTL naturally expires; cool down instead.
            runtime.foreign_lease_cooldown_until[loop_id] = (
                time.monotonic() + lp2_lease_ttl_seconds(project_dir)
            )
            continue
        state = _try_load_state(lc.loop_dir(loop_id, project_dir), project_dir)
        if state is None or not should_restart(state.status):
            continue
        runtime.workers[loop_id] = spawn_worker(loop_id, project_dir)
        respawned.append(loop_id)
    return respawned


def spawn_new_workers(
    runtime: SchedulerRuntime, project_dir: str, definition: ld.LoopDefinition
) -> list[str]:
    """Spawn newly discovered workers up to the concurrency cap (3.2 節)."""
    available = concurrency_limit(project_dir) - len(runtime.workers)
    if available <= 0:
        return []
    excluded = runtime.stopped_loop_ids | set(runtime.workers)
    candidates = discover_loop_ids(project_dir, definition, excluded=excluded)
    spawned: list[str] = []
    for loop_id in candidates[:available]:
        runtime.workers[loop_id] = spawn_worker(loop_id, project_dir)
        spawned.append(loop_id)
    return spawned


def run_cycle(runtime: SchedulerRuntime, project_dir: str, definition: ld.LoopDefinition) -> None:
    """One discovery -> cap check -> spawn / monitor / restart poll cycle."""
    reap_finished_workers(runtime, project_dir)
    spawn_new_workers(runtime, project_dir, definition)


# -- startup repo-identity verification (3.4 節: safety stop) ------------------------------------


def verify_repo_identity_at_startup(project_dir: str) -> list[str]:
    """Safety-stop any existing loop whose recorded repo-identity mismatches; return stopped ids."""
    expected = wm.resolve_repo_identity_hash(project_dir)
    root = lc.loop_root(project_dir)
    if not root.is_dir():
        return []
    stopped: list[str] = []
    for entry in sorted(root.iterdir()):
        state = _try_load_state(entry, project_dir)
        if state is None or state.status in _NON_RESTARTABLE_STATUSES:
            continue
        if state.repo_identity_hash == expected:
            continue
        _safe_stop_repo_identity_mismatch(state, project_dir)
        stopped.append(state.loop_id)
    return stopped


def _safe_stop_repo_identity_mismatch(state: lc.LoopState, project_dir: str) -> None:
    """Journal-first -> state -> mandatory macOS notify -> no Issue comment (3.4 節).

    Written directly (not via `loop_driver_support.persist_safe_stop`) because the
    scheduler holds no lease for a loop it is refusing to touch precisely because the
    repository identity looks wrong; a foreign/stale lease token is not evidence this
    write is unsafe the way it is for the driver's own in-flight writes.
    """
    lc.append_journal_event(
        state.loop_id,
        project_dir,
        "stopped",
        "scheduler",
        None,
        {"stop_reason": "repo_identity_mismatch"},
    )
    state.status = "stopped"
    state.stop_reason = "repo_identity_mismatch"
    state.pending_action = None
    state.state_version += 1
    state.updated_at = lc.now_iso()
    lc._write_state(state, project_dir)  # noqa: SLF001 - package-internal writer, see docstring
    lc.emit_loop_audit_event(
        "loop_stop",
        project_dir,
        {
            "loop_id": state.loop_id,
            "phase": state.phase,
            "final_status": "stopped",
            "stop_reason": "repo_identity_mismatch",
            "pr_number": state.pr_number,
        },
    )
    lds.notify_macos("loop-harness", lc.redact(f"{state.loop_id}: repo_identity_mismatch"))
    print(
        lc.redact(f"loop_scheduler: {state.loop_id} stopped (repo_identity_mismatch)"),
        file=sys.stderr,
    )


# -- main poll loop --------------------------------------------------------------------------


def run_scheduler(
    project_dir: str,
    definition_id: str = DEFAULT_DEFINITION_ID,
    *,
    max_cycles: int | None = None,
) -> None:
    """Run the resident scheduler loop (forever unless max_cycles is given for tests)."""
    definition = ld.load_all_definitions(project_dir).get(definition_id)
    if definition is None:
        raise ld.DefinitionValidationError(f"loop definition not found: {definition_id}")
    runtime = SchedulerRuntime()
    runtime.stopped_loop_ids.update(verify_repo_identity_at_startup(project_dir))
    poll_interval = resolve_poll_interval(definition)
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        run_cycle(runtime, project_dir, definition)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(poll_interval)


# -- cron / launchd templates (3.5 節: template generation only, no auto-install) ----------------


def render_launchd_plist(project_dir: str, script_path: Path | None = None) -> str:
    """Render a launchd plist template for `--install-launchd`-style manual setup."""
    script = str(script_path or _SCRIPT_DIR / "loop_scheduler.py")
    project = str(Path(project_dir).resolve())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        "  <string>com.ai-orchestra.loop-scheduler</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        "    <string>/usr/bin/python3</string>\n"
        f"    <string>{script}</string>\n"
        "    <string>--project</string>\n"
        f"    <string>{project}</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{project}/.claude/loop/scheduler.stdout.log</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{project}/.claude/loop/scheduler.stderr.log</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def render_cron_entry(project_dir: str, script_path: Path | None = None) -> str:
    """Render a cron entry template: `pgrep` guard that restarts the scheduler if it died."""
    script = str(script_path or _SCRIPT_DIR / "loop_scheduler.py")
    project = str(Path(project_dir).resolve())
    return (
        f"*/5 * * * * pgrep -f {script} || "
        f"/usr/bin/python3 {script} --project {project} "
        f">> {project}/.claude/loop/scheduler.log 2>&1\n"
    )


if __name__ == "__main__":
    sys.exit(main())
