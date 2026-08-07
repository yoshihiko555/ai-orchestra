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

import yaml

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_config_loading",
    "packages/meta-harness/lib/meta_harness_common.py",
)
broker = load_module(
    "meta_harness_common_config_loading_broker",
    "packages/docker-runtime/docker/broker/broker.py",
)
profile = load_module(
    "meta_harness_profile_config_loading",
    "packages/meta-harness/lib/scenario_docker_profile.py",
)


def test_repository_synced_config_matches_package_default() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    package_config = repository_root / "packages/meta-harness/config/meta-harness.yaml"
    synced_config = repository_root / ".claude/config/meta-harness/meta-harness.yaml"

    assert synced_config.read_bytes() == package_config.read_bytes()


def test_yaml_docker_image_defaults_match_python_single_source_constants() -> None:
    """Issue #307 review: guards `mh.DEFAULT_SCENARIO_IMAGE`/`DEFAULT_BROKER_IMAGE`/
    `DEFAULT_CLAUDE_VERSION_PIN` (the Python-side single source of truth, re-exported by
    scenario_docker_cli.py/scenario_docker_image.py) against silent drift from the
    config-data source of truth (config/meta-harness.yaml)."""
    repository_root = Path(__file__).resolve().parents[3]
    package_config = repository_root / "packages/meta-harness/config/meta-harness.yaml"
    config = yaml.safe_load(package_config.read_text(encoding="utf-8"))

    isolation = config["evaluate"]["isolation"]
    assert isolation["image"] == mh.DEFAULT_SCENARIO_IMAGE
    assert isolation["image_pin"] == mh.DEFAULT_CLAUDE_VERSION_PIN
    assert isolation["broker"]["image"] == mh.DEFAULT_BROKER_IMAGE


def test_yaml_input_bytes_per_token_default_matches_python_default() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    package_config = repository_root / "packages/meta-harness/config/meta-harness.yaml"
    config = yaml.safe_load(package_config.read_text(encoding="utf-8"))

    yaml_value = config["evaluate"]["isolation"]["broker"][mh.BROKER_INPUT_BYTES_PER_TOKEN_KEY]
    python_value = mh.DEFAULTS["evaluate"]["isolation"]["broker"][
        mh.BROKER_INPUT_BYTES_PER_TOKEN_KEY
    ]

    assert yaml_value == python_value == 3


def test_broker_and_meta_harness_input_bytes_per_token_defaults_intentionally_diverge() -> None:
    """Meta-harness wires 3 explicitly; broker.py's 1 is only the fail-safe for
    unwired callers such as loop-harness."""
    meta_harness_value = mh.DEFAULTS["evaluate"]["isolation"]["broker"][
        mh.BROKER_INPUT_BYTES_PER_TOKEN_KEY
    ]

    assert broker.DEFAULT_INPUT_BYTES_PER_TOKEN == 1
    assert meta_harness_value == 3


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

    def test_input_bytes_per_token_override_reaches_broker_env(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "evaluate:\n  isolation:\n    broker:\n      input_bytes_per_token: 2\n",
            encoding="utf-8",
        )

        config = mh.load_config(git_project)
        broker_env = profile.broker_env(config, "run-token", 8787)

        assert config["evaluate"]["isolation"]["broker"]["input_bytes_per_token"] == 2
        assert broker_env["DR_BROKER_INPUT_BYTES_PER_TOKEN"] == "2"
        assert broker_env["MH_BROKER_INPUT_BYTES_PER_TOKEN"] == "2"

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
        assert config["frontier"]["cost_axis"] == "total_cost_usd"
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
        assert isolation["broker"]["pricing_upper_bound_usd_per_million"]["output"] == 15.0

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
        # R3-4 (fail-closed): config load failure must not silently keep DEFAULTS'
        # config_patch.allowlist (the routing-config ceiling) active.
        assert config["config_patch"]["allowlist"] == []

    def test_corrupt_local_yaml_fails_closed_on_config_patch_allowlist(
        self, git_project: Path
    ) -> None:
        """R3-4: 実 hook_common(mock なし)経由でも、存在するが壊れている
        `meta-harness.local.yaml` は config_patch.allowlist を DEFAULTS の3件のまま
        有効化してはならない。`load_package_config` は読み込み失敗を「ファイル不在」
        として silently 握り潰すため、例外は一切発生しないが、それでも fail-closed
        されることを検証する。"""
        config_dir = git_project / ".claude" / "config" / "meta-harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "meta-harness.yaml").write_text(
            (mh.PACKAGE_DIR / "config" / "meta-harness.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (config_dir / "meta-harness.local.yaml").write_text(
            "config_patch:\n  allowlist: [2\n", encoding="utf-8"
        )

        config = mh.load_config(git_project)

        assert config["config_patch"]["allowlist"] == []

    def test_absent_local_config_keeps_default_config_patch_allowlist(
        self, git_project: Path
    ) -> None:
        """R3-4 の対比観点: ファイル不在は corrupt ではないため、通常どおり DEFAULTS の
        allowlist(routing-config 向け3件)が有効なままであること。"""
        config = mh.load_config(git_project)

        assert config["config_patch"]["allowlist"] == list(mh.CONFIG_PATCH_ALLOWLIST_CEILING)

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

    def test_import_error_fallback_with_corrupt_local_yaml_fails_closed(
        self, git_project: Path, monkeypatch
    ) -> None:
        """R3-4: hook_common 不在の fallback 経路でも、壊れた local yaml は
        config_patch.allowlist を fail-closed(空配列)にすること。"""
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "retention:\n  keep_generations: [2\n", encoding="utf-8"
        )
        real_import = builtins.__import__

        def import_without_hook_common(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "hook_common":
                raise ImportError("hook_common unavailable")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        monkeypatch.setattr(builtins, "__import__", import_without_hook_common)

        config = mh.load_config(git_project)

        assert config["config_patch"]["allowlist"] == []
