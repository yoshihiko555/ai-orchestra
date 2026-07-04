"""sync_engine.apply_codex_harness_config() のユニットテスト。"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# sync_engine は scripts/ からの相対 import を使うため sys.path にスクリプトルートを追加
_repo_root = Path(__file__).resolve().parents[2]
_scripts_dir = str(_repo_root / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from tests.module_loader import load_module

sync_engine = load_module("sync_engine_harness_config", "scripts/lib/sync_engine.py")

HARNESS_TOML = """\
default_permissions = "project-edit"

[features]
hooks = true

[permissions.project-edit]
extends = ":workspace"

[permissions.project-edit.filesystem.":workspace_roots"]
"." = "write"
"**/.env" = "deny"
"**/.codex/hooks/**" = "deny"
"**/.codex/hooks.json" = "deny"
"**/.codex/rules/**" = "deny"
"**/.codex/validation.json" = "deny"
"**/.codex/config.toml" = "deny"
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
    def test_adds_missing_keys_and_sections(self, tmp_path: Path) -> None:
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
        assert parsed["default_permissions"] == "project-edit"
        assert parsed["features"]["hooks"] is True
        assert parsed["permissions"]["project-edit"]["extends"] == ":workspace"

    def test_denies_guardrail_self_tamper_paths(self, tmp_path: Path) -> None:
        """Deny list must protect the agent's own guardrail files from self-edit."""
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, 'model = "gpt-5.5"\n')

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])

        content = (project_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
        roots = parsed["permissions"]["project-edit"]["filesystem"][":workspace_roots"]
        assert roots["**/.codex/hooks/**"] == "deny"
        assert roots["**/.codex/hooks.json"] == "deny"
        assert roots["**/.codex/rules/**"] == "deny"
        assert roots["**/.codex/validation.json"] == "deny"
        assert roots["**/.codex/config.toml"] == "deny"
        assert roots["**/.claude/orchestra.json"] == "deny"

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

    def test_upserts_permissions_section_even_if_user_edited(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(
            tmp_path,
            '[permissions.project-edit]\nextends = "stale-value"\n',
        )

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])

        content = (project_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
        assert parsed["permissions"]["project-edit"]["extends"] == ":workspace"

    def test_second_run_is_idempotent(self, tmp_path: Path) -> None:
        orchestra_path = tmp_path / "orchestra"
        _write_harness_source(orchestra_path)
        project_dir = _make_project(tmp_path, 'model = "gpt-5.5"\n')

        sync_engine.apply_codex_harness_config(project_dir, orchestra_path, ["codex-harness"])
        changed_again = sync_engine.apply_codex_harness_config(
            project_dir, orchestra_path, ["codex-harness"]
        )

        assert changed_again is False

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
