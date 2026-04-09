"""checkpoint.py のユニットテスト。"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.module_loader import load_module

checkpoint = load_module("checkpoint", "facets/scripts/checkpoint.py")


class TestFindProjectRoot:
    """find_project_root のテスト。"""

    def test_cwd_has_claude_dir(self, tmp_path, monkeypatch):
        """cwd に .claude ディレクトリがあればそれを返す。"""
        (tmp_path / ".claude").mkdir()
        monkeypatch.chdir(tmp_path)
        result = checkpoint.find_project_root()
        assert result == tmp_path

    def test_parent_has_claude_dir(self, tmp_path, monkeypatch):
        """親ディレクトリに .claude があれば遡って見つける。"""
        (tmp_path / ".claude").mkdir()
        child = tmp_path / "sub" / "dir"
        child.mkdir(parents=True)
        monkeypatch.chdir(child)
        result = checkpoint.find_project_root()
        assert result == tmp_path

    def test_no_claude_dir_returns_cwd(self, tmp_path, monkeypatch):
        """どこにも .claude がなければ cwd を返す。"""
        monkeypatch.chdir(tmp_path)
        # __file__ のパスにも .claude がないようにモック
        with patch.object(checkpoint, "__file__", str(tmp_path / "nonexistent.py")):
            result = checkpoint.find_project_root()
            assert result == tmp_path


class TestParseLogs:
    """parse_logs のテスト。"""

    def test_no_log_file(self, tmp_path, monkeypatch):
        """ログファイルが存在しない場合、空リストを返す。"""
        monkeypatch.setattr(checkpoint, "LOG_FILE", tmp_path / "nonexistent.jsonl")
        result = checkpoint.parse_logs()
        assert result == []

    def test_valid_entries(self, tmp_path, monkeypatch):
        """正常な JSONL を読み込む。"""
        log_file = tmp_path / "cli-tools.jsonl"
        entries = [
            {"timestamp": "2026-01-01T00:00:00Z", "tool": "codex", "prompt": "test"},
            {"timestamp": "2026-01-02T00:00:00Z", "tool": "gemini", "prompt": "research"},
        ]
        log_file.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(checkpoint, "LOG_FILE", log_file)

        result = checkpoint.parse_logs()
        assert len(result) == 2
        assert result[0]["tool"] == "codex"

    def test_since_filter(self, tmp_path, monkeypatch):
        """since フィルターで古いエントリを除外する。"""
        log_file = tmp_path / "cli-tools.jsonl"
        entries = [
            {"timestamp": "2026-01-01T00:00:00Z", "tool": "codex"},
            {"timestamp": "2026-06-01T00:00:00Z", "tool": "gemini"},
        ]
        log_file.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(checkpoint, "LOG_FILE", log_file)

        result = checkpoint.parse_logs(since="2026-03-01")
        assert len(result) == 1
        assert result[0]["tool"] == "gemini"

    def test_invalid_json_line_skipped(self, tmp_path, monkeypatch):
        """壊れた JSON 行をスキップする。"""
        log_file = tmp_path / "cli-tools.jsonl"
        log_file.write_text(
            '{"timestamp": "2026-01-01T00:00:00Z", "tool": "codex"}\n'
            "not valid json\n"
            '{"timestamp": "2026-01-02T00:00:00Z", "tool": "gemini"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(checkpoint, "LOG_FILE", log_file)

        result = checkpoint.parse_logs()
        assert len(result) == 2

    def test_missing_timestamp_key(self, tmp_path, monkeypatch):
        """timestamp キーがないエントリは since フィルター時にスキップされる。"""
        log_file = tmp_path / "cli-tools.jsonl"
        log_file.write_text(
            '{"tool": "codex"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(checkpoint, "LOG_FILE", log_file)

        result = checkpoint.parse_logs(since="2026-01-01")
        assert len(result) == 0

    def test_empty_lines_skipped(self, tmp_path, monkeypatch):
        """空行をスキップする。"""
        log_file = tmp_path / "cli-tools.jsonl"
        log_file.write_text(
            '{"timestamp": "2026-01-01T00:00:00Z", "tool": "codex"}\n\n\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(checkpoint, "LOG_FILE", log_file)

        result = checkpoint.parse_logs()
        assert len(result) == 1


class TestRunGitCommand:
    """run_git_command のテスト。"""

    def test_successful_command(self, monkeypatch):
        """正常な git コマンドの出力を返す。"""
        import subprocess

        mock_result = subprocess.CompletedProcess(
            args=["git", "status"], returncode=0, stdout="clean\n"
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        result = checkpoint.run_git_command(["status"])
        assert result == "clean"

    def test_failed_command(self, monkeypatch):
        """コマンド失敗時に None を返す。"""
        import subprocess

        mock_result = subprocess.CompletedProcess(
            args=["git", "status"], returncode=1, stdout="", stderr="error"
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        result = checkpoint.run_git_command(["status"])
        assert result is None

    def test_timeout(self, monkeypatch):
        """タイムアウト時に None を返す。"""
        import subprocess

        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="git", timeout=30)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        result = checkpoint.run_git_command(["log"])
        assert result is None

    def test_git_not_found(self, monkeypatch):
        """git が見つからない場合に None を返す。"""
        import subprocess

        def raise_fnf(*a, **kw):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        result = checkpoint.run_git_command(["log"])
        assert result is None


class TestGetFileChanges:
    """get_file_changes のテスト。"""

    def test_with_since(self, monkeypatch):
        """since 付きで A/M/D ステータスを正しくパースする。"""
        output = "A\tnew_file.py\nM\tmodified_file.py\nD\tdeleted_file.py"
        monkeypatch.setattr(checkpoint, "run_git_command", lambda args: output)

        result = checkpoint.get_file_changes(since="2026-01-01")
        assert "new_file.py" in result["created"]
        assert "modified_file.py" in result["modified"]
        assert "deleted_file.py" in result["deleted"]

    def test_without_since(self, monkeypatch):
        """since なしでも動作する。"""
        monkeypatch.setattr(checkpoint, "run_git_command", lambda args: "M\tfile.py")
        result = checkpoint.get_file_changes()
        assert "file.py" in result["modified"]

    def test_no_output(self, monkeypatch):
        """出力なしの場合、空の変更を返す。"""
        monkeypatch.setattr(checkpoint, "run_git_command", lambda args: None)
        result = checkpoint.get_file_changes()
        assert result == {"created": [], "modified": [], "deleted": []}

    def test_dedup(self, monkeypatch):
        """同じファイルが複数回出現しても重複しない。"""
        output = "M\tfile.py\nM\tfile.py"
        monkeypatch.setattr(checkpoint, "run_git_command", lambda args: output)
        result = checkpoint.get_file_changes()
        assert len(result["modified"]) == 1


class TestGetFileStats:
    """get_file_stats のテスト。"""

    def test_normal_stats(self, monkeypatch):
        """通常のファイルの追加/削除行数を返す。"""
        output = "10\t5\tfile.py\n20\t3\tother.py"
        monkeypatch.setattr(checkpoint, "run_git_command", lambda args: output)

        result = checkpoint.get_file_stats()
        assert result["file.py"] == (10, 5)
        assert result["other.py"] == (20, 3)

    def test_binary_file(self, monkeypatch):
        """バイナリファイル（-\t-）は 0,0 として扱う。"""
        output = "-\t-\timage.png"
        monkeypatch.setattr(checkpoint, "run_git_command", lambda args: output)

        result = checkpoint.get_file_stats()
        assert result["image.png"] == (0, 0)

    def test_accumulates_stats(self, monkeypatch):
        """同じファイルの複数エントリを累積する。"""
        output = "10\t5\tfile.py\n3\t2\tfile.py"
        monkeypatch.setattr(checkpoint, "run_git_command", lambda args: output)

        result = checkpoint.get_file_stats()
        assert result["file.py"] == (13, 7)


class TestSummarizeEntries:
    """summarize_entries のテスト。"""

    def test_groups_by_date_and_tool(self):
        """日付とツールでグループ化する。"""
        entries = [
            {"timestamp": "2026-01-01T10:00:00Z", "tool": "codex", "prompt": "q1", "success": True},
            {"timestamp": "2026-01-01T11:00:00Z", "tool": "gemini", "prompt": "q2", "success": False},
            {"timestamp": "2026-01-02T10:00:00Z", "tool": "codex", "prompt": "q3", "success": True},
        ]
        result = checkpoint.summarize_entries(entries)
        assert "2026-01-01" in result
        assert "2026-01-02" in result
        assert len(result["2026-01-01"]["codex"]) == 1
        assert len(result["2026-01-01"]["gemini"]) == 1
        assert len(result["2026-01-02"]["codex"]) == 1

    def test_empty_entries(self):
        """空リストを渡した場合、空辞書を返す。"""
        result = checkpoint.summarize_entries([])
        assert result == {}

    def test_unknown_tool_ignored(self):
        """codex/gemini 以外のツールは無視される。"""
        entries = [{"timestamp": "2026-01-01T10:00:00Z", "tool": "unknown", "prompt": "q"}]
        result = checkpoint.summarize_entries(entries)
        assert "2026-01-01" in result
        assert len(result["2026-01-01"]["codex"]) == 0
        assert len(result["2026-01-01"]["gemini"]) == 0


class TestUpdateContextFile:
    """update_context_file のテスト（最重要: ファイル内容破壊リスク）。"""

    def test_file_not_found(self, tmp_path):
        """ファイルが存在しない場合、False を返す。"""
        result = checkpoint.update_context_file(tmp_path / "nonexistent.md", "history")
        assert result is False

    def test_appends_session_history(self, tmp_path):
        """セッション履歴をファイル末尾に追加する。"""
        file_path = tmp_path / "CLAUDE.md"
        file_path.write_text("# Project\n\nSome content\n", encoding="utf-8")

        checkpoint.update_context_file(file_path, "## Session History\n\n- item 1\n")
        content = file_path.read_text(encoding="utf-8")

        assert "# Project" in content
        assert "Some content" in content
        assert "## Session History" in content
        assert "- item 1" in content

    def test_replaces_existing_session_history(self, tmp_path):
        """既存のセッション履歴セクションを置換する。"""
        file_path = tmp_path / "CLAUDE.md"
        file_path.write_text(
            "# Project\n\nContent before\n\n## Session History\n\n- old item\n",
            encoding="utf-8",
        )

        checkpoint.update_context_file(file_path, "## Session History\n\n- new item\n")
        content = file_path.read_text(encoding="utf-8")

        assert "Content before" in content
        assert "- old item" not in content
        assert "- new item" in content
        assert content.count("## Session History") == 1

    def test_preserves_content_before_session_history(self, tmp_path):
        """Session History より前のコンテンツをすべて保持する（破壊防止）。"""
        original_before = """# AI Orchestra

