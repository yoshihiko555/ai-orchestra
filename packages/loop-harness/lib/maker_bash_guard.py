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
so an ordinary lowercase `git_config=` shell variable is not false-flagged. It also hard-denies
the bare words `env` and `exec` appearing *anywhere* in the command text, and the literal
`--config-env` appearing anywhere (Codex review, PR #423, 3rd round): the 2nd round attempted a
precise wipe-flag/leading-option token scan for `env -i`/`-`/`--ignore-environment`/`-S`/
combined clusters, and a fixed-anchor `git ... --config-env` scan, but a 3rd Codex review found
8 further bypasses/false-positives against that approximation — an attached `--split-string=`
argument, a getopt_long abbreviated `--chd`, a quoted shell-metacharacter smuggled inside an
option's own argument (`-u 'A;B'`), the `exec -c` builtin (which wipes the environment the same
way `env -i` does but was not covered at all), a redirect sitting directly before the wipe flag
with no separating space (`env>/dev/null -i`), and `--config-env` scans that needed to treat
`&>`/`2>&1`-style redirects and even a real (non-continuation) newline as "still the same
statement" without also being crossable by an attacker. Approximating enough of shell option
parsing (getopt clustering/abbreviation, quoting, redirection) to close all of these precisely,
while not opening new ones, was judged an unwinnable game for a text-scan-only hook: the fix
instead **fails closed** by denying the trigger words outright, matching the deliberate
`insteadof`/`pushurl`/`credential\.helper`/blanket-`git config` precedent above. The Maker never
legitimately needs to invoke `env` at all (`NAME=VALUE cmd` sets variables without it) or `exec`
(a normal foreground `cmd ...` already replaces nothing it needs replaced), and every wipe-flag
shape denied by the 2nd round's token scan trivially reduces to "the word `env` is present
somewhere in the command", so the wipe-specific scan (`_find_env_wipe`) was deleted entirely
in favor of this. `env -i .../-S ...` still discards the `GIT_CONFIG_GLOBAL=/dev/null`/
`GIT_CONFIG_SYSTEM=/dev/null` selectors `loop_driver_support.maker_env()` sets, and
`--config-env=<envvar>` still injects config from an environment variable with no `-c`/`config`/
`GIT_CONFIG_*` token — this rule just denies the umbrella term instead of trying to characterize
every dangerous shape under it precisely. Accepted false positives: `echo env`, `docker exec` (a
Maker has no legitimate need for either; `find ... -exec` is unaffected since `-exec` is preceded
by a `-`, not a word boundary, so it does not match the bare-word pattern), and a `--config-env`
mention split across a genuine newline into two separate statements (denied anyway, since the
umbrella term match no longer needs to reconstruct one invocation). `.env`/`--env-file`/
`printenv`/`environment`/an `ENV=value` assignment/`$env_name`/`config/.env` are NOT denied: the
lookaround boundaries require `env` to stand alone as its own word (not preceded by a word/`.`/
`$`/`-` character — note this deliberately does NOT exclude `/`, see the 4th-round update below
— and not followed by a word/`.`/`=`/`-` character).

