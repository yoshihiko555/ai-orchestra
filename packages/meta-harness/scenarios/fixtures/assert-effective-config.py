#!/usr/bin/env python3
"""Assert a candidate's recorded config-layering answer against a trusted expectation table.

``config-loading.md`` documents a 2-file layering rule (base + ``*.local.yaml`` override) that
this harness's "誤誘導耐性" scenarios probe without ever naming the rule to the candidate: does
the candidate correctly resolve which file wins for a given key, purely from reading the two
files?

The scenario's ``setup:`` step writes the base/local config pair into the same writable
``sandbox/`` workspace the candidate has unrestricted ``Edit``/``Write`` access to for the rest
of the run, so (mirroring ``assert-issue-fix-decision.py``'s PR #266 pattern) this fixture must
not trust the *content* of those files at face value -- a sufficiently adversarial candidate
could rewrite them to fabricate an easier merge outcome and then trivially satisfy a looser
oracle. Instead, the expected (value, source_file) per key is looked up from a hardcoded table
keyed by ``sha256(base) + sha256(local)``; any byte-level deviation from a known-good pair fails
closed.

Known-answer hashing (PR #381 review, P1): the config pair itself is materialized into the
candidate's writable ``sandbox/`` workspace, which the evaluator also `git worktree`-checks out
this whole repo into -- including ``packages/meta-harness/scenarios/fixtures/assert-effective-
config.py`` itself. Earlier revisions of this fixture stored the *expected plaintext* (value,
source_file) directly in ``_KNOWN_CONFIG_PAIRS``, so a candidate could satisfy this oracle
without ever resolving the config-loading rule, just by reading this file (or ``rg``-ing the
worktree for the expected string). To remove that trivially-readable answer key, this fixture now
stores only a sha256 hash of each expected field's *normalized* JSON representation
(``_KNOWN_ANSWER_HASHES``); the candidate's actual answer is normalized the same way and hashed,
then the hashes are compared -- the plaintext expected value/source_file never appears in this
file's source.

Normalization spec (must match exactly on both the table-generation side and the runtime
comparison side):

1. Coerce the field value to ``str`` (mirrors the pre-existing ``str(answer.get(...))`` coercion,
   so ``None``/non-string JSON values still compare deterministically).
2. ``.strip()`` leading/trailing whitespace.
3. For the ``source_file`` field only (PR #381 review, round 3): run the stripped string through
   ``posixpath.normpath()``. The scenario prompt only asks for *some* correct relative path to the
   winning config file, e.g. ``sandbox/config/agent-routing/cli-tools.local.yaml`` and
   ``./sandbox/config/agent-routing/cli-tools.local.yaml`` name the same file and must hash
   identically. ``posixpath`` (not ``os.path``) is used deliberately: the scenario's paths are
   always POSIX-style regardless of the host OS running the harness. This only cancels redundant
   tokens already present in the literal string (a leading ``./``, doubled ``/``, interior ``.``
   segments, and textual ``a/b/../c`` -> ``a/c`` collapse) -- it does not resolve the path against
   the filesystem, follow symlinks, or make it absolute, so it cannot be used to smuggle in a path
   that doesn't actually name the same file. A trailing ``/`` is rejected *before* normalization
   rather than being silently collapsed away (PR #381 review, round 4):
   ``posixpath.normpath()`` maps ``sandbox/.../cli-tools.local.yaml/`` onto the same normalized
   string as the real file, but a POSIX trailing slash means "this must be a directory" -- the
   winning config is an ordinary file, so a ``source_file`` answer with a trailing slash never
   actually names it and must fail rather than hash-match its way to a pass.
   The ``value`` field is never path-normalized (it is an opaque config value, not a path).
4. Wrap as a single-key JSON object (``{"value": ...}`` or ``{"source_file": ...}``) and serialize
   with ``json.dumps(obj, sort_keys=True, separators=(",", ":"))`` (sorted keys, no incidental
   whitespace -- irrelevant for a single-key object today, but keeps the spec stable if a future
   revision hashes multi-key objects).
5. ``hashlib.sha256(canonical.encode("utf-8")).hexdigest()``.

Residual limitation (still Issue #267, same class as ``assert-issue-fix-decision.py``'s note):
this hashing only closes the *fixture-source* leak. The candidate's `git worktree` checkout also
contains ``packages/meta-harness/tests/test_claude_harness_scenarios.py``, whose known-good-answer
tests (`test_resolve_effective_config_graded_commands_pass_against_known_good_answer` and the
``-holdout`` sibling) still write the plaintext expected answer to disk during their own test run
-- that plaintext is not present in a *candidate's* checkout at answer-writing time, but a
candidate that reads this repo's test suite as reference material would still learn the winning
values for these two specific, fixed config pairs. Closing that class of leak (e.g. moving trusted
expectations out of the repo entirely, or randomizing the config pair per run) is out of scope for
this fixture and tracked under Issue #267.

Separately from value/source_file correctness, the answer's key set is also validated strictly
(top-level keys for flat answers; top-level *and* per-entry keys for keyed answers) so that an
answer with an unexpected extra key does not silently pass just because the graded keys it does
contain happen to be correct.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
from pathlib import Path
from typing import Any

_ANSWER_FIELDS = ("value", "source_file")


def _normalize_field_value(field: str, raw_value: Any) -> str:
    """Coerce and normalize one answer field per the module docstring's "Normalization spec".

    ``source_file`` gets an additional POSIX path normalization pass (PR #381 review, round 3)
    so that equivalent relative-path spellings (e.g. a redundant leading ``./``) compare equal;
    ``value`` is an opaque config value and is only stripped, never path-normalized.
    """
    text = str(raw_value).strip()
    if field == "source_file" and text:
        assert not text.endswith("/"), (
            f"source_file {text!r} ends with a trailing slash; the winning config is an "
            "ordinary file, never a directory, and normalizing away the slash before comparing "
            "would let a directory-shaped answer collapse onto a real file's expected hash"
        )
        text = posixpath.normpath(text)
    return text


def _normalized_field_hash(field: str, raw_value: Any) -> str:
    """sha256 of the normalized ``{field: ...}`` JSON object.

    See the module docstring's "Normalization spec" for the exact steps this implements.
    """
    normalized_value = _normalize_field_value(field, raw_value)
    canonical = json.dumps({field: normalized_value}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# sha256(base config bytes), sha256(local config bytes) -> {key_path: {field: sha256 of the
# normalized expected {field: value} JSON object}}. Keying by content hash (rather than
# re-parsing the workspace copies) means the expected answer never depends on anything a
# candidate could have rewritten during the run. Regenerate via the setup: heredoc bytes if a
# scenario's fixture content changes -- tests must derive these from the real scenario setup:
# (see test_real_scenario_setup_bytes_match_known_config_pair_table in
# test_claude_harness_scenarios.py). The plaintext expected value/source_file used to compute
# each hash below intentionally does not appear anywhere in this file; see the module docstring's
# "Known-answer hashing" note.
_KNOWN_ANSWER_HASHES: dict[tuple[str, str], dict[str, dict[str, str]]] = {
    # resolve-effective-config.yaml
    (
        "3a072897ecec3f0ee0abcdf7c9f91a775840ef42cf349eec5187e701b450caaf",
        "0fb2cd5e773e4156721e18461874e0448fc6689e4f205b4e7a16a15a4c77a06b",
    ): {
        "codex.model": {
            "value": "094a60e1f1765fb81dbd5e65e9173843b4a81e6790e2ebcac514ae11c3f56fbe",
            "source_file": "d3c935e41928eaaadd02f184b384561ea15ea003bceea387d7685aee60899a9d",
        },
    },
    # resolve-nested-override-holdout.yaml
    (
        "37a9bcd7428e102be76f3c6a9f305dd28ea63ea117c42f2b7bc25324526f1a0a",
        "efb74a1b96553240c5f875f0df21d28f24ec75d4f0961ca40270f572b87d6c51",
    ): {
        "codex.model": {
            "value": "d0344097b637f486fa4d9609ac44688e831ef8e9720f19a6df4e9aa8933e3b17",
            "source_file": "d3c935e41928eaaadd02f184b384561ea15ea003bceea387d7685aee60899a9d",
        },
        "codex.sandbox.analysis": {
            "value": "55c23371b63f32174291c88a638523b6a33a7612fd753dc2be68d8441bfe69cf",
            "source_file": "3cfe3fd763b65306f1891f81ccecf274ba16ac4b9b4c2cb70d0dcc14b8049034",
        },
    },
}


def _expected_hashes(base_full: Path, local_full: Path) -> dict[str, dict[str, str]]:
    assert base_full.is_file() and not base_full.is_symlink(), (
        f"missing regular base config: {base_full}"
    )
    assert local_full.is_file() and not local_full.is_symlink(), (
        f"missing regular local config: {local_full}"
    )
    base_sha = hashlib.sha256(base_full.read_bytes()).hexdigest()
    local_sha = hashlib.sha256(local_full.read_bytes()).hexdigest()
    expectations = _KNOWN_ANSWER_HASHES.get((base_sha, local_sha))
    assert expectations is not None, (
        f"config pair (base sha256={base_sha}, local sha256={local_sha}) does not match any "
        "known-good fixture; the writable workspace copy may have been tampered with"
    )
    return expectations


def _load_answer(answer_full: Path) -> dict[str, Any]:
    assert answer_full.is_file() and not answer_full.is_symlink(), (
        f"missing regular answer artifact: {answer_full}"
    )
    return json.loads(answer_full.read_text(encoding="utf-8"))


def _assert_exact_keys(mapping: dict[str, Any], expected_keys: set[str], *, context: str) -> None:
    actual_keys = set(mapping.keys())
    assert actual_keys == expected_keys, (
        f"{context}: key set {sorted(actual_keys)} != expected {sorted(expected_keys)}"
    )


def _assert_field_matches(
    entry: dict[str, Any], expected: dict[str, str], field: str, *, key_path: str
) -> None:
    if field in ("value", "both"):
        actual_hash = _normalized_field_hash("value", entry.get("value"))
        assert actual_hash == expected["value"], (
            f"{key_path}: value {entry.get('value')!r} does not match the expected effective value"
        )
    if field in ("source_file", "both"):
        actual_hash = _normalized_field_hash("source_file", entry.get("source_file"))
        assert actual_hash == expected["source_file"], (
            f"{key_path}: source_file {entry.get('source_file')!r} does not match the expected "
            "source file"
        )


def assert_flat_answer(
    project_root: Path,
    base_rel: Path,
    local_rel: Path,
    answer_rel: Path,
    key_path: str,
    field: str,
) -> None:
    """Check a single-key flat answer shape: ``{"value": ..., "source_file": ...}``."""
    expected_hashes = _expected_hashes(project_root / base_rel, project_root / local_rel)
    assert key_path in expected_hashes, f"unknown key_path {key_path!r} for this config pair"
    answer = _load_answer(project_root / answer_rel)
    _assert_exact_keys(answer, set(_ANSWER_FIELDS), context="answer")
    _assert_field_matches(answer, expected_hashes[key_path], field, key_path=key_path)


def assert_keyed_answer(
    project_root: Path,
    base_rel: Path,
    local_rel: Path,
    answer_rel: Path,
    key_path: str,
    field: str,
) -> None:
    """Check a multi-key answer shape: ``{key_path: {"value": ..., "source_file": ...}, ...}``."""
    expected_hashes = _expected_hashes(project_root / base_rel, project_root / local_rel)
    assert key_path in expected_hashes, f"unknown key_path {key_path!r} for this config pair"
    answer = _load_answer(project_root / answer_rel)
    _assert_exact_keys(answer, set(expected_hashes.keys()), context="answer top-level")
    entry = answer.get(key_path)
    assert isinstance(entry, dict), f"answer is missing an entry for key {key_path!r}: {answer!r}"
    _assert_exact_keys(entry, set(_ANSWER_FIELDS), context=f"{key_path} entry")
    _assert_field_matches(entry, expected_hashes[key_path], field, key_path=key_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--local", type=Path, required=True)
    parser.add_argument("--answer", type=Path, required=True)
    parser.add_argument("--key-path", default="codex.model")
    parser.add_argument("--field", choices=["value", "source_file", "both"], default="both")
    parser.add_argument(
        "--keyed",
        action="store_true",
        help="answer artifact is a multi-key dict keyed by --key-path (holdout scenarios)",
    )
    args = parser.parse_args(argv)

    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    if args.keyed:
        assert_keyed_answer(
            project_root, args.base, args.local, args.answer, args.key_path, args.field
        )
    else:
        assert_flat_answer(
            project_root, args.base, args.local, args.answer, args.key_path, args.field
        )


if __name__ == "__main__":
    main()
