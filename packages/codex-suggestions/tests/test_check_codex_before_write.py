import builtins
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

check_codex_before_write = load_module(
    "check_codex_before_write",
    "packages/codex-suggestions/hooks/check-codex-before-write.py",
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
            "check_codex_before_write_no_routing_dep",
            "packages/codex-suggestions/hooks/check-codex-before-write.py",
        )
        assert hasattr(module, "is_cli_enabled")
    finally:
        sys.path.extend(removed_paths)


def test_edit_reason_for_edit_does_not_claim_new_file_creation() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "tools/script.py", "class Engine(Protocol):\n    pass", tool_name="Edit"
    )
    assert should_suggest is True
    assert reason
    assert "Creating new file" not in reason


@pytest.mark.parametrize("tool_name", ["", "Edit", "Write"])
@pytest.mark.parametrize("file_path", ["tools/script.py", "src/feature.py"])
def test_large_content_does_not_suggest_for_any_write_operation(tool_name, file_path) -> None:
    """操作種別や src 配下にかかわらず長い本文だけでは提案しない。"""
    should_suggest, _ = check_codex_before_write.should_suggest_codex(
        file_path, "x" * 600, tool_name=tool_name
    )
    assert should_suggest is False


@pytest.mark.parametrize("absolute_path", [False, True])
@pytest.mark.parametrize(
    ("old_content", "content", "expected"),
    [
        (None, "class Engine(Protocol):\n    pass", True),
        ("", "class Engine(Protocol):\n    pass", True),
        ("class Engine:\n    pass", "class Engine(Protocol):\n    pass", True),
        ("class Engine(Protocol):\n    old: int", "class Engine(Protocol):\n    value: str", False),
        ("class Engine(Protocol):\n    pass", "", True),
        ("class OldEngine(Protocol):\n    pass", "class NewEngine(Protocol):\n    pass", True),
    ],
    ids=["new-file", "empty-file", "header-added", "body-only", "header-removed", "header-changed"],
)
def test_main_write_compares_disk_contract_headers_without_changing_file(
    enabled_project, absolute_path, old_content, content, expected
) -> None:
    """Write はディスクの旧本文と比較し、新規作成も上書きも hook 自体は行わない。"""
    target_file = enabled_project / "src/engine.py"
    target_file.parent.mkdir()
    if old_content is not None:
        target_file.write_text(old_content, encoding="utf-8")
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(target_file) if absolute_path else "src/engine.py",
                "content": content,
            },
            "cwd": str(enabled_project),
            "session_id": "write-disk-comparison",
        }
    )
    assert exit_code == 0
    assert stderr == ""
    if expected:
        assert "[Codex Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    else:
        assert stdout == ""
        assert not (enabled_project / ".claude/state").exists()
    if old_content is None:
        assert not target_file.exists()
    else:
        assert target_file.read_text(encoding="utf-8") == old_content


@pytest.mark.parametrize(
    "content",
    [
        "class Engine(\n    Protocol,\n):\n    pass\n",
        "class Engine(\n    ABC,\n):\n    pass\n",
        "class Engine[T](Protocol):\n    pass\n",
    ],
    ids=["multiline-protocol", "multiline-abc", "python312-type-parameters"],
)
def test_python_contract_header_addition_and_removal_suggest(content) -> None:
    """複数行や型パラメーター付きの契約宣言も追加・削除を提案する。"""
    added, add_reason = check_codex_before_write.should_suggest_codex(
        "src/engine.py", content, tool_name="Edit", old_content=""
    )
    removed, remove_reason = check_codex_before_write.should_suggest_codex(
        "src/engine.py", "", tool_name="Edit", old_content=content
    )
    assert added is True
    assert add_reason
    assert removed is True
    assert remove_reason