**4th-round update (Codex review, PR #423)**: a path-qualified invocation (`/usr/bin/env -i
...`, `/bin/env ...`) slipped past the 3rd round's rule, which excluded `/` from the lookbehind
(mirroring this file's other path-safety patterns) — that exclusion is now removed, so any
occurrence of the bare word `env`/`exec` preceded by a path separator is denied too (`.env`/
`config/.env` remain allowed regardless, since the `.` immediately before `env` is still
excluded). The same review also found `setpriv --reset-env sh -c '...'` — another
environment-wiping wrapper this hook had never covered — which is now denied alongside `sudo`/
`su`/`runuser`/`chpst`/`unshare`/`nsenter`/`busybox` as bare words, for the same reason `env`/
`exec` are (each can wipe/replace the process environment or elevate/change privileges without
literally containing `env`/`exec` as their own subcommand).

**Scope statement (Codex review, PR #423, 4th round)**: this module is layer 3 of the push
defense-in-depth described in `docs/design/loop-harness-cli.md` §2.2 "多層防御（defense-in-depth）
の追記" (層1〜層4) — a best-effort command-string screen, not the structural boundary. Given four
rounds of review have each found a new wrapper/bypass shape (`env` flag variants, `--config-env`,
`exec -c`, now `setpriv`/`sudo`/`su`/`runuser`/`chpst`/`unshare`/`nsenter`/`busybox`),
enumerating every environment-wiping or privilege-changing wrapper that could ever exist is
explicitly NOT a goal of this layer; further, as-yet-undiscovered wrapper bypasses of this text
scan are an accepted residual risk, *unless* they also defeat layer 4. The actual structural
boundaries are: 層2（env 認証隔離。主軸）in the same §2.2 (push credentials are stripped from the
Maker's child-process env regardless of which wrapper it runs through, see
`loop_driver_support.maker_env()`); 層4（push 後整合性検証。安全網）in the same §2.2 (the driver
records the expected local/remote HEAD around Maker execution and safely stops on any
unexpected remote advance, regardless of how the Maker got there); and, where LP-2 Docker
isolation is in effect, `docs/design/loop-harness-isolation.md` §1.2 "コンテナに入るもの / driver
に残るものの対照表", whose first row states the container the Maker runs in holds **no**
git/gh push or PR-creation credentials at all — a wrapper running inside that container has
nothing to wipe or steal in the first place, independent of this hook recognizing it. None of
this amounts to full shell parsing/evaluation, which is explicitly **not** a
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
        # Codex review (PR #423, 3rd round): fail-closed pivot. Two prior rounds tried to
        # precisely characterize `env`'s environment-wipe flags (`-i`/`-`/`--ignore-environment`/
        # `-S`/combined clusters/leading-option tolerance) and a fixed-anchor `git ...
        # --config-env` scan; a 3rd Codex review found 8 further bypasses/false-positives against
        # those approximations (an attached `--split-string=` argument, a getopt_long-abbreviated
        # `--chd`, a quoted shell metacharacter smuggled inside an option's own argument
        # (`-u 'A;B'`), the `exec -c` builtin — which was not covered at all — a redirect with no
        # separating space directly before the wipe flag (`env>/dev/null -i`), and `--config-env`
        # scans that needed `&>`/`2>&1`-style redirects and even a genuine newline to keep
        # counting as "the same statement" without becoming crossable by an attacker). Precisely
        # modeling getopt clustering/abbreviation/quoting/redirection with text-scan regexes alone
        # is an unwinnable arms race, so this rule denies the bare trigger words instead: the
        # Maker never legitimately needs `env` at all (`NAME=VALUE cmd` sets variables without
        # it) or `exec` (a plain foreground `cmd ...` needs no process replacement), and every
        # env-wipe shape from the prior rounds trivially reduces to "the word `env` is present in
        # the command" — so the whole flag/option token scan collapses into this single check.
        # `(?<![\w.$-])`/`(?![\w.=-])` require `env`/`exec` to stand alone as their own word (not
        # part of `.env`/`--env-file`/`printenv`/`ENV=value`/`$env_name`/`environment`/`execute`),
        # matching `env`/`exec` wherever they appear — start of command, after any separator/
        # redirect/subshell open, with no separate token-boundary tracking needed. Codex review
        # (PR #423, 4th round): the lookbehind does NOT exclude `/`, so a path-qualified
        # invocation (`/usr/bin/env -i ...`, `/bin/env ...`) is denied too — an earlier version of
        # this rule excluded `/` (to mirror the `--config-env`/`GIT_CONFIG` patterns' path-safety
        # habits elsewhere in this file) but that let `/usr/bin/env` slip through undetected.
        # `.env`/`config/.env` stay allowed regardless: the `.` immediately before `env` is still
        # excluded, so only a literal path SEPARATOR (`/`) or nothing before `env` triggers this.
        r"(?<![\w.$-])env(?![\w.=-])",
        # `exec -c`/`exec -c CMD` (bash builtin) wipes the environment the same way `env -i` does,
        # by replacing the shell with `CMD` running in a stripped environment; matched the same
        # way and for the same fail-closed reason as `env` above (including the same 4th-round
        # `/`-inclusive lookbehind, kept symmetric with the `env` rule above even though a
        # path-qualified `/usr/bin/exec` is not a real, separately-invokable binary in practice).
        # `find ... -exec` is unaffected: `-exec` is preceded by `-`, which the lookbehind still
        # excludes, not a word boundary. `docker exec` is also denied by this rule (acceptable:
        # the Maker has no legitimate need to exec into any container either).
        r"(?<![\w.$-])exec(?![\w.=-])",
        # Codex review (PR #423, 4th round): `setpriv`/`sudo`/`su`/`runuser`/`chpst`/`unshare`/
        # `nsenter`/`busybox` are all environment-wiping or privilege/namespace-changing wrapper
        # binaries in the same family as `env -i`/`exec -c` (e.g. `setpriv --reset-env sh -c
        # '...'` clears the environment the same way `env -i` does; `sudo`/`su`/`runuser` reset
        # env by default unless `-E`/`--preserve-environment` is passed; `busybox env -i ...`
        # reaches the same busybox-builtin `env` applet through a different binary name).
        # Enumerating every wrapper capable of this is explicitly not a goal (see the module
        # docstring's scope statement below) — this list is a best-effort, non-exhaustive
        # extension of the same fail-closed word-deny approach as `env`/`exec` above, covering
        # the wrapper families a Codex review has actually found so far. Same lookaround shape:
        # `su` does not false-positive on `sum`/`sudoers` (the lookahead requires a non-word/`.`/
        # `=`/`-` character immediately after, which `sudoers`' `d`/`sum`'s `m` are not).
        r"(?<![\w.$-])(?:setpriv|sudo|su|runuser|chpst|unshare|nsenter|busybox)(?![\w.=-])",
        # Codex review (PR #423, 3rd round): `--config-env` denied as a bare word wherever it
        # appears, with no `git` anchor or shell-separator scanning required at all — this
        # sidesteps every redirect/newline/subshell-boundary concern the 1st/2nd-round `git ...
        # --config-env` scans had to reason about. The lookaround boundaries only rule out it
        # being a fragment of a longer option/word (e.g. a hypothetical `--config-env-file`); an
        # unrelated mention of the exact literal (e.g. in a commit message) is still an accepted
        # false positive, the same tradeoff `insteadof`/`pushurl`/`credential\.helper` above make.
        r"(?<![\w-])--config-env(?![\w-])",
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


def find_denied_match(command: str) -> str | None:
    """Return the first matched denied substring in `command`, or None if it looks clean.

    Scans the raw `command` text, its line-continuation-joined form (`_strip_line_continuations`,
    which mirrors bash's pre-tokenization `\\`+newline removal), and the quote/backslash-stripped
    normalization of that joined form (`_normalize_for_bypass_scan`, SEC-MED) — a match against
    any of them counts as denied. Joining before the quote strip is required because the quote
    strip removes the continuation's backslash but leaves the newline, keeping the token split.
    """
    line_joined = _strip_line_continuations(command)
    for candidate in (command, line_joined, _normalize_for_bypass_scan(line_joined)):
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
