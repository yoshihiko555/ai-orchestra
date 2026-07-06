"""config-loading レイヤリングのテスト（EV-22, `config-loading.md`, Sec5）。

`storage.root` の上書き（main root 解決との組み合わせ）は `test_main_root.py` 側で
別途検証済み。ここでは `.local.yaml` による一般的なキー上書きと、未設定キーが
ベース値のまま残ることを検証する。
"""

from __future__ import annotations

from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_config_loading",
    "packages/meta-harness/lib/meta_harness_common.py",
)


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
