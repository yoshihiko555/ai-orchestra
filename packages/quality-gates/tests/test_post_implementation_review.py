from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

post_implementation_review = load_module(
    "post_implementation_review", "packages/quality-gates/hooks/post-implementation-review.py"
)

_HOOK_PATH = REPO_ROOT / "packages" / "quality-gates" / "hooks" / "post-implementation-review.py"


@pytest.fixture()
def _clean_state(tmp_path, monkeypatch):
    """Provide isolated real project dirs and bypass real git lookups.

    Project dirs must be real, writable directories (not fake path strings)
    because the state file now resolves to
    <project_dir>/.claude/state/post-implementation-review.json instead of a
    single overridable /tmp constant.
    """
    monkeypatch.setattr(
        post_implementation_review, "get_project_state_key", lambda project_dir: project_dir
    )
    project_a = str(tmp_path / "project-a")
    project_b = str(tmp_path / "project-b")
    yield project_a, project_b


# ---------------------------------------------------------------------------
# count_lines
# ---------------------------------------------------------------------------


def test_count_lines_ignores_empty_lines() -> None:
    content = "line1\n\nline2\n  \nline3\n"
    assert post_implementation_review.count_lines(content) == 3


# ---------------------------------------------------------------------------
# should_suggest_review / is_suggestion_stale
# ---------------------------------------------------------------------------


def test_should_suggest_review_false_below_thresholds() -> None:
    state = {
        "files": ["a.py"],
        "total_lines": 10,
        "review_suggested": False,
        "suggested_at": None,
    }
    assert not post_implementation_review.should_suggest_review(state)


def test_should_suggest_review_true_at_file_threshold() -> None:
    state = {
        "files": ["a.py", "b.py", "c.py"],
        "total_lines": 10,
        "review_suggested": False,
        "suggested_at": None,
    }
    assert post_implementation_review.should_suggest_review(state)


def test_should_suggest_review_true_at_line_threshold() -> None:
    state = {
        "files": ["a.py"],
        "total_lines": 100,
        "review_suggested": False,
        "suggested_at": None,
    }
    assert post_implementation_review.should_suggest_review(state)


def test_should_suggest_review_false_when_already_suggested_and_fresh() -> None:
    state = {
        "files": ["a.py", "b.py", "c.py"],
        "total_lines": 500,
        "review_suggested": True,
        "suggested_at": time.time(),
    }
    assert not post_implementation_review.should_suggest_review(state)


def test_is_suggestion_stale_false_when_never_suggested() -> None:
    assert not post_implementation_review.is_suggestion_stale({"suggested_at": None})


def test_is_suggestion_stale_false_within_ttl() -> None:
    state = {"suggested_at": time.time() - 10}
    assert not post_implementation_review.is_suggestion_stale(state)


def test_is_suggestion_stale_true_after_ttl() -> None:
    ttl = post_implementation_review.REVIEW_SUGGESTION_TTL_SECONDS
    state = {"suggested_at": time.time() - (ttl + 10)}
    assert post_implementation_review.is_suggestion_stale(state)


def test_should_suggest_review_stays_false_after_ttl_when_still_below_thresholds() -> None:
    """TTL経過で再提案可能になっても、閾値未満なら提案しない。"""
    ttl = post_implementation_review.REVIEW_SUGGESTION_TTL_SECONDS
    state = {
        "files": ["a.py"],
        "total_lines": 5,
        "review_suggested": True,
        "suggested_at": time.time() - (ttl + 10),
    }
    assert not post_implementation_review.should_suggest_review(state)


def test_should_suggest_review_true_after_ttl_when_thresholds_met_again() -> None:
    """TTL経過後、新たな蓄積が閾値に達すれば再提案する。"""
    ttl = post_implementation_review.REVIEW_SUGGESTION_TTL_SECONDS
    state = {
        "files": ["a.py", "b.py", "c.py"],
        "total_lines": 5,
        "review_suggested": True,
        "suggested_at": time.time() - (ttl + 10),
    }
    assert post_implementation_review.should_suggest_review(state)


# ---------------------------------------------------------------------------
# load_state / save_state (project isolation)
# ---------------------------------------------------------------------------


