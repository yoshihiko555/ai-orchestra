"""Tests for `packages/git-workflow/scripts/pr_review_threads.py`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.module_loader import load_module

prt = load_module("pr_review_threads_tests", "packages/git-workflow/scripts/pr_review_threads.py")


class FakeRun:
    """FIFO fake for `subprocess.run` that asserts call order loosely by matching a predicate."""

    def __init__(self, responses: list[tuple[Any, subprocess.CompletedProcess[str]]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if not self._responses:
            raise AssertionError(f"unexpected extra gh/git call: {cmd}")
        predicate, response = self._responses.pop(0)
        if not predicate(cmd):
            raise AssertionError(f"unexpected call {cmd}, did not match predicate")
        return response


def _ok(stdout: Any, *, raw: bool = False) -> subprocess.CompletedProcess[str]:
    text = stdout if raw else json.dumps(stdout)
    return subprocess.CompletedProcess([], 0, text, "")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, "", stderr)


def _is_git_branch(cmd: list[str]) -> bool:
    return cmd[:2] == ["git", "branch"]


def _is_pr_list(cmd: list[str]) -> bool:
    return cmd[:3] == ["gh", "pr", "list"]


def _is_repo_view(cmd: list[str]) -> bool:
    return cmd[:3] == ["gh", "repo", "view"]


def _is_graphql(cmd: list[str]) -> bool:
    return cmd[:3] == ["gh", "api", "graphql"]


def _is_issue_comments(cmd: list[str]) -> bool:
    return len(cmd) >= 3 and cmd[1] == "api" and "/issues/" in cmd[2] and "/comments" in cmd[2]


def _is_reply(cmd: list[str]) -> bool:
    return len(cmd) >= 3 and cmd[1] == "api" and "/comments" in cmd[2]


def _is_review_comments(cmd: list[str]) -> bool:
    """Match the REST review-comments join call (`.../pulls/{n}/comments`, no `/replies`)."""
    return (
        len(cmd) >= 3 and cmd[1] == "api" and "/pulls/" in cmd[2] and cmd[2].endswith("/comments")
    )


def _thread(
    thread_id: str,
    *,
    is_resolved: bool = False,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "isResolved": is_resolved,
        "isOutdated": False,
        "path": "src/app.py",
        "line": 10,
        "comments": {"nodes": comments or []},
    }


def _comment(
    database_id: int,
    login: str,
    body: str,
    *,
    typename: str = "Bot",
    author_association: str = "NONE",
    reply_to_database_id: int | None = None,
) -> dict[str, Any]:
    return {
        "databaseId": database_id,
        "author": {"login": login, "__typename": typename},
        "authorAssociation": author_association,
        "body": body,
        "url": f"https://github.com/o/r/pull/1#discussion_r{database_id}",
        "createdAt": "2026-07-14T00:00:00Z",
        "replyTo": {"databaseId": reply_to_database_id}
        if reply_to_database_id is not None
        else None,
    }


def _threads_page(
    nodes: list[dict[str, Any]], *, has_next: bool = False, end_cursor: str | None = None
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def _rest_review_comment(
    comment_id: int,
    *,
    app_slug: str | None = None,
    login: str | None = None,
    user_type: str = "User",
    author_association: str = "NONE",
) -> dict[str, Any]:
    """Build a REST `pulls/{n}/comments` item, the join source for `_raw_for_origin_check`."""
    item: dict[str, Any] = {
        "id": comment_id,
        "user": {"login": login, "type": user_type},
        "author_association": author_association,
    }
    if app_slug is not None:
        item["performed_via_github_app"] = {"slug": app_slug}
    return item


def _issue_comment(
    comment_id: int, login: str, body: str, *, user_type: str = "Bot"
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "user": {"login": login, "type": user_type},
        "author_association": "NONE",
        "body": body,
        "html_url": f"https://github.com/o/r/pull/1#issuecomment-{comment_id}",
        "created_at": "2026-07-14T00:00:00Z",
    }


def _patch_loop_harness_root(monkeypatch: pytest.MonkeyPatch, project_dir: Path) -> None:
    """Avoid loop_common's real `git rev-parse` call, which the fake gh/git run() intercepts."""
    prw_module = prt._import_pr_review_wait()
    assert prw_module is not None
    monkeypatch.setattr(prw_module.lc, "resolve_root_worktree", lambda _project_dir: project_dir)


