import io
import json
import sys

import pytest

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


def test_should_suggest_antigravity_true_when_research_indicator_outranks_simple_lookup() -> None:
    """明確な比較・移行意図は version 系の語があっても提案する。"""
    should_suggest, _ = suggest_antigravity_research.should_suggest_antigravity(
        "compare api versioning migration patterns"
    )
    assert should_suggest is True


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
        "tool_input": {"query": "compare FastAPI dependency injection alternatives"},
    }
    stdout, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


@pytest.fixture
def enabled_project(tmp_path, monkeypatch):
    """外部 CLI を起動せず提案を有効化する、独立したテスト用プロジェクト。"""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(
        suggest_antigravity_research,
        "load_cli_tools_config",
        lambda *_: {"antigravity": {"enabled": True}},
    )
    return project_dir


@pytest.mark.parametrize(
    "query",
    [
        "documentation",
        "library",
        "guide",
        "example",
        "best practice",
        "ordinary question " * 20,
        "how to compare two strings in python",
        "how to compare two arrays in numpy",
        "django migration error traceback",
        "移行時の例外を調べたい",
    ],
)
def test_main_websearch_generic_words_or_length_do_not_suggest(enabled_project, query) -> None:
    """比較・選定意図のない WebSearch では提案も状態保存もしない。"""
    stdout, exit_code = _run_main_with_stdin(
        {
            "tool_name": "WebSearch",
            "tool_input": {"query": query},
            "cwd": str(enabled_project),
            "session_id": "generic-query",
        }
    )
    assert exit_code == 0
    assert stdout == ""
    assert not (enabled_project / ".claude" / "state" / "suggestions").exists()