@pytest.mark.parametrize("base", ["Protocol", "ABC"])
@pytest.mark.parametrize("class_name", ["Engine", "Engine[T]"])
@pytest.mark.parametrize("trailing_comma", ["", ","], ids=["no-comma", "trailing-comma"])
@pytest.mark.parametrize(
    "reverse", [False, True], ids=["single-to-multiline", "multiline-to-single"]
)
def test_python_contract_header_reformatting_does_not_suggest(
    base, class_name, trailing_comma, reverse
) -> None:
    """同じ契約ヘッダの改行・インデントだけの変更は提案しない。"""
    single_line = f"class {class_name}({base}{trailing_comma}):\n    pass\n"
    multiline = f"class {class_name}(\n    {base}{trailing_comma}\n):\n    pass\n"
    old_content, content = (multiline, single_line) if reverse else (single_line, multiline)

    assert check_codex_before_write.should_suggest_codex(
        "src/engine.py", content, tool_name="Edit", old_content=old_content
    ) == (False, "")


@pytest.mark.parametrize(
    ("content", "is_contract"),
    [
        ("class Engine(make_base(" + "ABC, " * 32000 + ")):\n    pass\n", True),
        ('PAYLOAD = """\nclass Engine(' + "Protocol, " * 16000 + '\n"""\n', False),
    ],
    ids=["large-nested-base", "large-string-fixture"],
)
@pytest.mark.parametrize("operation", ["add", "remove", "unchanged"])
def test_large_python_headers_complete_with_expected_suggestion(
    content, is_contract, operation
) -> None:
    """巨大な旧・新本文でも停止せず、文字列 fixture 中の class は契約と見なさない。"""
    old_content = "" if operation == "add" else content
    new_content = "" if operation == "remove" else content
    # 同一プロセスの時間計測では、二次時間の正規表現が再発した際に打ち切れない。
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import sys
from tests.module_loader import load_module

hook = load_module(
    "check_codex_before_write_regression",
    "packages/codex-suggestions/hooks/check-codex-before-write.py",
)
payload = json.load(sys.stdin)
print(json.dumps(hook.should_suggest_codex(
    "src/engine.py", payload["content"], tool_name="Edit", old_content=payload["old_content"]
)))
""",
        ],
        input=json.dumps({"old_content": old_content, "content": new_content}),
        cwd=REPO_ROOT,
        env={**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    should_suggest, reason = json.loads(result.stdout)
    assert should_suggest is (is_contract and operation != "unchanged")
    assert bool(reason) is should_suggest


# --- main (stdin/stdout contract, EV-12/EV-14/EV-15) ---


def _run_main_with_stdin(data: dict) -> tuple[str, str, str | int]:
    """main() を stdin モックで実行し (stdout, stderr, exit_code) を返す。"""
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdin = io.StringIO(json.dumps(data))
    stdout = sys.stdout = io.StringIO()
    stderr = sys.stderr = io.StringIO()

    exit_code = 0
    try:
        check_codex_before_write.main()
    except SystemExit as e:
        exit_code = e.code if e.code is not None else 0
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return stdout.getvalue(), stderr.getvalue(), exit_code


def test_main_no_output_for_non_triggering_change(monkeypatch) -> None:
    """EV-12: 非該当時（提案条件を満たさない変更）は stdout に何も print しない。"""
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    data = {
        "tool_name": "Write",
        "tool_input": {"file_path": "tools/script.py", "content": "print('ok')"},
        "cwd": "/project",
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_edit_reason_uses_tool_name_from_stdin(monkeypatch) -> None:
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    data = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "tools/script.py",
            "old_string": "class Engine: pass",
            "new_string": "class Engine(Protocol): pass",
        },
        "cwd": "/project",
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0

    output = json.loads(stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "Creating new file" not in context


def test_main_fail_safe_on_internal_exception(monkeypatch) -> None:
    """EV-14: hook 内部で例外が発生しても stderr にエラーを出しつつ exit 0（fail-open）。"""

    def _raise(*_a: object, **_kw: object) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(check_codex_before_write, "load_package_config", _raise)
    data = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src/core/engine.py",
            "content": "class Engine(Protocol): pass",
        },
        "cwd": "/project",
    }
    stdout, stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""
    assert "Hook error:" in stderr
    assert "boom" in stderr


def test_main_no_output_when_codex_section_undefined(monkeypatch) -> None:
    """EV-15: codex セクション自体が config に未定義の場合、デフォルト無効として
    提案を出力しない（2026-07-03 人間レビュー裁定, Issue #129）。"""
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(check_codex_before_write, "load_package_config", lambda *_: {})
    data = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src/core/engine.py",
            "content": "class Engine(Protocol): pass",
        },
        "cwd": "/project",
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


