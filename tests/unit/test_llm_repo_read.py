"""読み取り道具の公開契約と、Git・実パスによる安全境界を検証する。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from lib.llm.tools.read_ledger import ReadLedger, parse_citation
from lib.llm.tools.repo_read import TOOL_DEFINITIONS, ReadLimits, RepoReadTools


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(root)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return root


def _write(repo: Path, path: str, text: str) -> Path:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _track(repo: Path, path: str) -> None:
    # 一時リポジトリの index だけで追跡状態を作り、コミットは不要にする。
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "--add", "--", path],
        check=True,
        capture_output=True,
        timeout=10,
    )


def _numbered_lines(text: str) -> list[tuple[int, str]]:
    return [
        (int(match[1]), match[2])
        for line in text.splitlines()
        if (match := re.fullmatch(r"(\d+)\t(.*)", line))
    ]


def _grep_matches(text: str) -> list[tuple[str, int, str]]:
    return [
        (match[1], int(match[2]), match[3])
        for line in text.splitlines()
        if (match := re.fullmatch(r"(.+):(\d+):(.*)", line))
    ]


def _assert_rejected(result) -> None:
    assert result.ok is False
    assert result.text.strip(), "拒否の理由を道具の結果に含める"


def _assert_no_private_content(text: str) -> None:
    for line in text.splitlines():
        for fragment in ("private-alpha", "private-omega"):
            assert fragment not in line.casefold()


def _remove_before_open(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[Path]:
    original_open = os.open
    removed = []

    def remove(path, flags, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None and not removed:
            target.unlink()
            removed.append(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", remove)
    return removed


def test_read_file_returns_only_requested_lines_with_original_numbers(repo: Path) -> None:
    _write(repo, "src/a.py", "one\ntwo\nthree\nfour\n")

    result = RepoReadTools(repo).read_file("src/a.py", start_line=2, end_line=3)

    assert result.ok is True
    assert result.text.splitlines() == ["2\ttwo", "3\tthree"]


def test_untracked_file_is_readable(repo: Path) -> None:
    _write(repo, ".gitignore", "build/\n")
    _write(repo, "notes/new.md", "hello\n")

    result = RepoReadTools(repo).read_file("notes/new.md")

    assert result.ok is True
    assert result.text.splitlines() == ["1\thello"]


def test_gitignored_file_and_directory_are_rejected(repo: Path, capsys, caplog) -> None:
    _write(repo, ".gitignore", "build/\n")
    _write(repo, "build/out.txt", "PRIVATE-ALPHA private-omega\n")
    tools = RepoReadTools(repo)

    results = [tools.read_file("build/out.txt"), tools.list_dir("build")]

    for result in results:
        _assert_rejected(result)
        _assert_no_private_content(result.text)
    captured = capsys.readouterr()
    _assert_no_private_content(captured.out + captured.err + caplog.text)


@pytest.mark.parametrize("path", ["docs/environment.md", "keys.txt"])
def test_names_outside_deny_patterns_remain_readable(repo: Path, path: str) -> None:
    _write(repo, path, "public text\n")
    _track(repo, path)

    result = RepoReadTools(repo).read_file(path)

    assert result.ok is True
    assert _numbered_lines(result.text) == [(1, "public text")]


@pytest.mark.parametrize(
    "path",
    [
        ".env.local",
        "config/.env.production",
        "a.pem",
        "x.key",
        "aws_credentials.json",
        "config/secrets/a.txt",
        ".git/private.txt",
        "config/.ENV.LOCAL",
    ],
)
def test_default_deny_patterns_reject_contents_even_with_extra_patterns(
    repo: Path, path: str, capsys, caplog
) -> None:
    _write(repo, path, "PRIVATE-ALPHA private-omega\n")
    if not path.startswith(".git/"):
        _track(repo, path)
    tools = RepoReadTools(repo, extra_deny_patterns=("*.sqlite",))

    result = tools.read_file(path)

    _assert_rejected(result)
    _assert_no_private_content(result.text)
    captured = capsys.readouterr()
    _assert_no_private_content(captured.out + captured.err + caplog.text)


def test_additional_deny_pattern_is_applied(repo: Path) -> None:
    _write(repo, "db.sqlite", "PRIVATE-ALPHA private-omega\n")
    assert RepoReadTools(repo).read_file("db.sqlite").ok is True

    result = RepoReadTools(repo, extra_deny_patterns=("*.sqlite",)).read_file("db.sqlite")

    _assert_rejected(result)
    _assert_no_private_content(result.text)


@pytest.mark.parametrize("pattern", ["docs/internal/*", "/DOCS/INTERNAL/*", "docs/**/internal/*"])
def test_path_deny_patterns_apply_to_all_tools_and_existing_citations(
    repo: Path, pattern: str
) -> None:
    blocked = [
        "docs/internal/a.md",
        "docs/internal/sub/b.md",
        "private/x.txt",
        "nested/private/z.md",
    ]
    public = ["docs/public.md", "internal/c.md", "other/docs/internal/d.md", "public.txt"]
    for path in blocked:
        _write(repo, path, "hi PRIVATE-ALPHA private-omega\n")
    for path in public:
        _write(repo, path, "hi public\n")
    (repo / "alias.md").symlink_to("docs/internal/a.md")
    (repo / "docs/internal/link.md").symlink_to("../../public.txt")
    blocked.extend(["alias.md", "docs/internal/link.md"])
    ledger = ReadLedger()
    unrestricted = RepoReadTools(repo, ledger=ledger)
    for path in blocked:
        assert unrestricted.read_file(path).ok is True
        assert unrestricted.check_citation(f"{path}:1-1").verified is True
    tools = RepoReadTools(repo, extra_deny_patterns=(pattern, "private/"), ledger=ledger)

    for path in blocked:
        result = tools.read_file(path)
        _assert_rejected(result)
        assert "deny_list" in result.text
        _assert_no_private_content(result.text)
        assert tools.check_citation(f"{path}:1-1").verified is False
    for path in public:
        assert tools.read_file(path).ok is True
        assert tools.check_citation(f"{path}:1-1").verified is True
    for directory in ("private", "docs/internal", "docs/internal/sub"):
        _assert_rejected(tools.list_dir(directory))
    assert set(tools.glob("**/*").text.splitlines()) == set(public)
    found = tools.grep("hi")
    assert found.ok is True
    assert {path for path, _, _ in _grep_matches(found.text)} == set(public)
    _assert_no_private_content(found.text)
    root_listing = tools.list_dir(".")
    assert root_listing.ok is True
    assert set(root_listing.text.splitlines()) == {"docs/", "internal/", "other/", "public.txt"}
    docs_listing = tools.list_dir("docs")
    assert docs_listing.ok is True
    assert docs_listing.text.splitlines() == ["public.md"]


@pytest.mark.parametrize(
    ("pattern", "blocked", "public"),
    [
        ("docs/*/a.md", ["docs/internal/a.md"], ["docs/internal/sub/a.md"]),
        ("docs/**/a.md", ["docs/a.md", "docs/internal/sub/a.md"], ["other/docs/a.md"]),
        ("docs/internal/", ["docs/internal/a.md"], ["other/docs/internal/a.md"]),
    ],
)
def test_path_deny_globs_respect_root_and_directory_boundaries(
    repo: Path, pattern: str, blocked: list[str], public: list[str]
) -> None:
    for path in [*blocked, *public]:
        _write(repo, path, "hi\n")
    tools = RepoReadTools(repo, extra_deny_patterns=(pattern,))

    for path in blocked:
        _assert_rejected(tools.read_file(path))
    for path in public:
        assert tools.read_file(path).ok is True
    assert set(tools.glob("**/*").text.splitlines()) == set(public)


@pytest.mark.parametrize("pattern", ["", "/", "a\x00b", "a/../b", "../private", "/../private"])
def test_invalid_extra_deny_pattern_is_rejected_at_construction(repo: Path, pattern: str) -> None:
    with pytest.raises(ValueError):
        RepoReadTools(repo, extra_deny_patterns=(pattern,))


def test_discovery_and_search_exclude_ignored_denied_and_escaped_paths(
    repo: Path, capsys, caplog
) -> None:
    _write(repo, ".gitignore", "build/\n")
    _write(repo, "public.txt", "needle public\n")
    blocked = ["build/out.txt", ".env.local", "config/secrets/a.txt", "db.sqlite"]
    for path in blocked:
        _write(repo, path, "needle PRIVATE-ALPHA private-omega\n")
    outside = repo.parent / "outside.txt"
    outside.write_text("needle PRIVATE-ALPHA private-omega\n", encoding="utf-8")
    os.symlink(outside, repo / "outlink")
    os.symlink(".env.local", repo / "inlink")
    tools = RepoReadTools(repo, extra_deny_patterns=("*.sqlite",))

    listing = tools.list_dir(".")
    globbed = tools.glob("**/*")
    found = tools.grep("needle")

    for result in (listing, globbed, found):
        assert result.ok is True
        _assert_no_private_content(result.text)
    assert "public.txt" in listing.text.splitlines()
    assert "public.txt" in globbed.text.splitlines()
    for result in (listing, globbed):
        entries = set(result.text.splitlines())
        assert entries.isdisjoint({*blocked, "build/", "config/", "outlink", "inlink", ".git/"})
    assert _grep_matches(found.text) == [("public.txt", 1, "needle public")]
    captured = capsys.readouterr()
    _assert_no_private_content(captured.out + captured.err + caplog.text)


@pytest.mark.parametrize("target", [".env.local", "build/out.txt"])
def test_internal_symlink_cannot_bypass_file_policy(repo: Path, target: str) -> None:
    _write(repo, ".gitignore", "build/\n")
    _write(repo, target, "PRIVATE-ALPHA private-omega\n")
    os.symlink(target, repo / "inlink")
    _track(repo, "inlink")

    result = RepoReadTools(repo).read_file("inlink")

    _assert_rejected(result)
    _assert_no_private_content(result.text)


def test_denied_symlink_name_cannot_expose_public_target(repo: Path) -> None:
    _write(repo, "public.txt", "public text\n")
    os.symlink("public.txt", repo / "secret-link")
    _track(repo, "secret-link")
    assert RepoReadTools(repo).read_file("public.txt").ok is True

    _assert_rejected(RepoReadTools(repo).read_file("secret-link"))


def test_internal_dotdot_and_absolute_paths_are_readable(repo: Path) -> None:
    target = _write(repo, "src/a.py", "public text\n")
    _track(repo, "src/a.py")
    ledger = ReadLedger()
    tools = RepoReadTools(repo, ledger=ledger)

    for path in ("src/../src/a.py", str(target)):
        result = tools.read_file(path)
        assert result.ok is True
        assert _numbered_lines(result.text) == [(1, "public text")]
    assert ledger.check_citation("src/a.py:1-1", exists=True, total_lines=1).verified is True


@pytest.mark.parametrize("directory_link", [False, True], ids=["file", "directory"])
def test_symlink_outside_root_is_rejected(repo: Path, directory_link: bool) -> None:
    outside_dir = repo.parent / "repo-other"
    outside_dir.mkdir()
    outside = _write(outside_dir, "outside.txt", "PRIVATE-ALPHA private-omega\n")
    os.symlink(outside_dir if directory_link else outside, repo / "outlink")
    _track(repo, "outlink")
    path = "outlink/outside.txt" if directory_link else "outlink"

    result = RepoReadTools(repo).read_file(path)

    _assert_rejected(result)
    _assert_no_private_content(result.text)


def test_dotdot_outside_root_is_rejected_by_read_and_grep(repo: Path) -> None:
    _write(repo.parent, "outside.txt", "PRIVATE-ALPHA private-omega\n")
    tools = RepoReadTools(repo)

    results = [tools.read_file("../outside.txt"), tools.grep("PRIVATE", path="..")]

    for result in results:
        _assert_rejected(result)
        _assert_no_private_content(result.text)


def test_absolute_path_with_root_prefix_but_outside_root_is_rejected(repo: Path) -> None:
    outside = _write(repo.parent, "repo-other/outside.txt", "PRIVATE-ALPHA private-omega\n")

    result = RepoReadTools(repo).read_file(str(outside))

    _assert_rejected(result)
    _assert_no_private_content(result.text)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("read_file", {"path": "public.txt"}),
        ("list_dir", {"path": "."}),
        ("glob", {"pattern": "**/*"}),
        ("grep", {"pattern": "PRIVATE"}),
    ],
)
def test_non_repository_fails_closed(repo: Path, name: str, arguments: dict) -> None:
    non_repo = repo.parent / "plain"
    non_repo.mkdir()
    _write(non_repo, "public.txt", "PRIVATE-ALPHA private-omega\n")

    result = RepoReadTools(non_repo).execute(name, arguments)

    _assert_rejected(result)
    _assert_no_private_content(result.text)


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("PRIVATE-ALPHA private-omega"),
        subprocess.CalledProcessError(128, ["git"], stderr="PRIVATE-ALPHA private-omega"),
        subprocess.TimeoutExpired(["git"], 1, stderr="PRIVATE-ALPHA private-omega"),
    ],
    ids=["missing-git", "git-error", "timeout"],
)
def test_git_failure_returns_reason_without_exception_details(
    repo: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception, capsys, caplog
) -> None:
    _write(repo, "public.txt", "PRIVATE-ALPHA private-omega\n")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=failure))
    tools = RepoReadTools(repo)

    results = [
        tools.read_file("public.txt"),
        tools.list_dir("."),
        tools.glob("**/*"),
        tools.grep("PRIVATE"),
    ]

    for result in results:
        _assert_rejected(result)
        _assert_no_private_content(result.text)
    captured = capsys.readouterr()
    _assert_no_private_content(captured.out + captured.err + caplog.text)


def test_list_dir_returns_immediate_children(repo: Path) -> None:
    _write(repo, "root.txt", "root\n")
    _write(repo, "src/a.py", "code\n")
    _write(repo, "notes/new.md", "notes\n")

    result = RepoReadTools(repo).list_dir(".")

    assert result.ok is True
    assert set(result.text.splitlines()) == {"root.txt", "src/", "notes/"}


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [("*.py", {"root.py"}), ("**/*.py", {"root.py", "src/a.py", "src/nested/b.py"})],
)
def test_glob_respects_directory_boundaries(repo: Path, pattern: str, expected: set) -> None:
    for path in ("root.py", "src/a.py", "src/nested/b.py", "src/a.txt"):
        _write(repo, path, "public\n")

    result = RepoReadTools(repo).glob(pattern)

    assert result.ok is True
    assert set(result.text.splitlines()) == expected


@pytest.mark.parametrize("pattern", ["../*", "/outside/*"])
def test_glob_rejects_patterns_outside_root(repo: Path, pattern: str) -> None:
    _assert_rejected(RepoReadTools(repo).glob(pattern))


def test_grep_applies_regex_path_and_glob_filters(repo: Path) -> None:
    _write(repo, "src/a.py", "no\nneedle 42\nneedle abc\n")
    _write(repo, "src/a.txt", "needle 42\n")
    _write(repo, "other/b.py", "needle 42\n")

    result = RepoReadTools(repo).grep(r"needle \d+", path="src", glob="**/*.py")

    assert result.ok is True
    assert _grep_matches(result.text) == [("src/a.py", 2, "needle 42")]


def test_invalid_grep_regex_returns_rejection(repo: Path) -> None:
    _write(repo, "public.txt", "public\n")
    _assert_rejected(RepoReadTools(repo).grep("["))


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("read_file", {}),
        ("read_file", {"path": 7}),
        ("read_file", {"path": "public.txt", "start_line": 0}),
        ("read_file", {"path": "public.txt", "start_line": 3, "end_line": 2}),
        ("read_file", {"path": "public.txt", "unknown": True}),
        ("write_file", {"path": "public.txt", "text": "changed"}),
    ],
)
def test_execute_rejects_invalid_tool_calls(repo: Path, name: str, arguments: dict) -> None:
    target = _write(repo, "public.txt", "public\n")

    result = RepoReadTools(repo).execute(name, arguments)

    _assert_rejected(result)
    assert target.read_text(encoding="utf-8") == "public\n"


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("read_file", {"path": "public.txt"}, "1\tneedle"),
        ("list_dir", {"path": "."}, "public.txt"),
        ("glob", {"pattern": "*.txt"}, "public.txt"),
        ("grep", {"pattern": "needle"}, "public.txt:1:needle"),
    ],
)
def test_execute_dispatches_read_only_tools(
    repo: Path, name: str, arguments: dict, expected: str
) -> None:
    _write(repo, "public.txt", "needle\n")

    result = RepoReadTools(repo).execute(name, arguments)

    assert result.ok is True
    assert result.text.splitlines() == [expected]


def test_exact_line_limit_does_not_truncate(repo: Path) -> None:
    _write(repo, "f.txt", "one\ntwo\nthree\nfour\nfive\n")

    result = RepoReadTools(repo, limits=ReadLimits(max_lines=5)).read_file("f.txt")

    assert result.ok is True
    assert _numbered_lines(result.text) == list(
        enumerate(["one", "two", "three", "four", "five"], 1)
    )
    assert "truncated" not in result.text.casefold()


def test_line_limit_truncates_and_resume_returns_next_line(repo: Path) -> None:
    _write(repo, "f.txt", "one\ntwo\nthree\nfour\nfive\nsix\n")
    tools = RepoReadTools(repo, limits=ReadLimits(max_lines=5))

    result = tools.read_file("f.txt")

    assert result.ok is True
    assert [number for number, _ in _numbered_lines(result.text)] == [1, 2, 3, 4, 5]
    assert "truncated" in result.text.casefold()
    assert "start_line=6" in result.text
    resumed = tools.read_file("f.txt", start_line=6)
    assert resumed.ok is True
    assert _numbered_lines(resumed.text) == [(6, "six")]


def test_utf8_byte_limit_truncates_before_line_limit(repo: Path) -> None:
    _write(repo, "f.txt", "日本語" * 10 + "\n" + "続き" * 10 + "\n")
    ledger = ReadLedger()
    tools = RepoReadTools(repo, limits=ReadLimits(max_lines=100, max_bytes=120), ledger=ledger)

    result = tools.read_file("f.txt")

    assert result.ok is True
    assert _numbered_lines(result.text) == [(1, "日本語" * 10)]
    assert (
        len(
            "\n".join(line for line in result.text.splitlines() if re.match(r"\d+\t", line)).encode(
                "utf-8"
            )
        )
        <= 120
    )
    assert "truncated" in result.text.casefold()
    assert "start_line=2" in result.text
    assert ledger.check_citation("f.txt:1-1", exists=True, total_lines=2).verified is True
    assert ledger.check_citation("f.txt:2-2", exists=True, total_lines=2).verified is False


def test_default_line_limit_is_enforced(repo: Path) -> None:
    _write(repo, "f.txt", "x\n" * 2001)

    result = RepoReadTools(repo).read_file("f.txt")

    assert result.ok is True
    assert [number for number, _ in _numbered_lines(result.text)] == list(range(1, 2001))
    assert "start_line=2001" in result.text


def test_default_byte_limit_is_enforced(repo: Path) -> None:
    _write(repo, "f.txt", ("あ" * 1000 + "\n") * 100)

    result = RepoReadTools(repo).read_file("f.txt")

    assert result.ok is True
    returned = _numbered_lines(result.text)
    assert 0 < len(returned) < 100
    assert sum(len((text + "\n").encode("utf-8")) for _, text in returned) <= 200 * 1024
    assert "truncated" in result.text.casefold()
    assert f"start_line={returned[-1][0] + 1}" in result.text


@pytest.mark.parametrize("limit", [2, 200])
def test_grep_match_limit_is_enforced_and_explained(repo: Path, limit: int) -> None:
    _write(repo, "f.txt", "needle\n" * (limit + 1))
    tools = (
        RepoReadTools(repo)
        if limit == 200
        else RepoReadTools(repo, limits=ReadLimits(max_grep_matches=limit))
    )

    result = tools.grep("needle")

    assert result.ok is True
    assert _grep_matches(result.text) == [
        ("f.txt", number, "needle") for number in range(1, limit + 1)
    ]
    assert "truncated" in result.text.casefold()
    assert "path" in result.text or "glob" in result.text


@pytest.mark.parametrize("name", ["list_dir", "glob"])
def test_directory_and_glob_entry_limits_are_enforced(repo: Path, name: str) -> None:
    for number in range(3):
        _write(repo, f"f{number}.txt", "public\n")
    tools = RepoReadTools(repo, limits=ReadLimits(max_entries=2))

    result = tools.execute(name, {"path": "."} if name == "list_dir" else {"pattern": "*.txt"})

    assert result.ok is True
    entries = [line for line in result.text.splitlines() if re.fullmatch(r"f\d+\.txt", line)]
    assert len(entries) == 2
    assert len(set(entries)) == 2
    assert "truncated" in result.text.casefold()


@pytest.mark.parametrize("content", [b"PRIVATE-ALPHA\x00private-omega\n", b"\xff\xfe\x80"])
def test_binary_content_is_not_returned_by_read_or_grep(repo: Path, content: bytes) -> None:
    (repo / "data.bin").write_bytes(content)
    _write(repo, "public.txt", "PRIVATE public\n")
    tools = RepoReadTools(repo)

    read = tools.read_file("data.bin")
    found = tools.grep("PRIVATE")

    _assert_rejected(read)
    _assert_no_private_content(read.text)
    assert found.ok is True
    assert _grep_matches(found.text) == [("public.txt", 1, "PRIVATE public")]


def test_read_ledger_verifies_only_returned_existing_in_bounds_ranges(repo: Path) -> None:
    _write(repo, "src/a.py", "x\n" * 200)
    ledger = ReadLedger()
    tools = RepoReadTools(repo, ledger=ledger)
    assert tools.read_file("src/a.py", end_line=50).ok is True

    verified = tools.check_citation("src/a.py:10-20")
    unread = tools.check_citation("src/a.py:120-130")
    crossing = tools.check_citation("src/a.py:45-60")
    missing = tools.check_citation("src/missing.py:1-5")
    out_of_bounds = tools.check_citation("src/a.py:190-210")

    assert verified.exists is True
    assert verified.in_bounds is True
    assert verified.within_read is True
    assert verified.verified is True
    for check in (unread, crossing):
        assert check.exists is True
        assert check.in_bounds is True
        assert check.within_read is False
        assert check.verified is False
    assert missing.exists is False
    assert missing.verified is False
    assert out_of_bounds.in_bounds is False
    assert out_of_bounds.verified is False


@pytest.mark.parametrize("change", ["delete", "shorten", "ignore", "external_link", "binary"])
def test_citation_checks_current_file_after_read(repo: Path, change: str) -> None:
    target = _write(repo, "f.txt", "public\n" * 5)
    tools = RepoReadTools(repo)
    assert tools.read_file("f.txt").ok is True
    assert tools.check_citation("f.txt:3-5").verified is True

    if change == "delete":
        target.unlink()
    elif change == "shorten":
        target.write_text("public\n", encoding="utf-8")
    elif change == "ignore":
        _write(repo, ".gitignore", "f.txt\n")
    elif change == "external_link":
        outside = _write(repo.parent, "outside.txt", "PRIVATE-ALPHA private-omega\n")
        target.unlink()
        target.symlink_to(outside)
    else:
        target.write_bytes(b"PRIVATE-ALPHA\x00private-omega\n")

    check = tools.check_citation("f.txt:3-5")

    assert check.verified is False
    assert check.in_bounds is False
    assert check.exists is (change == "shorten")
    if change == "shorten":
        assert tools.check_citation("f.txt:1-1").verified is True


def test_citation_validation_does_not_record_unreturned_content(repo: Path) -> None:
    _write(repo, "src/a.py", "public\n" * 5)
    tools = RepoReadTools(repo)

    first = tools.check_citation("src/a.py:1-5")
    repeated = tools.check_citation("src/a.py:1-5")

    for check in (first, repeated):
        assert check.exists is True
        assert check.in_bounds is True
        assert check.within_read is False
        assert check.verified is False


def test_citation_uses_canonical_path_for_read_evidence(repo: Path) -> None:
    _write(repo, "src/a.py", "public\n" * 5)
    (repo / "alias.py").symlink_to("src/a.py")
    tools = RepoReadTools(repo)
    assert tools.read_file("src/a.py", end_line=2).ok is True

    assert tools.check_citation("alias.py:1-2").verified is True
    assert tools.check_citation("alias.py:3-3").verified is False


@pytest.mark.parametrize("path", [".env.local", "build/out.txt", "profile.db"])
def test_citation_rejects_denied_files_even_with_recorded_ranges(repo: Path, path: str) -> None:
    _write(repo, ".gitignore", "build/\n")
    _write(repo, path, "PRIVATE-ALPHA private-omega\n")
    ledger = ReadLedger()
    ledger.record(path, 1, 1, 1)
    tools = RepoReadTools(repo, ledger=ledger, extra_deny_patterns=("*.db",))

    check = tools.check_citation(f"{path}:1-1")

    assert check.exists is False
    assert check.verified is False


@pytest.mark.parametrize("citation", ["bad", "f.txt:0-1", "../outside.txt:1-1"])
def test_invalid_citation_is_rejected_at_file_boundary(repo: Path, citation: str) -> None:
    tools = RepoReadTools(repo)

    check = tools.check_citation(citation)

    assert check.exists is False
    assert check.verified is False


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(),
        subprocess.CalledProcessError(1, "git"),
        subprocess.TimeoutExpired("git", 10),
    ],
)
def test_citation_fails_closed_when_git_fails(repo: Path, monkeypatch, failure: Exception) -> None:
    _write(repo, "f.txt", "public\n")
    tools = RepoReadTools(repo)
    assert tools.read_file("f.txt").ok is True
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=failure))

    check = tools.check_citation("f.txt:1-1")

    assert check.exists is False
    assert check.verified is False


def test_truncated_read_records_only_returned_lines(repo: Path) -> None:
    _write(repo, "f.txt", "x\n" * 6)
    ledger = ReadLedger()
    tools = RepoReadTools(repo, limits=ReadLimits(max_lines=5), ledger=ledger)

    result = tools.read_file("f.txt", end_line=6)

    assert result.ok is True
    assert ledger.check_citation("f.txt:1-5", exists=True, total_lines=6).verified is True
    assert ledger.check_citation("f.txt:6-6", exists=True, total_lines=6).verified is False


def test_grep_records_only_returned_matching_lines(repo: Path) -> None:
    _write(repo, "f.txt", "needle\nnot read\nneedle\nneedle\n")
    ledger = ReadLedger()
    tools = RepoReadTools(repo, limits=ReadLimits(max_grep_matches=2), ledger=ledger)

    result = tools.grep("needle")

    assert result.ok is True
    assert _grep_matches(result.text) == [("f.txt", 1, "needle"), ("f.txt", 3, "needle")]
    for citation in ("f.txt:1-1", "f.txt:3-3"):
        assert ledger.check_citation(citation, exists=True, total_lines=4).verified is True
    for citation in ("f.txt:2-2", "f.txt:4-4", "f.txt:1-3"):
        assert ledger.check_citation(citation, exists=True, total_lines=4).verified is False


def test_rejected_reads_and_path_discovery_do_not_record_content(repo: Path) -> None:
    _write(repo, ".env.local", "PRIVATE-ALPHA private-omega\n")
    _write(repo, "public.txt", "public\n")
    (repo / "data.bin").write_bytes(b"\x00binary\n")
    ledger = ReadLedger()
    tools = RepoReadTools(repo, ledger=ledger)

    _assert_rejected(tools.read_file(".env.local"))
    _assert_rejected(tools.read_file("data.bin"))
    assert tools.list_dir(".").ok is True
    assert tools.glob("*.txt").ok is True

    for path in (".env.local", "data.bin", "public.txt"):
        assert ledger.check_citation(f"{path}:1-1", exists=True, total_lines=1).verified is False


def test_ledger_is_isolated_between_sessions(repo: Path) -> None:
    _write(repo, "f.txt", "public\n")
    first_ledger = ReadLedger()
    second_ledger = ReadLedger()
    first = RepoReadTools(repo, ledger=first_ledger)

    assert first.read_file("f.txt").ok is True

    assert first_ledger.check_citation("f.txt:1-1", exists=True, total_lines=1).verified is True
    assert second_ledger.check_citation("f.txt:1-1", exists=True, total_lines=1).verified is False


def test_ledger_accumulates_reads_without_filling_unread_gaps() -> None:
    ledger = ReadLedger()
    ledger.record("f.txt", 1, 2, 6)
    ledger.record("f.txt", 4, 5, 6)

    assert ledger.check_citation("f.txt:1-5", exists=True, total_lines=6).verified is False
    ledger.record("f.txt", 3, 3, 6)
    assert ledger.check_citation("f.txt:1-5", exists=True, total_lines=6).verified is True


def test_tool_definitions_are_provider_neutral_json_schemas() -> None:
    definitions = json.loads(json.dumps(TOOL_DEFINITIONS))
    by_name = {definition["name"]: definition for definition in definitions}
    required = {
        "read_file": {"path"},
        "list_dir": {"path"},
        "glob": {"pattern"},
        "grep": {"pattern"},
    }

    assert len(definitions) == 4
    assert set(by_name) == set(required)
    for name, fields in required.items():
        definition = by_name[name]
        assert definition["description"].strip()
        schema = definition["parameters"]
        assert schema["type"] == "object"
        assert set(schema["required"]) == fields
        for field in fields:
            assert schema["properties"][field]["type"] == "string"
    read_properties = by_name["read_file"]["parameters"]["properties"]
    assert read_properties["start_line"]["type"] == "integer"
    assert read_properties["end_line"]["type"] == "integer"
    grep_properties = by_name["grep"]["parameters"]["properties"]
    assert grep_properties["path"]["type"] == "string"
    assert grep_properties["glob"]["type"] == "string"


def test_tools_work_without_site_packages(repo: Path) -> None:
    _write(repo, "public.txt", "public\n")
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from lib.llm.tools.repo_read import RepoReadTools
result = RepoReadTools(sys.argv[2]).read_file("public.txt")
assert result.ok is True
assert result.text.splitlines() == ["1\\tpublic"]
"""

    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(scripts), str(repo)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_tracked_gitignored_file_is_rejected(repo: Path) -> None:
    _write(repo, "build/out.txt", "PRIVATE-ALPHA private-omega\n")
    _track(repo, "build/out.txt")
    _write(repo, ".gitignore", "build/\n")
    tools = RepoReadTools(repo)

    _assert_rejected(tools.read_file("build/out.txt"))
    assert _grep_matches(tools.grep("PRIVATE").text) == []
    assert "build/out.txt" not in tools.glob("**/*").text.splitlines()


