"""Context Sharing / Task State / Plans.md アーカイブの E2E テスト。

テスト計画: .claude/docs/test-plans/context-taskstate-test-plan.md
git 初期化不要のため e2e_project fixture ではなく tmp_path を直接使用する。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "packages" / "core" / "hooks"


def _run_hook(
    script: str,
    payload: dict,
    *,
    project: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """hook スクリプトをサブプロセスで実行する。"""
    env = {**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)}
    if project:
        payload.setdefault("cwd", str(project))
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=10,
    )


def _run_load_task_state(
    project: Path,
) -> subprocess.CompletedProcess[str]:
    """load-task-state.py を SessionStart として実行する。"""
    payload = json.dumps({"cwd": str(project)})
    env = {**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / "load-task-state.py")],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=10,
    )


def _entries_dir(project: Path) -> Path:
    return project / ".claude" / "context" / "session" / "entries"


def _working_context_path(project: Path) -> Path:
    return project / ".claude" / "context" / "shared" / "working-context.json"


def _meta_path(project: Path) -> Path:
    return project / ".claude" / "context" / "session" / "meta.json"


# ===========================================================================
# 1. Task State
# ===========================================================================


class TestTaskStateSummary:
    """テスト計画 1.1: SessionStart サマリー出力"""

    def test_summary_with_all_states(self, tmp_path: Path) -> None:
        """#1: Plans.md に WIP/TODO/done タスクがある状態でサマリー出力"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Test\n"
            "### Phase 1: Dev `cc:WIP`\n"
            "- `cc:done` setup\n"
            "- `cc:WIP` implement\n"
            "- `cc:TODO` test\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "[task-memory] 3 tasks" in result.stdout
        assert "done: 1" in result.stdout
        assert "WIP: 1" in result.stdout
        assert "TODO: 1" in result.stdout

    def test_wip_displayed_before_todo(self, tmp_path: Path) -> None:
        """#2: WIP が TODO より先に表示される"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n## Project: Test\n### Phase 1\n- `cc:WIP` working\n- `cc:TODO` pending\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        wip_pos = result.stdout.find("WIP:")
        todo_pos = result.stdout.find("Next TODO:")
        assert wip_pos < todo_pos

    def test_blocked_with_reason(self, tmp_path: Path) -> None:
        """#3: blocked タスクに理由が表示される"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Test\n"
            "### Phase 1\n"
            "- `cc:blocked` stuck task — 理由: waiting for API\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "Blocked:" in result.stdout
        assert "stuck task" in result.stdout
        assert "waiting for API" in result.stdout

    def test_no_plans_file(self, tmp_path: Path) -> None:
        """#4: Plans.md が存在しない場合"""
        result = _run_load_task_state(tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_empty_plans_file(self, tmp_path: Path) -> None:
        """#5: Plans.md が空の場合"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text("", encoding="utf-8")
        result = _run_load_task_state(tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_max_display_truncation(self, tmp_path: Path) -> None:
        """#6: max_display_tasks を超えるタスク数で省略表示"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        tasks = "\n".join(f"- `cc:TODO` task {i}" for i in range(25))
        (plans_dir / "Plans.md").write_text(
            f"# Plans\n\n## Project: Big\n### Phase 1\n{tasks}\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "... and" in result.stdout
        assert "more" in result.stdout


class TestTaskStateCustomMarkers:
    """テスト計画 1.2: カスタムマーカー"""

    def test_duplicate_markers_fallback(self, tmp_path: Path) -> None:
        """#8: 重複マーカー設定でデフォルトにフォールバック"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n## Project: Test\n### Phase 1\n- `cc:TODO` task\n",
            encoding="utf-8",
        )
        config_dir = plans_dir / "config" / "core"
        config_dir.mkdir(parents=True)
        (config_dir / "task-memory.yaml").write_text(
            "plans_file: .claude/Plans.md\n"
            "show_summary_on_start: true\n"
            "max_display_tasks: 20\n"
            "markers:\n"
            "  todo: dup\n"
            "  wip: dup\n"
            "  done: done\n"
            "  blocked: blocked\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "fallback to defaults" in result.stderr
        assert "[task-memory]" in result.stdout


# ===========================================================================
# 2. Context Sharing
# ===========================================================================


class TestContextInit:
    """テスト計画 2.1: セッション初期化"""

    def test_session_start_creates_meta(self, tmp_path: Path) -> None:
        """#1: SessionStart で meta.json が作成される"""
        result = _run_load_task_state(tmp_path)
        assert result.returncode == 0
        assert _meta_path(tmp_path).is_file()

    def test_meta_contains_session_id_and_timestamp(self, tmp_path: Path) -> None:
        """#2: meta.json に session_id と started_at が含まれる"""
        _run_load_task_state(tmp_path)
        meta = json.loads(_meta_path(tmp_path).read_text(encoding="utf-8"))
        assert "session_id" in meta
        assert "started_at" in meta
        assert len(meta["session_id"]) == 36  # UUID format

    def test_idempotent_session_id(self, tmp_path: Path) -> None:
        """#3: 2回目の呼び出しで session_id が変わらない"""
        _run_load_task_state(tmp_path)
        meta1 = json.loads(_meta_path(tmp_path).read_text(encoding="utf-8"))
        _run_load_task_state(tmp_path)
        meta2 = json.loads(_meta_path(tmp_path).read_text(encoding="utf-8"))
        assert meta1["session_id"] == meta2["session_id"]


class TestUpdateWorkingContext:
    """テスト計画 2.2: ファイル変更追跡"""

    def test_edit_tracks_file(self, tmp_path: Path) -> None:
        """#4: Edit ツールでファイル編集時に追跡される"""
        result = _run_hook(
            "update-working-context.py",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "src" / "foo.py")},
            },
            project=tmp_path,
        )
        assert result.returncode == 0
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert "src/foo.py" in ctx["modified_files"]

    def test_write_tracks_file(self, tmp_path: Path) -> None:
        """#5: Write ツールでファイル作成時に追跡される"""
        result = _run_hook(
            "update-working-context.py",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "src" / "bar.py")},
            },
            project=tmp_path,
        )
        assert result.returncode == 0
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert "src/bar.py" in ctx["modified_files"]

    def test_excludes_claude_internal(self, tmp_path: Path) -> None:
        """#6: .claude/ 配下のファイルは追跡されない"""
        _run_hook(
            "update-working-context.py",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / ".claude" / "Plans.md")},
            },
            project=tmp_path,
        )
        assert not _working_context_path(tmp_path).exists()

    def test_deduplicates_same_file(self, tmp_path: Path) -> None:
        """#7: 同一ファイルを複数回編集しても重複なし"""
        for _ in range(3):
            _run_hook(
                "update-working-context.py",
                {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": str(tmp_path / "src" / "foo.py")},
                },
                project=tmp_path,
            )
        ctx = json.loads(_working_context_path(tmp_path).read_text(encoding="utf-8"))
        assert ctx["modified_files"].count("src/foo.py") == 1