@pytest.fixture()
def loop_harness_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project dir with a loop-harness config that has a reviewer_allowlist set."""
    config_dir = tmp_path / ".claude" / "config" / "loop-harness"
    config_dir.mkdir(parents=True)
    (config_dir / "loop-harness.local.yaml").write_text(
        "pr_review:\n  reviewer_allowlist:\n    - login: coderabbitai[bot]\n      type: Bot\n",
        encoding="utf-8",
    )
    _patch_loop_harness_root(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture()
def no_allowlist_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project dir with a loop-harness config lacking reviewer_allowlist."""
    config_dir = tmp_path / ".claude" / "config" / "loop-harness"
    config_dir.mkdir(parents=True)
    (config_dir / "loop-harness.local.yaml").write_text("pr_review: {}\n", encoding="utf-8")
    _patch_loop_harness_root(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture()
def app_slug_allowlist_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project dir with a loop-harness config allowlisting only an `app_slug` (no login)."""
    config_dir = tmp_path / ".claude" / "config" / "loop-harness"
    config_dir.mkdir(parents=True)
    (config_dir / "loop-harness.local.yaml").write_text(
        "pr_review:\n  reviewer_allowlist:\n    - app_slug: my-review-bot\n",
        encoding="utf-8",
    )
    _patch_loop_harness_root(monkeypatch, tmp_path)
    return tmp_path


# --- detect -------------------------------------------------------------------


def test_detect_pr_single_result(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(
        [
            (_is_git_branch, _ok("feat/x\n", raw=True)),
            (
                _is_pr_list,
                _ok([{"number": 42, "url": "https://x/42", "title": "T", "isDraft": False}]),
            ),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.detect_pr(30)
    assert result == {"pr_number": 42, "url": "https://x/42", "title": "T"}


def test_detect_pr_no_open_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(
        [
            (_is_git_branch, _ok("feat/x\n", raw=True)),
            (_is_pr_list, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    with pytest.raises(prt.GhCommandError) as exc_info:
        prt.detect_pr(30)
    assert exc_info.value.code == "no_open_pr"


def test_detect_pr_multiple_open_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(
        [
            (_is_git_branch, _ok("feat/x\n", raw=True)),
            (
                _is_pr_list,
                _ok(
                    [
                        {"number": 1, "url": "u1", "title": "t1", "isDraft": False},
                        {"number": 2, "url": "u2", "title": "t2", "isDraft": False},
                    ]
                ),
            ),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    with pytest.raises(prt.GhCommandError) as exc_info:
        prt.detect_pr(30)
    assert exc_info.value.code == "multiple_open_prs"


def test_cmd_detect_no_branch_prints_json_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRun([(_is_git_branch, _ok("\n", raw=True))])
    monkeypatch.setattr(prt.subprocess, "run", fake)
    args = prt.build_parser().parse_args(["detect"])
    exit_code = prt.cmd_detect(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["error"] == "no_current_branch"
    assert captured.err == ""


# --- fetch: unresolved filter + pagination -------------------------------------


def test_fetch_filters_unresolved_threads_only(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    resolved = _thread(
        "T_resolved", is_resolved=True, comments=[_comment(1, "coderabbitai[bot]", "hi")]
    )
    unresolved = _thread(
        "T_open", is_resolved=False, comments=[_comment(2, "coderabbitai[bot]", "issue here")]
    )
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([resolved, unresolved]))),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert [t["thread_id"] for t in result["unresolved_threads"]] == ["T_open"]
    assert result["origin_verified"] is True


def test_pagination_fetches_all_pages(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    page1 = _threads_page(
        [_thread("T1", comments=[_comment(1, "coderabbitai[bot]", "p1")])],
        has_next=True,
        end_cursor="cursor-1",
    )
    page2 = _threads_page([_thread("T2", comments=[_comment(2, "coderabbitai[bot]", "p2")])])
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(page1)),
            (_is_graphql, _ok(page2)),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert [t["thread_id"] for t in result["unresolved_threads"]] == ["T1", "T2"]
    # second graphql call must carry the cursor from page 1
    graphql_calls = [c for c in fake.calls if c[:3] == ["gh", "api", "graphql"]]
    assert any("cursor=cursor-1" in " ".join(c) for c in graphql_calls)


# --- fetch: bot allowlist filter ------------------------------------------------


def test_bot_issue_comments_have_no_thread_id(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    """EV-27: issue comments are structurally distinct from resolvable review threads.

    `resolve` only accepts a review-thread GraphQL id; issue comments never carry one,
    so a caller cannot accidentally pass a `bot_issue_comments` entry to `resolve`.
    """
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([]))),
            (_is_issue_comments, _ok([_issue_comment(10, "coderabbitai[bot]", "summary")])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert len(result["bot_issue_comments"]) == 1
    assert "thread_id" not in result["bot_issue_comments"][0]


def test_fetch_filters_bot_allowlist(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    thread = _thread(
        "T1",
        comments=[
            _comment(1, "coderabbitai[bot]", "bot says hi"),
            _comment(2, "human-reviewer", "human says hi", typename="User"),
        ],
    )
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (
                _is_issue_comments,
                _ok(
                    [
                        _issue_comment(10, "coderabbitai[bot]", "bot summary"),
                        _issue_comment(11, "human-reviewer", "human comment", user_type="User"),
                    ]
                ),
            ),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert len(result["unresolved_threads"]) == 1
    comment_authors = [c["author"] for c in result["unresolved_threads"][0]["comments"]]
    assert comment_authors == ["coderabbitai[bot]"]
    assert [c["author"] for c in result["bot_issue_comments"]] == ["coderabbitai[bot]"]


def test_fetch_drops_thread_with_no_bot_comments(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    thread = _thread("T1", comments=[_comment(1, "human-reviewer", "hi", typename="User")])
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert result["unresolved_threads"] == []


# --- fetch: allowlist not configured (fail-closed) ------------------------------


def test_fetch_allowlist_not_configured_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch, no_allowlist_project: Path
) -> None:
    thread = _thread("T1", comments=[_comment(1, "coderabbitai[bot]", "hi")])
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(no_allowlist_project), 30)
    assert result["error"] == "reviewer_allowlist_not_configured"
    assert "hint" in result

    args = prt.build_parser().parse_args(
        ["fetch", "--pr", "1", "--project-dir", str(no_allowlist_project)]
    )
    fake2 = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake2)
    exit_code = prt.cmd_fetch(args)
    assert exit_code == 2


# --- fetch: loop-harness import failure fallback --------------------------------


def test_fetch_import_failure_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    thread = _thread(
        "T1",
        comments=[
            _comment(1, "coderabbitai[bot]", "hi"),
            _comment(2, "human-reviewer", "hi", typename="User"),
        ],
    )
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    monkeypatch.setattr(prt, "_import_pr_review_wait", lambda: None)
    result = prt.fetch_review_threads(1, str(tmp_path), 30)
    assert result["origin_verified"] is False
    # unfiltered: both bot and human comments pass through
    comment_authors = [c["author"] for c in result["unresolved_threads"][0]["comments"]]
    assert comment_authors == ["coderabbitai[bot]", "human-reviewer"]
    assert all(c["severity"] is None for c in result["unresolved_threads"][0]["comments"])


# --- fetch: severity marker propagation -----------------------------------------


def test_fetch_severity_marker_propagation(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    thread = _thread(
        "T1",
        comments=[
            _comment(1, "coderabbitai[bot]", "[critical] fix this now"),
            _comment(2, "coderabbitai[bot]", "no marker, please review"),
        ],
    )
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    comments = result["unresolved_threads"][0]["comments"]
    severities = {c["comment_id"]: c["severity"] for c in comments}
    assert severities[1] == "critical"
    assert severities[2] is None


# --- fetch: REST join for app_slug origin verification --------------------------


def test_fetch_bot_allowlist_matches_via_rest_app_slug_join(
    monkeypatch: pytest.MonkeyPatch, app_slug_allowlist_project: Path
) -> None:
    """GraphQL alone cannot expose `performed_via_github_app`; the REST join must supply it."""
    thread = _thread("T1", comments=[_comment(5, "my-review-bot-bot-actor[bot]", "inline finding")])
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
            (
                _is_review_comments,
                _ok([_rest_review_comment(5, app_slug="my-review-bot", login="app/my-review-bot")]),
            ),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(app_slug_allowlist_project), 30)
    comment_authors = [c["author"] for c in result["unresolved_threads"][0]["comments"]]
    assert comment_authors == ["my-review-bot-bot-actor[bot]"]


def test_fetch_bot_allowlist_rest_join_miss_falls_back_to_pseudo(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    """When the REST join has no entry for a comment id, login-based matching still applies."""
    thread = _thread("T1", comments=[_comment(7, "coderabbitai[bot]", "inline finding")])
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    comment_authors = [c["author"] for c in result["unresolved_threads"][0]["comments"]]
    assert comment_authors == ["coderabbitai[bot]"]


# --- fetch: reply_target_id normalization (Fix B) --------------------------------


def test_fetch_normalizes_reply_target_id_to_root_comment(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    """A reply's `reply_target_id` must be the root comment's id, not its own."""
    thread = _thread(
        "T1",
        comments=[
            _comment(1, "coderabbitai[bot]", "root comment"),
            _comment(2, "coderabbitai[bot]", "reply comment", reply_to_database_id=1),
        ],
    )
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    by_id = {c["comment_id"]: c for c in result["unresolved_threads"][0]["comments"]}
    assert by_id[1]["reply_target_id"] == 1
    assert by_id[2]["reply_target_id"] == 1


# --- fetch: mixed-origin thread protection flag (Fix C) ---------------------------


def test_fetch_flags_thread_with_dropped_non_bot_comments(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    thread = _thread(
        "T1",
        comments=[
            _comment(1, "coderabbitai[bot]", "bot says hi"),
            _comment(2, "human-reviewer", "human says hi", typename="User"),
        ],
    )
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert result["unresolved_threads"][0]["has_non_bot_comments"] is True


def test_fetch_bot_only_thread_has_non_bot_comments_false(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    thread = _thread("T1", comments=[_comment(1, "coderabbitai[bot]", "bot says hi")])
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert result["unresolved_threads"][0]["has_non_bot_comments"] is False


# --- fetch: auto-generated issue comment filtering (Fix D) ------------------------


def test_fetch_skips_auto_generated_issue_comments(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    auto_generated_body = (
        "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\nSummary text"
    )
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([]))),
            (
                _is_issue_comments,
                _ok(
                    [
                        _issue_comment(10, "coderabbitai[bot]", auto_generated_body),
                        _issue_comment(11, "coderabbitai[bot]", "actionable finding"),
                    ]
                ),
            ),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert [c["comment_id"] for c in result["bot_issue_comments"]] == [11]
    assert result["skipped_issue_comments"] == 1


def test_fetch_no_skipped_issue_comments_when_none_auto_generated(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([]))),
            (
                _is_issue_comments,
                _ok([_issue_comment(10, "coderabbitai[bot]", "actionable finding")]),
            ),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert result["skipped_issue_comments"] == 0


# --- fetch: config loading error contract (Fix E) ----------------------------------


def test_fetch_config_load_unexpected_exception_returns_json_error(
    monkeypatch: pytest.MonkeyPatch, loop_harness_project: Path
) -> None:
    """Non-`ConfigError` failures (e.g. malformed YAML) must still hit the JSON contract."""
    prw_module = prt._import_pr_review_wait()
    assert prw_module is not None

    def _raise(_project_dir: str) -> None:
        raise ValueError("boom: invalid YAML")

    monkeypatch.setattr(prw_module, "load_pr_review_config", _raise)
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([]))),
            (_is_issue_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.fetch_review_threads(1, str(loop_harness_project), 30)
    assert result == {"error": "pr_review_config_invalid", "detail": "boom: invalid YAML"}


def test_cmd_fetch_config_load_unexpected_exception_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    loop_harness_project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prw_module = prt._import_pr_review_wait()
    assert prw_module is not None

    def _raise(_project_dir: str) -> None:
        raise ValueError("boom: invalid YAML")

    monkeypatch.setattr(prw_module, "load_pr_review_config", _raise)
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([]))),
            (_is_issue_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    args = prt.build_parser().parse_args(
        ["fetch", "--pr", "1", "--project-dir", str(loop_harness_project)]
    )
    exit_code = prt.cmd_fetch(args)
    captured = capsys.readouterr()
    assert exit_code == 2
    payload = json.loads(captured.out)
    assert payload["error"] == "pr_review_config_invalid"
    assert captured.err == ""


# --- fetch: --output writes full JSON, stdout gets a body-excerpt summary (Fix F) --


def test_cmd_fetch_output_option_writes_full_json_and_stdout_excerpt(
    monkeypatch: pytest.MonkeyPatch,
    loop_harness_project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    long_body = "x" * 300
    thread = _thread("T1", comments=[_comment(1, "coderabbitai[bot]", long_body)])
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([_issue_comment(10, "coderabbitai[bot]", long_body)])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    output_path = tmp_path / "full.json"
    args = prt.build_parser().parse_args(
        [
            "fetch",
            "--pr",
            "1",
            "--project-dir",
            str(loop_harness_project),
            "--output",
            str(output_path),
        ]
    )
    exit_code = prt.cmd_fetch(args)
    captured = capsys.readouterr()
    assert exit_code == 0

    full = json.loads(output_path.read_text(encoding="utf-8"))
    assert full["unresolved_threads"][0]["comments"][0]["body"] == long_body
    assert full["bot_issue_comments"][0]["body"] == long_body

    summary = json.loads(captured.out)
    assert summary["full_output"] == str(output_path)
    summary_comment = summary["unresolved_threads"][0]["comments"][0]
    assert "body" not in summary_comment
    assert summary_comment["body_excerpt"] == long_body[: prt.BODY_EXCERPT_CHARS]
    assert len(summary_comment["body_excerpt"]) == prt.BODY_EXCERPT_CHARS
    summary_issue_comment = summary["bot_issue_comments"][0]
    assert "body" not in summary_issue_comment
    assert summary_issue_comment["body_excerpt"] == long_body[: prt.BODY_EXCERPT_CHARS]


def test_cmd_fetch_without_output_prints_full_body(
    monkeypatch: pytest.MonkeyPatch,
    loop_harness_project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Backward compat: omitting `--output` still prints the full JSON to stdout."""
    thread = _thread("T1", comments=[_comment(1, "coderabbitai[bot]", "full body text")])
    fake = FakeRun(
        [
            (_is_repo_view, _ok({"nameWithOwner": "o/r"})),
            (_is_graphql, _ok(_threads_page([thread]))),
            (_is_issue_comments, _ok([])),
            (_is_review_comments, _ok([])),
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    args = prt.build_parser().parse_args(
        ["fetch", "--pr", "1", "--project-dir", str(loop_harness_project)]
    )
    exit_code = prt.cmd_fetch(args)
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["unresolved_threads"][0]["comments"][0]["body"] == "full body text"
    assert "full_output" not in payload


# --- reply ----------------------------------------------------------------------


def test_reply_posts_body_via_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Fixed in a1b2c3d.", encoding="utf-8")
    fake = FakeRun([(_is_reply, _ok({"id": 999, "html_url": "https://x/999"}))])
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.reply_to_comment("o", "r", 1, 123, body_file, issue_comment=False, timeout=30)
    assert result == {"status": "ok", "comment_id": 999, "url": "https://x/999"}
    reply_cmd = fake.calls[0]
    assert reply_cmd[2] == "repos/o/r/pulls/1/comments/123/replies"
    assert f"body=@{body_file}" in reply_cmd
    # EV-26 / Fix A: must use `-F` (--field), not `-f` (--raw-field). `-f` sends the
    # literal string `body=@/tmp/...` without expanding the file's contents.
    assert "-F" in reply_cmd
    assert "-f" not in reply_cmd
    body_flag_index = reply_cmd.index(f"body=@{body_file}")
    assert reply_cmd[body_flag_index - 1] == "-F"


def test_reply_issue_comment_uses_issue_comments_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Summary reply.", encoding="utf-8")
    fake = FakeRun([(_is_reply, _ok({"id": 1000, "html_url": "https://x/1000"}))])
    monkeypatch.setattr(prt.subprocess, "run", fake)
    prt.reply_to_comment("o", "r", 1, 0, body_file, issue_comment=True, timeout=30)
    reply_cmd = fake.calls[0]
    assert reply_cmd[2] == "repos/o/r/issues/1/comments"


def test_reply_missing_body_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    with pytest.raises(prt.GhCommandError) as exc_info:
        prt.reply_to_comment("o", "r", 1, 1, missing, issue_comment=False, timeout=30)
    assert exc_info.value.code == "body_file_not_found"


# --- resolve ----------------------------------------------------------------------


def test_resolve_mutation_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(
        [
            (
                _is_graphql,
                _ok(
                    {"data": {"resolveReviewThread": {"thread": {"id": "TID", "isResolved": True}}}}
                ),
            )
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    result = prt.resolve_thread("TID", 30)
    assert result == {"thread_id": "TID", "is_resolved": True}


def test_resolve_mutation_failure_graphql_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun([(_is_graphql, _ok({"data": None, "errors": [{"message": "not found"}]}))])
    monkeypatch.setattr(prt.subprocess, "run", fake)
    with pytest.raises(prt.GhCommandError) as exc_info:
        prt.resolve_thread("TID", 30)
    assert exc_info.value.code == "graphql_error"


def test_cmd_resolve_not_resolved_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRun(
        [
            (
                _is_graphql,
                _ok(
                    {
                        "data": {
                            "resolveReviewThread": {"thread": {"id": "TID", "isResolved": False}}
                        }
                    }
                ),
            )
        ]
    )
    monkeypatch.setattr(prt.subprocess, "run", fake)
    args = prt.build_parser().parse_args(["resolve", "--thread-id", "TID"])
    exit_code = prt.cmd_resolve(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out) == {"thread_id": "TID", "is_resolved": False}


# --- pagination helper: gh api --paginate concatenated JSON ---------------------


def test_parse_paginated_json_merges_list_pages() -> None:
    output = json.dumps([{"id": 1}]) + json.dumps([{"id": 2}])
    assert prt._parse_paginated_json(output) == [{"id": 1}, {"id": 2}]


def test_parse_paginated_json_empty_returns_none() -> None:
    assert prt._parse_paginated_json("") is None


# --- gh command failures ----------------------------------------------------------


def test_run_gh_json_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun([(lambda _cmd: True, _fail("gh: authentication required"))])
    monkeypatch.setattr(prt.subprocess, "run", fake)
    with pytest.raises(prt.GhCommandError) as exc_info:
        prt._run_gh_json(["gh", "repo", "view"], 30)
    assert exc_info.value.code == "gh_command_failed"
    assert "authentication required" in exc_info.value.detail
