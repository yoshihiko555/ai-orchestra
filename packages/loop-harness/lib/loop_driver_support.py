#!/usr/bin/env python3
"""LP-2 loop_driver.py helpers: push defense, subprocess control, safe-stop persistence.

This module holds the parts of the LP-2 headless worker that are pure enough (or thin
enough wrappers around subprocess/git) to unit test in isolation, so that
`scripts/loop_driver.py` itself can stay a thin orchestration layer. See
`docs/design/loop-harness-cli.md` 2 節 for the authoritative design this implements.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import loop_common as lc  # noqa: E402

# --- push multi-layer defense: layer 1/2/3 (claude -p command construction) ---------------

# 層1: Maker には push/PR 作成を一切指示しない（プロンプト側の責務。呼び出し側で担保）。
# 層3: push/remote/worktree/gh pr 系は allowedTools の動的組み立てと独立に常に固定 disallow する。
MAKER_FIXED_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash(git push:*)",
    "Bash(git remote:*)",
    "Bash(git worktree:*)",
    "Bash(gh pr:*)",
)

MAKER_BASE_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
)

# 層2 (主軸): Maker/Checker の子プロセス env から push 認証を剥奪する。
# GIT_ASKPASS/GIT_TERMINAL_PROMPT で対話認証を封じ、SSH_AUTH_SOCK と GH_TOKEN/GITHUB_TOKEN、
# GIT_SSH_COMMAND を継承させないことで、bash -c 経由の push であっても認証段階で必ず失敗させる。
_PUSH_AUTH_ENV_KEYS_TO_STRIP: tuple[str, ...] = (
    "SSH_AUTH_SOCK",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GIT_SSH_COMMAND",
)


def maker_env(
    base_env: Mapping[str, str],
    *,
    scratch_home: str | None = None,
    cwd: str | None = None,
) -> dict[str, str]:
    """Return a copy of base_env with push authentication stripped (layer 2, defense-in-depth).

    This isolation applies only to the Maker/Checker `claude -p` child process env; the
    driver's own env (and therefore its own push capability) is untouched.

    Beyond env-var-level credentials (SEC-H3), git can also authenticate via `$HOME`-relative
    paths (`~/.netrc`, `~/.git-credentials` with `credential.helper=store`, macOS Keychain's
    `osxkeychain` helper) or `gh`'s own `~/.config/gh/hosts.yml`. `GIT_CONFIG_GLOBAL`/
    `GIT_CONFIG_SYSTEM` are always redirected to `/dev/null` so no global/system git config
    (including any `credential.helper`) is read at all. If `scratch_home` is given, `HOME`/
    `XDG_CONFIG_HOME` are also redirected there so `~/.netrc`/`gh`'s config directory resolve to
    an empty scratch directory instead of the real one.

    If `cwd` is given, this also resolves the repository's git committer identity (local or
    global config, read from the *ambient*, not-yet-sanitized environment) and threads it
    through as `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL`
    env vars (code F15): once `GIT_CONFIG_GLOBAL` is redirected to `/dev/null` below, the child
    can no longer see `~/.gitconfig`'s `user.name`/`user.email`, so a Maker-authored
    `git commit` would otherwise fail with "Author identity unknown". These are plain env-var
    values only — no credential helper is invoked or threaded through, so this cannot
    reintroduce push authentication.
    """
    env = dict(base_env)
    if cwd is not None:
        env.update(_git_identity_env(cwd))
    env["GIT_ASKPASS"] = "/bin/false"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    for key in _PUSH_AUTH_ENV_KEYS_TO_STRIP:
        env.pop(key, None)
    if scratch_home is not None:
        env["HOME"] = scratch_home
        env["XDG_CONFIG_HOME"] = str(Path(scratch_home) / ".config")
    return env


def _git_identity_env(cwd: str) -> dict[str, str]:
    """Resolve the caller's git committer identity as env-var overrides (code F15).

    Reads via the ambient process environment (not the sanitized copy `maker_env()` is
    building), so a global `~/.gitconfig` `user.name`/`user.email` resolves normally here even
    though the child process's own env will have `GIT_CONFIG_GLOBAL=/dev/null`.
    """
    overrides: dict[str, str] = {}
    name = _git_config_value(cwd, "user.name")
    if name:
        overrides["GIT_AUTHOR_NAME"] = name
        overrides["GIT_COMMITTER_NAME"] = name
    email = _git_config_value(cwd, "user.email")
    if email:
        overrides["GIT_AUTHOR_EMAIL"] = email
        overrides["GIT_COMMITTER_EMAIL"] = email
    return overrides


def _git_config_value(cwd: str, key: str) -> str | None:
    """Return one resolved (local-or-global) git config value, or None if unset/unavailable."""
    try:
        completed = subprocess.run(
            ["git", *hardened_git_config_args(), "-C", cwd, "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


# --- SEC-CRIT (LP-2 2nd-round Codex security review): driver-owned git config hardening ----
#
# The Maker's *other* allowed tools (`Edit`/`Write`, see `MAKER_BASE_ALLOWED_TOOLS` above) are
# never inspected by the layer-3 `maker_bash_guard.py` PreToolUse hook at all -- that hook's
# `matcher` only ever fired for `Bash` tool calls (see its own docstring/2nd-round fix). A Maker
# therefore has a straightforward path that layers 1-3 as documented never accounted for: use
# `Edit` to write directly into the *shared* worktree's `.git/config` and add a
# `[url "<evil>"] insteadOf = <origin-url>` / `pushurl` / `credential.helper` entry there. Every
# later driver-owned git invocation that still resolves "origin" (or authenticates) through that
# same, now-tampered `.git/config` would then silently redirect the driver's own real-credential
# push/`ls-remote` to an attacker-controlled destination, or shell out to an attacker-supplied
# credential helper.
#
# Two complementary fixes close this, applied together (neither is sufficient alone -- see each
# docstring for why):
#   1. `resolve_origin_url()` + `hardened_git_config_args()`: pin the *literal* origin URL once,
#      at the earliest trustworthy moment (right after lease acquisition, before any Maker has
#      had a chance to run in this process -- see `LoopDriver._reconstruct_push_integrity_
#      baseline()`), and pass that literal string directly to every later push/`ls-remote`
#      instead of the bare `"origin"` remote name. This defeats a `remote.origin.url` rewrite
#      outright (there is no longer any remote-name lookup for a rewrite to intercept).
#   2. `find_dangerous_local_git_config()`: even a *literal*, previously-pinned URL is not safe
#      from `url.<base>.insteadOf`/`pushurl` rewriting -- git still rewrites a literal URL
#      argument that matches an `insteadOf` base, regardless of whether it was ever looked up by
#      remote name (empirically confirmed during the 2nd review round: a push using a pinned
#      literal URL was silently redirected to a second, attacker-designated local repo once a
#      matching `insteadOf` entry existed in `.git/config`). This function scans the *actual*
#      current local config immediately before every driver-owned push and lets the caller
#      (`LoopDriver._verify_no_git_config_tampering_or_stop`) hard-stop the loop the instant any
#      of these keys is present, closing the gap fix (1) alone cannot.


def hardened_git_config_args() -> list[str]:
    """Return `-c` overrides every driver-owned git invocation should apply (SEC-CRIT).

    `credential.helper=`: clears any credential helper `.git/config` might define (pre-existing,
    or Maker-written via `Edit`), so a driver-owned git invocation started with the driver's own
    real push credentials never shells out to an attacker-supplied helper command.

    Deliberately does *not* include a blanket `protocol.file.allow=never` here, even though an
    `insteadOf` rewrite could in principle redirect a push/fetch to a local `file://` path: this
    package's own test suite (and some legitimate on-disk "origin" setups) intentionally uses
    local-path remotes, so disabling the file transport outright would reject those too,
    indiscriminately, alongside any actual attack (confirmed empirically: `git -c
    protocol.file.allow=never push <local-path-remote> ...` fails with "transport 'file' not
    allowed" even with no tampering involved at all). The `insteadOf`/`pushurl` attack vector is
    instead closed directly and protocol-agnostically by `find_dangerous_local_git_config()`
    below, which every driver-owned push call site consults immediately beforehand regardless of
    what protocol a rewrite target would use.
    """
    return ["-c", "credential.helper="]


def resolve_origin_url(cwd: str, timeout_seconds: float = 10.0) -> str | None:
    """Resolve and return `origin`'s literal configured URL once, for trusted reuse (SEC-CRIT).

    Callers should invoke this exactly once per driver process, at the earliest trustworthy
    point (right after lease acquisition, before any Maker child has had a chance to run in this
    process -- see `LoopDriver._reconstruct_push_integrity_baseline()`), cache the result on the
    driver instance, and thread it through to every later driver-owned push/`ls-remote` call
    (`get_remote_head()`'s `origin_url` parameter) as an explicit URL argument instead of the
    bare `"origin"` remote name. See the module-level SEC-CRIT comment above for the full
    rationale and why this is only one of two complementary fixes.

    Returns `None` if the URL cannot be resolved (e.g. no `origin` remote configured, or the
    query itself errors/times out) -- callers must fail closed on `None` (fall back to the bare
    `"origin"` name only when there is truly no better option, never silently trust a `None` as
    if it were a resolved value).
    """
    try:
        completed = subprocess.run(
            [
                "git",
                *hardened_git_config_args(),
                "-C",
                cwd,
                "config",
                "--get",
                "remote.origin.url",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


_DANGEROUS_LOCAL_CONFIG_KEY_RE = re.compile(
    r"insteadof|pushurl|credential\.helper|^alias\.", re.IGNORECASE
)
"""Local git-config *keys* (not values) that must never be present in the shared worktree's
`.git/config` before a driver-owned push/`ls-remote` (SEC-CRIT). Mirrors the same key families
`maker_bash_guard.py`'s SC3/H1 deny patterns target for Maker *Bash* commands, but checked here
against the actual current config state (not a text scan of a command string), which is what
catches a Maker `Edit`-write into `.git/config` directly -- something the Bash-only hook can
never see at all."""


def find_dangerous_local_git_config(cwd: str, timeout_seconds: float = 10.0) -> str | None:
    """Return the first dangerous local git-config key found, or None if it looks clean (SEC-CRIT).

    Called by `LoopDriver._verify_no_git_config_tampering_or_stop()` immediately before every
    driver-owned push/`ls-remote` against the shared worktree. Reads via `git config --local
    --list` (this repo's own `.git/config`, not global/system) with `hardened_git_config_args()`
    applied, so listing the config cannot itself invoke a rogue credential helper. Matched
    line-by-line against `_DANGEROUS_LOCAL_CONFIG_KEY_RE`: not a full git-config parser (a
    multi-line config value containing an embedded newline could in principle confuse this
    simple per-line split), mirroring the same "text scan, not full parse" tradeoff
    `maker_bash_guard.py` already documents for its own Bash-command scan -- a false positive
    (an unrelated key merely containing one of these substrings) fails safe (refuses the push),
    which is the accepted direction of error here.

    Returns `None` (fail-open) only when the query itself cannot be completed at all (process
    error/timeout) -- this is one of several layers (see the push call sites), not the sole
    guard, so a transient failure here must not itself deadlock every push.
    """
    try:
        completed = subprocess.run(
            ["git", *hardened_git_config_args(), "-C", cwd, "config", "--local", "--list"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        key = line.split("=", 1)[0]
        if _DANGEROUS_LOCAL_CONFIG_KEY_RE.search(key):
            return key
    return None


def maker_scratch_home(project_dir: str, loop_id: str) -> str:
    """Return (creating if absent) an isolated `$HOME` scratch dir for one loop's child procs.

    Reused across a loop's Maker/Checker/LLM-reviewer child processes (SEC-H3) so credential
    lookups relative to `$HOME` (`~/.netrc`, `~/.git-credentials`, `gh`'s hosts.yml) resolve to
    an empty directory instead of the driver's real home. Lives under the per-loop state
    directory so `loop_status.py purge`'s existing directory-tree removal cleans it up too.

    Also copies the operator's existing Claude Code OAuth session (`~/.claude.json`,
    `~/.claude/.credentials.json`) into the scratch dir on every call (code F14, FT-17 "must":
    headless Maker/Checker/reviewer `claude -p` children must be able to authenticate using the
    operator's existing `claude` login) — without giving them any of the git/gh push
    credentials this scratch `$HOME` otherwise isolates from (SEC-H3): `~/.netrc`,
    `~/.git-credentials`, and `~/.config/gh` are never copied here.

    G6 (PR #210 review round 3) defense-in-depth: copying live OAuth credentials under the
    *root worktree's* `.claude/loop/` tree means a careless `git add -A`/`git add .` in that
    worktree could stage them if `.claude/loop/` were ever untracked-but-not-ignored. This
    call always (re)writes `.claude/loop/.gitignore` (`*`) first — see
    `_ensure_loop_root_gitignore()` — so the whole per-loop state tree, including this
    `maker_home/` copy, is excluded from `git add` regardless of the operator's own
    `.gitignore` setup. This does not address the complementary risk that the Maker's own
    sandboxed `claude -p` process can read its own `$HOME` (and therefore these credential
    files): that access is inherent to giving the Maker a working `claude` login at all
    (FT-17) and is not something a repo-side `.gitignore` can close.
    """
    _ensure_loop_root_gitignore(project_dir)
    path = lc.loop_dir(loop_id, project_dir) / "maker_home"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    _copy_claude_auth_files(Path(os.path.expanduser("~")), path)
    return str(path)


def _ensure_loop_root_gitignore(project_dir: str) -> None:
    """Ensure `<root worktree>/.claude/loop/.gitignore` (`*`) exists (G6 defense-in-depth).

    Idempotent and safe under concurrent callers: content is fixed, so a re-write by a
    second worker racing this one is a no-op in effect. This is independent of whatever the
    operator's own top-level `.gitignore` does or doesn't cover, so `maker_home/`'s copied
    OAuth credentials (code F14) stay excluded from `git add -A`/`git add .` even in a fresh
    checkout that has never customized its `.gitignore`.
    """
    root = lc.loop_root(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text("*\n", encoding="utf-8")


def _copy_claude_auth_files(real_home: Path, scratch_home: Path) -> None:
    """Copy only Claude Code auth files from real_home into scratch_home (code F14).

    No-op for any file that does not exist at the source: a fresh CI/sandbox environment
    without a prior `claude` login still gets a usable (just unauthenticated) scratch dir
    instead of failing `loop_driver.py` outright. Never copies `~/.netrc`, `~/.git-credentials`,
    or `~/.config/gh` (git/gh push credentials, which this scratch `$HOME` must stay isolated
    from per SEC-H3).
    """
    claude_json = real_home / ".claude.json"
    if claude_json.is_file():
        _copy_file_0600(claude_json, scratch_home / ".claude.json")
    credentials = real_home / ".claude" / ".credentials.json"
    if credentials.is_file():
        target_dir = scratch_home / ".claude"
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target_dir, 0o700)
        _copy_file_0600(credentials, target_dir / ".credentials.json")


def _copy_file_0600(source: Path, target: Path) -> None:
    """Copy a file's contents with 0600 permissions (never inherits the source's own mode)."""
    target.write_bytes(source.read_bytes())
    os.chmod(target, 0o600)


def _command_prefix(command: str) -> str | None:
    """Return the first whitespace-delimited token of a mechanical command, if any."""
    token = command.strip().split(" ", 1)[0] if command.strip() else ""
    return token or None


def build_allowed_tools(mechanical_commands: Sequence[str]) -> str:
    """Build the --allowedTools value from the base list plus a dynamic mechanical whitelist.

    Push/PR/worktree commands are never added here regardless of loop definition content;
    they are excluded independently via `build_disallowed_tools()` (layer 3).
    """
    allowed = list(MAKER_BASE_ALLOWED_TOOLS)
    for command in mechanical_commands:
        prefix = _command_prefix(command)
        if prefix is None:
            continue
        entry = f"Bash({prefix} *)"
        if entry not in allowed:
            allowed.append(entry)
    return ",".join(allowed)


def build_disallowed_tools() -> str:
    """Return the fixed --disallowedTools value (layer 3)."""
    return ",".join(MAKER_FIXED_DISALLOWED_TOOLS)


def _maker_hook_script_path() -> Path:
    """Absolute path to the layer-3 PreToolUse Bash-guard hook script (`maker_bash_guard.py`).

    Lives alongside this module so packaging/distribution always ships them together;
    resolved via `__file__` (not a config lookup) so it works regardless of which
    project/worktree `loop_driver.py` is invoked from.
    """
    return _LIB_DIR / "maker_bash_guard.py"


def _maker_hook_settings_dict() -> dict[str, Any]:
    """Return the `claude -p --settings` JSON dict wiring in the layer-3 guard hook.

    SEC-CRIT (LP-2 2nd-round Codex security review): the matcher used to be `"Bash"` only, so
    `maker_bash_guard.py` never even saw an `Edit`/`Write` tool call -- a Maker could write
    directly into the shared worktree's `.git/config` (see the SEC-CRIT comment above
    `hardened_git_config_args()` in this module) without the layer-3 hook being invoked at all.
    `maker_bash_guard.py` now also hard-denies `Edit`/`Write` writes anywhere under a `.git`
    path component (`is_git_metadata_path()`), so the matcher is widened to `"Bash|Edit|Write"`
    to route those tool calls through the same hook script too.
    """
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{sys.executable} {_maker_hook_script_path()}",
                        }
                    ],
                }
            ]
        }
    }


@functools.lru_cache(maxsize=1)
def maker_hook_settings_path() -> str:
    """Materialize (once per process) the layer-3 hook settings JSON and return its path.

    Memoized: the settings content only depends on this module's own file location, so every
    `build_claude_p_command()` call within one `loop_driver.py` process (one loop run = one
    process; design doc 2.1 節) shares the same scratch file instead of writing a fresh temp
    file for every Maker/Checker/reviewer child it launches. Tests can call
    `maker_hook_settings_path.cache_clear()` to force regeneration.
    """
    directory = tempfile.mkdtemp(prefix="loop-harness-maker-hook-")
    path = Path(directory) / "settings.json"
    path.write_text(json.dumps(_maker_hook_settings_dict()), encoding="utf-8")
    return str(path)


def build_claude_p_command(
    prompt: str,
    *,
    allowed_tools: str,
    add_dirs: Sequence[str],
    claude_bin: str = "claude",
) -> list[str]:
    """Build the full `claude -p` argv for a Maker/Checker headless run.

    Always injects `--settings <layer-3 hook settings file>` alongside the fixed
    `--disallowedTools`, wiring in the `maker_bash_guard.py` PreToolUse hook (design doc 2.2 節
    層3; EV-49/EV-63) that hard-denies Bash push/remote/gh-pr commands even when wrapped in
    `bash -c "..."` — a bypass `--disallowedTools`'s literal-prefix match alone cannot catch.
    """
    cmd = [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        allowed_tools,
        "--disallowedTools",
        build_disallowed_tools(),
        "--settings",
        maker_hook_settings_path(),
    ]
    for add_dir in add_dirs:
        cmd.extend(["--add-dir", add_dir])
    cmd.append(prompt)
    return cmd


class ClaudePTimeoutError(RuntimeError):
    """Raised when a `claude -p` child process is killed after exceeding its timeout."""


def kill_process_tree(pid: int, term_wait_seconds: float = 10.0) -> None:
    """Escalate SIGTERM -> (wait) -> SIGKILL to an entire process group."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + term_wait_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_claude_p(
    cmd: list[str],
    cwd: str,
    timeout_seconds: float,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run one non-interactive `claude -p` child, kill-tree on timeout.

    Note (code M2): `LoopDriver._run_child()` in `loop_driver.py` does not call this function
    in production; it needs to additionally register the child pid (`_set_current_child`)
    under `_child_lock` for heartbeat-triggered kill-tree (see code H3), which this simpler,
    pid-tracking-free variant cannot express. Kept as a standalone, independently-testable
    reference implementation of the kill-tree/non-interactive-subprocess contract rather than
    unified with `_run_child`, to avoid adding risk to the H3 locking fix.

    stdin is always DEVNULL so a hung `claude -p` never blocks on stdin. The child is
    started in its own process group (`start_new_session=True`) so `kill_process_tree`
    can reach any grandchildren (e.g. `pytest`/`git` spawned by the Bash tool).
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=dict(env),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_TERM_WAIT_GRACE_SECONDS)
        raise ClaudePTimeoutError(f"claude -p timed out after {timeout_seconds}s") from None


_TERM_WAIT_GRACE_SECONDS = 5.0


def parse_claude_p_json(stdout: str) -> dict[str, Any]:
    """Parse `claude -p --output-format json` stdout into its JSON object."""
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("claude -p --output-format json did not return a JSON object")
    return data


# --- push multi-layer defense: layer 4 (post-push integrity verification) -----------------


REMOTE_HEAD_ABSENT = "<remote-branch-absent>"
"""Sentinel `get_remote_head()` return value for a *confirmed* absence (Issue F6 / PR #210
review): `git ls-remote` completed successfully but found no ref for `branch` on `origin` at
all (e.g. a brand-new loop's branch that has never been pushed yet). Deliberately distinct
from `None`, which means the query itself could not be completed (non-zero exit, timeout, or
`OSError`) and is therefore unverifiable, not a confirmed answer. `classify_push_integrity()`
relies on this distinction: without it, a new Issue loop's first `advance_phase` would see
`baseline_head=None, current_head=None` (both "confirmed absent" collapsed into "unknown") and
fail-closed into `push_integrity_unverifiable` forever, blocking every first-push new-Issue
loop. Not a valid sha (non-hex), so it can never collide with a real commit sha."""


def get_remote_head(
    cwd: str, branch: str, *, origin_url: str | None = None, timeout_seconds: float = 10.0
) -> str | None:
    """Return the current remote HEAD sha for branch (Issue F6: three distinguishable outcomes).

    - a real sha string: `git ls-remote` succeeded and found `branch` on `origin`.
    - `REMOTE_HEAD_ABSENT`: `git ls-remote` succeeded but `branch` does not exist on `origin`
      yet -- a confirmed, verifiable absence.
    - `None`: the query itself could not be completed (process error, timeout, or non-zero
      exit) -- unverifiable; callers must fail closed on this case (SEC-H1).

    `origin_url` (SEC-CRIT): when given, queried directly instead of the bare `"origin"` remote
    name -- pass the process's own pre-resolved, trusted URL (`resolve_origin_url()`, cached on
    the driver instance) so a `.git/config` rewrite of what `"origin"` resolves to (Maker `Edit`
    write, see the module-level SEC-CRIT comment above `hardened_git_config_args()`) after that
    resolution point cannot redirect this query. Falls back to the bare `"origin"` name when
    omitted/`None`, matching the pre-fix behavior (e.g. call sites that have no cached URL yet).
    """
    remote = origin_url if origin_url else "origin"
    try:
        completed = subprocess.run(
            ["git", *hardened_git_config_args(), "ls-remote", remote, f"refs/heads/{branch}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    stdout = completed.stdout.strip()
    if not stdout:
        return REMOTE_HEAD_ABSENT
    first_line = next(iter(stdout.splitlines()), "")
    sha = first_line.split("\t", 1)[0].strip() if first_line else ""
    return sha or REMOTE_HEAD_ABSENT


def classify_push_integrity(baseline_head: str | None, current_head: str | None) -> str:
    """Classify layer-4 push integrity as `"ok"` / `"violation"` / `"unverifiable"` (SEC-H1).

    `current_head is None` (e.g. `git ls-remote` sabotaged/failing) is fail-closed:
    `"unverifiable"`, never silently `"ok"`, so an attacker cannot wave a push through by
    disrupting remote-HEAD verification. `baseline_head is None` (no baseline recorded yet,
    e.g. right after a crash-restart before reconstruction runs) is also `"unverifiable"`
    rather than "assume ok", for the same reason; callers should reconstruct the baseline
    right after `attach()` (see `loop_driver.py`) so this case is rare on the hot path.

    `get_remote_head()`'s `REMOTE_HEAD_ABSENT` sentinel (a *confirmed* "branch does not exist
    on origin" answer, as opposed to `None`'s "the query failed") needs no special-casing here:
    when both sides equal `REMOTE_HEAD_ABSENT` (a brand-new loop's first push -- nothing has
    ever landed on `origin` for this branch), the plain equality check below already returns
    `"ok"`. When only one side is `REMOTE_HEAD_ABSENT`, it already returns `"violation"` (e.g.
    the branch appeared on `origin` without this driver having pushed it). Only an
    actually-failed query (`None`) stays fail-closed; a confirmed absence does not (Issue F6 /
    PR #210 review), otherwise every first-push new-Issue loop would be blocked forever.
    """
    if baseline_head is None or current_head is None:
        return "unverifiable"
    if current_head == baseline_head:
        return "ok"
    return "violation"


def detect_push_integrity_violation(baseline_head: str | None, current_head: str | None) -> bool:
    """Return True when remote HEAD advanced past baseline without the driver pushing.

    Pure comparison (layer 4 of the push multi-layer defense, EV-80). Thin wrapper around
    `classify_push_integrity()`: only the `"violation"` classification is True here: both
    `"unverifiable"` (missing baseline/current) and `"ok"` are False, matching this function's
    original "not a violation" contract for either side being unknown (callers that need to
    additionally fail-closed on `"unverifiable"` should call `classify_push_integrity()`
    directly, as `loop_driver.py`'s `_run_advance_phase` now does).
    """
    return classify_push_integrity(baseline_head, current_head) == "violation"


# --- push multi-layer defense: secret-leak scan (SH5, additional safety net) ---------------

# Generic vendor token prefixes checked against a pending push's diff regardless of provenance
# (SH5). Intentionally a plain substring check, not a full-shape regex (unlike
# `loop_common.SECRET_PATTERNS`, which this list deliberately does not replace/duplicate): the
# goal here is a fast, cheap, low-false-negative last-mile check immediately before a
# driver-owned push, not the canonical secret-redaction pass `loop_common.redact()` already
# applies to artifacts/journal/audit output elsewhere.
_SECRET_TOKEN_PREFIXES: tuple[str, ...] = ("sk-ant-", "ghp_", "gho_", "github_pat_")

_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
"""Git's well-known empty-tree object hash: diffing against it yields "the whole tree" (used
when there is no prior baseline commit to diff from, e.g. a brand-new branch's first push)."""


def get_push_diff(cwd: str, baseline_head: str | None, timeout_seconds: float = 30.0) -> str | None:
    """Return the diff of everything a pending push is about to send (SH5), or None on error.

    `baseline_head` is the layer-4 push-integrity baseline (the last known-good remote HEAD):
    when it is a real sha, diffs `baseline_head..HEAD` (only the commits this push newly
    contributes). When it is `None` (no baseline known) or `REMOTE_HEAD_ABSENT` (a confirmed
    brand-new branch that has never been pushed), there is no meaningful prior point to diff
    from, so this diffs HEAD against git's empty-tree object instead (the whole current tree).

    Returns `None` (not raising) on any git failure/timeout: this is an additional safety net
    on top of the existing 4-layer push defense, not the sole guard, so a transient `git diff`
    hiccup must not itself block every push.
    """
    if baseline_head is not None and baseline_head != REMOTE_HEAD_ABSENT:
        diff_range = f"{baseline_head}..HEAD"
    else:
        diff_range = f"{_EMPTY_TREE_SHA}..HEAD"
    try:
        completed = subprocess.run(
            ["git", *hardened_git_config_args(), "-C", cwd, "diff", diff_range],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def extract_known_secrets(scratch_home: str) -> list[str]:
    """Return credential-bearing string values copied into `scratch_home` (SH5).

    Feeds `find_leaked_secret()`'s exact-value check: a Maker that copies its own scratch-
    `$HOME` OAuth credential file contents into a tracked file and commits them would otherwise
    only be caught (if at all) by the generic-prefix check, which cannot recognize a live
    Claude Code session token by shape alone. Best-effort: a missing/unreadable/non-JSON file
    yields fewer (or zero) values rather than raising, since this feeds a defense-in-depth scan
    rather than being a hard dependency the driver must have to keep operating. Short strings
    (under 16 chars) are dropped as not credential-shaped, to avoid every scan trivially
    "matching" on some short, non-secret config value.
    """
    values: list[str] = []
    for relative_path in (Path(".claude") / ".credentials.json", Path(".claude.json")):
        values.extend(_string_leaves(_read_json_best_effort(Path(scratch_home) / relative_path)))
    return [value for value in values if len(value) >= 16]


def _read_json_best_effort(path: Path) -> Any:
    """Return path's parsed JSON contents, or None if missing/unreadable/not valid JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _string_leaves(value: Any) -> list[str]:
    """Recursively collect every string leaf value out of a nested JSON-ish structure."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


def find_leaked_secret(diff_text: str, known_secrets: Sequence[str]) -> str | None:
    """Return a redaction-safe label for the first leaked secret found in `diff_text` (SH5).

    Checks two independent signal families, in order:
    1. `known_secrets` (exact scratch-credential values, see `extract_known_secrets()`): a
       substring match here means the Maker exfiltrated its own live scratch-`$HOME` OAuth
       credentials into a commit.
    2. `_SECRET_TOKEN_PREFIXES`: generic vendor token prefixes that look like a real credential
       regardless of where it came from.
    Returns `None` (not the matched text) when nothing is found; the returned label never
    contains the secret value itself, so a caller can safely log/journal/notify with it.
    """
    for secret in known_secrets:
        if secret and secret in diff_text:
            return "scratch_credential_leak"
    for prefix in _SECRET_TOKEN_PREFIXES:
        if prefix in diff_text:
            return f"token_prefix_leak:{prefix}"
    return None


# --- wall-clock monitoring -----------------------------------------------------------------


def wall_clock_exceeded(start_monotonic: float, timeout_seconds: int) -> bool:
    """Return True once timeout_seconds have elapsed since start_monotonic."""
    return (time.monotonic() - start_monotonic) >= timeout_seconds


def apportioned_timeout(remaining_seconds: float, fixed_cap_seconds: int) -> float:
    """Return the per-child timeout apportioned from wall-clock remaining time (design 2.5 節).

    Never exceeds `fixed_cap_seconds`; never negative (`remaining_seconds <= 0` yields `0`,
    signaling to the caller that the wall-clock budget is already exhausted and no child
    process should be spawned at all).
    """
    return max(min(fixed_cap_seconds, remaining_seconds), 0)


# --- safe-stop persistence (journal-first, state-after) ------------------------------------


def _persist_forced_terminal(
    loop_id: str,
    project_dir: str,
    lease_token: str,
    action_id: str | None,
    *,
    journal_event: str,
    status: str,
    stop_reason: str,
    payload: dict[str, Any],
) -> None:
    """Write a driver-forced terminal status directly (journal first, then state).

    Used when the driver must abandon the normal propose/complete two-phase cycle for a
    condition that cycle cannot express as a pending-action result (there is no untrusted
    caller between "compute the outcome" and "persist it", so this does not need the
    byte-for-byte artifact re-validation `loop_step.py` applies to its untrusted CLI
    callers). This mirrors the existing internal write order used by
    `loop_common._persist_preproposal_stop` (durable journal event before `state.json` is
    updated).

    Raises `loop_common.WriteRejectedError` if the caller-held lease is no longer valid
    (lease fencing: never write state/journal without a live lease). Lease validation and
    the journal/state writes below run inside `lc.guarded_lease_section()` so a concurrent
    lease reacquisition by another worker cannot race between the validity check and the
    write (code review #3).
    """
    with lc.guarded_lease_section(loop_id, project_dir, lease_token):
        lc.append_journal_event(
            loop_id,
            project_dir,
            journal_event,
            "driver",
            action_id,
            {"stop_reason": stop_reason, **payload},
        )
        state = lc.load_state(loop_id, project_dir)
        state.status = status
        state.stop_reason = stop_reason
        state.pending_action = None
        state.state_version += 1
        state.updated_at = lc.now_iso()
        lc._write_state(state, project_dir)  # noqa: SLF001 - package-internal writer, see docstring


def persist_safe_stop(
    loop_id: str,
    project_dir: str,
    lease_token: str,
    action_id: str | None,
    stop_reason: str,
    payload: dict[str, Any],
) -> None:
    """Persist a safety stop (`status="stopped"`) for a driver-detected condition.

    Currently used for `push_integrity_violation` (layer 4 of the push multi-layer
    defense), which the two-phase propose/complete cycle has no result shape for.
    """
    _persist_forced_terminal(
        loop_id,
        project_dir,
        lease_token,
        action_id,
        journal_event="stopped",
        status="stopped",
        stop_reason=stop_reason,
        payload=payload,
    )


def persist_forced_failure(
    loop_id: str,
    project_dir: str,
    lease_token: str,
    action_id: str | None,
    stop_reason: str,
    payload: dict[str, Any],
) -> None:
    """Persist a forced failure (`status="failed"`), e.g. wall-clock timeout (not a safety stop).

    Unlike a safety stop, this is a normal failure exit: `on_failure.exec` (Draft PR, etc.)
    still runs afterwards.
    """
    _persist_forced_terminal(
        loop_id,
        project_dir,
        lease_token,
        action_id,
        journal_event=stop_reason,
        status="failed",
        stop_reason=stop_reason,
        payload=payload,
    )


# --- notifications / issue comments (best-effort, non-fatal) -------------------------------


def notify_macos(title: str, message: str) -> bool:
    """Best-effort macOS notification; never raises."""
    try:
        script = (
            f'display notification "{_escape_applescript(message)}" '
            f'with title "{_escape_applescript(title)}"'
        )
        completed = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _escape_applescript(value: str) -> str:
    """Escape a string for embedding in an AppleScript string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def post_issue_comment(cwd: str, issue_number: int, body: str) -> bool:
    """Best-effort `gh issue comment`; never raises."""
    try:
        completed = subprocess.run(
            ["gh", "issue", "comment", str(issue_number), "--body", body],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


# --- misc -----------------------------------------------------------------------------------


_ISSUE_LOOP_ID_RE = re.compile(r"^[0-9a-f]{8}-issue-([1-9][0-9]*)$")


def issue_number_from_loop_id(loop_id: str) -> int | None:
    """Parse the issue number out of the canonical `issue-loop` loop id, if present."""
    match = _ISSUE_LOOP_ID_RE.fullmatch(loop_id)
    return int(match.group(1)) if match is not None else None
