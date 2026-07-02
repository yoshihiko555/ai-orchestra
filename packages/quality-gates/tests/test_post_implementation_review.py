from __future__ import annotations

import json
import sys
import time
from io import StringIO

import pytest

from tests.module_loader import load_module

post_implementation_review = load_module(
    "post_implementation_review", "packages/quality-gates/hooks/post-implementation-review.py"
)

FAKE_PROJECT_A = "/fake/project-a"
FAKE_PROJECT_B = "/fake/project-b"


@pytest.fixture()
def _clean_state(tmp_path, monkeypatch):
    """Redirect state file to tmp_path and bypass real git lookups."""
    state_file = tmp_path / "impl-review-state.json"
    monkeypatch.setattr(post_implementation_review, "STATE_FILE", state_file)
    monkeypatch.setattr(
        post_implementation_review, "get_project_state_key", lambda project_dir: project_dir
    )
    yield state_file


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
    state_a = post_implementation_review.load_state(FAKE_PROJECT_A)
    state_a["files"] = ["a.py", "b.py", "c.py"]
    state_a["total_lines"] = 500
    state_a["review_suggested"] = True
    state_a["suggested_at"] = time.time()
    post_implementation_review.save_state(FAKE_PROJECT_A, state_a)

    state_b = post_implementation_review.load_state(FAKE_PROJECT_B)
    assert state_b == post_implementation_review._DEFAULT_IMPL_REVIEW_STATE

    reloaded_a = post_implementation_review.load_state(FAKE_PROJECT_A)
    assert reloaded_a["files"] == ["a.py", "b.py", "c.py"]
    assert reloaded_a["total_lines"] == 500
    assert reloaded_a["review_suggested"] is True


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
    # Large single edit crosses the line threshold immediately.
    big_content = "\n".join(f"line {i}" for i in range(150))
    _write_payload(monkeypatch, "src/big_module.py", big_content, FAKE_PROJECT_A)

    with pytest.raises(SystemExit) as exc_info:
        post_implementation_review.main()
    assert exc_info.value.code == 0

    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Review Suggestion]" in context

    state = post_implementation_review.load_state(FAKE_PROJECT_A)
    assert state["review_suggested"] is True
    assert state["suggested_at"] is not None
    # Counters reset so the next accumulation window starts fresh.
    assert state["files"] == []
    assert state["total_lines"] == 0

    # A second invocation right after should NOT suggest again.
    _write_payload(monkeypatch, "src/other_module.py", "line\n", FAKE_PROJECT_A)
    with pytest.raises(SystemExit) as exc_info2:
        post_implementation_review.main()
    assert exc_info2.value.code == 0
    assert capsys.readouterr().out == ""

    # Simulate TTL expiry by rewriting the saved suggested_at to an old timestamp.
    stale_state = post_implementation_review.load_state(FAKE_PROJECT_A)
    ttl = post_implementation_review.REVIEW_SUGGESTION_TTL_SECONDS
    stale_state["suggested_at"] = time.time() - (ttl + 10)
    stale_state["files"] = ["x.py", "y.py", "z.py"]
    stale_state["total_lines"] = 10
    post_implementation_review.save_state(FAKE_PROJECT_A, stale_state)

    _write_payload(monkeypatch, "src/another_module.py", "line\n", FAKE_PROJECT_A)
    with pytest.raises(SystemExit) as exc_info3:
        post_implementation_review.main()
    assert exc_info3.value.code == 0

    output3 = json.loads(capsys.readouterr().out)
    context3 = output3["hookSpecificOutput"]["additionalContext"]
    assert "[Review Suggestion]" in context3