# --- EV-15: package fallback config を project opt-in 扱いしない
# (Issue #129 PR #247 レビュー指摘の回帰テスト) ---


def test_main_no_output_when_only_package_fallback_config_exists(tmp_path, monkeypatch) -> None:
    """project-local な cli-tools.yaml が無く、パッケージ同梱フォールバックのみ
    存在する場合（agent-routing 未導入で codex-suggestions のみ導入した
    project を想定）は、フォールバック側の codex.enabled: true を project の
    明示 opt-in として扱わず no-op（無出力・exit 0）になる。"""
    orchestra_dir = tmp_path / "orchestra"
    project_dir = tmp_path / "project"

    fallback_path = orchestra_dir / "packages" / "agent-routing" / "config" / "cli-tools.yaml"
    fallback_path.parent.mkdir(parents=True)
    fallback_path.write_text("codex:\n  enabled: true\n", encoding="utf-8")

    # project 側の manifest は core のみに依存し、agent-routing の
    # project-local config を一切持たない（.claude/config/agent-routing/ 自体が無い）。
    (project_dir / ".claude").mkdir(parents=True)

    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

    data = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src/core/engine.py",
            "content": "class Engine(Protocol): pass",
        },
        "cwd": str(project_dir),
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_outputs_suggestion_when_project_local_config_exists(tmp_path, monkeypatch) -> None:
    """project-local な cli-tools.yaml に codex.enabled: true がある通常環境
    （agent-routing 導入済み）では、従来どおり提案が出る（後方互換の回帰確認）。"""
    project_dir = tmp_path / "project"
    config_path = project_dir / ".claude" / "config" / "agent-routing" / "cli-tools.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("codex:\n  enabled: true\n", encoding="utf-8")

    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

    data = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src/core/engine.py",
            "content": "class Engine(Protocol): pass",
        },
        "cwd": str(project_dir),
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0

    output = json.loads(stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Codex Suggestion]" in context


# --- EV-19: プロジェクト外パスの除外 ---


def test_main_ev_19_no_output_for_outside_absolute_path(tmp_path, monkeypatch) -> None:
    """EV-19: プロジェクト外の絶対パスには提案を出力しない。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    data = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / "outside" / "engine.py"),
            "content": "class Engine(Protocol): pass",
        },
        "cwd": str(tmp_path / "project"),
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_ev_19_outputs_suggestion_for_inside_absolute_path(tmp_path, monkeypatch) -> None:
    """EV-19: プロジェクト内の絶対パスには従来どおり提案を出力する。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    data = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / "project" / "src" / "core" / "engine.py"),
            "content": "class Engine(Protocol): pass",
        },
        "cwd": str(tmp_path / "project"),
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0

    output = json.loads(stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Codex Suggestion]" in context