@pytest.mark.parametrize(
    ("max_bytes", "oversized_content"),
    [(32, "あ" * 100), (32, "a" * 31), (32, "あ" * 10), (204800, "a" * 204799)],
    ids=["raw-over", "rendered-over-at-limit", "rendered-over-utf8", "rendered-over-default"],
)
@pytest.mark.parametrize(
    ("prefix", "suffix", "start_line", "end_line"),
    [
        ("", "tail\n", 1, None),
        ("ok\n", "tail\n", 1, None),
        ("unread\n", "tail\n", 2, None),
        ("ok\n", "", 1, None),
        ("ok\n", "tail\n", 1, 2),
        ("", "", 1, None),
    ],
    ids=["first", "middle", "requested-start", "last", "end-line", "only-line"],
)
def test_oversized_line_continuation_advances_without_recording_skipped_line(
    repo: Path,
    prefix: str,
    suffix: str,
    start_line: int,
    end_line: int | None,
    max_bytes: int,
    oversized_content: str,
) -> None:
    content = prefix + oversized_content + "\n" + suffix
    _write(repo, "f.txt", content)
    oversized_line = len(prefix.splitlines()) + 1
    total = len(content.splitlines())
    ledger = ReadLedger()
    tools = RepoReadTools(repo, limits=ReadLimits(max_bytes=max_bytes), ledger=ledger)
    arguments = {"path": "f.txt", "start_line": start_line}
    if end_line is not None:
        arguments["end_line"] = end_line

    result = tools.execute("read_file", arguments)

    assert result.ok is True
    returned = [
        (number, line) for number, line in enumerate(prefix.splitlines(), 1) if number >= start_line
    ]
    assert _numbered_lines(result.text) == returned
    assert "truncated" in result.text.casefold()
    assert f"{oversized_line} 行目" in result.text
    assert "バイト上限" in result.text
    assert "max_bytes" not in result.text
    returned_numbers = {number for number, _ in returned}
    for number in range(1, total + 1):
        check = ledger.check_citation(f"f.txt:{number}-{number}", exists=True, total_lines=total)
        assert check.verified is (number in returned_numbers)
    continuation = re.search(r"start_line=(\d+)", result.text)
    if oversized_line < total and (end_line is None or oversized_line < end_line):
        assert continuation is not None
        assert int(continuation[1]) == oversized_line + 1
        arguments["start_line"] = int(continuation[1])

        resumed = tools.execute("read_file", arguments)

        assert resumed.ok is True
        assert _numbered_lines(resumed.text) == [(oversized_line + 1, "tail")]
        assert tools.check_citation(f"f.txt:{oversized_line + 1}-{oversized_line + 1}").verified
        assert tools.check_citation(f"f.txt:{start_line}-{total}").verified is False
    else:
        assert continuation is None
    assert tools.check_citation(f"f.txt:{oversized_line}-{oversized_line}").verified is False


