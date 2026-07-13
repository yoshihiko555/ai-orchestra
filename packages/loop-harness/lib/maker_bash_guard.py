#!/usr/bin/env python3
"""PreToolUse hook (layer 3, defense-in-depth): hard-deny Bash push/PR-mutation bypasses.

Injected into a Maker/Checker `claude -p` child process via `--settings` (see
`maker_hook_settings_path()` / `build_claude_p_command()` in
`packages/loop-harness/lib/loop_driver_support.py`). Design: `docs/design/loop-harness-cli.md`
2.2 節「多層防御」層3. Evaluation: `docs/evaluation/loop-harness.md` EV-49 / EV-63.

Why this exists: `--disallowedTools "Bash(git push:*)"` only pattern-matches the literal
command-string *prefix* Claude Code sees for a Bash tool call. It reliably blocks Maker
directly calling `git push ...`, but a wrapped invocation such as
`bash -c "git push origin main"` has a *different* literal prefix ("bash -c ...") and slips
through this check — confirmed by real-world testing (EV-63; a bare-remote push actually
went through in that experiment). This hook inspects the *entire* Bash command string
(including any `bash -c` / `sh -c` / `eval` payload embedded inside it, since that payload is
just more text inside the outer command string regardless of quoting depth) for push/remote/
PR-mutation patterns and hard-denies the tool call outright (exit code 2), regardless of how
the dangerous subcommand is wrapped.

This hook intentionally trades precision for safety: it does not attempt to fully parse shell
syntax, so a benign command that merely *mentions* one of these phrases inside a string
argument (e.g. a commit message reading "please run git push later") will also be denied.
That is an accepted false positive — the Maker never legitimately needs to actually run any of
these commands, so refusing to run something merely because its text looks like one is
strictly safer than trying to reason about shell semantics precisely and risking a false
negative instead (a real bypass slipping through undetected). Obfuscation (base64 encoding,
building the string up piece-by-piece via variable expansion, etc.) can still evade this text
scan; that is a documented limit of this layer (see the design doc), which is why this is
layer 3 of 4 rather than the sole safeguard (layer 2's env-level credential stripping is the
structural guarantee; this layer and layer 4 are additional safety nets).

Protocol: reads stdin per Claude Code's PreToolUse hook contract — a single JSON object with
(among other fields) `tool_name` and `tool_input`. For the `Bash` tool, `tool_input.command`
holds the full command string. Non-Bash tool calls, and any input this hook cannot make sense
of, are always allowed (exit 0): the injected settings' `matcher: "Bash"` should already
restrict when Claude Code invokes this hook at all, and a hook-protocol hiccup is an
infrastructure concern, not a security signal (layers 1/2/4 remain regardless).

Deny -> stderr message + exit code 2 (Claude Code's PreToolUse "block this tool call"
contract). Allow -> exit code 0, no stdout output.

Stdlib only, no imports from elsewhere in this repository: this script's absolute path is
baked verbatim into the `--settings` JSON that `loop_driver_support.py` generates, so it must
keep working unmodified no matter which project/worktree it is invoked from — it never needs
`AI_ORCHESTRA_DIR` or any project config at run time.
"""

from __future__ import annotations

import json
import re
import sys

# Deny patterns are matched against the *entire* raw command string (not a shell-aware
# tokenization) so that any wrapper depth (`bash -c "..."`, `sh -c '...'`, `eval "..."`, ...) is
# caught: the wrapped payload is embedded verbatim as text inside the outer command string
# regardless of how many quoting layers surround it.
#
# `_SEP` is the separator between tokens in a denied invocation. Plain whitespace is the common
# case, but shell IFS-substitution bypasses (`git${IFS}push`, `git$IFS'push'`, ...) replace the
# literal space character entirely while keeping the exact same meaning to the shell, so a
# `\s+`-only separator let e.g. `git${IFS}push` slip through undetected (SC2). `${IFS}`/`$IFS`
# are matched here as literal text (this hook does no shell parsing/expansion of its own); a
# trailing run of quote characters is also absorbed since `$IFS'push'` (an unbraced `$IFS`
# immediately followed by a quote, a common idiom to stop word-splitting ambiguity) is a very
# common form of this bypass — the `\b` word boundary immediately before/after the denied verb
# in each pattern below still matches correctly regardless of an adjacent quote character.
_SEP = r"(?:\s|\$\{IFS\}|\$IFS)+[\"']*"


def _filler(max_tokens: int) -> str:
    """Return a non-greedy `{0,max_tokens}` filler-token group, separated by `_SEP`.

    Allows a small, bounded number of intervening tokens (git global options like `-c foo=bar`
    or `--git-dir=...`, or wrapper tokens like `bash -c`) between the leading binary name and
    the denied subcommand, without being able to cross an actual shell command separator (`;`,
    `&&`, `||`, `|`): those separator characters are excluded from the filler token's character
    class, so e.g. "git status && git push" is still caught (via the second, separator-free
    "git push" occurrence) while filler expansion cannot itself "hop over" a separator to
    falsely fuse two unrelated statements into one match.
    """
    return rf"(?:{_SEP}[^\s;&|]+){{0,{max_tokens}}}?"