def test_main_ev_19_outputs_suggestion_for_inside_relative_path(tmp_path, monkeypatch) -> None:
    """EV-19: 相対パスはプロジェクトルート基準で解決して提案を出力する。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    data = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src/core/engine.py",
            "content": "class Engine(Protocol): pass",
        },
        "cwd": str(tmp_path / "project"),
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0

    output = json.loads(stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Codex Suggestion]" in context


def test_main_ev_19_no_output_for_inside_symlink_to_outside(tmp_path, monkeypatch) -> None:
    """EV-19: プロジェクト内から外を指すシンボリックリンクには提案を出力しない。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    project_dir = tmp_path / "project"
    outside_dir = tmp_path / "outside"
    project_dir.mkdir()
    outside_dir.mkdir()
    outside_file = outside_dir / "engine.py"
    outside_file.write_text("class Engine(Protocol): pass", encoding="utf-8")
    linked_file = project_dir / "linked_engine.py"
    os.symlink(outside_file, linked_file)

    data = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(linked_file), "content": "class Engine(Protocol): pass"},
        "cwd": str(project_dir),
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_ev_19_outputs_suggestion_when_project_root_is_unresolved(monkeypatch) -> None:
    """EV-19: プロジェクトルート不明時は fail-open で従来どおり提案を出力する。"""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    data = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "src/core/engine.py",
            "content": "class Engine(Protocol): pass",
        },
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0

    output = json.loads(stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Codex Suggestion]" in context


# --- EV-16: 性能（should）---


def test_should_suggest_codex_is_fast_for_many_calls() -> None:
    """EV-16: 外部 I/O・プロセス起動を伴わないため、大量呼び出しでも高速に完了する。"""
    start = time.monotonic()
    for _ in range(2000):
        check_codex_before_write.should_suggest_codex("src/service_logic.py", "class Service: pass")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0


@pytest.fixture
def enabled_project(tmp_path, monkeypatch):
    """提案を明示的に有効化した、テスト専用のプロジェクト。"""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    return project_dir


@pytest.mark.parametrize("tool_name", ["", "Read", "Bash", "MultiEdit"])
def test_main_only_edit_and_write_can_suggest(enabled_project, tool_name) -> None:
    """設計ファイルでも対象外ツールでは提案も状態保存もしない。"""
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": tool_name,
            "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
            "cwd": str(enabled_project),
            "session_id": "tool-scope",
        }
    )
    assert (stdout, stderr, exit_code) == ("", "", 0)
    assert not (enabled_project / ".claude" / "state" / "suggestions").exists()


@pytest.mark.parametrize(
    ("old_content", "content", "expected"),
    [
        ("class Engine(Protocol):\n    old: int", "class Engine(Protocol):\n    value: str", False),
        ("class Engine:\n    pass", "class Engine(Protocol):\n    pass", True),
        ("class Engine(Protocol):\n    pass", "", True),
    ],
)
def test_main_edit_uses_old_string_for_declaration_changes(
    enabled_project, old_content, content, expected
) -> None:
    """main は old_string を渡し、宣言追加・削除と本文だけの変更を区別する。"""
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "src/engine.py",
                "old_string": old_content,
                "new_string": content,
            },
            "cwd": str(enabled_project),
        }
    )
    assert exit_code == 0
    assert stderr == ""
    if expected:
        assert "[Codex Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    else:
        assert stdout == ""


@pytest.mark.parametrize("parent_name", ["design", "architecture", "contracts"])
def test_main_classifies_only_project_relative_path(enabled_project, parent_name) -> None:
    """プロジェクトの親ディレクトリ名で設計変更と誤判定しない。"""
    project_dir = enabled_project / parent_name / "nested-project"
    project_dir.mkdir(parents=True)
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(project_dir / "worker.py"), "content": "value = 1"},
            "cwd": str(project_dir),
        }
    )
    assert (stdout, stderr, exit_code) == ("", "", 0)


@pytest.mark.parametrize(
    "agent_context",
    [{"agent_id": "child"}, {"agent_id": "child", "agent_type": "implementation"}],
)
def test_main_suppresses_subagent_without_creating_state(enabled_project, agent_context) -> None:
    """agent_type の有無にかかわらずトップレベルの agent_id で再帰提案を抑制する。"""
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
            "cwd": str(enabled_project),
            "session_id": "subagent-session",
            **agent_context,
        }
    )
    assert (stdout, stderr, exit_code) == ("", "", 0)
    assert not (enabled_project / ".claude" / "state" / "suggestions").exists()


