"""orchestra-manager.py の .gitignore マージ処理テスト。"""

from __future__ import annotations

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager


class TestMergeGitignoreContent:
    def test_empty_creates_managed_block(self) -> None:
        merged = OrchestraManager.merge_gitignore_content("")
        assert OrchestraManager.GITIGNORE_BLOCK_START in merged
        assert OrchestraManager.GITIGNORE_BLOCK_END in merged
        for entry in OrchestraManager.GITIGNORE_CLAUDE_ENTRIES:
            assert entry in merged

    def test_appends_block_to_existing_content(self) -> None:
        existing = "node_modules/\n.env\n"
        merged = OrchestraManager.merge_gitignore_content(existing)
        assert merged.startswith("node_modules/\n.env\n")
        assert merged.count(OrchestraManager.GITIGNORE_BLOCK_START) == 1
        assert ".claude/Plans.md" in merged

    def test_replaces_existing_managed_block(self) -> None:
        existing = "\n".join(
            [
                "node_modules/",
                OrchestraManager.GITIGNORE_BLOCK_START,
                ".claude/old/",
                OrchestraManager.GITIGNORE_BLOCK_END,
                "coverage/",
                "",
            ]
        )
        merged = OrchestraManager.merge_gitignore_content(existing)
        assert ".claude/old/" not in merged
        assert ".claude/checkpoints/" in merged
        assert "node_modules/" in merged
        assert "coverage/" in merged
        assert merged.count(OrchestraManager.GITIGNORE_BLOCK_START) == 1

    def test_keeps_existing_when_all_entries_already_present(self) -> None:
        existing = (
            ".claude/docs/\n"
            ".claude/logs/\n"
            ".claude/state/\n"
            ".claude/checkpoints/\n"
            ".claude/Plans.md\n"
        )
        merged = OrchestraManager.merge_gitignore_content(existing)
        assert merged == existing