def test_state_is_isolated_per_project(_clean_state) -> None:
    project_a, project_b = _clean_state
    state_a = post_implementation_review.load_state(project_a)
    state_a["files"] = ["a.py", "b.py", "c.py"]
    state_a["total_lines"] = 500
    state_a["review_suggested"] = True
    state_a["suggested_at"] = time.time()
    post_implementation_review.save_state(project_a, state_a)

    state_b = post_implementation_review.load_state(project_b)
    assert state_b == post_implementation_review._DEFAULT_IMPL_REVIEW_STATE

    reloaded_a = post_implementation_review.load_state(project_a)
    assert reloaded_a["files"] == ["a.py", "b.py", "c.py"]
    assert reloaded_a["total_lines"] == 500
    assert reloaded_a["review_suggested"] is True


def test_state_resolves_under_claude_state_dir(tmp_path, monkeypatch) -> None:
    """The state file must land under <project_dir>/.claude/state/, not /tmp."""
    monkeypatch.setattr(
        post_implementation_review, "get_project_state_key", lambda project_dir: project_dir
    )

    post_implementation_review.save_state(str(tmp_path), {"files": ["a.py"], "total_lines": 1})

    expected = tmp_path / ".claude" / "state" / post_implementation_review.STATE_FILENAME
    assert expected.is_file()


# ---------------------------------------------------------------------------
# main() end-to-end
# ---------------------------------------------------------------------------


def _write_payload(monkeypatch, file_path: str, content: str, project_dir: str) -> None:
    payload = {
        "tool_name": "Write",
        "cwd": project_dir,
        "tool_input": {"file_path": file_path, "content": content},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))


def test_main_suggests_review_once_then_suppresses_until_ttl(
    _clean_state, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project_a, _project_b = _clean_state

    # Large single edit crosses the line threshold immediately.
    big_content = "\n".join(f"line {i}" for i in range(150))
    _write_payload(monkeypatch, "src/big_module.py", big_content, project_a)

    with pytest.raises(SystemExit) as exc_info:
        post_implementation_review.main()
    assert exc_info.value.code == 0

    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Review Suggestion]" in context

    state = post_implementation_review.load_state(project_a)
    assert state["review_suggested"] is True
    assert state["suggested_at"] is not None
    # Counters reset so the next accumulation window starts fresh.
    assert state["files"] == []
    assert state["total_lines"] == 0

    # A second invocation right after should NOT suggest again.
    _write_payload(monkeypatch, "src/other_module.py", "line\n", project_a)
    with pytest.raises(SystemExit) as exc_info2:
        post_implementation_review.main()
    assert exc_info2.value.code == 0
    assert capsys.readouterr().out == ""

    # Simulate TTL expiry by rewriting the saved suggested_at to an old timestamp.
    stale_state = post_implementation_review.load_state(project_a)
    ttl = post_implementation_review.REVIEW_SUGGESTION_TTL_SECONDS
    stale_state["suggested_at"] = time.time() - (ttl + 10)
    stale_state["files"] = ["x.py", "y.py", "z.py"]
    stale_state["total_lines"] = 10
    post_implementation_review.save_state(project_a, stale_state)

    _write_payload(monkeypatch, "src/another_module.py", "line\n", project_a)
    with pytest.raises(SystemExit) as exc_info3:
        post_implementation_review.main()
    assert exc_info3.value.code == 0

    output3 = json.loads(capsys.readouterr().out)
    context3 = output3["hookSpecificOutput"]["additionalContext"]
    assert "[Review Suggestion]" in context3