@pytest.mark.parametrize("max_bytes", [32, 204800])
@pytest.mark.parametrize("prefix", ["", "ok\n"], ids=["first", "after-output"])
def test_numbered_line_at_byte_limit_can_be_read_without_skipping(
    repo: Path, max_bytes: int, prefix: str
) -> None:
    line = "a" * (max_bytes - 3)
    number = len(prefix.splitlines()) + 1
    _write(repo, "f.txt", prefix + line + "\ntail\n")
    tools = RepoReadTools(repo, limits=ReadLimits(max_bytes=max_bytes))

    result = tools.read_file("f.txt")

    assert result.ok is True
    if prefix:
        assert _numbered_lines(result.text) == [(1, "ok")]
        assert f"start_line={number}" in result.text
        assert tools.check_citation(f"f.txt:{number}-{number}").verified is False
        result = tools.read_file("f.txt", start_line=number)
        assert result.ok is True
    assert _numbered_lines(result.text) == [(number, line)]
    assert tools.check_citation(f"f.txt:{number}-{number}").verified is True
    assert f"start_line={number + 1}" in result.text
    resumed = tools.read_file("f.txt", start_line=number + 1)
    assert resumed.ok is True
    assert _numbered_lines(resumed.text) == [(number + 1, "tail")]


@pytest.mark.parametrize("component", ["a.py", "src"])
def test_symlink_replacement_between_resolution_and_open_is_rejected(
    repo: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    _write(repo, "src/a.py", "public\n")
    outside = _write(repo.parent, "outside/a.py", "PRIVATE-ALPHA private-omega\n")
    original_open = os.open
    replaced = []

    def swap_before_open(path, flags, *args, **kwargs):
        if path == component and not replaced:
            target = repo / "src" if component == "src" else repo / "src/a.py"
            if component == "src":
                target.rename(repo / "old-src")
            else:
                target.unlink()
            os.symlink(outside.parent if component == "src" else outside, target)
            replaced.append(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    ledger = ReadLedger()

    result = RepoReadTools(repo, ledger=ledger).read_file("src/a.py")

    assert len(replaced) == 1
    _assert_rejected(result)
    _assert_no_private_content(result.text)
    assert ledger.check_citation("src/a.py:1-1", exists=True, total_lines=1).verified is False


def test_read_file_deleted_before_open_returns_failure_without_content(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys, caplog
) -> None:
    target = _write(repo, "a.py", "PRIVATE-ALPHA private-omega\n")
    _track(repo, target.name)
    removed = _remove_before_open(monkeypatch, target)
    tools = RepoReadTools(repo)

    result = tools.read_file(target.name)

    assert removed == [target]
    _assert_rejected(result)
    assert result.text.startswith("not_found:")
    assert tools.ledger.files() == ()
    captured = capsys.readouterr()
    for text in (result.text, captured.out, captured.err, caplog.text):
        _assert_no_private_content(text)
        assert str(repo) not in text
        assert "No such file" not in text


@pytest.mark.parametrize(
    "error_type", [PermissionError, IsADirectoryError, NotADirectoryError, OSError]
)
def test_read_file_open_errors_return_safe_reason(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys, caplog, error_type
) -> None:
    _write(repo, "a.py", "PRIVATE-ALPHA private-omega\n")
    original_open = os.open

    def fail(path, flags, *args, **kwargs):
        if path == "a.py" and kwargs.get("dir_fd") is not None:
            raise error_type("private-alpha private-omega")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail)
    tools = RepoReadTools(repo)

    result = tools.read_file("a.py")

    _assert_rejected(result)
    reasons = {
        PermissionError: "permission_denied",
        IsADirectoryError: "not_regular_file",
        NotADirectoryError: "not_regular_file",
        OSError: "unreadable",
    }
    assert result.text.startswith(reasons[error_type] + ":")
    assert tools.ledger.files() == ()
    captured = capsys.readouterr()
    for text in (result.text, captured.out, captured.err, caplog.text):
        _assert_no_private_content(text)


def test_grep_skips_file_deleted_before_open_and_records_remaining_match(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write(repo, "a.py", "needle PRIVATE-ALPHA private-omega\n")
    _write(repo, "b.py", "needle public\n")
    _track(repo, target.name)
    removed = _remove_before_open(monkeypatch, target)
    tools = RepoReadTools(repo)

    result = tools.grep("needle")

    assert removed == [target]
    assert result.ok is True
    assert _grep_matches(result.text) == [("b.py", 1, "needle public")]
    assert tools.ledger.files() == ("b.py",)


@pytest.mark.parametrize("name", ["list_dir", "glob"])
def test_enumeration_skips_file_deleted_after_git_listing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    target = _write(repo, "a.py", "unused\n")
    _write(repo, "b.py", "public\n")
    _track(repo, target.name)
    original_run = subprocess.run
    removed = []

    def list_then_remove(argv, **kwargs):
        result = original_run(argv, **kwargs)
        if "ls-files" in argv and "--ignored" not in argv and not removed:
            assert b"a.py\0" in result.stdout
            target.unlink()
            removed.append(target)
        return result

    monkeypatch.setattr(subprocess, "run", list_then_remove)
    tools = RepoReadTools(repo)
    arguments = {"path": "."} if name == "list_dir" else {"pattern": "*.py"}

    result = tools.execute(name, arguments)

    assert removed == [target]
    assert result.ok is True
    assert result.text.splitlines() == ["b.py"]


def test_binary_detected_after_output_limit_discards_pending_content(repo: Path) -> None:
    _write(repo, "f.txt", "needle\n" * 10 + "\0binary\n")
    ledger = ReadLedger()
    tools = RepoReadTools(repo, limits=ReadLimits(max_lines=1, max_grep_matches=1), ledger=ledger)

    _assert_rejected(tools.read_file("f.txt"))
    found = tools.grep("needle")

    assert found.ok is True
    assert _grep_matches(found.text) == []
    assert ledger.check_citation("f.txt:1-1", exists=True, total_lines=11).verified is False


@pytest.mark.parametrize(
    "citation", ["", "f.txt:0-1", "f.txt:3-2", "../f.txt:1-1", "/f.txt:1-1", "f.txt:one-two"]
)
def test_invalid_citation_is_not_verified(citation: str) -> None:
    ledger = ReadLedger()
    ledger.record("f.txt", 1, 3, 3)

    assert parse_citation(citation) is None
    assert ledger.check_citation(citation, exists=True, total_lines=3).verified is False


def test_citation_normalization_and_recorded_total_lines() -> None:
    ledger = ReadLedger()
    ledger.record("src/../f.txt", 1, 2, 3)

    assert parse_citation("./f.txt:1-2") == ("f.txt", 1, 2)
    assert ledger.check_citation("./f.txt:1-2", exists=True).verified is True
    assert ledger.check_citation("f.txt:1-4", exists=True).in_bounds is False


@pytest.mark.parametrize(
    ("path", "start", "end", "total"),
    [("../f.txt", 1, 1, 1), ("f.txt", 0, 1, 1), ("f.txt", 1, 2, 1)],
)
def test_invalid_ledger_record_is_rejected(path, start, end, total) -> None:
    with pytest.raises(ValueError):
        ReadLedger().record(path, start, end, total)


@pytest.mark.parametrize("field", ["max_lines", "max_bytes", "max_grep_matches", "max_entries"])
@pytest.mark.parametrize("value", [0, -1, True])
def test_invalid_read_limits_are_rejected(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        ReadLimits(**{field: value})


def test_empty_file_does_not_create_read_evidence(repo: Path) -> None:
    _write(repo, "empty.txt", "")
    ledger = ReadLedger()

    result = RepoReadTools(repo, ledger=ledger).read_file("empty.txt")

    assert result.ok is True
    assert _numbered_lines(result.text) == []
    assert ledger.check_citation("empty.txt:1-1", exists=True, total_lines=0).verified is False
