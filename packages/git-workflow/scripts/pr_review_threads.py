"""PR review bot threads/comments automation for the `/review-respond` skill.

Subcommands:
    detect  - Detect the open PR for the current git branch.
    fetch   - Fetch unresolved review threads + bot issue comments for a PR.
    reply   - Reply to a review comment or post an issue comment.
    resolve - Resolve a PR review thread via GraphQL mutation.

All output is machine-readable JSON (stdout) so a calling skill can parse it.
Bot origin verification and severity classification reuse loop-harness's
`pr_review_wait` module (`packages/loop-harness/lib/pr_review_wait.py`) when
available; if that module cannot be imported, `fetch` falls back to
unfiltered results and flags this via `"origin_verified": false`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_GH_TIMEOUT_SECONDS = 30
REVIEW_THREADS_PAGE_SIZE = 50
THREAD_COMMENTS_PAGE_SIZE = 50
BODY_EXCERPT_CHARS = 200

# Fallback marker used only when loop-harness's `pr_review_wait` module (which carries
# the full, config-driven `auto_generated_markers` list) cannot be imported.
FALLBACK_AUTO_GENERATED_MARKER = "<!-- This is an auto-generated comment"

_LOOP_HARNESS_LIB = Path(__file__).resolve().parents[2] / "loop-harness" / "lib"

REVIEWER_ALLOWLIST_HINT = (
    "Set pr_review.reviewer_allowlist in "
    ".claude/config/loop-harness/loop-harness.local.yaml before running "
    "review-respond (fail-closed: bot origin cannot be verified without it)."
)

REVIEW_THREADS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $cursor: String = null) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      reviewThreads(first: {REVIEW_THREADS_PAGE_SIZE}, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          isResolved
          isOutdated
          path
          line
          comments(first: {THREAD_COMMENTS_PAGE_SIZE}) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{
              databaseId
              author {{ login __typename }}
              authorAssociation
              body
              url
              createdAt
              replyTo {{ databaseId }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

# Issue #235 (PR #276 review, medium): `REVIEW_THREADS_QUERY`'s inner `comments` connection is
# capped at `THREAD_COMMENTS_PAGE_SIZE` with no cursor; a thread with more comments than that
# silently drops the tail, which can hide a human reply past the page boundary and cause
# `has_non_bot_comments` to wrongly stay False (`_normalize_thread` never sees the dropped
# comment). This follow-up query fetches later comment pages for one thread node by id so
# `fetch_all_review_threads` can fill in every comment before origin verification runs.
THREAD_COMMENTS_QUERY = f"""
query($threadId: ID!, $cursor: String = null) {{
  node(id: $threadId) {{
    ... on PullRequestReviewThread {{
      comments(first: {THREAD_COMMENTS_PAGE_SIZE}, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          databaseId
          author {{ login __typename }}
          authorAssociation
          body
          url
          createdAt
          replyTo {{ databaseId }}
        }}
      }}
    }}
  }}
}}
"""

RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


class GhCommandError(RuntimeError):
    """Raised when a `gh`/`git` subprocess call fails or returns unusable output."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --- subprocess helpers -----------------------------------------------------


