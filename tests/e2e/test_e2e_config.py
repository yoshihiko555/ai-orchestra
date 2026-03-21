"""E2E テスト: Config loading フロー。

テスト計画 e2e-test-plan.md セクション 4 に対応。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.conftest import REPO_ROOT, run_orchex, run_session_start

# hook_common を動的にロード
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
from hook_common import load_package_config  # noqa: E402


def _setup_with_config(project: Path) -> None:
    """setup essential + SessionStart でベースラインを作る。"""
    run_orchex("setup", "essential", project=project)
    run_session_start(project, "init")


class TestConfigLoading:
    """4. Config loading フロー"""

    def test_base_config_only(self, e2e_project: Path) -> None:
        """#40: cli-tools.yaml のみでベース値が使用される"""
        _setup_with_config(e2e_project)
        config = load_package_config("agent-routing", "cli-tools.yaml", str(e2e_project))
        assert config["codex"]["model"] is not None
        assert isinstance(config["codex"]["model"], str)

    def test_local_override(self, e2e_project: Path) -> None:
        """#41: cli-tools.local.yaml で local のキーが上書き"""
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "codex:\n  model: e2e-override\n", encoding="utf-8"
        )
        config = load_package_config("agent-routing", "cli-tools.yaml", str(e2e_project))
        assert config["codex"]["model"] == "e2e-override"

    def test_deep_merge(self, e2e_project: Path) -> None:
        """#42: local でネストされたキーの一部を上書きしても他は維持"""
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "codex:\n  model: e2e-override\n", encoding="utf-8"
        )
        config = load_package_config("agent-routing", "cli-tools.yaml", str(e2e_project))
        assert config["codex"]["model"] == "e2e-override"
        assert config["codex"]["sandbox"]["analysis"] is not None

    def test_codex_disabled(self, e2e_project: Path) -> None:
        """#43: codex.enabled: false"""
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "codex:\n  enabled: false\n", encoding="utf-8"
        )
        config = load_package_config("agent-routing", "cli-tools.yaml", str(e2e_project))
        assert config["codex"]["enabled"] is False

    def test_gemini_disabled(self, e2e_project: Path) -> None:
        """#44: gemini.enabled: false"""
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "gemini:\n  enabled: false\n", encoding="utf-8"
        )
        config = load_package_config("agent-routing", "cli-tools.yaml", str(e2e_project))
        assert config["gemini"]["enabled"] is False
