"""turn-end-summary.py のユニットテスト。"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

# worktree の stale コピーを参照しないよう、テスト中は REPO_ROOT を強制する。
os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)

_qg_hooks = str(REPO_ROOT / "packages" / "quality-gates" / "hooks")
_core_hooks = str(REPO_ROOT / "packages" / "core" / "hooks")
_audit_hooks = str(REPO_ROOT / "packages" / "audit" / "hooks")
for p in [_qg_hooks, _core_hooks, _audit_hooks]:
    if p not in sys.path:
        sys.path.insert(0, p)

turn_end = load_module(
    "turn_end_summary",
    "packages/quality-gates/hooks/turn-end-summary.py",
)


class TestExtractCodeFiles:
    """`_extract_code_files` のテスト。"""

    def test_filters_to_code_extensions(self) -> None:
        """コード拡張子のみ残ることを確認する。"""
        ctx = {
            "modified_files": [
                "a.py",
                "README.md",
                "b.ts",
                "data.json",
                "c.go",
                "notes.txt",
            ]
        }
        files = turn_end._extract_code_files(ctx)
        assert set(files) == {"a.py", "b.ts", "c.go"}

    def test_handles_missing_list(self) -> None:
        """modified_files が無い場合は空リストを返す。"""
        assert turn_end._extract_code_files({}) == []


class TestBuildSummaryText:
    """`build_summary_text` のテスト。"""

    def test_empty_returns_empty_string(self) -> None:
        """何も変更なし & Plans 空なら空文字を返すことを確認する。"""
        assert turn_end.build_summary_text(code_files=[], plans_counts={}, total_modified=0) == ""

    def test_includes_modified_and_plans(self) -> None:
        """変更数と Plans 件数の両方が含まれることを確認する。"""
        text = turn_end.build_summary_text(
            code_files=["a.py"],
            plans_counts={"WIP": 2, "TODO": 3, "blocked": 0, "done": 5},
            total_modified=4,
        )
        assert "Turn Summary" in text
        assert "4 files" in text
        assert "WIP 2" in text
        assert "TODO 3" in text
        # done は表示しない（運用ノイズ削減）
        assert "done 5" not in text

    def test_reminder_only_when_code_files(self) -> None:
        """code_files があるときだけ Reminder 行が出ることを確認する。"""
        text_without = turn_end.build_summary_text(
            code_files=[], plans_counts={"WIP": 1}, total_modified=2
        )
        assert "Reminder" not in text_without

        text_with = turn_end.build_summary_text(
            code_files=["foo.py"], plans_counts={}, total_modified=1
        )
        assert "Reminder" in text_with
        assert "foo.py" in text_with


class TestMain:
    """`main` の入出力動作を確認する。"""

    def _invoke(
        self, payload: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> str:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        turn_end.main()
        return capsys.readouterr().out

    def test_stop_hook_active_skips(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """stop_hook_active=true のときは出力しないことを確認する（再入防止）。"""
        (tmp_path / ".claude").mkdir()
        payload = {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "stop_hook_active": True,
        }
        output = self._invoke(payload, monkeypatch, capsys)
        assert output == ""

    def test_no_changes_no_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Plans.md も working-context も空なら何も出力しないことを確認する。"""
        (tmp_path / ".claude").mkdir()
        payload = {"session_id": "s1", "cwd": str(tmp_path)}
        output = self._invoke(payload, monkeypatch, capsys)
        assert output == ""

    def test_outputs_additional_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Plans.md と working-context から additionalContext を組み立てることを確認する。"""
        claude_dir = tmp_path / ".claude"
        (claude_dir / "context" / "shared").mkdir(parents=True)
        (claude_dir / "context" / "shared" / "working-context.json").write_text(
            json.dumps(
                {
                    "modified_files": ["main.py", "docs.md", "server.ts"],
                    "updated_at": "2026-04-11T03:00:00+00:00",
                }
            )
        )
        (claude_dir / "Plans.md").write_text("# Plans\n- `cc:WIP` A task\n- `cc:TODO` B task\n")

        payload = {"session_id": "s1", "cwd": str(tmp_path)}
        output = self._invoke(payload, monkeypatch, capsys)
        assert output
        parsed = json.loads(output)
        text = parsed["hookSpecificOutput"]["additionalContext"]
        assert "Modified: 3 files" in text
        # main.py と server.ts はコード
        assert "code: 2" in text
        assert "WIP 1" in text
        assert "TODO 1" in text
        # Stop hook は decision を一切返さない
        assert "decision" not in parsed
