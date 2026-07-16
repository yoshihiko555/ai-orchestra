import io
import json
import sys
import time
from pathlib import Path

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


def test_validate_input_accepts_reasonable_values() -> None:
    assert check_codex_before_write.validate_input("src/app.py", "print('ok')")


def test_validate_input_rejects_invalid_values() -> None:
    assert not check_codex_before_write.validate_input("", "x")
    assert not check_codex_before_write.validate_input("a" * 4097, "x")
    assert not check_codex_before_write.validate_input("src/app.py", "x" * 1_000_001)
    assert not check_codex_before_write.validate_input("../secret.py", "x")


def test_should_suggest_codex_skips_simple_edit_files() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "README.md", "class A: pass"
    )
    assert not should_suggest
    assert reason == ""


def test_should_suggest_codex_for_design_path_indicator() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "docs/ARCHITECTURE.md", "small"
    )
    assert should_suggest
    assert "File path contains" in reason


def test_should_suggest_codex_for_large_content() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "src/new_feature.py", "x" * 600
    )
    assert should_suggest
    assert reason == "Creating new file with significant content"


def test_should_suggest_codex_for_content_indicator() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "src/service_logic.py", "class Service:\n    pass"
    )
    assert should_suggest
    assert "Content contains" in reason


def test_should_suggest_codex_for_large_src_file() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "src/feature.py", "y" * 250
    )
    assert should_suggest
    assert reason == "New source file"


def test_should_suggest_codex_false_for_small_regular_file() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "tools/script.py", "print('ok')"
    )
    assert not should_suggest
    assert reason == ""


# --- main (stdin/stdout contract, EV-12/EV-14/EV-15) ---


def _run_main_with_stdin(data: dict) -> tuple[str, str, str | int]:
    """main() を stdin モックで実行し (stdout, stderr, exit_code) を返す。"""
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdin = io.StringIO(json.dumps(data))
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

    exit_code = 0
    try:
        check_codex_before_write.main()
    except SystemExit as e:
        exit_code = e.code if e.code is not None else 0

    stdout = sys.stdout.getvalue()
    stderr = sys.stderr.getvalue()
    sys.stdin = old_stdin
    sys.stdout = old_stdout
    sys.stderr = old_stderr
    return stdout, stderr, exit_code


def test_main_outputs_suggestion_for_triggering_change(monkeypatch) -> None:
    """codex.enabled: true 明示時、設計系変更で `[Codex Suggestion]` を出力する。"""
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    data = {
        "tool_input": {"file_path": "src/core/engine.py", "content": "class Engine: pass"},
        "cwd": "/project",
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0

    output = json.loads(stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Codex Suggestion]" in context


def test_main_no_output_for_non_triggering_change(monkeypatch) -> None:
    """EV-12: 非該当時（提案条件を満たさない変更）は stdout に何も print しない。"""
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": True}}
    )
    data = {
        "tool_input": {"file_path": "tools/script.py", "content": "print('ok')"},
        "cwd": "/project",
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_fail_safe_on_internal_exception(monkeypatch) -> None:
    """EV-14: hook 内部で例外が発生しても stderr にエラーを出しつつ exit 0（fail-open）。"""

    def _raise(*_a: object, **_kw: object) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(check_codex_before_write, "load_package_config", _raise)
    data = {
        "tool_input": {"file_path": "src/core/engine.py", "content": "class Engine: pass"},
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
        "tool_input": {"file_path": "src/core/engine.py", "content": "class Engine: pass"},
        "cwd": "/project",
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_no_output_when_codex_explicitly_disabled(monkeypatch) -> None:
    """codex.enabled: false が明示された場合も提案を出力しない（既存挙動の回帰確認）。"""
    monkeypatch.setattr(check_codex_before_write, "has_project_config", lambda *_: True)
    monkeypatch.setattr(
        check_codex_before_write, "load_package_config", lambda *_: {"codex": {"enabled": False}}
    )
    data = {
        "tool_input": {"file_path": "src/core/engine.py", "content": "class Engine: pass"},
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
        "tool_input": {"file_path": "src/core/engine.py", "content": "class Engine: pass"},
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
        "tool_input": {"file_path": "src/core/engine.py", "content": "class Engine: pass"},
        "cwd": str(project_dir),
    }
    stdout, _stderr, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0

    output = json.loads(stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Codex Suggestion]" in context


# --- EV-16: 性能（should）---


def test_should_suggest_codex_uses_no_regex() -> None:
    """EV-16: 判定は正規表現ではなく単純な文字列包含（`in`）のみで行う。"""
    source = Path(check_codex_before_write.__file__).read_text(encoding="utf-8")
    assert "import re" not in source
    assert "re.compile" not in source
    assert "re.search" not in source
    assert "re.match" not in source


def test_should_suggest_codex_is_fast_for_many_calls() -> None:
    """EV-16: 外部 I/O・プロセス起動を伴わないため、大量呼び出しでも高速に完了する。"""
    start = time.monotonic()
    for _ in range(2000):
        check_codex_before_write.should_suggest_codex("src/service_logic.py", "class Service: pass")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
