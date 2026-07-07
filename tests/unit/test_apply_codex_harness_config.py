"""sync_engine.apply_codex_harness_config() のユニットテスト。"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

# sync_engine は scripts/ からの相対 import を使うため sys.path にスクリプトルートを追加
_repo_root = Path(__file__).resolve().parents[2]
_scripts_dir = str(_repo_root / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from tests.module_loader import load_module

sync_engine = load_module("sync_engine_harness_config", "scripts/lib/sync_engine.py")

# Import the exact `lib.toml_merge` module instance that sync_engine imports
# internally (via sys.modules caching), so `except TomlMergeError` below
# matches the real exception class raised by sync_engine's merge calls.
from lib.toml_merge import TomlMergeError  # noqa: E402

HARNESS_TOML = """\
[features]
hooks = true
"""

LEGACY_PROJECT_EDIT_CONFIG = """\
default_permissions = "project-edit"

[features]
hooks = true

[permissions.project-edit]
extends = ":workspace"

[permissions.project-edit.filesystem.":workspace_roots"]
"." = "write"
"**/.env" = "deny"
"**/*.env" = "deny"
"**/.ssh/**" = "deny"
"**/.aws/**" = "deny"
"**/*.pem" = "deny"
"**/*.key" = "deny"
"**/.codex/hooks/**" = "deny"
"**/.codex/hooks.json" = "deny"
"**/.codex/rules/**" = "deny"
"**/.codex/validation.json" = "deny"
"**/.codex/config.toml" = "deny"
"**/.codex/schemas/**" = "deny"
"**/.claude/orchestra.json" = "deny"
"""


def _write_harness_source(orchestra_path: Path) -> None:
    pkg_dir = orchestra_path / "packages" / "codex-harness" / "codex"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "config-harness.toml").write_text(HARNESS_TOML, encoding="utf-8")


def _make_project(tmp_path: Path, config_content: str | None) -> Path:
    project_dir = tmp_path / "project"
    codex_dir = project_dir / ".codex"
    codex_dir.mkdir(parents=True)
    if config_content is not None:
        (codex_dir / "config.toml").write_text(config_content, encoding="utf-8")
    return project_dir


class TestSkipConditions:
    def test_skips_when_codex_harness_not_installed(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, 'model = "gpt-5.5"\n')

        changed = sync_engine.apply_codex_harness_config(project_dir, orchestra_path, [])

        assert changed is False
        assert (project_dir / ".codex" / "config.toml").read_text(encoding="utf-8") == (
            'model = "gpt-5.5"\n'
        )

    def test_skips_when_config_toml_missing(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, None)

        changed = sync_engine.apply_codex_harness_config(
            project_dir, orchestra_path, ["codex-harness"]
        )

        assert changed is False
        assert not (project_dir / ".codex" / "config.toml").exists()

    def test_skips_when_harness_source_missing(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        (orchestra_path / "packages" / "codex-harness" / "codex").mkdir(parents=True)
        project_dir = _make_project(tmp_path, 'model = "gpt-5.5"\n')

        changed = sync_engine.apply_codex_harness_config(
            project_dir, orchestra_path, ["codex-harness"]
        )

        assert changed is False


class TestMergeBehavior:
    def test_adds_missing_hooks_feature_only(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, 'model = "gpt-5.5"\n')

        changed = sync_engine.apply_codex_harness_config(
            project_dir, orchestra_path, ["codex-harness"]
        )

        content = (project_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
        assert changed is True
        assert parsed["model"] == "gpt-5.5"
        assert parsed["features"]["hooks"] is True
        assert "default_permissions" not in parsed
        assert "permissions" not in parsed

    def test_removes_legacy_project_edit_profile(self, tmp_path: Path) -> None:
        """Upgrade migration removes the old harness-owned permission profile."""
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, LEGACY_PROJECT_EDIT_CONFIG)

        changed = sync_engine.apply_codex_harness_config(
            project_dir, orchestra_path, ["codex-harness"]
        )

        content = (project_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
        assert changed is True
        assert parsed["features"]["hooks"] is True
        assert "default_permissions" not in parsed
        assert "permissions" not in parsed

    def test_does_not_overwrite_existing_default_permissions(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(
            tmp_path, 'default_permissions = "custom"\n\n[features]\nhooks = false\n'
        )

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])

        content = (project_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
        assert parsed["default_permissions"] == "custom"
        assert parsed["features"]["hooks"] is False

    def test_does_not_remove_user_edited_project_edit_profile(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(
            tmp_path,
            'default_permissions = "project-edit"\n\n'
            '[permissions.project-edit]\nextends = "custom-parent"\n',
        )

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])

        content = (project_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
        assert parsed["default_permissions"] == "project-edit"
        assert parsed["permissions"]["project-edit"]["extends"] == "custom-parent"

    def test_second_run_is_idempotent(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, 'model = "gpt-5.5"\n')

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])
        changed_again = sync_engine.apply_codex_harness_config(
            project_dir, orchestra_path, ["codex-harness"]
        )

        assert changed_again is False

    def test_warns_when_features_hooks_kept_as_false(self, tmp_path: Path, capsys) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, "[features]\nhooks = false\n")

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])

        captured = capsys.readouterr()
        assert "features.hooks=false" in captured.err

    def test_no_warning_when_existing_values_are_kept(self, tmp_path: Path, capsys) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(
            tmp_path, 'default_permissions = "project-edit"\n\n[features]\nhooks = true\n'
        )

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_no_warning_when_keys_missing_and_added(self, tmp_path: Path, capsys) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, 'model = "gpt-5.5"\n')

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_non_dict_features_does_not_raise_attribute_error(self, tmp_path: Path) -> None:
        """R11: 既存 `features` がテーブルでない（スカラー等）場合に AttributeError で
        クラッシュしないこと。

        `features = "oops"` は tomllib で正当にパースできるが辞書ではないため、
        修正前は ``original_data.get("features", {}).get("hooks")`` が
        ``AttributeError: 'str' object has no attribute 'get'`` を送出していた。
        既存 `features` をテーブルへ書き換えること自体は別の構造的な TOML
        競合になり得るため（``TomlMergeError`` として fail-closed、呼び出し元
        `run_initial_sync` は既にこれを捕捉する）、ここでは AttributeError が
        発生しないことだけを確認する。
        """
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, 'features = "oops"\n')

        try:
            sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])
        except AttributeError:
            pytest.fail(
                "apply_codex_harness_config crashed with AttributeError for non-dict features"
            )
        except TomlMergeError:
            # A structural TOML conflict is a separate, already fail-closed /
            # caller-handled failure mode (see R19 / run_initial_sync's
            # try/except around this call).
            pass

    def test_does_not_touch_mcp_servers_section(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(
            tmp_path,
            '[mcp_servers.cocoindex]\ncommand = "uvx"\n',
        )

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])

        content = (project_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
        assert parsed["mcp_servers"]["cocoindex"]["command"] == "uvx"
