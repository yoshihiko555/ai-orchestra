"""precompact-dump.py のユニットテスト。"""

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

HOOKS_DIR = REPO_ROOT / "packages" / "core" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

precompact = load_module("precompact_dump", "packages/core/hooks/precompact-dump.py")


class TestFormatWorkingContext:
    """`_format_working_context` のテスト。"""

    def test_empty_returns_placeholder(self) -> None:
        """空辞書の場合は empty プレースホルダが返ることを確認する。"""
        result = precompact._format_working_context({})
        assert "empty" in result

    def test_formats_known_fields(self) -> None:
        """既知のキーが適切に整形されることを確認する。"""
        ctx = {
            "current_phase": "implementation",
            "updated_at": "2026-04-11T03:00:00+00:00",
            "modified_files": ["a.py", "b.ts"],
            "decisions": ["use PostgreSQL"],
        }
        text = precompact._format_working_context(ctx)
        assert "implementation" in text
        assert "a.py" in text
        assert "b.ts" in text
        assert "use PostgreSQL" in text

    def test_includes_extra_keys(self) -> None:
        """既知以外のキーも other セクションに含まれることを確認する。"""
        ctx = {"current_phase": "x", "custom_field": "hello"}
        text = precompact._format_working_context(ctx)
        assert "custom_field" in text


class TestBuildDumpText:
    """`build_dump_text` のテスト。"""

    def test_includes_metadata_and_plans(self) -> None:
        """session_id / trigger / plans が全て含まれることを確認する。"""
        text = precompact.build_dump_text(
            session_id="s1",
            trigger="auto",
            working_ctx={"current_phase": "design"},
            plans_text="■ example (1 tasks: WIP: 1)\n  WIP:\n    - task",
        )
        assert "session_id" in text
        assert "s1" in text
        assert "auto" in text
        assert "design" in text
        assert "■ example (1 tasks: WIP: 1)" in text
        assert "## Plans\n" in text
        assert "```markdown" not in text

    def test_missing_plans_uses_placeholder(self) -> None:
        """plan ファイルが無い場合はプレースホルダが入ることを確認する。"""
        text = precompact.build_dump_text(
            session_id="s1",
            trigger="manual",
            working_ctx={},
            plans_text="",
        )
        assert "_(no plans)_" in text


class TestWriteDump:
    """`write_dump` のテスト。"""

    def test_creates_file_in_shared_dir(self, tmp_path: Path) -> None:
        """ダンプが .claude/context/shared/ 配下に作られることを確認する。"""
        project_dir = str(tmp_path)
        path = precompact.write_dump(project_dir, "# hello\n")

        shared_dir = tmp_path / ".claude" / "context" / "shared"
        assert Path(path).parent == shared_dir
        assert Path(path).name.startswith("precompact-")
        assert Path(path).name.endswith(".md")
        assert Path(path).read_text(encoding="utf-8") == "# hello\n"

    def test_prunes_old_dumps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """古いダンプは MAX_DUMP_FILES を超えると削除され、新規 dump は残ることを確認する。

        実行時刻に依存しないよう `_now_stamp` を固定値にモンキーパッチする。
        古いダンプよりも必ず後ろに並ぶタイムスタンプを使うことで、ソート順が
        実時刻に関係なく決定的になる。
        """
        project_dir = str(tmp_path)
        shared_dir = tmp_path / ".claude" / "context" / "shared"
        shared_dir.mkdir(parents=True)

        # 既存ダンプを 21 個作る（MAX_DUMP_FILES + 1）。
        # `_now_stamp` が返すフォーマットに合わせてゼロ埋めし、辞書順 = 時系列順にする。
        old_names = [f"precompact-20260101T000000-{i:06d}Z.md" for i in range(21)]
        for name in old_names:
            (shared_dir / name).write_text("x")

        # 新規 dump は必ず old_names の最後より後ろに並ぶタイムスタンプにする
        fixed_stamp = "20991231T235959-999999Z"
        monkeypatch.setattr(precompact, "_now_stamp", lambda: fixed_stamp)

        new_path = precompact.write_dump(project_dir, "new")

        remaining_names = sorted(
            p.name for p in shared_dir.iterdir() if p.name.startswith("precompact-")
        )
        # 件数が MAX_DUMP_FILES 以内に収まっている
        assert len(remaining_names) == precompact.MAX_DUMP_FILES
        # 新規 dump は必ず残る（固定スタンプが最後尾なので確実に保持される）
        assert Path(new_path).name == f"precompact-{fixed_stamp}.md"
        assert Path(new_path).exists()
        assert Path(new_path).name in remaining_names
        # 最古のダンプ（先頭）は削除されている
        assert old_names[0] not in remaining_names