def test_main_subagent_skips_contract_analysis(enabled_project, monkeypatch) -> None:
    """サブエージェントの抑制は契約宣言の解析より前に行う。"""

    def reject_contract_analysis(*_args):
        pytest.fail("サブエージェントでは契約宣言を解析しない")

    monkeypatch.setattr(
        check_codex_before_write, "_contract_declarations", reject_contract_analysis
    )
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "src/engine.py",
                "old_string": "class Engine:\n    pass\n",
                "new_string": "class Engine(Protocol):\n    pass\n",
            },
            "cwd": str(enabled_project),
            "session_id": "subagent-before-analysis",
            "agent_id": "child",
        }
    )
    assert (stdout, stderr, exit_code) == ("", "", 0)
    assert not (enabled_project / ".claude" / "state" / "suggestions").exists()


@pytest.mark.parametrize("tool_name", ["Edit", "Write"])
def test_main_agent_type_only_suggests_and_deduplicates_as_main(enabled_project, tool_name) -> None:
    """--agent のメインセッションも提案でき、同一対象の通知権は一度だけ消費する。"""
    tool_input = {"file_path": "DESIGN.md"}
    if tool_name == "Edit":
        tool_input.update(old_string="旧設計", new_string="新設計")
    else:
        tool_input["content"] = "設計内容"
    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(enabled_project),
        "session_id": "main-agent-session",
        "agent_type": "implementation",
    }

    stdout, stderr, exit_code = _run_main_with_stdin(payload)
    assert exit_code == 0
    assert stderr == ""
    assert "[Codex Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert _run_main_with_stdin(payload) == ("", "", 0)


def test_main_tool_input_subagent_type_does_not_suppress_suggestion(enabled_project) -> None:
    """tool_input.subagent_type は現在の実行コンテキストではない。"""
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "DESIGN.md",
                "content": "設計内容",
                "subagent_type": "planner",
            },
            "cwd": str(enabled_project),
        }
    )
    assert exit_code == 0
    assert stderr == ""
    assert "[Codex Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


def test_main_session_deduplicates_resolved_file_paths(enabled_project) -> None:
    """相対・絶対・内部 symlink は同じ実ファイルに対して一度だけ提案する。"""
    target_file = enabled_project / "DESIGN.md"
    target_file.write_text("設計内容", encoding="utf-8")
    (enabled_project / "linked.md").symlink_to(target_file)
    paths = ["DESIGN.md", str(target_file), "linked.md", "ARCHITECTURE.md"]
    for index, file_path in enumerate(paths):
        stdout, stderr, exit_code = _run_main_with_stdin(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": file_path, "content": "設計内容"},
                "cwd": str(enabled_project),
                "session_id": "same-session",
            }
        )
        assert exit_code == 0
        assert stderr == ""
        if index in (0, 3):
            assert (
                "[Codex Suggestion]"
                in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
            )
        else:
            assert stdout == ""


def test_main_new_session_can_suggest_same_file(enabled_project) -> None:
    """別セッションでは同じファイルへの提案を抑制しない。"""
    for session_id in ("session-one", "session-two"):
        stdout, stderr, exit_code = _run_main_with_stdin(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
                "cwd": str(enabled_project),
                "session_id": session_id,
            }
        )
        assert exit_code == 0
        assert stderr == ""
        assert "[Codex Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


def test_main_without_session_repeats_without_saving_state(enabled_project) -> None:
    """session_id がない場合は毎回提案し、永続状態を作らない。"""
    for _ in range(2):
        stdout, stderr, exit_code = _run_main_with_stdin(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
                "cwd": str(enabled_project),
            }
        )
        assert exit_code == 0
        assert stderr == ""
        assert "[Codex Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert not (enabled_project / ".claude" / "state" / "suggestions").exists()


