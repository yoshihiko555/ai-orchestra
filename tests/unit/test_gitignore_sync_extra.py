"""gitignore_sync.py の追加ユニットテスト。"""

from __future__ import annotations

from pathlib import Path

from tests.module_loader import REPO_ROOT, load_module

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

    def test_contains_plan_directory_without_legacy_files(self) -> None:
        """plan ディレクトリを無視対象にし、旧形式のファイルは含めない。"""
        lines = gitignore_mod.build_block().splitlines()

        assert ".claude/plans/" in lines
        assert ".claude/Plans.md" not in lines
        assert ".claude/Plans.archive.md" not in lines
        assert ".claude/plans/" in gitignore_mod.ENTRIES
        assert ".claude/Plans.md" not in gitignore_mod.ENTRIES
        assert ".claude/Plans.archive.md" not in gitignore_mod.ENTRIES

    def test_repository_managed_block_matches_build_block(self) -> None:
        """配布元の管理ブロックも生成結果と一致する。"""
        content = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        start = content.index(gitignore_mod.BLOCK_START)
        end = content.index(gitignore_mod.BLOCK_END, start) + len(gitignore_mod.BLOCK_END)

        assert content[start:end] + "\n" == gitignore_mod.build_block()

    def test_excludes_skill_evolution_v1_data_entries(self) -> None:
        """skill-evolution の v1 データディレクトリは管理エントリに含めない。"""
        assert ".claude/skill-evolution/metrics/" not in gitignore_mod.ENTRIES
        assert ".claude/skill-evolution/pending/" not in gitignore_mod.ENTRIES
        assert ".claude/skill-evolution/metrics/" not in gitignore_mod.build_block()
        assert ".claude/skill-evolution/pending/" not in gitignore_mod.build_block()
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

    def test_contains_loop_harness_state_entry(self) -> None:
        """loop-harness の実行状態（state.json/journal/認証情報コピー等）は無視対象。"""
        assert ".claude/loop/" in gitignore_mod.ENTRIES
        assert ".claude/loop/" in gitignore_mod.build_block()


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
        assert ".claude/Plans.md" not in content

    def test_managed_block_regeneration_adds_plan_directory(self, tmp_path: Path) -> None:
        """plan ディレクトリの除外を加え、旧形式のファイルは含めない。"""
        path = tmp_path / ".gitignore"
        stale_entries = [entry for entry in gitignore_mod.ENTRIES if entry != ".claude/plans/"]
        path.write_text(
            "\n".join([gitignore_mod.BLOCK_START, *stale_entries, gitignore_mod.BLOCK_END]) + "\n",
            encoding="utf-8",
        )

        changed = gitignore_mod.sync_gitignore(tmp_path)

        assert changed is True
        content = path.read_text(encoding="utf-8")
        assert content == gitignore_mod.build_block()
        assert ".claude/plans/" in content.splitlines()
        assert ".claude/Plans.md" not in content.splitlines()
        assert ".claude/Plans.archive.md" not in content.splitlines()

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