def test_main_uses_atomic_update_not_separate_load_and_save(
    _clean_state, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() must go through update_project_scoped_state (single critical
    section) instead of a separate load_state()/save_state() pair, so the
    accumulate-and-maybe-suggest decision can't race across processes.
    """
    project_a, _project_b = _clean_state

    save_state_calls = []
    monkeypatch.setattr(
        post_implementation_review,
        "save_state",
        lambda *args, **kwargs: save_state_calls.append((args, kwargs)),
    )

    update_calls = []
    original_update = post_implementation_review.update_project_scoped_state

    def _spy_update(state_file, project_key, mutate_fn, default_state):
        update_calls.append((state_file, project_key))
        return original_update(state_file, project_key, mutate_fn, default_state)

    monkeypatch.setattr(post_implementation_review, "update_project_scoped_state", _spy_update)

    _write_payload(monkeypatch, "src/module.py", "line\n", project_a)
    with pytest.raises(SystemExit) as exc_info:
        post_implementation_review.main()
    assert exc_info.value.code == 0

    assert len(update_calls) == 1
    assert save_state_calls == []


def test_main_keeps_project_isolation_through_atomic_update(
    _clean_state, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Accumulating state for project A via main() must not affect project B."""
    project_a, project_b = _clean_state

    _write_payload(monkeypatch, "src/module.py", "line\n", project_a)
    with pytest.raises(SystemExit):
        post_implementation_review.main()
    capsys.readouterr()

    state_b = post_implementation_review.load_state(project_b)
    assert state_b == post_implementation_review._DEFAULT_IMPL_REVIEW_STATE

    state_a = post_implementation_review.load_state(project_a)
    assert state_a["files"] == ["src/module.py"]


# ---------------------------------------------------------------------------
# EV-21: quality_gate.enabled 遵守
# ---------------------------------------------------------------------------


def test_main_no_op_when_quality_gate_disabled(
    _clean_state, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """quality_gate.enabled=false のときは状態記録・提案を含む全動作を行わない。"""
    project_a, _project_b = _clean_state
    monkeypatch.setattr(
        post_implementation_review,
        "load_package_config",
        lambda *_args: {"features": {"quality_gate": {"enabled": False}}},
    )

    big_content = "\n".join(f"line {i}" for i in range(150))
    _write_payload(monkeypatch, "src/big_module.py", big_content, project_a)

    with pytest.raises(SystemExit) as exc_info:
        post_implementation_review.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""
    # State must remain untouched (no accumulation happened).
    state = post_implementation_review.load_state(project_a)
    assert state == post_implementation_review._DEFAULT_IMPL_REVIEW_STATE


def test_main_normalizes_subdirectory_before_disabled_config_lookup(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """subdirectory cwd でも root の disabled 設定を使い、状態を変更しない。"""
    repo_root = tmp_path / "repo"
    (repo_root / ".claude").mkdir(parents=True)
    subdirectory = repo_root / "packages" / "sub"
    subdirectory.mkdir(parents=True)
    config_calls = []

    def _load_config(package_name: str, filename: str, project_dir: str) -> dict:
        config_calls.append((package_name, filename, project_dir))
        return {"features": {"quality_gate": {"enabled": False}}}

    def _fail_if_state_updated(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        pytest.fail("state must not be updated when quality_gate is disabled")

    monkeypatch.setattr(post_implementation_review, "load_package_config", _load_config)
    monkeypatch.setattr(
        post_implementation_review, "update_project_scoped_state", _fail_if_state_updated
    )
    _write_payload(monkeypatch, "src/module.py", "line\n", str(subdirectory))

    with pytest.raises(SystemExit) as exc_info:
        post_implementation_review.main()

    assert exc_info.value.code == 0
    assert config_calls == [("audit", "audit-flags.json", str(repo_root))]
    assert capsys.readouterr().out == ""
    state_file = repo_root / ".claude" / "state" / post_implementation_review.STATE_FILENAME
    assert not state_file.exists()


# ---------------------------------------------------------------------------
# EV-10: main() の fail-open（例外捕捉 → stderr ログ + exit 0）
# ---------------------------------------------------------------------------


def test_main_fails_open_on_unexpected_exception(
    _clean_state, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project_a, _project_b = _clean_state

    def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(post_implementation_review, "load_package_config", _raise)
    _write_payload(monkeypatch, "src/module.py", "line\n", project_a)

    with pytest.raises(SystemExit) as exc_info:
        post_implementation_review.main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Hook error" in captured.err
    assert "boom" in captured.err


# ---------------------------------------------------------------------------
# Issue #134 レビュー指摘: AI_ORCHESTRA_DIR 未設定時の hook_common フォールバック
# ---------------------------------------------------------------------------


def test_hook_runs_without_ai_orchestra_dir_env_var(tmp_path: Path) -> None:
    """AI_ORCHESTRA_DIR 未設定の開発・検証環境で hook を絶対パスから直接実行
    しても、hook_common の import に失敗せず fail-open（exit 0）で終わることを
    確認する（従来はフォールバック欠落により ModuleNotFoundError で exit 1 に
    なっていた）。"""
    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": "src/module.py", "content": "line\n"},
    }
    env = {k: v for k, v in os.environ.items() if k != "AI_ORCHESTRA_DIR"}

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(_HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=30,
    )

    assert result.returncode == 0
    assert "ModuleNotFoundError" not in result.stderr