def test_main_non_trigger_does_not_consume_file_claim(enabled_project) -> None:
    """非該当の変更が後続の契約宣言追加の提案を消費しない。"""
    for content, expected in [("value = 1", False), ("class Engine(Protocol):\n    pass", True)]:
        stdout, stderr, exit_code = _run_main_with_stdin(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "engine.py", "content": content},
                "cwd": str(enabled_project),
                "session_id": "later-contract",
            }
        )
        assert exit_code == 0
        assert stderr == ""
        if expected:
            assert (
                "[Codex Suggestion]"
                in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
            )
        else:
            assert stdout == ""


def test_main_state_failure_suppresses_suggestion_without_blocking(enabled_project) -> None:
    """状態ディレクトリを作れなくてもツール実行は止めない。"""
    (enabled_project / ".claude").mkdir()
    (enabled_project / ".claude" / "state").write_text("not a directory", encoding="utf-8")
    stdout, _stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
            "cwd": str(enabled_project),
            "session_id": "state-failure",
        }
    )
    assert exit_code == 0
    assert stdout == ""


def test_main_suggestion_is_optional_routing_first_and_nonblocking(enabled_project) -> None:
    """通知は routing 優先の任意提案であり permissionDecision や block を返さない。"""
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
            "cwd": str(enabled_project),
        }
    )
    assert exit_code == 0
    assert stderr == ""
    output = json.loads(stdout)
    assert set(output) == {"hookSpecificOutput"}
    hook_output = output["hookSpecificOutput"]
    assert set(hook_output) == {"hookEventName", "additionalContext"}
    assert hook_output["hookEventName"] == "PreToolUse"
    context = hook_output["additionalContext"].casefold()
    assert "optional" in context or "任意" in context
    assert "routing" in context
    assert any(word in context for word in ("priority", "precedence", "優先"))


def _reject_target_reads(monkeypatch, target_file, *, read_error=False) -> None:
    """製品側の読取 helper 名や open の選択には依存しない。"""

    def guard(open_file):
        def guarded_open(file, *args, **kwargs):
            if (
                isinstance(file, (str, bytes, os.PathLike))
                and os.path.basename(os.fsdecode(file)) == target_file.name
            ):
                if read_error:
                    raise PermissionError("旧本文を読み取れない")
                pytest.fail("この対象の旧本文は読み込まない")
            return open_file(file, *args, **kwargs)

        return guarded_open

    for owner in (builtins, io, os, Path):
        monkeypatch.setattr(owner, "open", guard(owner.open))


@pytest.mark.parametrize(
    "old_file_kind", ["oversized", "non-utf8", "directory", "read-error", "outside-symlink"]
)
def test_main_write_unsafe_old_file_suppresses_only_notification(
    enabled_project, monkeypatch, old_file_kind
) -> None:
    """旧本文を安全に読めない場合も無出力・exit 0 とし、対象を変更しない。"""
    target_file = enabled_project / "src/engine.py"
    target_file.parent.mkdir()
    original = b"old content"
    original_file = target_file
    if old_file_kind == "directory":
        target_file.mkdir()
    else:
        if old_file_kind == "oversized":
            original = b"x" * (check_codex_before_write.MAX_CONTENT_LENGTH + 1)
        elif old_file_kind == "non-utf8":
            original = b"\xff\xfe"
        elif old_file_kind == "outside-symlink":
            original_file = enabled_project.parent / "outside.py"
            target_file.symlink_to(original_file)
        original_file.write_bytes(original)
    with monkeypatch.context() as reads:
        if old_file_kind in ("read-error", "outside-symlink"):
            _reject_target_reads(reads, original_file, read_error=old_file_kind == "read-error")
        if old_file_kind == "outside-symlink":
            _reject_target_reads(reads, target_file)
        stdout, _stderr, exit_code = _run_main_with_stdin(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "src/engine.py",
                    "content": "class Engine(Protocol): pass",
                },
                "cwd": str(enabled_project),
                "session_id": "unsafe-old-file",
            }
        )
    assert (stdout, exit_code) == ("", 0)
    assert not (enabled_project / ".claude/state").exists()
    if old_file_kind == "directory":
        assert target_file.is_dir()
    else:
        assert original_file.read_bytes() == original
    if old_file_kind == "outside-symlink":
        assert target_file.is_symlink()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO を利用できない環境")
