"""gitignore_sync.py の追加ユニットテスト。"""

from __future__ import annotations

from pathlib import Path

from tests.module_loader import load_module

gitignore_mod = load_module("gitignore_sync_extra", "scripts/lib/gitignore_sync.py")


class TestBuildBlock:
    """build_block のテスト。"""

    def test_contains_markers_and_all_entries(self) -> None:
        """管理ブロックにマーカーと全エントリが含まれる。"""
        block = gitignore_mod.build_block()
        lines = block.splitlines()

        assert lines[0] == gitignore_mod.BLOCK_START
        assert lines[-1] == gitignore_mod.BLOCK_END
        assert lines[1:-1] == gitignore_mod.ENTRIES
        assert block.endswith("\n")


class TestSyncGitignore:
    """sync_gitignore のテスト。"""

    def test_creates_gitignore_when_missing(self, tmp_path: Path) -> None:
        """`.gitignore` がなければ新規作成する。"""
        changed = gitignore_mod.sync_gitignore(tmp_path)

        assert changed is True
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert gitignore_mod.BLOCK_START in content

    def test_returns_false_when_no_change_needed(self, tmp_path: Path) -> None:
        """内容に変更がなければ False を返す。"""
        path = tmp_path / ".gitignore"
        path.write_text(gitignore_mod.build_block(), encoding="utf-8")

        changed = gitignore_mod.sync_gitignore(tmp_path)

        assert changed is False
        assert path.read_text(encoding="utf-8") == gitignore_mod.build_block()

    def test_updates_existing_outdated_block(self, tmp_path: Path) -> None:
        """古い管理ブロックは置き換える。"""
        path = tmp_path / ".gitignore"
        path.write_text(
            "\n".join(
                [
                    "node_modules/",
                    gitignore_mod.BLOCK_START,
                    ".claude/old/",
                    gitignore_mod.BLOCK_END,
                    "",
                ]
            ),
            encoding="utf-8",
        )

        changed = gitignore_mod.sync_gitignore(tmp_path)

        assert changed is True
        content = path.read_text(encoding="utf-8")
        assert ".claude/old/" not in content
        assert ".claude/Plans.md" in content
