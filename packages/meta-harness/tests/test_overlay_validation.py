"""overlay / config-patch 検証のテスト（EV-04, EV-31 register 側, Sec1-7, Sec1-8）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_overlay",
    "packages/meta-harness/lib/meta_harness_common.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO_ROOT / "packages" / "meta-harness" / "schemas"

_DEFAULT_OVERLAY_CONFIG = mh.DEFAULTS


class TestValidateOverlayAccepts:
    def test_normal_facets_file_is_accepted(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "facets" / "foo").mkdir(parents=True)
        (overlay_dir / "facets" / "foo" / "SKILL.md").write_text("ok", encoding="utf-8")

        assert (
            mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG, target="claude-harness") == []
        )


class TestValidateOverlayFileUnit:
    """`_validate_overlay_file` を直接検証する（`..` を含む相対パスは実ファイルシステム上に
    そのまま再現できないため、`entry` / `overlay_dir` を手動構築して検証する）。"""

    _allowed = ("facets/",)
    _denied = ("packages/meta-harness/", ".claude/meta-harness/", "docs/evaluation/", ".github/")

    # EV-04
    def test_dot_dot_segment_is_rejected(self) -> None:
        overlay_dir = Path("/fake-overlay")
        entry = overlay_dir / "facets" / ".." / "escape.txt"

        errors = mh._validate_overlay_file(entry, overlay_dir, self._allowed, self._denied)

        assert any("'..'" in e for e in errors)


class TestValidateOverlayRejects:
    # EV-04
    def test_symlink_is_rejected(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "facets").mkdir(parents=True)
        target = tmp_path / "outside-target.txt"
        target.write_text("secret", encoding="utf-8")
        symlink_path = overlay_dir / "facets" / "linked.txt"
        symlink_path.symlink_to(target)

        errors = mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG, target="claude-harness")

        assert any("symlink" in e for e in errors)

    # EV-04
    def test_path_outside_allowed_prefixes_is_rejected(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "not-facets").mkdir(parents=True)
        (overlay_dir / "not-facets" / "file.txt").write_text("x", encoding="utf-8")

        errors = mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG, target="claude-harness")

        assert any("outside allowed prefixes" in e for e in errors)

    # EV-31 (register side)
    def test_denied_prefix_is_rejected_even_if_matches_allowed(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "facets" / "meta-harness-self").mkdir(parents=True)
        (overlay_dir / "facets" / "meta-harness-self" / "x.txt").write_text("x", encoding="utf-8")
        config = {
            "overlay": {
                "allowed_prefixes": ["facets/"],
                "denied_prefixes": ["facets/meta-harness-self/"],
            }
        }

        errors = mh.validate_overlay(overlay_dir, config, target="claude-harness")

        assert any("denied prefix" in e for e in errors)

    # EV-31 (register side): 実際の denied_prefixes 既定値を使ったケース
    def test_default_denied_prefixes_reject_packages_meta_harness_path(
        self, tmp_path: Path
    ) -> None:
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "packages" / "meta-harness").mkdir(parents=True)
        (overlay_dir / "packages" / "meta-harness" / "hack.py").write_text("x", encoding="utf-8")

        errors = mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG, target="claude-harness")

        # allowed_prefixes ("facets/") にも合致しないため、少なくとも1つエラーが出る
        assert errors != []

    def test_nonexistent_overlay_dir_is_rejected(self, tmp_path: Path) -> None:
        errors = mh.validate_overlay(
            tmp_path / "does-not-exist",
            _DEFAULT_OVERLAY_CONFIG,
            target="claude-harness",
        )
        assert any("does not exist" in e for e in errors)

    # PR #162 レビュー指摘 (FIX D): config-patch.json という予約サイドカー名であっても、
    # symlink なら（早期 continue で検査を迂回させず）symlink として拒否されること
    def test_config_patch_json_symlink_is_rejected_not_exempted(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / "facets" / "foo").mkdir(parents=True)
        (overlay_dir / "facets" / "foo" / "SKILL.md").write_text("ok", encoding="utf-8")
        outside_target = tmp_path / "outside-config-patch.json"
        outside_target.write_text("[]", encoding="utf-8")
        symlink_path = overlay_dir / mh.CONFIG_PATCH_FILENAME
        symlink_path.symlink_to(outside_target)

        errors = mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG, target="claude-harness")

        assert any("symlink" in e for e in errors)


class TestValidateConfigPatch:
    # EV-62 / EV-64 (lib レベル)
    def test_allowlisted_human_routing_patch_is_accepted(self) -> None:
        config = _DEFAULT_OVERLAY_CONFIG
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "codex.model",
                "value": "gpt-5.6-sol",
            }
        ]

        errors = mh.validate_config_patch(
            patch,
            config,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert errors == []

    def test_empty_patch_array_is_not_rejected(self) -> None:
        config = _DEFAULT_OVERLAY_CONFIG
        errors = mh.validate_config_patch(
            [], config, SCHEMA_DIR, target="claude-harness", created_by="human"
        )
        assert errors == []

    def test_routing_target_requires_non_empty_patch(self) -> None:
        errors = mh.validate_config_patch(
            [],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )
        assert any("require a non-empty" in error for error in errors)

    def test_malformed_patch_shape_returns_schema_errors(self) -> None:
        config = _DEFAULT_OVERLAY_CONFIG
        patch = [{"file": "x.yaml", "key_path": "a.b"}]  # missing "value"

        errors = mh.validate_config_patch(
            patch,
            config,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("value" in e for e in errors)

    def test_allowlist_cannot_exceed_frozen_ceiling(self) -> None:
        config = {"config_patch": {"allowlist": ["agent-routing/cli-tools.yaml#codex.flags"]}}
        patch = [{"file": "agent-routing/cli-tools.yaml", "key_path": "codex.model", "value": "x"}]

        errors = mh.validate_config_patch(
            patch,
            config,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("CONFIG_PATCH_ALLOWLIST_CEILING" in error for error in errors)

    # EV-80
    @pytest.mark.parametrize(
        ("key_path", "value"),
        [
            ("agents.debugger.tool", "auto"),
            ("antigravity.model", "gemini-3.1-pro"),
        ],
    )
    def test_proposer_allowed_key_kinds_are_accepted(self, key_path: str, value: str) -> None:
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": key_path,
                "value": value,
            }
        ]

        errors = mh.validate_config_patch(
            patch,
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="proposer",
        )

        assert errors == []

    # EV-80
    def test_proposer_codex_model_patch_is_rejected(self) -> None:
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "codex.model",
                "value": "gpt-5.6-sol",
            }
        ]

        errors = mh.validate_config_patch(
            patch,
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="proposer",
        )

        assert any("created_by='proposer' is not allowed" in error for error in errors)

    def test_allowlist_ceiling_is_exactly_the_initial_release(self) -> None:
        assert mh.CONFIG_PATCH_ALLOWLIST_CEILING == (
            "agent-routing/cli-tools.yaml#agents.*.tool",
            "agent-routing/cli-tools.yaml#codex.model",
            "agent-routing/cli-tools.yaml#antigravity.model",
        )

    # EV-80
    def test_created_by_policy_is_exactly_the_frozen_phase_a_map(self) -> None:
        assert mh.CONFIG_PATCH_ALLOWED_CREATED_BY == {
            "agent-routing/cli-tools.yaml#agents.*.tool": frozenset({"human", "proposer"}),
            "agent-routing/cli-tools.yaml#codex.model": frozenset({"human"}),
            "agent-routing/cli-tools.yaml#antigravity.model": frozenset({"human", "proposer"}),
        }

    # EV-80
    def test_runtime_config_cannot_expand_created_by_policy(self) -> None:
        config = {
            "config_patch": {
                "allowlist": list(mh.CONFIG_PATCH_ALLOWLIST_CEILING),
                "allowed_created_by": {
                    "agent-routing/cli-tools.yaml#codex.model": ["human", "proposer"]
                },
            }
        }
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
            config,
            SCHEMA_DIR,
            target="routing-config",
            created_by="proposer",
        )

        assert any("created_by='proposer' is not allowed" in error for error in errors)

    # EV-80
    def test_ceiling_entry_missing_from_created_by_map_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mh,
            "CONFIG_PATCH_ALLOWED_CREATED_BY",
            {
                key: value
                for key, value in mh.CONFIG_PATCH_ALLOWED_CREATED_BY.items()
                if not key.endswith("#antigravity.model")
            },
        )
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "antigravity.model",
                    "value": "gemini-3.1-pro",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("no created_by policy for ceiling entry" in error for error in errors)

    @pytest.mark.parametrize(
        ("patch", "expected"),
        [
            (
                [{"file": "other/cli-tools.yaml", "key_path": "codex.model", "value": "x"}],
                "not allowlisted",
            ),
            (
                [
                    {
                        "file": "agent-routing/cli-tools.yaml",
                        "key_path": "codex.flags",
                        "value": "x",
                    }
                ],
                "not allowlisted",
            ),
            (
                [
                    {
                        "file": "agent-routing/cli-tools.yaml",
                        "key_path": "agents.a.b.tool",
                        "value": "auto",
                    }
                ],
                "not allowlisted",
            ),
            (
                [
                    {
                        "file": "agent-routing/cli-tools.yaml",
                        "key_path": "agents.__proto__.tool",
                        "value": "auto",
                    }
                ],
                "dangerous key segment",
            ),
            (
                [
                    {
                        "file": "agent-routing/cli-tools.yaml",
                        "key_path": "agents..tool",
                        "value": "auto",
                    }
                ],
                "empty segment",
            ),
        ],
    )
    def test_non_allowlisted_and_dangerous_patch_targets_are_rejected(
        self, patch: list[dict], expected: str
    ) -> None:
        errors = mh.validate_config_patch(
            patch,
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any(expected in error for error in errors)

    @pytest.mark.parametrize(
        "entry",
        [
            "agent-routing/cli-tools.yaml#agents.**.tool",
            "agent-routing/cli-tools.yaml#agents.foo*.tool",
            "agent-routing/cli-tools.yaml#codex.model#extra",
            "agent-routing/cli-tools.yaml#agents..tool",
            "agent-*/cli-tools.yaml#codex.model",
        ],
    )
    def test_malformed_allowlist_entries_fail_closed(self, entry: str) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "x",
                }
            ],
            {"config_patch": {"allowlist": [entry]}},
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert errors

    def test_agents_wildcard_matches_exactly_one_segment(self) -> None:
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.debugger.tool",
                "value": "auto",
            }
        ]

        assert (
            mh.validate_config_patch(
                patch,
                _DEFAULT_OVERLAY_CONFIG,
                SCHEMA_DIR,
                target="routing-config",
                created_by="human",
            )
            == []
        )

    def test_known_agent_name_is_accepted(self) -> None:
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.backend-python-dev.tool",
                "value": "codex",
            }
        ]

        assert (
            mh.validate_config_patch(
                patch,
                _DEFAULT_OVERLAY_CONFIG,
                SCHEMA_DIR,
                target="routing-config",
                created_by="human",
            )
            == []
        )

    def test_unknown_agent_name_is_rejected(self) -> None:
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.no-such-agent.tool",
                "value": "codex",
            }
        ]

        errors = mh.validate_config_patch(
            patch,
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("unknown agent name: no-such-agent" in error for error in errors)

    def test_duplicate_patch_targets_are_rejected(self) -> None:
        item = {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": "codex.model",
            "value": "x",
        }

        errors = mh.validate_config_patch(
            [item, dict(item)],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("duplicate config patch target" in error for error in errors)

    @pytest.mark.parametrize("tool", ["codex", "antigravity", "claude-direct", "auto"])
    def test_all_released_agent_tool_values_are_accepted(self, tool: str) -> None:
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.debugger.tool",
                "value": tool,
            }
        ]

        assert (
            mh.validate_config_patch(
                patch,
                _DEFAULT_OVERLAY_CONFIG,
                SCHEMA_DIR,
                target="routing-config",
                created_by="human",
            )
            == []
        )

    @pytest.mark.parametrize(
        ("key_path", "value", "expected"),
        [
            ("agents.debugger.tool", "bogus", "must be one of"),
            ("agents.debugger.tool", True, "must be one of"),
            ("codex.model", 123, "injection-safe slug"),
            ("codex.model", False, "injection-safe slug"),
            ("codex.model", "", "injection-safe slug"),
            ("codex.model", "bad:model", "injection-safe slug"),
            ("antigravity.model", "not-in-the-model-allowlist", "not in model_allowlist"),
        ],
    )
    def test_invalid_patch_values_are_rejected(
        self, key_path: str, value: object, expected: str
    ) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": key_path,
                    "value": value,
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any(expected in error for error in errors)

    def test_allowlisted_antigravity_model_is_accepted(self) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "antigravity.model",
                    "value": "gemini-3.1-pro",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert errors == []

    # EV-81
    def test_configured_codex_model_is_the_only_initial_allowlisted_value(self) -> None:
        loaded = mh._load_agent_routing_config(SCHEMA_DIR)

        assert loaded["codex"]["model_allowlist"] == [loaded["codex"]["model"]]
        assert loaded["codex"]["model_allowlist"] == ["gpt-5.6-sol"]

    # EV-81
    def test_allowlisted_codex_model_is_accepted(self) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert errors == []

    # EV-81
    def test_non_allowlisted_codex_model_is_rejected(self) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "not-in-the-model-allowlist",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("codex model is not in model_allowlist" in error for error in errors)

    # EV-81
    @pytest.mark.parametrize("codex_config", [{}, {"model_allowlist": []}])
    def test_missing_or_empty_codex_model_allowlist_rejects_any_model(
        self, codex_config: dict
    ) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
            agent_routing_config={"codex": codex_config},
        )

        assert any("codex model is not in model_allowlist" in error for error in errors)

    def test_empty_antigravity_model_allowlist_rejects_any_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mh,
            "_load_antigravity_model_allowlist",
            lambda _schema_dir, _agent_routing_config: frozenset(),
        )

        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "antigravity.model",
                    "value": "gemini-3.1-pro",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("not in model_allowlist" in error for error in errors)

    @pytest.mark.parametrize("value", ["off", "123", "1.5", "null", "no"])
    def test_yaml_ambiguous_model_values_are_rejected(self, value: str) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": value,
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("YAML-ambiguous" in error for error in errors)

    def test_unambiguous_allowlisted_model_value_is_accepted(self) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert errors == []

    def test_config_patch_file_with_backslashes_is_rejected(self) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing\\..\\..\\etc\\passwd",
                    "key_path": "codex.model",
                    "value": "gpt-5.3-codex",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert any("must not contain backslashes" in error for error in errors)

    def test_unknown_creator_is_rejected(self) -> None:
        errors = mh.validate_config_patch(
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
            _DEFAULT_OVERLAY_CONFIG,
            SCHEMA_DIR,
            target="routing-config",
            created_by="automation",
        )

        assert any("created_by='automation' is not allowed" in error for error in errors)