def test_main_write_fifo_does_not_block_or_change_target(tmp_path) -> None:
    """FIFO の読み込みで停止する退行は子プロセスの timeout で打ち切る。"""
    project_dir = tmp_path / "project"
    config_path = project_dir / ".claude/config/agent-routing/cli-tools.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("codex:\n  enabled: true\n", encoding="utf-8")
    target_file = project_dir / "engine.py"
    os.mkfifo(target_file)
    original_mode = target_file.stat().st_mode
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "packages/codex-suggestions/hooks/check-codex-before-write.py"),
        ],
        input=json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "engine.py", "content": "class Engine(Protocol): pass"},
                "cwd": str(project_dir),
                "session_id": "fifo-target",
            }
        ),
        cwd=project_dir,
        env={
            **os.environ,
            "AI_ORCHESTRA_DIR": str(REPO_ROOT),
            "CLAUDE_PROJECT_DIR": str(project_dir),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert target_file.stat().st_mode == original_mode
    assert not (project_dir / ".claude/state").exists()


@pytest.mark.parametrize("over_limit", [False, True])
def test_main_write_old_content_limit_includes_boundary(enabled_project, over_limit) -> None:
    """旧本文の上限ちょうどまでは宣言削除を検出し、超過時だけ通知を抑制する。"""
    target_file = enabled_project / "engine.py"
    header = "class Engine(Protocol):\n    pass\n#"
    size = check_codex_before_write.MAX_CONTENT_LENGTH + int(over_limit)
    old_content = header + "x" * (size - len(header))
    target_file.write_text(old_content, encoding="utf-8")
    stdout, _stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "engine.py", "content": ""},
            "cwd": str(enabled_project),
        }
    )
    assert exit_code == 0
    if over_limit:
        assert stdout == ""
    else:
        assert "[Codex Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert target_file.read_text(encoding="utf-8") == old_content


@pytest.mark.parametrize("suffix", [".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".java"])
def test_main_write_reads_old_headers_for_each_source_extension(enabled_project, suffix) -> None:
    """対象ソース拡張子では同じ契約ヘッダを残した本文変更を提案しない。"""
    target_file = enabled_project / f"engine{suffix}"
    if suffix in (".py", ".pyi"):
        old_content = "class Engine(Protocol):\n    pass\n"
    elif suffix == ".java":
        old_content = "public interface Engine {\n}\n"
    else:
        old_content = "abstract class Engine {\n}\n"
    content = (
        old_content.replace("    pass", "    value: str")
        if suffix in (".py", ".pyi")
        else old_content.replace("}", "    // 本文だけの変更\n}")
    )
    target_file.write_text(old_content, encoding="utf-8")
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": target_file.name, "content": content},
            "cwd": str(enabled_project),
        }
    )
    assert (stdout, stderr, exit_code) == ("", "", 0)
    assert target_file.read_text(encoding="utf-8") == old_content


@pytest.mark.parametrize("suffix", [".md", ".txt", ".json"])
def test_main_write_non_source_does_not_read_old_content(
    enabled_project, monkeypatch, suffix
) -> None:
    """通常の非ソースファイルは、旧本文を読むことなく非提案にする。"""
    target_file = enabled_project / f"notes{suffix}"
    target_file.write_bytes(b"\xff\xfe")
    with monkeypatch.context() as reads:
        _reject_target_reads(reads, target_file)
        stdout, stderr, exit_code = _run_main_with_stdin(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": target_file.name, "content": "更新したメモ"},
                "cwd": str(enabled_project),
            }
        )
    assert (stdout, stderr, exit_code) == ("", "", 0)
    assert target_file.read_bytes() == b"\xff\xfe"


