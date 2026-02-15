import json

from tests.module_loader import load_module

log_cli_tools = load_module(
    "log_cli_tools", "packages/cli-logging/hooks/log-cli-tools.py"
)


def test_extract_codex_prompt_from_full_auto_double_quote() -> None:
    command = 'codex exec --model gpt-5.2-codex --full-auto "debug this failure"'
    assert log_cli_tools.extract_codex_prompt(command) == "debug this failure"


def test_extract_codex_prompt_from_full_auto_single_quote() -> None:
    command = "codex exec --full-auto 'investigate regression'"
    assert log_cli_tools.extract_codex_prompt(command) == "investigate regression"


def test_extract_codex_prompt_returns_none_when_missing() -> None:
    command = "codex exec --model gpt-5.2-codex --help"
    assert log_cli_tools.extract_codex_prompt(command) is None


def test_extract_gemini_prompt() -> None:
    command = 'gemini -p "summarize architecture" 2>/dev/null'
    assert log_cli_tools.extract_gemini_prompt(command) == "summarize architecture"


def test_extract_gemini_prompt_with_model_flag() -> None:
    command = 'gemini -m gemini-2.5-pro -p "summarize architecture" 2>/dev/null'
    assert log_cli_tools.extract_gemini_prompt(command) == "summarize architecture"


def test_extract_model() -> None:
    command = "codex exec --model gpt-5.3-codex --full-auto 'x'"
    assert log_cli_tools.extract_model(command) == "gpt-5.3-codex"
    assert log_cli_tools.extract_model("codex exec --full-auto 'x'") is None


def test_truncate_text() -> None:
    assert log_cli_tools.truncate_text("abc", max_length=3) == "abc"
    truncated = log_cli_tools.truncate_text("abcdefgh", max_length=3)
    assert truncated == "abc... [truncated, 8 total chars]"


def test_get_log_path_uses_nearest_claude_directory(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    path = log_cli_tools.get_log_path()

    assert path == project / ".claude" / "logs" / "cli-tools.jsonl"
    assert (project / ".claude" / "logs").is_dir()


def test_get_log_path_falls_back_to_current_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    path = log_cli_tools.get_log_path()

    assert path == tmp_path / ".claude" / "logs" / "cli-tools.jsonl"
    assert (tmp_path / ".claude" / "logs").is_dir()


def test_log_entry_appends_jsonl_line(tmp_path, monkeypatch) -> None:
    output = tmp_path / "cli-tools.jsonl"
    monkeypatch.setattr(log_cli_tools, "get_log_path", lambda: output)
    entry = {"tool": "codex", "success": True}

    log_cli_tools.log_entry(entry)

    line = output.read_text(encoding="utf-8").strip()
    assert json.loads(line) == entry