class TestCaptureTaskResult:
    """テスト計画 2.3: サブエージェント結果キャプチャ"""

    def test_agent_tool_creates_entry(self, tmp_path: Path) -> None:
        """#9: tool_name='Agent' で entries が作成される"""
        result = _run_hook(
            "capture-task-result.py",
            {
                "tool_name": "Agent",
                "tool_input": {
                    "subagent_type": "tester",
                    "description": "Run tests",
                    "prompt": "pytest -q",
                },
                "tool_response": "All tests passed.",
            },
            project=tmp_path,
        )
        assert result.returncode == 0
        entries = list(_entries_dir(tmp_path).glob("tester_*.json"))
        assert len(entries) == 1

    def test_entry_contains_required_fields(self, tmp_path: Path) -> None:
        """#10: エントリに必須フィールドが含まれる"""
        _run_hook(
            "capture-task-result.py",
            {
                "tool_name": "Agent",
                "tool_input": {
                    "subagent_type": "debugger",
                    "description": "Fix bug",
                    "prompt": "find the issue",
                },
                "tool_response": "Fixed null pointer.",
            },
            project=tmp_path,
        )
        entries = list(_entries_dir(tmp_path).glob("debugger_*.json"))
        stored = json.loads(entries[0].read_text(encoding="utf-8"))
        assert stored["agent_id"] == "debugger"
        assert stored["task_name"] == "Fix bug"
        assert stored["status"] == "done"
        assert stored["summary"] == "Fixed null pointer."
        assert "timestamp" in stored

    def test_multiple_entries_for_same_agent(self, tmp_path: Path) -> None:
        """#11: 同一 agent_id で複数エントリが作成される"""
        for i in range(3):
            _run_hook(
                "capture-task-result.py",
                {
                    "tool_name": "Agent",
                    "tool_input": {
                        "subagent_type": "tester",
                        "description": f"Run {i}",
                        "prompt": "test",
                    },
                    "tool_response": f"result {i}",
                },
                project=tmp_path,
            )
        entries = list(_entries_dir(tmp_path).glob("tester_*.json"))
        assert len(entries) == 3

    def test_task_tool_name_backward_compat(self, tmp_path: Path) -> None:
        """後方互換: tool_name='Task' でも動作する"""
        _run_hook(
            "capture-task-result.py",
            {
                "tool_name": "Task",
                "tool_input": {"subagent_type": "tester", "prompt": "test"},
                "tool_response": "ok",
            },
            project=tmp_path,
        )
        entries = list(_entries_dir(tmp_path).glob("tester_*.json"))
        assert len(entries) == 1

    def test_edit_tool_does_not_create_entry(self, tmp_path: Path) -> None:
        """#13: Edit ツールではトリガーされない"""
        _run_hook(
            "capture-task-result.py",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "foo.py"},
                "tool_response": "done",
            },
            project=tmp_path,
        )
        assert not _entries_dir(tmp_path).exists()


