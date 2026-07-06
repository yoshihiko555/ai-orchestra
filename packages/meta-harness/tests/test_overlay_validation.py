"""overlay / config-patch 検証のテスト（EV-04, EV-31 register 側, Sec1-7, Sec1-8）。"""

from __future__ import annotations

from pathlib import Path

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

        assert mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG) == []


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

        errors = mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG)

        assert any("symlink" in e for e in errors)

    # EV-04
    def test_path_outside_allowed_prefixes_is_rejected(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "not-facets").mkdir(parents=True)
        (overlay_dir / "not-facets" / "file.txt").write_text("x", encoding="utf-8")

        errors = mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG)

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

        errors = mh.validate_overlay(overlay_dir, config)

        assert any("denied prefix" in e for e in errors)

    # EV-31 (register side): 実際の denied_prefixes 既定値を使ったケース
    def test_default_denied_prefixes_reject_packages_meta_harness_path(
        self, tmp_path: Path
    ) -> None:
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "packages" / "meta-harness").mkdir(parents=True)
        (overlay_dir / "packages" / "meta-harness" / "hack.py").write_text("x", encoding="utf-8")

        errors = mh.validate_overlay(overlay_dir, _DEFAULT_OVERLAY_CONFIG)

        # allowed_prefixes ("facets/") にも合致しないため、少なくとも1つエラーが出る
        assert errors != []

    def test_nonexistent_overlay_dir_is_rejected(self, tmp_path: Path) -> None:
        errors = mh.validate_overlay(tmp_path / "does-not-exist", _DEFAULT_OVERLAY_CONFIG)
        assert any("does not exist" in e for e in errors)


class TestValidateConfigPatch:
    # EV-05 (lib レベル)
    def test_valid_shaped_patch_still_rejected_in_phase1a(self) -> None:
        config = _DEFAULT_OVERLAY_CONFIG
        patch = [{"file": "agent-routing/cli-tools.yaml", "key_path": "codex.model", "value": "x"}]

        errors = mh.validate_config_patch(patch, config, SCHEMA_DIR)

        assert len(errors) == 1
        assert "rejected in Phase 1a" in errors[0]

    def test_empty_patch_array_is_not_rejected(self) -> None:
        config = _DEFAULT_OVERLAY_CONFIG
        errors = mh.validate_config_patch([], config, SCHEMA_DIR)
        assert errors == []

    def test_malformed_patch_shape_returns_schema_errors(self) -> None:
        config = _DEFAULT_OVERLAY_CONFIG
        patch = [{"file": "x.yaml", "key_path": "a.b"}]  # missing "value"

        errors = mh.validate_config_patch(patch, config, SCHEMA_DIR)

        assert any("value" in e for e in errors)
        assert not any("rejected in Phase 1a" in e for e in errors)

    def test_allowlist_populated_does_not_reject_outright(self) -> None:
        # allowlist が空でなければ Phase1a 拒否メッセージは出ない（allowlist 自体の
        # チェックまでは Phase 1a では到達しない設計だが、関数の分岐自体は検証する）
        config = {"config_patch": {"allowlist": ["agent-routing/cli-tools.yaml#codex.model"]}}
        patch = [{"file": "agent-routing/cli-tools.yaml", "key_path": "codex.model", "value": "x"}]

        errors = mh.validate_config_patch(patch, config, SCHEMA_DIR)

        assert errors == []