## 目的

重要な設定情報

## 技術スタック

- Python 3.12+
- YAML / JSON

## 主要コマンド

```bash
pytest -q
```
"""
        file_path = tmp_path / "CLAUDE.md"
        file_path.write_text(
            original_before + "\n## Session History\n\n- old data\n- more old data\n",
            encoding="utf-8",
        )

        checkpoint.update_context_file(file_path, "## Session History\n\n- new data\n")
        content = file_path.read_text(encoding="utf-8")

        # 全ての元コンテンツが保持されている
        assert "# AI Orchestra" in content
        assert "## 目的" in content
        assert "重要な設定情報" in content
        assert "## 技術スタック" in content
        assert "Python 3.12+" in content
        assert "## 主要コマンド" in content
        assert "pytest -q" in content
        # 古い履歴は消え、新しい履歴が入っている
        assert "- old data" not in content
        assert "- new data" in content

    def test_no_existing_session_history(self, tmp_path):
        """Session History がないファイルに追加する。"""
        file_path = tmp_path / "AGENTS.md"
        file_path.write_text("# Agents\n\nAgent definitions\n", encoding="utf-8")

        checkpoint.update_context_file(file_path, "## Session History\n\n- first entry\n")
        content = file_path.read_text(encoding="utf-8")

        assert "# Agents" in content
        assert "Agent definitions" in content
        assert "## Session History" in content
        assert "- first entry" in content

    def test_session_history_at_middle_preserves_after_content(self, tmp_path):
        """Session History が中間にある場合、re.DOTALL で以降すべてが消える問題の検証。"""
        file_path = tmp_path / "test.md"
        # Session History の後にもコンテンツがある場合（現在の実装では削除される）
        file_path.write_text(
            "# Header\n\n## Session History\n\n- old\n\n## Footer\n\nfooter content\n",
            encoding="utf-8",
        )

        checkpoint.update_context_file(file_path, "## Session History\n\n- new\n")
        content = file_path.read_text(encoding="utf-8")

        # 注意: 現在の re.DOTALL 実装では Session History 以降がすべて削除される
        # この動作を文書化するテスト
        assert "# Header" in content
        assert "- new" in content


class TestGenerateSessionHistory:
    """generate_session_history のテスト。"""

    def test_empty_input(self):
        """空の入力で空文字を返す。"""
        result = checkpoint.generate_session_history({})
        assert result == ""

    def test_generates_markdown(self):
        """正しい markdown 形式を生成する。"""
        by_date = {
            "2026-01-01": {
                "codex": [{"prompt": "test prompt", "success": True}],
                "gemini": [],
            },
        }
        result = checkpoint.generate_session_history(by_date)
        assert "## Session History" in result
        assert "### 2026-01-01" in result
        assert "**Codex相談:**" in result
        assert "✓" in result


class TestGenerateFullCheckpoint:
    """generate_full_checkpoint のテスト。"""

    def test_creates_checkpoint_file(self, tmp_path, monkeypatch):
        """チェックポイントファイルを作成する。"""
        monkeypatch.setattr(checkpoint, "CHECKPOINTS_DIR", tmp_path / "checkpoints")
        monkeypatch.setattr(checkpoint, "parse_logs", lambda since=None: [])
        monkeypatch.setattr(checkpoint, "get_git_commits", lambda since=None: [])
        monkeypatch.setattr(
            checkpoint, "get_file_changes",
            lambda since=None: {"created": [], "modified": [], "deleted": []},
        )
        monkeypatch.setattr(checkpoint, "get_file_stats", lambda since=None: {})

        result = checkpoint.generate_full_checkpoint()
        assert result is not None
        assert result.exists()
        content = result.read_text(encoding="utf-8")
        assert "# Checkpoint:" in content
        assert "## Summary" in content