class TestInjectSharedContext:
    """テスト計画 2.4: コンテキスト注入"""

    def _setup_entries(self, project: Path, count: int = 1) -> None:
        """テスト用のエントリーを直接作成する。"""
        entries_dir = _entries_dir(project)
        entries_dir.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            entry = {
                "agent_id": f"agent-{i}",
                "task_name": f"task-{i}",
                "summary": f"result-{i}" if i < 6 else "x" * 300,
                "timestamp": f"2026-01-0{i + 1}T00:00:00+00:00",
                "status": "done",
            }
            (entries_dir / f"agent-{i}_{i:04d}.json").write_text(
                json.dumps(entry), encoding="utf-8"
            )

    def test_injects_context_into_prompt(self, tmp_path: Path) -> None:
        """#14: entries がある状態で Agent ツール実行時にコンテキスト注入"""
        self._setup_entries(tmp_path, count=1)
        result = _run_hook(
            "inject-shared-context.py",
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "backend-python-dev", "prompt": "implement X"},
            },
            project=tmp_path,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "[Shared Context]" in ctx
        assert "agent-0" in ctx

    def test_limits_to_five_entries(self, tmp_path: Path) -> None:
        """#15: 最新5件のみ注入される"""
        self._setup_entries(tmp_path, count=7)
        result = _run_hook(
            "inject-shared-context.py",
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "tester", "prompt": "test"},
            },
            project=tmp_path,
        )
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        # 最新5件（agent-2〜agent-6）が注入、agent-0/agent-1 は含まれない
        assert "agent-0" not in ctx
        assert "agent-1" not in ctx
        assert "agent-2" in ctx
        assert "agent-6" in ctx

    def test_truncates_long_summary(self, tmp_path: Path) -> None:
        """#16: 200文字超のサマリーがトランケートされる"""
        self._setup_entries(tmp_path, count=7)
        result = _run_hook(
            "inject-shared-context.py",
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "tester", "prompt": "test"},
            },
            project=tmp_path,
        )
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        # agent-6 は 300文字のサマリー → 200文字 + "..." にトランケート
        assert "..." in ctx

    def test_includes_working_context(self, tmp_path: Path) -> None:
        """#17: working-context.json の内容が注入される"""
        # working-context を作成
        shared_dir = tmp_path / ".claude" / "context" / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / "working-context.json").write_text(
            json.dumps(
                {
                    "modified_files": ["src/main.py", "src/utils.py"],
                    "current_phase": "implementation",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        result = _run_hook(
            "inject-shared-context.py",
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "tester", "prompt": "test"},
            },
            project=tmp_path,
        )
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "src/main.py" in ctx
        assert "implementation" in ctx

    def test_no_injection_when_empty(self, tmp_path: Path) -> None:
        """#18: entries も working-context も空の場合は注入なし"""
        result = _run_hook(
            "inject-shared-context.py",
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "tester", "prompt": "test"},
            },
            project=tmp_path,
        )
        # 注入するものがないので stdout は空（approve 出力なし）
        assert result.stdout.strip() == ""