class TestResolveProjectDir:
    """`_resolve_project_dir` の hook 入力検証テスト。"""

    def test_returns_absolute_path_for_valid_dir(self, tmp_path: Path) -> None:
        """正常な cwd が渡されたとき絶対パスが返ることを確認する。"""
        result = precompact._resolve_project_dir({"cwd": str(tmp_path)})
        assert result == os.path.abspath(str(tmp_path))

    def test_falls_back_for_nonexistent_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """存在しないパスが渡されたとき CWD にフォールバックし、stderr にログを出すことを確認する。"""
        bogus = tmp_path / "does-not-exist"
        result = precompact._resolve_project_dir({"cwd": str(bogus)})

        assert result == os.getcwd()
        err = capsys.readouterr().err
        assert "invalid cwd" in err
        assert "fallback" in err

    def test_falls_back_for_empty_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """空文字 cwd かつ CLAUDE_PROJECT_DIR が無効な場合、CWD にフォールバックすることを確認する。"""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "nonexistent"))
        result = precompact._resolve_project_dir({"cwd": ""})
        assert result == os.getcwd()

    def test_normalizes_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """相対パスが絶対パスに正規化されることを確認する。"""
        monkeypatch.chdir(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        result = precompact._resolve_project_dir({"cwd": "sub"})
        assert result == os.path.abspath(str(sub))
        assert os.path.isabs(result)


class TestMain:
    """`main` のエンドツーエンド動作を確認する。"""

    def _invoke(self, payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        precompact.main()

    def test_writes_dump_with_plans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """EV-56: 要約と未完了のパスだけを退避し、本文や旧ファイルは写さない。"""
        claude_dir = tmp_path / ".claude"
        (claude_dir / "plans").mkdir(parents=True)
        (claude_dir / "plans" / "example.md").write_text(
            "## Project: Feature\n#### Goal\n- Ship feature\n"
            "#### Context\n- context-unique-private-text\n"
            "#### Constraints\n- constraints-unique-private-text\n"
            "### Phase 1\n- `cc:WIP` build feature\n"
            "## Decisions\n- decisions-unique-private-text\n"
            "## Notes\n- notes-unique-private-text\n",
            encoding="utf-8",
        )
        (claude_dir / "plans" / "finished.md").write_text(
            "- `cc:done` completed-unique-private-text\n", encoding="utf-8"
        )
        (claude_dir / "Plans.md").write_bytes(b"\xfflegacy-unique-private-text")

        # audit への副作用を遮断（テストは Markdown ダンプだけを検証する）
        emit_calls: list[tuple] = []

        def _fake_emit(*args, **kwargs) -> None:
            emit_calls.append((args, kwargs))

        monkeypatch.setattr(precompact, "_emit_event", _fake_emit)

        payload = {
            "session_id": "sess-x",
            "cwd": str(tmp_path),
            "trigger": "manual",
        }
        self._invoke(payload, monkeypatch)

        # _emit_event は 1 回だけ呼ばれ、event_type と session_id が正しいこと
        assert len(emit_calls) == 1
        args, kwargs = emit_calls[0]
        assert args[0] == "precompact"
        assert kwargs.get("session_id") == "sess-x"

        dumps = list((tmp_path / ".claude" / "context" / "shared").iterdir())
        assert len(dumps) == 1
        assert dumps[0].name.startswith("precompact-")
        content = dumps[0].read_text(encoding="utf-8")
        assert "sess-x" in content
        assert "build feature" in content

        assert "## Plans\n" in content
        assert "Goal: Ship feature" in content
        assert "- `.claude/plans/example.md`" in content
        assert "`.claude/plans/finished.md`" not in content
        for label in ("context", "constraints", "decisions", "notes", "completed", "legacy"):
            assert f"{label}-unique-private-text" not in content


def test_precompact_reuses_session_summary_and_keeps_paths_after_cap(tmp_path) -> None:
    plans_dir = tmp_path / ".claude" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "first.md").write_text("- `cc:WIP` visible\n- `cc:TODO` omitted\n")
    (plans_dir / "second.md").write_text("- `cc:TODO` hidden\n")
    config_dir = tmp_path / ".claude" / "config" / "core"
    config_dir.mkdir(parents=True)
    (config_dir / "task-memory.yaml").write_text("max_display_tasks: 1\n")
    task_state = load_module("precompact_test_task_state", "packages/core/hooks/load-task-state.py")
    summary = task_state.build_plans_summary(str(tmp_path), task_state.load_config(str(tmp_path)))

    text = precompact._summarize_plans(str(tmp_path))

    assert text.startswith(summary + "\n")
    assert "- `.claude/plans/first.md`" in text
    assert "- `.claude/plans/second.md`" in text
    assert "    - omitted" not in text
    assert "    - hidden" not in text


def test_import_failure_writes_dump_and_continues(tmp_path, monkeypatch, capsys) -> None:
    def fail_import(*_args, **_kwargs):
        raise ImportError("unavailable parser")

    monkeypatch.setattr(precompact.importlib.util, "spec_from_file_location", fail_import)
    monkeypatch.setattr(precompact, "read_hook_input", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(precompact, "_emit_event", None)

    precompact.main()

    dump = next((tmp_path / ".claude" / "context" / "shared").glob("precompact-*.md"))
    assert "_(plans unavailable)_" in dump.read_text()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("unsafe", ["symlink-file", "symlink-dir", "non-utf8"])
def test_precompact_skips_unsafe_plan_sources(tmp_path, unsafe) -> None:
    plans_dir = tmp_path / ".claude" / "plans"
    plans_dir.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    outside = external / "external.md"
    outside.write_text("- `cc:WIP` external-unique-text\n")
    if unsafe == "symlink-dir":
        plans_dir.rmdir()
        plans_dir.symlink_to(external, target_is_directory=True)
    else:
        (plans_dir / "good.md").write_text("- `cc:TODO` visible\n")
        if unsafe == "symlink-file":
            (plans_dir / "bad.md").symlink_to(outside)
        else:
            (plans_dir / "bad.md").write_bytes(b"\xffexternal-unique-text")

    text = precompact._summarize_plans(str(tmp_path))

    assert "external-unique-text" not in text
    if unsafe == "symlink-dir":
        assert text == ""
    else:
        assert "visible" in text
