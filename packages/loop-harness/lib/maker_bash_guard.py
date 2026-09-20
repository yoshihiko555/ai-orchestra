#!/usr/bin/env python3
r"""PreToolUse hook (layer 3, defense-in-depth): hard-deny Bash push/PR-mutation bypasses and
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
boundaries, e.g. `g"i"t push`, would otherwise slip past a literal-substring match) and a
line-continuation-joined normalization (a Maker splitting a denied token/env-var name across a
`\`+newline, e.g. `GIT_CONFIG_GLO\<newline>BAL=...`, which bash itself rejoins before
tokenization), and denies
git's env-var-based config mechanism — `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`/`GIT_CONFIG_COUNT`/
`GIT_CONFIG_PARAMETERS`, the bare `GIT_CONFIG=` assignment, and the `GIT_CONFIG_GLOBAL=`/
`GIT_CONFIG_SYSTEM=` file selectors (which point git at an attacker-controlled config file that
can carry the same `insteadOf`/`credential.helper`/`alias.` keys). These are alternate ways to
set those keys the patterns already deny via `-c`/`git config`, without a literal `-c` or `config`
token appearing anywhere; they are matched case-sensitively (git only honors the uppercase names)
so an ordinary lowercase `git_config=` shell variable is not false-flagged. It also denies any
`env` invocation that wipes its child environment (Codex review, PR #423) — `env -i`, a lone
`env -` (a GNU-`env` synonym for `-i`), `env --ignore-environment` (and any getopt_long
unambiguous prefix of it, e.g. `--ignore-e`), a combined short-option cluster containing `i`
(`-iu`, ...), or `-S`/`--split-string` (which re-parses its own argument as more `env` options,
so `env -S '-i CMD'` is a wipe one level of indirection deep) — wherever it appears among `env`'s
own other leading options (`NAME=VALUE` assignments; `-u`/`--unset`/`-C`/`--chdir`/`-a`/
`--argv0`/`-P`/`-L`/`-U`, each consuming its own separate following argument), via a dedicated
token scan (`_find_env_wipe`) rather than a single regex. Wiping the child environment this way
discards the `GIT_CONFIG_GLOBAL=/dev/null`/`GIT_CONFIG_SYSTEM=/dev/null` selectors
`loop_driver_support.maker_env()` sets to suppress the user's own gitconfig, so an
attacker-written `~/.gitconfig` alias is honored again with no denied token present. Also denied:
`git --config-env=`/`git --config-env <name>=<envvar>` (same review), which injects a config
value sourced from an environment variable for one invocation with neither a `-c`/`config` token
nor a `GIT_CONFIG_*` name anywhere in the command text — matched by an unbounded scan to the
next shell statement separator rather than a fixed token count, so it cannot be evaded by padding
with enough harmless-looking `git` options. None of this amounts to full shell parsing/evaluation, which is explicitly **not** a
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
        # SEC-MED: `GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>`/`GIT_CONFIG_COUNT`, plus the other
        # members of git's env-var-based config mechanism that a Maker could weaponize —
        # `GIT_CONFIG_PARAMETERS` (git's own transport for propagating `-c key=value` pairs to a
        # child process; `GIT_CONFIG_PARAMETERS="'url.evil.insteadof=...'" git push ...` injects
        # an arbitrary config for one invocation with neither `-c` nor `config` appearing as a
        # literal token); the bare `GIT_CONFIG=<path>` assignment (redirects a scope-less
        # `git config` write, or supplies the config file a `git -c ...`/`git ...` invocation
        # reads, again without a `config`/`-c` token — the `git ... config` blanket deny only
        # catches the `git config` subcommand shape, not `GIT_CONFIG=... git -c ...`); and the
        # config *file* selectors `GIT_CONFIG_GLOBAL=<path>`/`GIT_CONFIG_SYSTEM=<path>`, which
        # point git at an arbitrary attacker-controlled config file for one invocation. That file
        # can define an `[alias]` (e.g. `p = push`) or `insteadOf`/`credential.helper` entry the
        # Maker created earlier via an allowed `Write` to a non-`.git` path, so
        # `GIT_CONFIG_GLOBAL=/tmp/evil.cfg git p ...` runs an aliased push with no literal
        # `push`/`-c`/`config`/`alias.` token anywhere — evading every pattern above. Each of
        # these sets or sources config for one invocation without a literal `-c`/`config` token
        # (e.g. `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.evil.insteadof GIT_CONFIG_VALUE_0=... git
        # push ...`). This mirrors the lib-side `_stripped_host_env` stripping (EV-170) so the
        # guard and lib recognize the same env-var mechanism.
        #
        # `GIT_CONFIG_NOSYSTEM` is deliberately NOT denied: it is a boolean toggle (`=1`) that
        # only *disables* reading the system config, so it cannot inject any config key/file. The
        # `_(?:...GLOBAL|SYSTEM)\b` branch's `\b` end-boundary keeps `GIT_CONFIG_NOSYSTEM` out
        # (its text after `GIT_CONFIG_` is `NOSYSTEM`, which none of the alternatives match), and
        # the bare `=` branch requires the assignment `=` immediately after `GIT_CONFIG`.
        #
        # This whole `GIT_CONFIG...` group is matched case-*sensitively* via `(?-i:...)` (which
        # locally overrides the module-wide `re.IGNORECASE`): git only honors these variables in
        # uppercase, so a case-insensitive match would false-positive on an ordinary lowercase
        # shell variable such as `git_config=/tmp/x` that has no effect on git at all.
        r"(?-i:\bGIT_CONFIG(?:_(?:KEY_\d+|VALUE_\d+|COUNT|PARAMETERS|GLOBAL|SYSTEM)\b|=))",
        # Codex review (PR #423, 2nd round): `git --config-env=alias.p=X ...` / `git
        # --config-env alias.p=X ...` injects a config value sourced from an *environment
        # variable* for one invocation, with neither `-c` nor `config` nor any `GIT_CONFIG_*`
        # token present for the blanket `git ... config`/`GIT_CONFIG` rules above to catch.
        # Unlike most patterns above, this is NOT bounded by `_filler()`'s fixed token count: a
        # 1st-round fix used `_filler(8)`, but a Maker can pad arbitrarily many harmless-looking
        # options before `--config-env` (`git -C /tmp/work -C . -C . -C . -C . --config-env=...`,
        # 5 `-C` pairs = 10 tokens > 8), so the token repetition here is unbounded — it still
        # cannot cross a real shell statement separator (`;`/`&`/`|`) or a subshell/command-
        # substitution boundary (backtick, `(`/`)`, which also blocks `$( ... )`), so it cannot
        # falsely fuse two unrelated statements into one match, matching the exact tradeoff
        # `_filler()`'s docstring describes for its own bounded case. `$` itself stays part of
        # the allowed token character class (unlike the excluded separator characters) so an
        # `${IFS}`-obfuscated separator between the padding tokens is still recognized as a
        # separator by `_SEP` rather than swallowed as inert token text (SC2, mirrored here).
        rf"\bgit\b(?:{_SEP}[^\s;&|`()]+)*?{_SEP}--config-env\b",
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

# SEC-MED (PR review): Bash removes a backslash-newline line continuation from the input stream
# *before* tokenization, so a denied token/env-var name split across a line continuation —
# `GIT_CONFIG_GLO\<newline>BAL=/tmp/evil.cfg git status` — is rejoined by the shell into the exact
# same `GIT_CONFIG_GLOBAL=...` invocation, yet a scan of the raw text sees the two halves broken
# by the `\`+newline. Stripping the whole `\`+newline sequence (matching bash's own removal)
# before scanning reconstructs the token. The quote/backslash strip alone is insufficient here:
# it removes the backslash but leaves the newline, so the two halves stay split. Only a bare
# backslash *immediately* followed by a newline is a continuation; a backslash followed by other
# text (`gi\t push`) is left for `_QUOTE_STRIP_RE` to handle as before.
_LINE_CONTINUATION_RE = re.compile(r"\\\r?\n")


def _strip_line_continuations(command: str) -> str:
    """Return `command` with bash `\\`+newline line continuations removed (SEC-MED best-effort)."""
    return _LINE_CONTINUATION_RE.sub("", command)


def _normalize_for_bypass_scan(command: str) -> str:
    """Return `command` with quote/backslash characters removed (SEC-MED best-effort)."""
    return _QUOTE_STRIP_RE.sub("", command)


# --------------------------------------------------------------------------------------------
# Codex review (PR #423, 2nd round): `env -i`/`env --ignore-environment`/`env -` (a lone `-` is
# a GNU-`env` synonym for `-i`)/`env -S ...` (re-parses its argument as MORE `env` options/args,
# so `env -S '-i CMD'` is equivalent to `env -i CMD` — a wipe hidden one level of indirection
# deep) wipes the entire child environment, discarding the `GIT_CONFIG_GLOBAL=/dev/null`/
# `GIT_CONFIG_SYSTEM=/dev/null` selectors `loop_driver_support.maker_env()` sets to suppress the
# user's own gitconfig; an attacker-written `~/.gitconfig` `[alias] p = push` is then honored
# again with no denied token (`push`/`-c`/`config`/`GIT_CONFIG_*`) anywhere in the command text.
#
# This is deliberately NOT one of the `_DENY_PATTERNS` regexes: whether a given `-i`/`-S`-shaped
# token is the wipe flag depends on *where* it sits relative to `env`'s own other options
# (`NAME=VALUE` assignments; `-u`/`--unset`/`-C`/`--chdir`/`-a`/`--argv0`/`-P`/`-L`/`-U`, each of
# which consumes its own following argument) versus the *wrapped command's own name* and args
# (e.g. `sed`'s own `-i` in `env FOO=bar sed -i 's/a/b/' f`, which has nothing to do with `env`
# and must never be treated as a wipe) — a single fixed-shape regex for "the wipe flag can be
# preceded by an unbounded, variably-shaped run of other env options" was judged unreadable, so
# this is implemented as a small dedicated left-to-right token scan instead.
_ENV_WORD_RE = re.compile(r"(?<![\w.-])env(?=\s|\$\{IFS\}|\$IFS)", re.IGNORECASE)
_ENV_STATEMENT_SPLIT_RE = re.compile(r"[;&|\n]")
_ENV_TOKEN_SPLIT_RE = re.compile(r"(?:\s|\$\{IFS\}|\$IFS)+")
# Short-option cluster containing `i` (`-i`, `-iu`, `-vi`, ...) or `S` (`-S`, `-uS`, ...)
# anywhere: GNU getopt clusters short flags together, and either letter enables the wipe
# (`-S`'s re-parsed string commonly starts with `-i`, so failing closed on `-S` itself — without
# trying to parse what it re-parses into — is the safe choice, same rationale as the `-c
# alias.<name>=<value>` fail-closed choice above). `S` is matched case-sensitively together with
# `i` in one pattern for simplicity; GNU env has no lowercase `-s` option, so this cannot
# false-positive on a real flag, only (acceptably) on characters that merely happen to spell one.
_ENV_WIPE_SHORT_RE = re.compile(r"-[a-zA-Z0-9]*[iS][a-zA-Z0-9]*")
# Long form, plus GNU getopt_long's unambiguous-prefix matching (`--ignore-e`, `--ignore-env`,
# ... all resolve to `--ignore-environment`) — anchored on the `--i` prefix rather than the full
# word so any accepted abbreviation is still caught; `--split-string` is `-S`'s long form.
_ENV_WIPE_LONG_RE = re.compile(r"--(?:i[\w-]*|split-string)", re.IGNORECASE)
# A short-option cluster whose LAST letter takes a separate following argument (its own value is
# consumed as the next token, so it must be skipped along with the flag itself rather than
# inspected for a wipe): `-u`/`-C`/`-a`/`-P`/`-L`/`-U` (GNU `-u NAME`/`-C DIR`/`-a ARGV0`; BSD
# `-P altpath`/`-L`/`-U user[/class]`). A cluster ending in `i`/`S` is already caught by
# `_ENV_WIPE_SHORT_RE` above and never reaches this check.
_ENV_ARG_TAKING_SHORT_RE = re.compile(r"-[a-zA-Z0-9]*[uCaPLU]")
_ENV_ARG_TAKING_LONG = frozenset({"--unset", "--chdir", "--argv0"})


def _find_env_wipe(command: str) -> str | None:
    """Return a description of the first `env` environment-wipe found in `command`, or None.

    For each `env` occurrence (anchored so it cannot match inside `--env-file`/`.env`/
    `printenv`), scans the tokens of that same shell statement (stopping at `;`/`&`/`|`/newline,
    same boundary `_filler()` respects for the regex patterns above) left-to-right: a `NAME=VALUE`
    assignment or a recognized `env` option (optionally consuming its own separate argument) is
    skipped, a wipe flag (`-i`/`-S`/a lone `-`/`--ignore-environment`/`--split-string`, in any
    position among those leading options) is denied immediately, and the first token that is
    none of these — the wrapped command's own name — stops the scan for that `env` occurrence
    without denying (that command's own subsequent flags, e.g. `sed -i`, are irrelevant to `env`).
    """
    for env_match in _ENV_WORD_RE.finditer(command):
        statement = _ENV_STATEMENT_SPLIT_RE.split(command[env_match.end() :], maxsplit=1)[0]
        tokens = [token for token in _ENV_TOKEN_SPLIT_RE.split(statement) if token]
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if (
                token == "-"
                or _ENV_WIPE_SHORT_RE.fullmatch(token)
                or _ENV_WIPE_LONG_RE.fullmatch(token)
            ):
                return f"env ... {token}"
            if "=" in token:
                index += 1
                continue
            if _ENV_ARG_TAKING_SHORT_RE.fullmatch(token) or token in _ENV_ARG_TAKING_LONG:
                index += 2  # flag + its separate argument
                continue
            if token.startswith("-"):
                index += 1  # some other env option/flag, not a wipe
                continue
            break  # first non-option, non-assignment token: the wrapped command's own name
    return None


def find_denied_match(command: str) -> str | None:
    """Return the first matched denied substring in `command`, or None if it looks clean.

    Scans the raw `command` text, its line-continuation-joined form (`_strip_line_continuations`,
    which mirrors bash's pre-tokenization `\\`+newline removal), and the quote/backslash-stripped
    normalization of that joined form (`_normalize_for_bypass_scan`, SEC-MED) — a match against
    any of them counts as denied. Joining before the quote strip is required because the quote
    strip removes the continuation's backslash but leaves the newline, keeping the token split.
    Also runs `_find_env_wipe` against each of the same three candidates (SEC-MED).
    """
    line_joined = _strip_line_continuations(command)
    for candidate in (command, line_joined, _normalize_for_bypass_scan(line_joined)):
        for pattern in _DENY_PATTERNS:
            match = pattern.search(candidate)
            if match is not None:
                return match.group(0)
        env_wipe = _find_env_wipe(candidate)
        if env_wipe is not None:
            return env_wipe
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
