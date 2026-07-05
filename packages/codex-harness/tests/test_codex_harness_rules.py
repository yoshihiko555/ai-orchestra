"""codex-harness.rules (Codex native prefix-rule policy) static content check.

This file is a declarative policy fragment consumed by the Codex CLI's own
rule engine (not parsed by any Python code in this repo), so there is no
Python function to unit test directly. This module instead asserts the
source of truth (packages/codex-harness/codex/rules/codex-harness.rules)
contains the expected `rm -rf` flag-variant guardrail entries (R6), as a
smoke test against accidental removal/typos in future edits.
"""

from __future__ import annotations

from pathlib import Path

_RULES_PATH = Path(__file__).resolve().parents[1] / "codex" / "rules" / "codex-harness.rules"


class TestRmRfFlagVariants:
    """R6: flag-reordering/spelling variants of `rm -rf` must all be forbidden."""

    def test_rules_file_exists(self) -> None:
        assert _RULES_PATH.is_file()

    def test_contains_base_rm_rf_rule(self) -> None:
        content = _RULES_PATH.read_text(encoding="utf-8")
        assert 'pattern=["rm", "-rf"]' in content

    def test_contains_reversed_short_flags_variant(self) -> None:
        content = _RULES_PATH.read_text(encoding="utf-8")
        assert 'pattern=["rm", "-fr"]' in content

    def test_contains_split_short_flags_variants(self) -> None:
        content = _RULES_PATH.read_text(encoding="utf-8")
        assert 'pattern=["rm", "-r", "-f"]' in content
        assert 'pattern=["rm", "-f", "-r"]' in content

    def test_contains_long_flag_variants(self) -> None:
        content = _RULES_PATH.read_text(encoding="utf-8")
        assert 'pattern=["rm", "--recursive", "--force"]' in content
        assert 'pattern=["rm", "--force", "--recursive"]' in content

    def test_all_rm_variants_are_forbidden(self) -> None:
        content = _RULES_PATH.read_text(encoding="utf-8")
        variants = [
            '["rm", "-rf"]',
            '["rm", "-fr"]',
            '["rm", "-r", "-f"]',
            '["rm", "-f", "-r"]',
            '["rm", "--recursive", "--force"]',
            '["rm", "--force", "--recursive"]',
        ]
        for variant in variants:
            idx = content.index(variant)
            block = content[idx : idx + 200]
            assert 'decision="forbidden"' in block, f"{variant} is not forbidden"
