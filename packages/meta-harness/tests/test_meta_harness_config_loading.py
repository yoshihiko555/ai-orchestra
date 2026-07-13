"""config-loading レイヤリングのテスト（EV-22, `config-loading.md`, Sec5）。

`storage.root` の上書き（main root 解決との組み合わせ）は `test_main_root.py` 側で
別途検証済み。ここでは `.local.yaml` による一般的なキー上書きと、未設定キーが
ベース値のまま残ることを検証する。
"""

from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_config_loading",
    "packages/meta-harness/lib/meta_harness_common.py",
)


def test_repository_synced_config_matches_package_default() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    package_config = repository_root / "packages/meta-harness/config/meta-harness.yaml"
    synced_config = repository_root / ".claude/config/meta-harness/meta-harness.yaml"

    assert synced_config.read_bytes() == package_config.read_bytes()


class TestConfigLocalOverride:
    # EV-22
    def test_local_yaml_overrides_base_value(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "retention:\n  keep_generations: 2\n", encoding="utf-8"
        )

        config = mh.load_config(git_project)

        assert config["retention"]["keep_generations"] == 2

    # EV-22
    def test_unset_keys_keep_base_default(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "retention:\n  keep_generations: 2\n", encoding="utf-8"
        )

        config = mh.load_config(git_project)

        # local で触れていないキーはパッケージ既定の config/meta-harness.yaml の値のまま
        assert config["frontier"]["cost_axis"] == "total_tokens"
        assert config["scoring"]["critical_weight"] == 70

    def test_without_local_override_uses_package_default(self, git_project: Path) -> None:
        config = mh.load_config(git_project)
        assert config["retention"]["keep_generations"] == 5

    def test_package_default_enables_verified_docker_backend(self, git_project: Path) -> None:
        config = mh.load_config(git_project)
        isolation = config["evaluate"]["isolation"]
        assert isolation["backend"] == "docker"
        assert isolation["execution_backend"] == "docker"
        assert isolation["image_pin"] == "2.1.207 (Claude Code)"
        assert isolation["broker"]["pricing_upper_bound_usd_per_million"]["output"] == 75.0

    def test_malformed_local_yaml_warns_and_falls_back_to_defaults(
        self, git_project: Path, monkeypatch, capsys
    ) -> None:
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "retention:\n  keep_generations: [2\n", encoding="utf-8"
        )

        hook_common = types.ModuleType("hook_common")

        def load_package_config(package_name: str, filename: str, project_dir: str) -> dict:
            raise ValueError("failed to parse meta-harness.local.yaml")

        hook_common.load_package_config = load_package_config
        monkeypatch.setitem(sys.modules, "hook_common", hook_common)

        config = mh.load_config(git_project)
        stderr = capsys.readouterr().err

        assert config["retention"]["keep_generations"] == 5
        assert "warning" in stderr
        assert "meta-harness config" in stderr

    def test_import_error_fallback_stays_silent(
        self, git_project: Path, monkeypatch, capsys
    ) -> None:
        real_import = builtins.__import__

        def import_without_hook_common(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "hook_common":
                raise ImportError("hook_common unavailable")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        monkeypatch.setattr(builtins, "__import__", import_without_hook_common)

        config = mh.load_config(git_project)
        stderr = capsys.readouterr().err

        assert config["retention"]["keep_generations"] == 5
        assert "warning" not in stderr.lower()

    def test_import_error_fallback_applies_local_override(
        self, git_project: Path, monkeypatch
    ) -> None:
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "promote:\n  reservation_ttl_hours: 3\n", encoding="utf-8"
        )
        real_import = builtins.__import__

        def import_without_hook_common(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "hook_common":
                raise ImportError("hook_common unavailable")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        monkeypatch.setattr(builtins, "__import__", import_without_hook_common)

        config = mh.load_config(git_project)

        assert config["promote"]["reservation_ttl_hours"] == 3
        assert config["promote"]["allow_stale"] is False
