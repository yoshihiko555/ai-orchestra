import io
import json
import sys

from tests.module_loader import REPO_ROOT, load_module

suggest_antigravity_research = load_module(
    "suggest_antigravity_research",
    "packages/antigravity-suggestions/hooks/suggest-antigravity-research.py",
)


def test_imports_without_agent_routing_hooks_on_path() -> None:
    """agent-routing の hooks ディレクトリが sys.path に無くても import できる。

    manifest.json の depends は ["core"] のみであり、is_cli_enabled は
    hook_common（core）から直接 import するため agent-routing への暗黙依存が
    無いことを保証する回帰テスト。
    """
    routing_hooks_dir = str(REPO_ROOT / "packages" / "agent-routing" / "hooks")
    removed_paths = [p for p in sys.path if p == routing_hooks_dir]
    for path in removed_paths:
        sys.path.remove(path)
    sys.modules.pop("route_config", None)
    try:
        assert routing_hooks_dir not in sys.path
        module = load_module(
            "suggest_antigravity_research_no_routing_dep",
            "packages/antigravity-suggestions/hooks/suggest-antigravity-research.py",
        )
        assert hasattr(module, "is_cli_enabled")
    finally:
        sys.path.extend(removed_paths)


def test_should_suggest_antigravity_false_for_simple_lookup() -> None:
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "check latest version and release notes"
    )
    assert not should_suggest
    assert reason == ""


def test_should_suggest_antigravity_true_for_research_indicator_in_query() -> None:
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "fastapi best practice for dependency injection"
    )
    assert should_suggest
    assert "best practice" in reason


def test_should_suggest_antigravity_true_for_research_indicator_in_url() -> None:
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "quick check", "https://docs.acme.test/tutorial/python"
    )
    assert should_suggest
    assert "tutorial" in reason


def test_should_suggest_antigravity_true_for_long_query() -> None:
    query = "a" * 101
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(query)
    assert should_suggest
    assert reason == "Complex research query detected"


def test_should_suggest_antigravity_false_when_no_condition_matches() -> None:
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "what time is it now?"
    )
    assert not should_suggest
    assert reason == ""


def test_should_suggest_antigravity_true_when_research_indicator_outranks_simple_lookup() -> None:
    """研究シグナル（migration/pattern）は "version" 系の単純確認より優先される。"""
    should_suggest, _ = suggest_antigravity_research.should_suggest_antigravity(
        "compare api versioning migration patterns"
    )
    assert should_suggest is True


def test_should_suggest_antigravity_false_for_simple_version_lookup() -> None:
    """研究シグナルを含まない単純な version 確認は引き続き抑制される。"""
    should_suggest, reason = suggest_antigravity_research.should_suggest_antigravity(
        "what is the latest version of react"
    )
    assert should_suggest is False
    assert reason == ""


# --- EV-10: matcher スコープ（WebSearch|WebFetch 限定）---


def test_manifest_matcher_is_limited_to_websearch_webfetch() -> None:
    """manifest.json の PreToolUse matcher が WebSearch|WebFetch に限定されている。"""
    manifest_path = REPO_ROOT / "packages" / "antigravity-suggestions" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    matchers = [entry["matcher"] for entry in manifest["hooks"]["PreToolUse"]]
    assert matchers == ["WebSearch|WebFetch"]


def _run_main_with_stdin(data: dict) -> tuple[str, str | int]:
    """main() を stdin モックで実行し (stdout, exit_code) を返す。"""
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = io.StringIO(json.dumps(data))
    sys.stdout = io.StringIO()

    exit_code = 0
    try:
        suggest_antigravity_research.main()
    except SystemExit as e:
        exit_code = e.code if e.code is not None else 0

    stdout = sys.stdout.getvalue()
    sys.stdin = old_stdin
    sys.stdout = old_stdout
    return stdout, exit_code


def test_main_does_not_suggest_for_non_matching_tool() -> None:
    """EV-10: WebSearch/WebFetch 以外の tool_name では、research 系キーワードを
    含む入力があっても提案が発火しない（matcher スコープ外の関数レベル検証）。

    hook 本体は tool_name が WebSearch/WebFetch のときのみ query/url を
    tool_input から取り出すため、非対象ツールでは常に空クエリとなり
    should_suggest_antigravity が False を返す。
    """
    data = {
        "tool_name": "Read",
        "tool_input": {"query": "fastapi best practice for dependency injection"},
    }
    stdout, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_suggests_for_matching_tool_webfetch() -> None:
    """比較対象: WebFetch では同等の research シグナルで提案が発火する。"""
    data = {
        "tool_name": "WebFetch",
        "tool_input": {
            "url": "https://docs.acme.test/tutorial/python",
            "prompt": "fastapi best practice for dependency injection",
        },
    }
    stdout, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    output = json.loads(stdout)
    assert "[Antigravity Suggestion]" in output["hookSpecificOutput"]["additionalContext"]
