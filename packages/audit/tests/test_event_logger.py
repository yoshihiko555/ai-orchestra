"""event_logger.py のユニットテスト。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "audit" / "hooks"))
event_logger = load_module("event_logger", "packages/audit/hooks/event_logger.py")


# ---------------------------------------------------------------------------
# generate_id
# ---------------------------------------------------------------------------


class TestGenerateId:
    """`generate_id` のテスト。"""

    def test_returns_12_char_hex(self) -> None:
        """生成される ID が 12 文字の 16 進文字列であることを確認する。"""
        id_ = event_logger.generate_id()
        assert len(id_) == 12
        assert all(c in "0123456789abcdef" for c in id_)

    def test_unique(self) -> None:
        """100 回の生成で重複が発生しないことを確認する。"""
        ids = {event_logger.generate_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# emit_event
# ---------------------------------------------------------------------------


class TestEmitEvent:
    """`emit_event` のテスト。"""

    def test_schema_v1_fields(self, tmp_path: object) -> None:
        """v1 スキーマの全フィールドが正しく設定されることを確認する。"""
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        record = event_logger.emit_event(
            "session_start",
            {"packages": ["core", "audit"]},
            session_id="test-session-123",
            project_dir=project_dir,
        )

        assert record["v"] == 1
        assert record["type"] == "session_start"
        assert record["sid"] == "test-session-123"
        assert record["data"]["packages"] == ["core", "audit"]
        assert record["eid"]
        assert record["tid"]
        assert record["ts"]
        assert record["ctx"] == {"skill": None, "phase": None}
        assert record["ptid"] is None
        assert record["aid"] is None

    def test_writes_to_session_file(self, tmp_path: object) -> None:
        """emit_event がセッションログファイルに書き込むことを確認する。"""
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        event_logger.emit_event(
            "prompt",
            {
                "user_input_excerpt": "test",
                "expected_route": "claude-direct",
                "matched_rule": None,
            },
            session_id="sess-abc",
            project_dir=project_dir,
        )

        log_path = event_logger.get_session_log_path("sess-abc", project_dir)
        assert os.path.exists(log_path)

        with open(log_path, encoding="utf-8") as f:
            line = f.readline()
        record = json.loads(line)
        assert record["type"] == "prompt"
        assert record["sid"] == "sess-abc"

    def test_invalid_event_type_raises(self, tmp_path: object) -> None:
        """未知の event_type で ValueError が上がることを確認する。"""
        with pytest.raises(ValueError, match="Unknown event_type"):
            event_logger.emit_event("invalid_type", {}, session_id="s1")

    def test_loop_event_types_are_additive(self) -> None:
        """loop-harness 用の audit event_type が既存 set に追加されていることを確認する。"""
        assert {
            "session_start",
            "quality_gate",
            "loop_start",
            "loop_iteration",
            "loop_stop",
        } <= event_logger.EVENT_TYPES

    def test_no_session_id_returns_record_without_write(self, tmp_path: object) -> None:
        """session_id が空の場合、書き込みせずにレコードを返すことを確認する。"""
        record = event_logger.emit_event("session_start", {"packages": []})
        assert record["v"] == 1
        assert record["sid"] == ""

    def test_custom_trace_and_context(self, tmp_path: object) -> None:
        """tid/ptid/aid/ctx を明示指定した場合の値保持を確認する。"""
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        record = event_logger.emit_event(
            "cli_call",
            {"tool": "codex", "success": True},
            session_id="s1",
            tid="my-trace",
            ptid="parent-trace",
            aid="agent-123",
            ctx={"skill": "issue-fix", "phase": "implementation"},
            project_dir=project_dir,
        )

        assert record["tid"] == "my-trace"
        assert record["ptid"] == "parent-trace"
        assert record["aid"] == "agent-123"
        assert record["ctx"]["skill"] == "issue-fix"

    def test_multiple_events_append(self, tmp_path: object) -> None:
        """複数イベントが同一ファイルに追記されることを確認する。"""
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        for _ in range(3):
            event_logger.emit_event(
                "route_decision",
                {
                    "expected": "claude-direct",
                    "actual": {"tool": "Bash", "detail": "codex"},
                    "matched": False,
                },
                session_id="s1",
                project_dir=project_dir,
            )

        log_path = event_logger.get_session_log_path("s1", project_dir)
        with open(log_path, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# Trace State
# ---------------------------------------------------------------------------


class TestTraceState:
    """`save_trace_state` / `load_trace_state` のテスト。"""

    def test_save_and_load(self, tmp_path: object) -> None:
        """保存したトレース情報をそのまま読み戻せることを確認する。"""
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude", "state"), exist_ok=True)

        event_logger.save_trace_state(
            "tid-123",
            session_id="s1",
            expected_route="codex",
            project_dir=project_dir,
        )

        state = event_logger.load_trace_state(project_dir)
        assert state["tid"] == "tid-123"
        assert state["session_id"] == "s1"
        assert state["expected_route"] == "codex"

    def test_load_missing_returns_empty(self, tmp_path: object) -> None:
        """state ファイルが存在しない場合は空辞書を返すことを確認する。"""
        state = event_logger.load_trace_state(str(tmp_path))
        assert state == {}


# ---------------------------------------------------------------------------
# Session Lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """`init_session_dir` のテスト。"""

    def test_init_creates_sessions_dir(self, tmp_path: object) -> None:
        """init_session_dir がディレクトリを作成し、正しいパスを返すことを確認する。"""
        project_dir = str(tmp_path)
        os.makedirs(os.path.join(project_dir, ".claude"), exist_ok=True)

        path = event_logger.init_session_dir("test-sess", project_dir)
        assert os.path.isdir(os.path.dirname(path))
        assert "test-sess.jsonl" in path


# ---------------------------------------------------------------------------
# Root Worktree Resolution (delegates to core hook_common; Issue #333)
# ---------------------------------------------------------------------------


def _require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not available on PATH")


def _run_git(cwd, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )


class TestResolveLogRootDelegation:
    """`_resolve_log_root` が core `resolve_root_worktree`/`resolve_log_root` に正しく委譲することを確認する。"""

    def test_ordinary_repo_logs_to_repo_root(self, tmp_path) -> None:
        """通常 repo では repo ルート（.claude/ 直下）にログパスが解決される。"""
        _require_git()
        repo = tmp_path / "ordinary-repo"
        repo.mkdir()
        _run_git(repo, "init")
        os.makedirs(os.path.join(str(repo), ".claude"), exist_ok=True)

        resolved = event_logger._resolve_log_root(str(repo))

        assert os.path.realpath(resolved) == os.path.realpath(str(repo))

    def test_linked_worktree_aggregates_to_root(self, tmp_path) -> None:
        """linked worktree では root worktree の .claude/ にログが集約される。"""
        _require_git()
        root_repo = tmp_path / "root-repo"
        linked_worktree = tmp_path / "linked-worktree"
        root_repo.mkdir()

        _run_git(root_repo, "init")
        _run_git(root_repo, "config", "user.email", "test@example.com")
        _run_git(root_repo, "config", "user.name", "Test User")
        _run_git(root_repo, "config", "commit.gpgsign", "false")
        (root_repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        _run_git(root_repo, "add", "tracked.txt")
        _run_git(root_repo, "commit", "-m", "initial")
        _run_git(root_repo, "worktree", "add", "-b", "test-linked", str(linked_worktree))
        os.makedirs(os.path.join(str(root_repo), ".claude"), exist_ok=True)

        resolved = event_logger._resolve_log_root(str(linked_worktree))

        assert os.path.realpath(resolved) == os.path.realpath(str(root_repo))
        assert os.path.realpath(resolved) != os.path.realpath(str(linked_worktree))

    def test_git_unavailable_falls_back_to_project_dir(self, tmp_path, monkeypatch) -> None:
        """git が使えない（結果不正）環境では project_dir にフォールバックする。"""
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        os.makedirs(os.path.join(str(plain_dir), ".claude"), exist_ok=True)

        resolved = event_logger._resolve_log_root(str(plain_dir))

        assert os.path.realpath(resolved) == os.path.realpath(str(plain_dir))

    def test_separate_git_dir_resolves_correct_toplevel(self, tmp_path) -> None:
        """separate-git-dir 構成でも誤った親ディレクトリではなく正しい toplevel に解決される
        （旧実装のバグ修正: 旧実装は git-common-dir の dirname を取るだけだったため
        separate-git-dir の物理配置先の親を誤って root と報告していた）。"""
        _require_git()
        repo = tmp_path / "separate-git-dir-repo"
        external_git_dir = tmp_path / "external.git"
        repo.mkdir()
        _run_git(repo, "init", f"--separate-git-dir={external_git_dir}")
        os.makedirs(os.path.join(str(repo), ".claude"), exist_ok=True)

        resolved = event_logger._resolve_log_root(str(repo))

        assert os.path.realpath(resolved) == os.path.realpath(str(repo))
        assert os.path.realpath(resolved) != os.path.realpath(str(tmp_path))

    def test_ambient_git_dir_does_not_pollute_resolution(self, tmp_path, monkeypatch) -> None:
        """ambient な GIT_DIR / GIT_WORK_TREE が cwd より優先されて誤ったリポジトリを
        解決してしまわないことを確認する（旧実装のバグ修正）。"""
        _require_git()
        unrelated_repo = tmp_path / "unrelated-repo"
        cwd_repo = tmp_path / "cwd-repo"
        unrelated_repo.mkdir()
        cwd_repo.mkdir()
        _run_git(unrelated_repo, "init")
        _run_git(cwd_repo, "init")
        os.makedirs(os.path.join(str(cwd_repo), ".claude"), exist_ok=True)
        monkeypatch.setenv("GIT_DIR", str(unrelated_repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(unrelated_repo))

        resolved = event_logger._resolve_log_root(str(cwd_repo))

        assert os.path.realpath(resolved) == os.path.realpath(str(cwd_repo))


class TestEventLoggerScriptImportPattern:
    """packages/audit/scripts/ と同じ import 方式で event_logger が import できることを確認する
    （AI_ORCHESTRA_DIR 未設定・audit/hooks のみ sys.path 追加、__file__ 相対フォールバックの検証）。"""

    def test_importable_with_only_audit_hooks_on_syspath(self) -> None:
        """AI_ORCHESTRA_DIR が未設定でも、event_logger.py の __file__ 相対フォールバックにより
        hook_common が解決できることを確認する（packages/audit/scripts/ の各スクリプトが
        audit/hooks のみを sys.path に追加して event_logger を単体 import するのと同じ条件）。

        import 成功だけでなく、解決された hook_common.__file__ が __file__ 相対フォールバック
        経由の repo 側 packages/core/hooks/hook_common.py を指すことまで検証する
        （優先順位が意図通りであることの確認。Issue #333 H1 レビュー対応）。
        既存プロセスの sys.modules 汚染を避けるため、AI_ORCHESTRA_DIR を除去した環境で
        サブプロセスとして実行する。
        """
        module_path = REPO_ROOT / "packages" / "audit" / "hooks" / "event_logger.py"
        expected_hook_common = REPO_ROOT / "packages" / "core" / "hooks" / "hook_common.py"
        env = {k: v for k, v in os.environ.items() if k != "AI_ORCHESTRA_DIR"}
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('event_logger_reload_check', {str(module_path)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)  # populates sys.modules['hook_common']\n"
            "print(sys.modules['hook_common'].__file__)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(module_path.parent),
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        loaded_hook_common_path = result.stdout.strip().splitlines()[-1]
        assert os.path.realpath(loaded_hook_common_path) == os.path.realpath(
            str(expected_hook_common)
        )