def _run(
    cmd: list[str], *, timeout: int, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _parse_paginated_json(output: str) -> Any:
    """Parse single or concatenated JSON documents from `gh api --paginate`."""
    text = output.strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    while index < len(text):
        value, index = decoder.raw_decode(text, index)
        values.append(value)
        while index < len(text) and text[index].isspace():
            index += 1
    if len(values) == 1:
        return values[0]
    if all(isinstance(value, list) for value in values):
        merged: list[Any] = []
        for value in values:
            merged.extend(value)
        return merged
    return values


def _run_gh_json(cmd: list[str], timeout: int, *, cwd: str | None = None) -> Any:
    result = _run(cmd, timeout=timeout, cwd=cwd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "gh command failed").strip()
        raise GhCommandError("gh_command_failed", detail)
    try:
        return _parse_paginated_json(result.stdout)
    except json.JSONDecodeError as exc:
        raise GhCommandError("invalid_gh_json", str(exc)) from exc


def _raise_on_graphql_errors(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GhCommandError("invalid_gh_json", "graphql response is not an object")
    errors = payload.get("errors")
    if errors:
        raise GhCommandError("graphql_error", json.dumps(errors, ensure_ascii=False))
    return payload


# --- loop-harness integration (optional) ------------------------------------


def _import_pr_review_wait() -> Any | None:
    """Import loop-harness's `pr_review_wait` module if the package is present."""
    lib_dir = str(_LOOP_HARNESS_LIB)
    if _LOOP_HARNESS_LIB.is_dir() and lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    try:
        import pr_review_wait  # type: ignore[import-not-found]
    except ImportError:
        return None
    return pr_review_wait


def _allowlist_error(exc: Exception) -> dict[str, Any]:
    message = str(exc)
    code = (
        "reviewer_allowlist_not_configured"
        if "reviewer_allowlist" in message
        else "pr_review_config_invalid"
    )
    return {"error": code, "detail": message, "hint": REVIEWER_ALLOWLIST_HINT}


def _classify_severity(body: str, prw_module: Any | None, config: Any | None) -> str | None:
    """Return an explicit severity, or None when caller-side LLM classification is needed."""
    if prw_module is None or config is None:
        return None
    decision = prw_module.classify_severity(body, config)
    if decision.needs_classification:
        return None
    return decision.severity


def _pseudo_raw_from_graphql_comment(raw: dict[str, Any]) -> dict[str, Any]:
    """Adapt a GraphQL comment node to the REST-like shape `verify_origin` expects.

    Used as a fallback when the comment's `databaseId` is not present in the REST
    review-comments join (see `_raw_for_origin_check`). This shape cannot carry
    `performed_via_github_app`, so app_slug-only allowlist entries never match here.
    """
    author = raw.get("author") or {}
    return {
        "user": {"login": author.get("login"), "type": author.get("__typename")},
        "author_association": raw.get("authorAssociation"),
    }


def _raw_for_origin_check(
    raw: dict[str, Any], rest_comments_by_id: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    """Prefer the REST review-comment payload (carries `performed_via_github_app`).

    GraphQL `reviewThreads` has no field equivalent to `performed_via_github_app`, so
    app_slug-based allowlist entries can only be verified against the REST join.
    Falls back to the pseudo-converted GraphQL shape when the join has no match.
    """
    database_id = raw.get("databaseId")
    rest_raw = rest_comments_by_id.get(database_id) if database_id is not None else None
    if rest_raw is not None:
        return rest_raw
    return _pseudo_raw_from_graphql_comment(raw)


# --- repo / PR resolution ----------------------------------------------------


def resolve_repo(timeout: int, *, cwd: str | None = None) -> tuple[str, str]:
    """Resolve the target repo's `owner, name` via `gh repo view` (no explicit `-R`).

    PR #276 review (high): `gh repo view` infers its target repo from the process's
    *actual* working directory, not from any `project_dir` a caller passes elsewhere in
    the call chain. Callers resolving the repo for a specific `project_dir` (e.g.
    `fetch_review_threads`, invoked in-process by loop-harness with `--project` possibly
    pointing outside this process's cwd) must pass `cwd=project_dir` here so this never
    silently targets whatever repo the *caller's* process happened to start in.
    """
    payload = _run_gh_json(["gh", "repo", "view", "--json", "nameWithOwner"], timeout, cwd=cwd)
    if not isinstance(payload, dict) or "nameWithOwner" not in payload:
        raise GhCommandError("invalid_gh_json", "gh repo view: missing nameWithOwner")
    owner, name = str(payload["nameWithOwner"]).split("/", 1)
    return owner, name


def _current_branch(timeout: int) -> str:
    result = _run(["git", "branch", "--show-current"], timeout=timeout)
    return result.stdout.strip() if result.returncode == 0 else ""


def detect_pr(timeout: int) -> dict[str, Any]:
    branch = _current_branch(timeout)
    if not branch:
        raise GhCommandError("no_current_branch", "Could not determine current git branch.")
    cmd = [
        "gh",
        "pr",
        "list",
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        "number,url,title,isDraft",
    ]
    prs = _run_gh_json(cmd, timeout)
    prs = prs if isinstance(prs, list) else []
    if len(prs) == 0:
        raise GhCommandError("no_open_pr", f"No open PR found for branch '{branch}'.")
    if len(prs) > 1:
        raise GhCommandError("multiple_open_prs", f"Multiple open PRs found for branch '{branch}'.")
    pr = prs[0]
    return {"pr_number": pr["number"], "url": pr["url"], "title": pr["title"]}


# --- fetch: review threads + issue comments ---------------------------------


def _fetch_review_threads_page(
    owner: str, name: str, number: int, cursor: str | None, timeout: int
) -> dict[str, Any]:
    cmd = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={REVIEW_THREADS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={number}",
    ]
    if cursor:
        cmd += ["-F", f"cursor={cursor}"]
    return _raise_on_graphql_errors(_run_gh_json(cmd, timeout))


def _fetch_thread_comments_page(thread_id: str, cursor: str | None, timeout: int) -> dict[str, Any]:
    cmd = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={THREAD_COMMENTS_QUERY}",
        "-F",
        f"threadId={thread_id}",
    ]
    if cursor:
        cmd += ["-F", f"cursor={cursor}"]
    return _raise_on_graphql_errors(_run_gh_json(cmd, timeout))


def _fill_thread_comments(thread: dict[str, Any], timeout: int) -> None:
    """Fetch and append every remaining comment page for one thread, mutating it in place.

    Issue #235 (PR #276 review, medium): a no-op for the overwhelming majority of threads,
    which never exceed `THREAD_COMMENTS_PAGE_SIZE` comments and so never have
    `comments.pageInfo.hasNextPage` set -- this only issues extra `gh api graphql` calls for
    the rare thread whose comment count exceeds the first page.
    """
    comments = thread.get("comments")
    if not isinstance(comments, dict):
        return
    page_info = comments.get("pageInfo") or {}
    cursor = page_info.get("endCursor")
    nodes = list(comments.get("nodes") or [])
    while page_info.get("hasNextPage") and cursor:
        payload = _fetch_thread_comments_page(str(thread.get("id")), cursor, timeout)
        connection = payload["data"]["node"]["comments"]
        nodes.extend(connection["nodes"])
        page_info = connection["pageInfo"]
        cursor = page_info.get("endCursor")
    thread["comments"] = {**comments, "nodes": nodes, "pageInfo": page_info}


def fetch_all_review_threads(
    owner: str, name: str, number: int, timeout: int
) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload = _fetch_review_threads_page(owner, name, number, cursor, timeout)
        connection = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            _fill_thread_comments(thread, timeout)
            threads.append(thread)
        page_info = connection["pageInfo"]
        if not page_info.get("hasNextPage"):
            return threads
        cursor = page_info.get("endCursor")


def fetch_issue_comments(owner: str, name: str, number: int, timeout: int) -> list[dict[str, Any]]:
    cmd = ["gh", "api", f"repos/{owner}/{name}/issues/{number}/comments", "--paginate"]
    result = _run_gh_json(cmd, timeout)
    return result if isinstance(result, list) else []


def fetch_review_comments_raw(
    owner: str, name: str, number: int, timeout: int
) -> dict[int, dict[str, Any]]:
    """Fetch REST review comments, indexed by id (== GraphQL `databaseId`).

    REST review comments carry `performed_via_github_app`, which GraphQL
    `reviewThreads` cannot express; this join lets `verify_origin` see it.
    """
    cmd = ["gh", "api", f"repos/{owner}/{name}/pulls/{number}/comments", "--paginate"]
    result = _run_gh_json(cmd, timeout)
    items = result if isinstance(result, list) else []
    return {item["id"]: item for item in items if isinstance(item, dict) and "id" in item}


def _reply_target_id(raw: dict[str, Any]) -> int | None:
    """Resolve the top-level comment id a reply must target.

    `POST /pulls/{n}/comments/{id}/replies` fails when `{id}` is itself a reply's id;
    it must be the root comment's id. GraphQL `replyTo` links a reply to its root, so
    when present we normalize to `replyTo.databaseId`; top-level comments fall back to
    their own `databaseId`.
    """
    reply_to = raw.get("replyTo") or {}
    reply_to_id = reply_to.get("databaseId")
    return reply_to_id if reply_to_id is not None else raw.get("databaseId")


def _normalize_comment(
    raw: dict[str, Any], prw_module: Any | None, config: Any | None
) -> dict[str, Any]:
    author = raw.get("author") or {}
    body = raw.get("body") or ""
    return {
        "comment_id": raw.get("databaseId"),
        "reply_target_id": _reply_target_id(raw),
        "author": author.get("login"),
        "body": body,
        "url": raw.get("url"),
        "created_at": raw.get("createdAt"),
        "severity": _classify_severity(body, prw_module, config),
    }


def _normalize_thread(
    thread: dict[str, Any],
    prw_module: Any | None,
    config: Any | None,
    rest_comments_by_id: dict[int, dict[str, Any]],
    *,
    origin_verified: bool,
) -> dict[str, Any]:
    raw_comments = thread.get("comments", {}).get("nodes", [])
    comments: list[dict[str, Any]] = []
    has_non_bot_comments = False
    for raw in raw_comments:
        is_bot_origin = not origin_verified or prw_module.verify_origin(
            _raw_for_origin_check(raw, rest_comments_by_id), config.reviewer_allowlist
        )
        if is_bot_origin:
            comments.append(_normalize_comment(raw, prw_module, config))
        else:
            # Origin check dropped a non-bot (e.g. human) comment from this thread; the
            # thread mixes bot and non-bot commentary, so callers should not blindly
            # resolve it based on the (bot-only) comments returned here.
            has_non_bot_comments = True
    return {
        "thread_id": thread.get("id"),
        "is_outdated": bool(thread.get("isOutdated", False)),
        "path": thread.get("path"),
        "line": thread.get("line"),
        "has_non_bot_comments": has_non_bot_comments,
        "comments": comments,
    }


def _normalize_issue_comment(
    raw: dict[str, Any], prw_module: Any | None, config: Any | None
) -> dict[str, Any]:
    user = raw.get("user") or {}
    body = raw.get("body") or ""
    return {
        "comment_id": raw.get("id"),
        "author": user.get("login"),
        "body": body,
        "url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
        "severity": _classify_severity(body, prw_module, config),
    }


def _is_auto_generated_issue_comment(body: str, prw_module: Any | None, config: Any | None) -> bool:
    """Detect auto-generated bot summaries/boilerplate that aren't actionable review items.

    Prefers loop-harness's `pr_review.auto_generated_markers` (covers rate-limit and
    in-progress boilerplate too) when available; otherwise falls back to the single
    marker every CodeRabbit auto-generated comment shares.
    """
    if prw_module is not None and config is not None:
        return bool(prw_module._is_auto_generated_comment(body, config))
    return FALLBACK_AUTO_GENERATED_MARKER in body


def _build_fetch_result(
    pr_number: int,
    threads: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
    prw_module: Any | None,
    config: Any | None,
    rest_comments_by_id: dict[int, dict[str, Any]],
    *,
    origin_verified: bool,
) -> dict[str, Any]:
    unresolved = [
        _normalize_thread(
            thread, prw_module, config, rest_comments_by_id, origin_verified=origin_verified
        )
        for thread in threads
        if not thread.get("isResolved")
    ]
    if origin_verified:
        unresolved = [thread for thread in unresolved if thread["comments"]]
    origin_matched_issue_comments = [
        raw
        for raw in issue_comments
        if not origin_verified or prw_module.verify_origin(raw, config.reviewer_allowlist)
    ]
    bot_comments: list[dict[str, Any]] = []
    skipped_issue_comments = 0
    for raw in origin_matched_issue_comments:
        if _is_auto_generated_issue_comment(raw.get("body") or "", prw_module, config):
            skipped_issue_comments += 1
            continue
        bot_comments.append(_normalize_issue_comment(raw, prw_module, config))
    return {
        "pr_number": pr_number,
        "unresolved_threads": unresolved,
        "bot_issue_comments": bot_comments,
        "skipped_issue_comments": skipped_issue_comments,
        "origin_verified": origin_verified,
    }


def fetch_review_threads(pr_number: int, project_dir: str, timeout: int) -> dict[str, Any]:
    owner, name = resolve_repo(timeout, cwd=project_dir)
    threads = fetch_all_review_threads(owner, name, pr_number, timeout)
    issue_comments = fetch_issue_comments(owner, name, pr_number, timeout)
    prw_module = _import_pr_review_wait()
    if prw_module is None:
        return _build_fetch_result(
            pr_number, threads, issue_comments, None, None, {}, origin_verified=False
        )
    try:
        config = prw_module.load_pr_review_config(project_dir)
    except prw_module.ConfigError as exc:
        return _allowlist_error(exc)
    except Exception as exc:  # noqa: BLE001 - convert any unexpected config-loading
        # failure (e.g. malformed YAML in loop-harness.local.yaml) to the same JSON
        # error contract instead of letting it crash as a raw traceback.
        return {"error": "pr_review_config_invalid", "detail": str(exc)}
    # REST join: only needed to verify origin, so fetch it after the allowlist is
    # confirmed to exist. Keeps this to exactly one extra (paginated) gh call.
    rest_comments_by_id = fetch_review_comments_raw(owner, name, pr_number, timeout)
    return _build_fetch_result(
        pr_number,
        threads,
        issue_comments,
        prw_module,
        config,
        rest_comments_by_id,
        origin_verified=True,
    )


# --- reply / resolve ---------------------------------------------------------


def reply_to_comment(
    owner: str,
    name: str,
    pr_number: int,
    comment_id: int,
    body_file: Path,
    *,
    issue_comment: bool,
    timeout: int,
) -> dict[str, Any]:
    if not body_file.is_file():
        raise GhCommandError("body_file_not_found", str(body_file))
    if issue_comment:
        path = f"repos/{owner}/{name}/issues/{pr_number}/comments"
    else:
        path = f"repos/{owner}/{name}/pulls/{pr_number}/comments/{comment_id}/replies"
    # `-F/--field` (not `-f/--raw-field`) is required so `@<file>` is expanded to the
    # file's contents by `gh`; `-f` sends the literal string `body=@/tmp/...` as-is.
    cmd = ["gh", "api", path, "-F", f"body=@{body_file}"]
    payload = _run_gh_json(cmd, timeout)
    payload = payload if isinstance(payload, dict) else {}
    return {
        "status": "ok",
        "comment_id": payload.get("id"),
        "url": payload.get("html_url"),
    }


def resolve_thread(thread_id: str, timeout: int) -> dict[str, Any]:
    cmd = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={RESOLVE_THREAD_MUTATION}",
        "-F",
        f"threadId={thread_id}",
    ]
    payload = _raise_on_graphql_errors(_run_gh_json(cmd, timeout))
    thread = payload["data"]["resolveReviewThread"]["thread"]
    return {"thread_id": thread["id"], "is_resolved": bool(thread["isResolved"])}


# --- CLI ----------------------------------------------------------------------


def _error_payload(exc: GhCommandError) -> dict[str, Any]:
    return {"error": exc.code, "detail": exc.detail}


def cmd_detect(args: argparse.Namespace) -> int:
    try:
        result = detect_pr(args.timeout)
    except GhCommandError as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _excerpt_comment(comment: dict[str, Any]) -> dict[str, Any]:
    """Replace a comment's full `body` with a `body_excerpt` (first `BODY_EXCERPT_CHARS`)."""
    excerpted = {k: v for k, v in comment.items() if k != "body"}
    excerpted["body_excerpt"] = (comment.get("body") or "")[:BODY_EXCERPT_CHARS]
    return excerpted


def _summarize_fetch_result(result: dict[str, Any], output_path: str) -> dict[str, Any]:
    """Build a stdout-safe summary: same shape as `result`, bodies truncated to excerpts."""
    summary = dict(result)
    if "unresolved_threads" in summary:
        summary["unresolved_threads"] = [
            {**thread, "comments": [_excerpt_comment(c) for c in thread.get("comments", [])]}
            for thread in summary["unresolved_threads"]
        ]
    if "bot_issue_comments" in summary:
        summary["bot_issue_comments"] = [_excerpt_comment(c) for c in summary["bot_issue_comments"]]
    summary["full_output"] = output_path
    return summary


def cmd_fetch(args: argparse.Namespace) -> int:
    project_dir = args.project_dir or str(Path.cwd())
    try:
        result = fetch_review_threads(args.pr, project_dir, args.timeout)
    except GhCommandError as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False))
        return 1
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(_summarize_fetch_result(result, args.output), ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 2 if "error" in result else 0


def cmd_reply(args: argparse.Namespace) -> int:
    try:
        owner, name = resolve_repo(args.timeout)
        result = reply_to_comment(
            owner,
            name,
            args.pr,
            args.comment_id,
            Path(args.body_file),
            issue_comment=args.issue_comment,
            timeout=args.timeout,
        )
    except GhCommandError as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        result = resolve_thread(args.thread_id, args.timeout)
    except GhCommandError as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["is_resolved"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PR review bot threads/comments automation for /review-respond.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_GH_TIMEOUT_SECONDS,
        help="gh/git subprocess timeout in seconds.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("detect", help="Detect the open PR for the current git branch.")

    fetch_parser = subparsers.add_parser(
        "fetch", help="Fetch unresolved review threads and bot issue comments."
    )
    fetch_parser.add_argument("--pr", type=int, required=True)
    fetch_parser.add_argument(
        "--project-dir", default=None, help="Project dir for loop-harness config (default: cwd)."
    )
    fetch_parser.add_argument(
        "--output",
        default=None,
        help=(
            "Write the full JSON result to this path; stdout gets a summary with "
            "comment bodies truncated to body_excerpt instead."
        ),
    )

    reply_parser = subparsers.add_parser("reply", help="Reply to a review or issue comment.")
    reply_parser.add_argument("--pr", type=int, required=True)
    reply_parser.add_argument("--comment-id", type=int, required=True)
    reply_parser.add_argument("--body-file", required=True)
    reply_parser.add_argument(
        "--issue-comment",
        action="store_true",
        help="Post as an issue comment instead of a review comment reply.",
    )

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a PR review thread.")
    resolve_parser.add_argument("--thread-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "detect": cmd_detect,
        "fetch": cmd_fetch,
        "reply": cmd_reply,
        "resolve": cmd_resolve,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
