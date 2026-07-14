"""E2E テスト: Config loading フロー。

テスト計画 e2e-test-plan.md セクション 4 に対応。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.conftest import REPO_ROOT, run_orchex, run_session_start

# hook_common を動的にロード
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
from hook_common import (  # noqa: E402
    load_cli_tools_config,
    load_package_config,
    normalize_cli_tools_config,
)


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

    def test_antigravity_disabled(self, e2e_project: Path) -> None:
        """antigravity.enabled: false"""
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "antigravity:\n  enabled: false\n", encoding="utf-8"
        )
        config = load_package_config("agent-routing", "cli-tools.yaml", str(e2e_project))
        assert config["antigravity"]["enabled"] is False

    def test_legacy_gemini_disabled_normalizes_to_antigravity(self, e2e_project: Path) -> None:
        """#44 (EV-04): base に antigravity.enabled の明示設定が無い場合、
        旧 gemini.enabled: false の .local.yaml がフォールバックとして
        antigravity.enabled: false に正規化される。"""
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        # プロジェクト直下の base を「antigravity 未対応の旧バージョン」相当で上書きし、
        # antigravity.enabled が一切明示されていない状態を再現する。
        (config_dir / "cli-tools.yaml").write_text("gemini:\n  enabled: false\n", encoding="utf-8")
        config = normalize_cli_tools_config(
            load_package_config("agent-routing", "cli-tools.yaml", str(e2e_project))
        )
        assert config["antigravity"]["enabled"] is False

    def test_legacy_gemini_disabled_applies_fallback_in_migrated_project(
        self, e2e_project: Path
    ) -> None:
        """EV-04 (migrated-project regression, Issue #125 PR レビュー指摘):
        base（現行 cli-tools.yaml）が antigravity.enabled: true を明示している
        通常の移行済みプロジェクトでも、.local.yaml に旧 gemini.enabled: false
        のみが残っている（antigravity キー自体が無い）場合は、後方互換
        フォールバックとして antigravity が無効化される。

        base/local を merge してから正規化する load_package_config +
        normalize_cli_tools_config の組み合わせでは、base の既定値
        antigravity.enabled: true を「ユーザーの明示設定」と誤認してしまい
        フォールバックが機能しない。load_cli_tools_config はレイヤーごとに
        正規化してから merge するため、この移行ケースを正しく処理する。
        """
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "gemini:\n  enabled: false\n", encoding="utf-8"
        )
        config = load_cli_tools_config(str(e2e_project))
        assert config["antigravity"]["enabled"] is False

    def test_legacy_gemini_disabled_does_not_override_explicit_antigravity(
        self, e2e_project: Path
    ) -> None:
        """EV-13（2026-07-04 人間レビュー裁定）: 同一レイヤー（.local.yaml）内で
        antigravity.enabled と旧 gemini.enabled: false が両方明示されている
        場合、antigravity.enabled が優先される（競合時は antigravity 優先）。"""
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "antigravity:\n  enabled: true\ngemini:\n  enabled: false\n", encoding="utf-8"
        )
        config = load_cli_tools_config(str(e2e_project))
        assert config["antigravity"]["enabled"] is True

    def test_legacy_tool_gemini_normalizes_to_antigravity(self, e2e_project: Path) -> None:
        """旧 agents.*.tool: gemini の .local.yaml が antigravity に読み替えられる。"""
        _setup_with_config(e2e_project)
        config_dir = e2e_project / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "cli-tools.local.yaml").write_text(
            "agents:\n  researcher:\n    tool: gemini\n", encoding="utf-8"
        )
        config = normalize_cli_tools_config(
            load_package_config("agent-routing", "cli-tools.yaml", str(e2e_project))
        )
        assert config["agents"]["researcher"]["tool"] == "antigravity"
