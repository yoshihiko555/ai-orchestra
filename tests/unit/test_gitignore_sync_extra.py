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

    def test_contains_codd_entry(self) -> None:
        """codd の生成物ディレクトリが管理エントリに含まれる。"""
        assert ".claude/codd/" in gitignore_mod.ENTRIES
        assert ".claude/codd/" in gitignore_mod.build_block()

    def test_contains_skill_evolution_data_entries(self) -> None:
        """skill-evolution の metrics/pending は無視対象だが lessons/ は追跡対象のまま。"""
        assert ".claude/skill-evolution/metrics/" in gitignore_mod.ENTRIES
        assert ".claude/skill-evolution/pending/" in gitignore_mod.ENTRIES
        assert ".claude/skill-evolution/metrics/" in gitignore_mod.build_block()
        assert ".claude/skill-evolution/pending/" in gitignore_mod.build_block()
        assert ".claude/skill-evolution/" not in gitignore_mod.ENTRIES

    def test_contains_codex_harness_generated_entries(self) -> None:
        """Codex CLI Harness の実行成果物ディレクトリ（runs/reports）が無視対象。"""
        assert ".codex/runs/" in gitignore_mod.ENTRIES
        assert ".codex/reports/" in gitignore_mod.ENTRIES
        assert ".codex/runs/" in gitignore_mod.build_block()
        assert ".codex/reports/" in gitignore_mod.build_block()

    def test_contains_meta_harness_store_entry(self) -> None:
        """meta-harness の store（candidates/runs/ledger 等）は無視対象（Phase 1a 実装漏れの修正）。"""
        assert ".claude/meta-harness/" in gitignore_mod.ENTRIES
        assert ".claude/meta-harness/" in gitignore_mod.build_block()


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

    def test_managed_block_regeneration_restores_meta_harness_entry(self, tmp_path: Path) -> None:
        """Phase 1a の実装漏れ回帰確認: 手編集で `.claude/meta-harness/` 行が管理ブロック内から
        消えていても、次回の同期（managed block 再生成）で復活すること。"""
        path = tmp_path / ".gitignore"
        stale_entries = [e for e in gitignore_mod.ENTRIES if e != ".claude/meta-harness/"]
        stale_block = "\n".join(
            [gitignore_mod.BLOCK_START, *stale_entries, gitignore_mod.BLOCK_END]
        )
        path.write_text(stale_block + "\n", encoding="utf-8")
        assert ".claude/meta-harness/" not in path.read_text(encoding="utf-8")

        changed = gitignore_mod.sync_gitignore(tmp_path)

        assert changed is True
        assert ".claude/meta-harness/" in path.read_text(encoding="utf-8")