_DENY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, flags)
    for pattern, flags in (
        (rf"\bgit\b{_filler(8)}{_SEP}push\b", 0),  # git push (incl. `git -c x=y push`, wrappers)
        (
            rf"\bgit\b{_filler(8)}{_SEP}remote\b",
            0,
        ),  # git remote add/set-url/... (repoint push target)
        (rf"\bgit\b{_filler(8)}{_SEP}send-pack\b", 0),  # git send-pack (low-level push transport)
        # SC1: the low-level transport binaries can also be invoked directly as a single,
        # hyphenated token (`git-send-pack`/`git-receive-pack`/`git-upload-pack`, as e.g. the
        # ssh push/fetch transport does) with no literal "git " prefix + whitespace before the
        # subcommand word at all, so the `\bgit\b{_filler}{_SEP}send-pack\b`-shaped patterns
        # above never get a chance to match: `\bgit\b` itself already matches inside
        # "git-send-pack" (word boundary at the `t`/`-` transition), but the patterns above all
        # require at least one `_SEP` between "git" and the subcommand, which a bare hyphen is
        # not. Denied as their own standalone, prefix-free patterns instead.
        (r"\bgit-send-pack\b", 0),
        (r"\bgit-receive-pack\b", 0),
        (r"\bgit-upload-pack\b", 0),  # fetch transport, denied by association (defense-in-depth)
        (rf"\bgit\b{_filler(8)}{_SEP}worktree\b", 0),  # align w/ MAKER_FIXED_DISALLOWED_TOOLS
        # H1: `git -c alias.<name>=<value> <name> ...` defines a *temporary* (this-invocation-only)
        # alias and immediately invokes it, so a deny-verb value (e.g. `-c alias.p=push`) never
        # appears as a literal `push`/`remote`/... token the patterns above can match. Rather than
        # try to parse/resolve the alias's own value (which can itself be obfuscated further, e.g.
        # `-c alias.p='!git push'`), fail closed on the `-c alias.` construct itself: the Maker has
        # no legitimate need to define any git alias, temporary or not.
        (rf"\bgit\b{_filler(8)}{_SEP}-c{_SEP}alias\.", 0),  # git -c alias.<name>=... (temporary)
        # H1: `git config alias.<name> <value>` (optionally `--global`/`--local`/`--add`/...)
        # defines a *persistent* alias in gitconfig, which a later, separate Bash call could then
        # invoke under an innocuous-looking name. Same fail-closed rationale as above.
        (
            rf"\bgit\b{_filler(4)}{_SEP}config\b{_filler(4)}{_SEP}alias\.",
            0,
        ),  # git config alias.<name>
        # SC3: `git config url.<base>.insteadOf`/`pushUrl`/`remote.<name>.pushurl` can repoint
        # where a later `git push` in the *driver's own* subsequent invocation actually lands
        # (shared `.git/config` mutation), silently redirecting the driver's push to an
        # attacker-controlled remote without ever touching a `push`/`remote` literal token
        # itself. Case-insensitive substring match: git config keys are case-insensitive and
        # this hook does no config-key parsing of its own, so matching the substring anywhere
        # (regardless of surrounding `git config`/`git -c` wrapper shape) is the fail-closed
        # choice here.
        (r"insteadof", re.IGNORECASE),
        (r"pushurl", re.IGNORECASE),
        # SC3: deny `git config` wholesale (not just the `alias.`/`insteadOf`/`pushurl` special
        # cases above) — the Maker never legitimately needs to read or write any git config at
        # all (its committer identity is already env-injected via `GIT_AUTHOR_*`/
        # `GIT_COMMITTER_*`, see `loop_driver_support.maker_env()`), so refusing every `git
        # config` invocation outright is strictly safer than trying to enumerate every
        # config key that could repoint a push or otherwise sabotage the repo.
        (rf"\bgit\b{_filler(8)}{_SEP}config\b", 0),
        # SC3: `git -c url.<base>.insteadOf=<evil> <anything>` rewrites the push/fetch URL for
        # the *duration of this one invocation* without ever using the word "config" at all
        # (the `-c` flag sets the config key inline), so it would otherwise evade the blanket
        # `git ... config` deny above.
        (rf"\bgit\b{_filler(8)}{_SEP}-c{_SEP}url\.", 0),
        (rf"\bgh\b{_filler(4)}{_SEP}pr\b", 0),  # gh pr create/merge/close/edit/...
        (rf"\bgh\b{_filler(4)}{_SEP}api\b", 0),  # gh api (REST bypass for PR mutation)
        (r"\bssh\b", 0),  # direct ssh (custom push transport / remote command execution)
    )
)


def find_denied_match(command: str) -> str | None:
    """Return the first matched denied substring in `command`, or None if it looks clean."""
    for pattern in _DENY_PATTERNS:
        match = pattern.search(command)
        if match is not None:
            return match.group(0)
    return None


def _extract_bash_command(payload: object) -> str | None:
    """Return the Bash `command` string from a PreToolUse hook payload, or None if N/A."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command", "")
    return command if isinstance(command, str) and command else None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # Malformed hook input is an infrastructure hiccup, not a security signal: fail open
        # (allow) so a Maker run is never blocked by a hook-protocol bug. Layers 1/2/4 remain.
        print(f"[maker-bash-guard] failed to parse hook input: {exc}", file=sys.stderr)
        sys.exit(0)

    command = _extract_bash_command(payload)
    if command is None:
        sys.exit(0)

    matched = find_denied_match(command)
    if matched is None:
        sys.exit(0)

    print(
        "[maker-bash-guard] Blocked: Bash command matched a denied push/PR-mutation pattern "
        f"({matched!r}). The Maker process must never push, alter git remotes, or create/"
        "modify pull requests directly — that is loop_driver.py's responsibility, after the "
        "push-integrity checks pass (docs/design/loop-harness-cli.md 2.2/2.6 節).",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
