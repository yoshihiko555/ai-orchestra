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

Known limitation (Issue #267, same class as ``assert-issue-fix-decision.py``'s note): the answer
artifact itself also lives in the candidate-writable workspace, so a sufficiently adversarial
candidate could still forge an answer file that happens to match the trusted table's expected
value without actually having derived it correctly. This fixture raises the bar (a legitimate
run must report an answer that matches the exact known-good expectation for the exact config
pair served) but does not by itself close that gap; see Issue #267 for the harness-level fix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

# sha256(base config bytes), sha256(local config bytes) -> {key_path: (expected_value,
# expected_source_file)}. Keying by content hash (rather than re-parsing the workspace copies)
# means the expected answer never depends on anything a candidate could have rewritten during
# the run. Regenerate via the setup: heredoc bytes if a scenario's fixture content changes --
# tests must derive these from the real scenario setup: (see
# test_effective_config_oracle_matches_real_scenario_setup in test_skill_scenarios.py).
_KNOWN_CONFIG_PAIRS: dict[tuple[str, str], dict[str, tuple[str, str]]] = {
    # resolve-effective-config.yaml
    (
        "3a072897ecec3f0ee0abcdf7c9f91a775840ef42cf349eec5187e701b450caaf",
        "0fb2cd5e773e4156721e18461874e0448fc6689e4f205b4e7a16a15a4c77a06b",
    ): {
        "codex.model": (
            "harness-local-override-model",
            "sandbox/config/agent-routing/cli-tools.local.yaml",
        ),
    },
    # resolve-nested-override-holdout.yaml
    (
        "37a9bcd7428e102be76f3c6a9f305dd28ea63ea117c42f2b7bc25324526f1a0a",
        "efb74a1b96553240c5f875f0df21d28f24ec75d4f0961ca40270f572b87d6c51",
    ): {
        "codex.model": (
            "harness-nested-local-model",
            "sandbox/config/agent-routing/cli-tools.local.yaml",
        ),
        "codex.sandbox.analysis": (
            "read-only",
            "sandbox/config/agent-routing/cli-tools.yaml",
        ),
    },
}


def _trusted_expectations(base_full: Path, local_full: Path) -> dict[str, tuple[str, str]]:
    assert base_full.is_file() and not base_full.is_symlink(), (
        f"missing regular base config: {base_full}"
    )
    assert local_full.is_file() and not local_full.is_symlink(), (
        f"missing regular local config: {local_full}"
    )
    base_sha = hashlib.sha256(base_full.read_bytes()).hexdigest()
    local_sha = hashlib.sha256(local_full.read_bytes()).hexdigest()
    expectations = _KNOWN_CONFIG_PAIRS.get((base_sha, local_sha))
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


def assert_flat_answer(
    project_root: Path,
    base_rel: Path,
    local_rel: Path,
    answer_rel: Path,
    key_path: str,
    field: str,
) -> None:
    """Check a single-key flat answer shape: ``{"value": ..., "source_file": ...}``."""
    expected_value, expected_source = _trusted_expectations(
        project_root / base_rel, project_root / local_rel
    )[key_path]
    answer = _load_answer(project_root / answer_rel)
    if field in ("value", "both"):
        actual_value = str(answer.get("value"))
        assert actual_value == expected_value, (
            f"value {actual_value!r} != expected {expected_value!r}"
        )
    if field in ("source_file", "both"):
        actual_source = str(answer.get("source_file"))
        assert actual_source == expected_source, (
            f"source_file {actual_source!r} != expected {expected_source!r}"
        )


def assert_keyed_answer(
    project_root: Path,
    base_rel: Path,
    local_rel: Path,
    answer_rel: Path,
    key_path: str,
    field: str,
) -> None:
    """Check a multi-key answer shape: ``{key_path: {"value": ..., "source_file": ...}, ...}``."""
    expected_value, expected_source = _trusted_expectations(
        project_root / base_rel, project_root / local_rel
    )[key_path]
    answer = _load_answer(project_root / answer_rel)
    entry = answer.get(key_path)
    assert isinstance(entry, dict), f"answer is missing an entry for key {key_path!r}: {answer!r}"
    if field in ("value", "both"):
        actual_value = str(entry.get("value"))
        assert actual_value == expected_value, (
            f"{key_path}: value {actual_value!r} != expected {expected_value!r}"
        )
    if field in ("source_file", "both"):
        actual_source = str(entry.get("source_file"))
        assert actual_source == expected_source, (
            f"{key_path}: source_file {actual_source!r} != expected {expected_source!r}"
        )


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
