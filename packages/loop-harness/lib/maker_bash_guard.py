#!/usr/bin/env python3
"""PreToolUse hook (layer 3, defense-in-depth): hard-deny Bash push/PR-mutation bypasses and
`Edit`/`Write` writes into the shared worktree's `.git/` tree.

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

SEC-CRIT (LP-2 2nd-round Codex security review): the Maker's *other* allowed tools (`Edit`/
`Write`, see `loop_driver_support.MAKER_BASE_ALLOWED_TOOLS`) were never inspected by this hook
at all — the injected `--settings` only ever registered it against a `matcher: "Bash"`. A Maker
therefore had a path around every guard above: use `Edit` to write directly into the shared
worktree's `.git/config` and add a `[url "<evil>"] insteadOf = <origin-url>` / `pushurl` /
`credential.helper` entry there, silently repointing where the *driver's own subsequent* push
(run with its real credentials, outside any of this hook's reach — it only ever inspects a
Maker/Checker child's tool calls) actually lands. This hook now also inspects `Edit`/`Write`
tool calls and hard-denies any `file_path` under a `.git` path component
(`is_git_metadata_path()`), closing that gap at the Maker-tool-call layer; see
`loop_driver_support.hardened_git_config_args()` / `find_dangerous_local_git_config()` for the
complementary driver-side hardening this pairs with.

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

**Best-effort status (SEC-MED, LP-2 2nd-round Codex security review)**: this whole module is a
best-effort auxiliary layer, not a primary defense boundary. Its Bash-command text scan is
case-insensitive (`re.IGNORECASE` on every `_DENY_PATTERNS` entry), also matches a quote/
backslash-stripped normalization of the command (a Maker splitting a denied token across quote
boundaries, e.g. `g"i"t push`, would otherwise slip past a literal-substring match), and denies
the `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`/`GIT_CONFIG_COUNT` env-var-based config-injection
mechanism (an alternate way to set the same `insteadOf`/`credential.helper`/`alias.` keys the
patterns already deny via `-c`/`git config`, without a literal `-c` or `config` token appearing
anywhere). None of this amounts to full shell parsing/evaluation, which is explicitly **not** a
goal of this hook — the actual structural guarantees are layer 2 (env-level credential
stripping, see `loop_driver_support.maker_env()`) and, for the `.git/config`-tampering vector
specifically, the driver-side hardening in `loop_driver_support.hardened_git_config_args()` /
`find_dangerous_local_git_config()`, both of which do not depend on recognizing every possible
obfuscated Bash/Edit/Write invocation shape in advance.

**Structural limit (LP-2 3rd-round Codex security review)**: every check in this module (and
its `loop_driver_support.py` counterparts) is config/text-scan hardening layered on top of a
single, unavoidable structural fact -- the Maker and the driver share the same OS-user (UID)
filesystem access to this worktree's `.git/` tree, so nothing this module denies is a hard
filesystem-permission boundary; it is all best-effort closing of *known* paths a same-UID
process could otherwise use to tamper with `.git/config`/hooks. A sufficiently novel bypass this
module does not yet recognize could still exist. Complete protection requires actually removing
that shared same-UID access (a separate OS user or container running the Maker, so its
filesystem writes to `.git/` fail at the kernel/permission level regardless of what this hook
recognizes) -- tracked as Issue #211, not implemented by this module.

Protocol: reads stdin per Claude Code's PreToolUse hook contract — a single JSON object with
(among other fields) `tool_name` and `tool_input`. For the `Bash` tool, `tool_input.command`
holds the full command string; for `Edit`/`Write`, `tool_input.file_path` holds the target path.
Any other tool call, and any input this hook cannot make sense of, is always allowed (exit 0):
the injected settings' `matcher: "Bash|Edit|Write"` should already restrict when Claude Code
invokes this hook at all, and a hook-protocol hiccup is an infrastructure concern, not a
security signal (layers 1/2/4 remain regardless).

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
from pathlib import PurePath
from typing import Any

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


# SEC-MED (LP-2 2nd-round Codex security review): every pattern below is matched
# case-insensitively — a Maker splitting a denied token's case (`GIT PUSH`, `Git Push`, ...)
# would otherwise slip past a case-sensitive literal match; this scan makes no attempt at real
# shell parsing either way, so folding case is a strictly-safe, zero-cost hardening.
_DENY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"\bgit\b{_filler(8)}{_SEP}push\b",  # git push (incl. `git -c x=y push`, wrappers)
        rf"\bgit\b{_filler(8)}{_SEP}remote\b",  # git remote add/set-url/... (repoint push target)
        rf"\bgit\b{_filler(8)}{_SEP}send-pack\b",  # git send-pack (low-level push transport)
        # SC1: the low-level transport binaries can also be invoked directly as a single,
        # hyphenated token (`git-send-pack`/`git-receive-pack`/`git-upload-pack`, as e.g. the
        # ssh push/fetch transport does) with no literal "git " prefix + whitespace before the
        # subcommand word at all, so the `\bgit\b{_filler}{_SEP}send-pack\b`-shaped patterns
        # above never get a chance to match: `\bgit\b` itself already matches inside
        # "git-send-pack" (word boundary at the `t`/`-` transition), but the patterns above all
        # require at least one `_SEP` between "git" and the subcommand, which a bare hyphen is
        # not. Denied as their own standalone, prefix-free patterns instead.
        r"\bgit-send-pack\b",
        r"\bgit-receive-pack\b",
        r"\bgit-upload-pack\b",  # fetch transport, denied by association (defense-in-depth)
        rf"\bgit\b{_filler(8)}{_SEP}worktree\b",  # align w/ MAKER_FIXED_DISALLOWED_TOOLS
        # H1: `git -c alias.<name>=<value> <name> ...` defines a *temporary* (this-invocation-only)
        # alias and immediately invokes it, so a deny-verb value (e.g. `-c alias.p=push`) never
        # appears as a literal `push`/`remote`/... token the patterns above can match. Rather than
        # try to parse/resolve the alias's own value (which can itself be obfuscated further, e.g.
        # `-c alias.p='!git push'`), fail closed on the `-c alias.` construct itself: the Maker has
        # no legitimate need to define any git alias, temporary or not.
        rf"\bgit\b{_filler(8)}{_SEP}-c{_SEP}alias\.",  # git -c alias.<name>=... (temporary)
        # H1: `git config alias.<name> <value>` (optionally `--global`/`--local`/`--add`/...)
        # defines a *persistent* alias in gitconfig, which a later, separate Bash call could then
        # invoke under an innocuous-looking name. Same fail-closed rationale as above.
        rf"\bgit\b{_filler(4)}{_SEP}config\b{_filler(4)}{_SEP}alias\.",  # git config alias.<name>
        # SC3: `git config url.<base>.insteadOf`/`pushUrl`/`remote.<name>.pushurl` can repoint
        # where a later `git push` in the *driver's own* subsequent invocation actually lands
        # (shared `.git/config` mutation), silently redirecting the driver's push to an
        # attacker-controlled remote without ever touching a `push`/`remote` literal token
        # itself. Substring match: git config keys are case-insensitive and this hook does no
        # config-key parsing of its own, so matching the substring anywhere (regardless of
        # surrounding `git config`/`git -c` wrapper shape) is the fail-closed choice here.
        r"insteadof",
        r"pushurl",
        # SEC-MED: `credential.helper` can be repointed the same way (`git config
        # credential.helper '!...'` / `git -c credential.helper=...`), letting a later
        # driver-owned git invocation shell out to an attacker-supplied helper command instead
        # of just redirecting the remote URL. Denied as its own substring for the same
        # fail-closed reason as `insteadof`/`pushurl` above.
        r"credential\.helper",
        # SC3: deny `git config` wholesale (not just the `alias.`/`insteadOf`/`pushurl` special
        # cases above) — the Maker never legitimately needs to read or write any git config at
        # all (its committer identity is already env-injected via `GIT_AUTHOR_*`/
        # `GIT_COMMITTER_*`, see `loop_driver_support.maker_env()`), so refusing every `git
        # config` invocation outright is strictly safer than trying to enumerate every
        # config key that could repoint a push or otherwise sabotage the repo.
        rf"\bgit\b{_filler(8)}{_SEP}config\b",
        # SC3: `git -c url.<base>.insteadOf=<evil> <anything>` rewrites the push/fetch URL for
        # the *duration of this one invocation* without ever using the word "config" at all
        # (the `-c` flag sets the config key inline), so it would otherwise evade the blanket
        # `git ... config` deny above.
        rf"\bgit\b{_filler(8)}{_SEP}-c{_SEP}url\.",
        # SEC-MED: `GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>`/`GIT_CONFIG_COUNT` (git's
        # env-var-based config-injection mechanism, e.g. `GIT_CONFIG_COUNT=1
        # GIT_CONFIG_KEY_0=url.evil.insteadof GIT_CONFIG_VALUE_0=... git push ...`) sets an
        # arbitrary config key/value pair for one invocation without a literal `-c` or
        # `config` token anywhere in the command string, evading every pattern above.
        r"\bGIT_CONFIG_(?:KEY_\d+|VALUE_\d+|COUNT)\b",
        rf"\bgh\b{_filler(4)}{_SEP}pr\b",  # gh pr create/merge/close/edit/...
        rf"\bgh\b{_filler(4)}{_SEP}api\b",  # gh api (REST bypass for PR mutation)
        r"\bssh\b",  # direct ssh (custom push transport / remote command execution)
        # RC2 (LP-2 3rd-round Codex security review): none of the patterns above are `git ...`/
        # `gh ...` invocations at all, so a Maker could still write directly into the shared
        # worktree's `.git/config` (or `.git/hooks/...`) via plain shell redirection --
        # `printf '...' >> .git/config`, `echo ... > worktree/.git/config`, a heredoc piped into
        # a redirect (`cat <<'EOF' > .git/config` -- still just a `>` redirect target textually,
        # already covered by the same pattern), or `tee .git/hooks/pre-push` -- none of which
        # this hook denied before. `is_git_metadata_path()` below already closes this gap for
        # `Edit`/`Write` tool calls; these two patterns close the equivalent gap for `Bash`.
        # Requires a `.git` path *segment* boundary (`/`, quote, whitespace, or end-of-string
        # immediately after the literal `.git`), not merely the substring ".git", so a legitimate
        # `> .gitignore`/`> .github/workflows/x.yml` redirect is not denied.
        r">>?\s*[\"']?(?:[^\s;&|]*/)?\.git(?:/|[\"'\s]|$)",
        r"\btee\b(?:\s+-[\w-]+)*\s+[\"']?(?:[^\s;&|]*/)?\.git(?:/|[\"'\s]|$)",
    )
)

# SEC-MED: a second scan pass, with quote/backslash characters stripped from the command first,
# catches a Maker splitting a denied token across quote boundaries (e.g. `g"i"t push`,
# `gi\t push`) — the shell still parses either as the exact same command, but a literal-
# substring match against the *unstripped* text would miss it. Not a real shell tokenizer: this
# is a blunt char-class strip, matching this module's existing "text scan, not full parse"
# tradeoff (see the module docstring).
_QUOTE_STRIP_RE = re.compile(r"[\"'\\]")


def _normalize_for_bypass_scan(command: str) -> str:
    """Return `command` with quote/backslash characters removed (SEC-MED best-effort)."""
    return _QUOTE_STRIP_RE.sub("", command)


def find_denied_match(command: str) -> str | None:
    """Return the first matched denied substring in `command`, or None if it looks clean.

    Scans both the raw `command` text and its quote/backslash-stripped normalization
    (`_normalize_for_bypass_scan`, SEC-MED) — a match against either counts as denied.
    """
    for candidate in (command, _normalize_for_bypass_scan(command)):
        for pattern in _DENY_PATTERNS:
            match = pattern.search(candidate)
            if match is not None:
                return match.group(0)
    return None


def _extract_bash_command(payload: dict[str, Any]) -> str | None:
    """Return the Bash `command` string from a PreToolUse hook payload, or None if N/A."""
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command", "")
    return command if isinstance(command, str) and command else None


def is_git_metadata_path(file_path: str) -> bool:
    """Return True when file_path has a `.git` path component anywhere (SEC-CRIT).

    Matches a plain `.git` component regardless of position, so it covers both the shared
    repository's own `.git/config`/`.git/hooks/...` tree and a git *worktree*'s own top-level
    `.git` entry (a file, not a directory, pointing back at the shared repo's
    `.git/worktrees/<name>` -- itself also under a `.git` component and therefore already
    covered by this same check without needing to resolve that indirection at all).

    RH3 (LP-2 3rd-round Codex security review): each path component is `casefold()`-ed before
    comparison. On a case-insensitive-but-case-preserving filesystem (macOS's default APFS/
    HFS+ configuration), `.GIT/config`/`.Git/config` resolve to the exact same on-disk file as
    `.git/config` -- a strict `part == ".git"` comparison let a Maker route around this deny
    entirely by writing to a differently-cased spelling of the same path.
    """
    return any(part.casefold() == ".git" for part in PurePath(file_path).parts)


def _extract_edit_write_file_path(payload: dict[str, Any]) -> str | None:
    """Return the `Edit`/`Write` `tool_input.file_path` from a hook payload, or None if N/A."""
    if payload.get("tool_name") not in ("Edit", "Write"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path", "")
    return file_path if isinstance(file_path, str) and file_path else None


def _deny(message: str) -> None:
    """Print a hook-deny stderr message and exit with Claude Code's "block" contract (code 2)."""
    print(f"[maker-bash-guard] Blocked: {message}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # Malformed hook input is an infrastructure hiccup, not a security signal: fail open
        # (allow) so a Maker run is never blocked by a hook-protocol bug. Layers 1/2/4 remain.
        print(f"[maker-bash-guard] failed to parse hook input: {exc}", file=sys.stderr)
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    command = _extract_bash_command(payload)
    if command is not None:
        matched = find_denied_match(command)
        if matched is not None:
            _deny(
                f"Bash command matched a denied push/PR-mutation pattern ({matched!r}). The "
                "Maker process must never push, alter git remotes, or create/modify pull "
                "requests directly — that is loop_driver.py's responsibility, after the "
                "push-integrity checks pass (docs/design/loop-harness-cli.md 2.2/2.6 節)."
            )

    file_path = _extract_edit_write_file_path(payload)
    if file_path is not None and is_git_metadata_path(file_path):
        _deny(
            f"Edit/Write targeted a path under a `.git` directory ({file_path!r}). The Maker "
            "process must never write to the shared worktree's git metadata (SEC-CRIT: this "
            "could otherwise repoint the driver's own subsequent push via `.git/config` "
            "insteadOf/pushurl/credential.helper rewrites) — see "
            "docs/design/loop-harness-cli.md 2.2 節."
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