@pytest.mark.parametrize("tool_name", ["WebSearch", "WebFetch"])
@pytest.mark.parametrize(
    "query", ["asyncio vs. trio", "移行エラーを踏まえて Django と Flask を比較したい"]
)
def test_main_explicit_comparison_suggests_across_web_tools(
    enabled_project, tool_name, query
) -> None:
    """比較意図は両 Web ツールで提案し、移行トラブル語だけを優先して抑制しない。"""
    stdout, exit_code = _run_main_with_stdin(
        {
            "tool_name": tool_name,
            "tool_input": {"query": query, "prompt": query},
            "cwd": str(enabled_project),
        }
    )
    assert exit_code == 0
    assert (
        "[Antigravity Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    )


@pytest.mark.parametrize("prompt", ["", "read documentation", "get info"])
def test_main_webfetch_url_does_not_supply_research_intent(enabled_project, prompt) -> None:
    """URL に比較・移行語があっても prompt の調査意図なしでは提案しない。"""
    stdout, exit_code = _run_main_with_stdin(
        {
            "tool_name": "WebFetch",
            "tool_input": {
                "url": "https://docs.example.com/comparison/migration",
                "prompt": prompt,
            },
            "cwd": str(enabled_project),
        }
    )
    assert exit_code == 0
    assert stdout == ""


@pytest.mark.parametrize(
    "agent_context",
    [{"agent_id": "child"}, {"agent_id": "child", "agent_type": "researcher"}],
)
@pytest.mark.parametrize("tool_name", ["WebSearch", "WebFetch"])
def test_main_subagent_does_not_suggest_or_persist_state(
    enabled_project, agent_context, tool_name
) -> None:
    """両ツールとも agent_type の有無によらず agent_id で再帰提案を抑制する。"""
    stdout, exit_code = _run_main_with_stdin(
        {
            "tool_name": tool_name,
            "tool_input": {
                "query": "compare queues and streams",
                "prompt": "compare queues and streams",
            },
            "cwd": str(enabled_project),
            "session_id": "subagent-session",
            **agent_context,
        }
    )
    assert exit_code == 0
    assert stdout == ""
    assert not (enabled_project / ".claude" / "state" / "suggestions").exists()


@pytest.mark.parametrize("tool_name", ["WebSearch", "WebFetch"])
def test_main_agent_type_only_suggests_and_deduplicates_as_main(enabled_project, tool_name) -> None:
    """--agent のメインセッションでは調査を提案し、同一質問の重複だけを抑制する。"""
    query = "compare queues and streams"
    tool_input = (
        {"query": query}
        if tool_name == "WebSearch"
        else {"url": "https://docs.example.com/guide", "prompt": query}
    )
    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(enabled_project),
        "session_id": "main-agent-session",
        "agent_type": "researcher",
    }

    stdout, exit_code = _run_main_with_stdin(payload)
    assert exit_code == 0
    assert (
        "[Antigravity Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    )
    assert _run_main_with_stdin(payload) == ("", 0)


def test_main_tool_input_subagent_type_does_not_suppress_suggestion(enabled_project) -> None:
    """呼び出すエージェントの種別と、現在の実行コンテキストを混同しない。"""
    stdout, exit_code = _run_main_with_stdin(
        {
            "tool_name": "WebSearch",
            "tool_input": {"query": "compare queues and streams", "subagent_type": "researcher"},
            "cwd": str(enabled_project),
        }
    )
    assert exit_code == 0
    assert (
        "[Antigravity Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    )


def test_main_deduplicates_casefolded_whitespace_normalized_query_across_web_tools(
    enabled_project,
) -> None:
    """casefold と空白正規化後に同じ query は URL・ツールが違っても一度だけ提案する。"""
    requests = [
        ("WebSearch", {"query": "  COMPARE Straße AND REDIS  "}),
        ("WebSearch", {"query": "compare strasse\n and\tredis"}),
        ("WebFetch", {"url": "https://one.test/guide", "prompt": "compare strasse and redis"}),
        ("WebFetch", {"url": "https://two.test/guide", "prompt": "COMPARE STRASSE AND REDIS"}),
    ]
    for index, (tool_name, tool_input) in enumerate(requests):
        stdout, exit_code = _run_main_with_stdin(
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "cwd": str(enabled_project),
                "session_id": "normalized-query",
            }
        )
        assert exit_code == 0
        if index == 0:
            assert (
                "[Antigravity Suggestion]"
                in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
            )
        else:
            assert stdout == ""


def test_main_different_queries_and_sessions_can_each_suggest(enabled_project) -> None:
    """同一セッションの別 query と、別セッションの同じ query は独立する。"""
    for session_id, query in [
        ("session-one", "compare queues and streams"),
        ("session-one", "compare MySQL and PostgreSQL"),
        ("session-two", "compare queues and streams"),
    ]:
        stdout, exit_code = _run_main_with_stdin(
            {
                "tool_name": "WebSearch",
                "tool_input": {"query": query},
                "cwd": str(enabled_project),
                "session_id": session_id,
            }
        )
        assert exit_code == 0
        assert (
            "[Antigravity Suggestion]"
            in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        )


def test_main_without_session_repeats_without_saving_state(enabled_project) -> None:
    """session_id がない場合は毎回提案し、永続状態は作らない。"""
    for _ in range(2):
        stdout, exit_code = _run_main_with_stdin(
            {
                "tool_name": "WebSearch",
                "tool_input": {"query": "compare queues and streams"},
                "cwd": str(enabled_project),
            }
        )
        assert exit_code == 0
        assert (
            "[Antigravity Suggestion]"
            in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        )
    assert not (enabled_project / ".claude" / "state" / "suggestions").exists()


def test_main_state_failure_suppresses_suggestion_without_blocking(enabled_project) -> None:
    """状態保存できなくても提案だけを抑制しツールを止めない。"""
    (enabled_project / ".claude").mkdir()
    (enabled_project / ".claude" / "state").write_text("not a directory", encoding="utf-8")
    stdout, exit_code = _run_main_with_stdin(
        {
            "tool_name": "WebSearch",
            "tool_input": {"query": "compare queues and streams"},
            "cwd": str(enabled_project),
            "session_id": "state-failure",
        }
    )
    assert exit_code == 0
    assert stdout == ""


def test_main_suggestion_is_optional_routing_first_and_nonblocking(enabled_project) -> None:
    """通知は routing 優先の任意提案であり permissionDecision や block を返さない。"""
    stdout, exit_code = _run_main_with_stdin(
        {
            "tool_name": "WebSearch",
            "tool_input": {"query": "API versioning migration patterns"},
            "cwd": str(enabled_project),
        }
    )
    assert exit_code == 0
    output = json.loads(stdout)
    assert set(output) == {"hookSpecificOutput"}
    hook_output = output["hookSpecificOutput"]
    assert set(hook_output) == {"hookEventName", "additionalContext"}
    assert hook_output["hookEventName"] == "PreToolUse"
    context = hook_output["additionalContext"].casefold()
    assert "optional" in context or "任意" in context
    assert "routing" in context
    assert any(word in context for word in ("priority", "precedence", "優先"))


@pytest.mark.parametrize(
    "shadow_config",
    [
        "antigravity:\n  enabled: false\n",
        "antigravity: [\n",
        "antigravity:\n  enabled: true\n  model: shadow-model\n",
    ],
    ids=["disabled", "malformed", "different-model"],
)
@pytest.mark.parametrize("local_disabled", [False, True])
def test_main_project_root_config_and_local_override_outrank_cwd_shadow(
    tmp_path, monkeypatch, shadow_config, local_disabled
) -> None:
    """root の設定・local 無効化を優先し、通知 marker も root だけに保存する。"""
    project_dir = tmp_path / "project"
    subdir = project_dir / "src"
    for directory, content in [
        (project_dir, "antigravity:\n  enabled: true\n  model: root-model\n"),
        (subdir, shadow_config),
    ]:
        config_path = directory / ".claude/config/agent-routing/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(content, encoding="utf-8")
    if local_disabled:
        local_path = project_dir / ".claude/config/agent-routing/cli-tools.local.yaml"
        local_path.write_text("antigravity:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    payload = {
        "tool_name": "WebSearch",
        "tool_input": {"query": "compare asyncio and trio"},
        "cwd": str(subdir),
        "session_id": "root-config",
    }

    stdout, exit_code = _run_main_with_stdin(payload)
    assert exit_code == 0
    state_dir = project_dir / ".claude/state/suggestions"
    if local_disabled:
        assert stdout == ""
        assert not state_dir.exists()
    else:
        context = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "[Antigravity Suggestion]" in context
        assert "root-model" in context
        assert "shadow-model" not in context
        assert len([path for path in state_dir.rglob("*") if path.is_file()]) == 1
        assert _run_main_with_stdin(payload) == ("", 0)
    assert not (subdir / ".claude/state").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_main_without_project_env_uses_cwd_config(tmp_path, monkeypatch, enabled) -> None:
    """環境変数がなければ親ではなく payload の cwd を設定・marker の基準にする。"""
    project_dir = tmp_path / "project"
    subdir = project_dir / "src"
    for directory, is_enabled, model in [
        (project_dir, not enabled, "parent-model"),
        (subdir, enabled, "cwd-model"),
    ]:
        config_path = directory / ".claude/config/agent-routing/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            f"antigravity:\n  enabled: {str(is_enabled).lower()}\n  model: {model}\n",
            encoding="utf-8",
        )
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    stdout, exit_code = _run_main_with_stdin(
        {
            "tool_name": "WebSearch",
            "tool_input": {"query": "compare asyncio and trio"},
            "cwd": str(subdir),
            "session_id": "cwd-config",
        }
    )
    assert exit_code == 0
    if enabled:
        context = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "[Antigravity Suggestion]" in context
        assert "cwd-model" in context
        assert "parent-model" not in context
        assert len([path for path in (subdir / ".claude/state").rglob("*") if path.is_file()]) == 1
    else:
        assert stdout == ""
        assert not (subdir / ".claude/state").exists()
    assert not (project_dir / ".claude/state").exists()