@pytest.mark.parametrize(
    "file_path", ["DESIGN.md", "docs/architecture/overview.md", "contracts/engine.py"]
)
def test_main_write_design_path_suggests_once_without_reading_old_content(
    enabled_project, monkeypatch, file_path
) -> None:
    """設計パスは旧本文の比較と独立し、読取不能な旧本文でも一度だけ通知する。"""
    target_file = enabled_project / file_path
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(b"\xff\xfe")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "更新した設計"},
        "cwd": str(enabled_project),
        "session_id": "design-path-independent",
    }
    with monkeypatch.context() as reads:
        _reject_target_reads(reads, target_file)
        stdout, stderr, exit_code = _run_main_with_stdin(payload)
        assert exit_code == 0
        assert stderr == ""
        assert "[Codex Suggestion]" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert _run_main_with_stdin(payload) == ("", "", 0)
    assert target_file.read_bytes() == b"\xff\xfe"


@pytest.mark.parametrize(
    "shadow_config",
    [
        "codex:\n  enabled: false\n",
        "codex: [\n",
        "codex:\n  enabled: true\n  model: shadow-model\n",
    ],
    ids=["disabled", "malformed", "different-model"],
)
@pytest.mark.parametrize("local_disabled", [False, True])
def test_main_project_root_config_and_local_override_outrank_cwd_shadow(
    tmp_path, monkeypatch, shadow_config, local_disabled
) -> None:
    """設定・local 無効化・通知 marker はすべて CLAUDE_PROJECT_DIR を優先する。"""
    project_dir = tmp_path / "project"
    subdir = project_dir / "src"
    for directory, content in [
        (project_dir, "codex:\n  enabled: true\n  model: root-model\n"),
        (subdir, shadow_config),
    ]:
        config_path = directory / ".claude/config/agent-routing/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(content, encoding="utf-8")
    if local_disabled:
        local_path = project_dir / ".claude/config/agent-routing/cli-tools.local.yaml"
        local_path.write_text("codex:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
        "cwd": str(subdir),
        "session_id": "root-config",
    }
    stdout, stderr, exit_code = _run_main_with_stdin(payload)
    assert exit_code == 0
    assert stderr == ""
    state_dir = project_dir / ".claude/state/suggestions"
    if local_disabled:
        assert stdout == ""
        assert not state_dir.exists()
    else:
        context = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "[Codex Suggestion]" in context
        assert "root-model" in context
        assert "shadow-model" not in context
        assert len([path for path in state_dir.rglob("*") if path.is_file()]) == 1
        assert _run_main_with_stdin(payload) == ("", "", 0)
    assert not (subdir / ".claude/state").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_main_without_project_env_uses_cwd_config(tmp_path, monkeypatch, enabled) -> None:
    """環境変数がなければ親ではなく payload の cwd が設定・marker の基準になる。"""
    project_dir = tmp_path / "project"
    subdir = project_dir / "src"
    for directory, is_enabled, model in [
        (project_dir, not enabled, "parent-model"),
        (subdir, enabled, "cwd-model"),
    ]:
        config_path = directory / ".claude/config/agent-routing/cli-tools.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            f"codex:\n  enabled: {str(is_enabled).lower()}\n  model: {model}\n", encoding="utf-8"
        )
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    stdout, stderr, exit_code = _run_main_with_stdin(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
            "cwd": str(subdir),
            "session_id": "cwd-config",
        }
    )
    assert exit_code == 0
    assert stderr == ""
    if enabled:
        context = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "[Codex Suggestion]" in context
        assert "cwd-model" in context
        assert "parent-model" not in context
        assert len([path for path in (subdir / ".claude/state").rglob("*") if path.is_file()]) == 1
    else:
        assert stdout == ""
        assert not (subdir / ".claude/state").exists()
    assert not (project_dir / ".claude/state").exists()