class TestCleanupSessionContext:
    """テスト計画 2.5: セッション終了クリーンアップ"""

    def test_removes_session_dir(self, tmp_path: Path) -> None:
        """#19: session/ ディレクトリが削除される"""
        # まず context を初期化
        _run_load_task_state(tmp_path)
        assert _meta_path(tmp_path).is_file()

        result = _run_hook("cleanup-session-context.py", {}, project=tmp_path)
        assert result.returncode == 0
        assert not (tmp_path / ".claude" / "context" / "session").exists()

    def test_removes_working_context(self, tmp_path: Path) -> None:
        """#20: working-context.json が削除される"""
        _run_hook(
            "update-working-context.py",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "src" / "foo.py")},
            },
            project=tmp_path,
        )
        assert _working_context_path(tmp_path).is_file()

        _run_hook("cleanup-session-context.py", {}, project=tmp_path)
        assert not _working_context_path(tmp_path).exists()

    def test_shared_dir_remains(self, tmp_path: Path) -> None:
        """#21: shared/ ディレクトリ自体は残る"""
        _run_load_task_state(tmp_path)
        _run_hook(
            "update-working-context.py",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "foo.py")},
            },
            project=tmp_path,
        )

        _run_hook("cleanup-session-context.py", {}, project=tmp_path)
        assert (tmp_path / ".claude" / "context" / "shared").is_dir()

    def test_removes_lock_file(self, tmp_path: Path) -> None:
        """#22: ロックファイルも削除される"""
        _run_hook(
            "update-working-context.py",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "foo.py")},
            },
            project=tmp_path,
        )
        lock_path = tmp_path / ".claude" / "context" / "shared" / "working-context.json.lock"
        assert lock_path.is_file()

        _run_hook("cleanup-session-context.py", {}, project=tmp_path)
        assert not lock_path.exists()


# ===========================================================================
# 3. Plans.md アーカイブ
# ===========================================================================


class TestArchiveDetection:
    """テスト計画 3.1: 完了判定"""

    def test_all_phases_done_is_archived(self, tmp_path: Path) -> None:
        """#1: 全フェーズ cc:done のプロジェクトがアーカイブされる"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Complete\n"
            "### Phase 1: Done `cc:done`\n"
            "- `cc:done` task1\n"
            "### Phase 2: Done `cc:done`\n"
            "- `cc:done` task2\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "archived 1" in result.stdout
        archive = plans_dir / "Plans.archive.md"
        assert archive.is_file()
        assert "## Project: Complete" in archive.read_text(encoding="utf-8")

    def test_partial_todo_not_archived(self, tmp_path: Path) -> None:
        """#2: 一部フェーズが TODO のプロジェクトはアーカイブされない"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Partial\n"
            "### Phase 1: Done `cc:done`\n"
            "- `cc:done` task1\n"
            "### Phase 2: Pending `cc:TODO`\n"
            "- `cc:TODO` task2\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "archived" not in result.stdout
        assert not (plans_dir / "Plans.archive.md").exists()

    def test_empty_phase_not_completed(self, tmp_path: Path) -> None:
        """#4: 空フェーズがあるプロジェクトは未完了"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: EmptyPhase\n"
            "### Phase 1: Done `cc:done`\n"
            "- `cc:done` task1\n"
            "### Phase 2: Empty\n"
            "#### TODO\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "archived" not in result.stdout

    def test_no_phases_not_completed(self, tmp_path: Path) -> None:
        """#5: フェーズが1つもないプロジェクトは未完了"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n## Project: NoPhase\n- `cc:done` orphan\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "archived" not in result.stdout


