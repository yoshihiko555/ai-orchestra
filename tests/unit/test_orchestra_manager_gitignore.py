"""gitignore_sync モジュールの .gitignore マージ処理テスト。"""

from __future__ import annotations

from tests.module_loader import load_module

gitignore_mod = load_module("gitignore_sync", "scripts/lib/gitignore_sync.py")
BLOCK_START = gitignore_mod.BLOCK_START
BLOCK_END = gitignore_mod.BLOCK_END
ENTRIES = gitignore_mod.ENTRIES
merge_content = gitignore_mod.merge_content


class TestMergeGitignoreContent:
    def test_empty_creates_managed_block(self) -> None:
        merged = merge_content("")
        assert BLOCK_START in merged
        assert BLOCK_END in merged
        for entry in ENTRIES:
            assert entry in merged

    def test_appends_block_to_existing_content(self) -> None:
        existing = "node_modules/\n.env\n"
        merged = merge_content(existing)
        assert merged.startswith("node_modules/\n.env\n")
        assert merged.count(BLOCK_START) == 1
        assert ".claude/Plans.md" in merged

    def test_replaces_existing_managed_block(self) -> None:
        existing = "\n".join(
            [
                "node_modules/",
                BLOCK_START,
                ".claude/old/",
                BLOCK_END,
                "coverage/",
                "",
            ]
        )
        merged = merge_content(existing)
        assert ".claude/old/" not in merged
        assert ".claude/checkpoints/" in merged
        assert "node_modules/" in merged
        assert "coverage/" in merged
        assert merged.count(BLOCK_START) == 1

    def test_keeps_existing_when_all_entries_already_present(self) -> None:
        entries = "\n".join(ENTRIES) + "\n"
        merged = merge_content(entries)
        assert merged == entries