class TestArchiveExecution:
    """テスト計画 3.2: アーカイブ実行"""

    def test_removes_completed_from_plans(self, tmp_path: Path) -> None:
        """#6: 完了 PJ が Plans.md から除去される"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Done\n"
            "### Phase 1: Done `cc:done`\n"
            "- `cc:done` task\n"
            "---\n\n"
            "## Project: Active\n"
            "### Phase 1: WIP `cc:WIP`\n"
            "- `cc:WIP` working\n",
            encoding="utf-8",
        )
        _run_load_task_state(tmp_path)
        updated = (plans_dir / "Plans.md").read_text(encoding="utf-8")
        assert "## Project: Done" not in updated
        assert "## Project: Active" in updated

    def test_archive_file_format(self, tmp_path: Path) -> None:
        """#7: Plans.archive.md の形式が正しい"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n## Project: Alpha\n### Phase 1: Done `cc:done`\n- `cc:done` task\n",
            encoding="utf-8",
        )
        _run_load_task_state(tmp_path)
        archive = (plans_dir / "Plans.archive.md").read_text(encoding="utf-8")
        assert archive.startswith("# Archived Plans")
        assert "## Archived: " in archive
        assert "## Project: Alpha" in archive

    def test_mixed_projects_only_done_archived(self, tmp_path: Path) -> None:
        """#8: 混在ケースで done PJ のみアーカイブ"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Done1\n"
            "### Phase 1 `cc:done`\n"
            "- `cc:done` task\n"
            "---\n\n"
            "## Project: Done2\n"
            "### Phase 1 `cc:done`\n"
            "- `cc:done` task\n"
            "---\n\n"
            "## Project: Active\n"
            "### Phase 1 `cc:TODO`\n"
            "- `cc:TODO` task\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "archived 2" in result.stdout
        updated = (plans_dir / "Plans.md").read_text(encoding="utf-8")
        assert "Done1" not in updated
        assert "Done2" not in updated
        assert "Active" in updated

    def test_decisions_remain_when_active_projects(self, tmp_path: Path) -> None:
        """#10: 一部 PJ 残存時に Decisions/Notes は Plans.md に残留"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Done\n"
            "### Phase 1 `cc:done`\n"
            "- `cc:done` task\n"
            "---\n\n"
            "## Project: Active\n"
            "### Phase 1 `cc:TODO`\n"
            "- `cc:TODO` task\n"
            "---\n\n"
            "## Decisions\n"
            "- 2026-03-22: keep API\n"
            "\n"
            "## Notes\n"
            "- important note\n",
            encoding="utf-8",
        )
        _run_load_task_state(tmp_path)
        updated = (plans_dir / "Plans.md").read_text(encoding="utf-8")
        assert "## Decisions" in updated
        assert "## Notes" in updated
        archive = (plans_dir / "Plans.archive.md").read_text(encoding="utf-8")
        assert "## Decisions" not in archive

    def test_decisions_archived_when_all_done(self, tmp_path: Path) -> None:
        """#9: 全 PJ 完了時に Decisions/Notes もアーカイブ"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n"
            "## Project: Done\n"
            "### Phase 1 `cc:done`\n"
            "- `cc:done` task\n"
            "---\n\n"
            "## Decisions\n"
            "- 2026-03-22: keep API\n"
            "\n"
            "## Notes\n"
            "- important note\n",
            encoding="utf-8",
        )
        _run_load_task_state(tmp_path)
        updated = (plans_dir / "Plans.md").read_text(encoding="utf-8")
        assert "## Decisions" not in updated
        assert "## Notes" not in updated
        archive = (plans_dir / "Plans.archive.md").read_text(encoding="utf-8")
        assert "## Decisions" in archive
        assert "## Notes" in archive

    def test_append_to_existing_archive(self, tmp_path: Path) -> None:
        """#11: 既存 archive への追記"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.archive.md").write_text(
            "# Archived Plans\n\n## Archived: 2026-03-01\n\n## Project: Old\n\n---\n",
            encoding="utf-8",
        )
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n## Project: New\n### Phase 1 `cc:done`\n- `cc:done` task\n",
            encoding="utf-8",
        )
        _run_load_task_state(tmp_path)
        archive = (plans_dir / "Plans.archive.md").read_text(encoding="utf-8")
        assert archive.count("# Archived Plans") == 1
        assert "## Project: Old" in archive
        assert "## Project: New" in archive

    def test_archive_runs_even_with_summary_disabled(self, tmp_path: Path) -> None:
        """#12: show_summary_on_start: false でもアーカイブ実行される"""
        plans_dir = tmp_path / ".claude"
        plans_dir.mkdir(parents=True)
        (plans_dir / "Plans.md").write_text(
            "# Plans\n\n## Project: Done\n### Phase 1 `cc:done`\n- `cc:done` task\n",
            encoding="utf-8",
        )
        config_dir = plans_dir / "config" / "core"
        config_dir.mkdir(parents=True)
        (config_dir / "task-memory.yaml").write_text(
            "plans_file: .claude/Plans.md\nshow_summary_on_start: false\nmax_display_tasks: 20\n",
            encoding="utf-8",
        )
        result = _run_load_task_state(tmp_path)
        assert "archived 1" in result.stdout
        assert (plans_dir / "Plans.archive.md").is_file()
