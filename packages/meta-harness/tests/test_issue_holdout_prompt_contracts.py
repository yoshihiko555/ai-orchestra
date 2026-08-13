"""Issue #363 holdout prompt の機械可読な完了契約テスト。"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from tests.module_loader import load_module

load_module(
    "meta_harness_common",
    "packages/meta-harness/lib/meta_harness_common.py",
)
ev = load_module(
    "meta_harness_evaluator_issue_holdout_prompt_contracts",
    "packages/meta-harness/lib/evaluator.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "packages" / "meta-harness"
SCHEMA_DIR = PACKAGE_DIR / "schemas"

_SCENARIO_PATHS = {
    "create-bug-issue-holdout": (
        PACKAGE_DIR / "scenarios" / "skill" / "issue-create" / "create-bug-issue-holdout.yaml"
    ),
    "fix-formal-greeting-feature-holdout": (
        PACKAGE_DIR
        / "scenarios"
        / "skill"
        / "issue-fix"
        / "fix-formal-greeting-feature-holdout.yaml"
    ),
}

_AC_LINE = re.compile(
    r"^\s*- \[ \] (?P<condition>.+?) — (?P<kind>verify|judge): (?P<evidence>.+?)\s*$"
)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_PLACEHOLDER = re.compile(r"\{[^{}\n]+\}")


def _prompt(scenario_id: str) -> str:
    scenario = ev.load_scenario(_SCENARIO_PATHS[scenario_id], SCHEMA_DIR)
    return scenario["prompt"]


def _acceptance_criteria(prompt: str) -> list[dict[str, str]]:
    return [
        match.groupdict()
        for line in prompt.splitlines()
        if (match := _AC_LINE.fullmatch(line)) is not None
    ]


def _shell_commands(prompt: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for code_span in _INLINE_CODE.findall(prompt):
        try:
            tokens = shlex.split(code_span)
        except ValueError:
            continue
        if tokens:
            commands.append(tokens)
    return commands


def _matching_command(prompt: str, prefix: list[str]) -> list[str]:
    matches = [command for command in _shell_commands(prompt) if command[: len(prefix)] == prefix]
    assert len(matches) == 1, f"expected one command starting with {prefix!r}, got {matches!r}"
    return matches[0]


def _option_value(command: list[str], option: str) -> str:
    assert command.count(option) == 1, f"expected one {option}: {command!r}"
    index = command.index(option)
    assert index + 1 < len(command), f"{option} requires a value: {command!r}"
    value = command[index + 1]
    assert value not in {"...", "…"}
    assert _PLACEHOLDER.search(value) is None
    return value


def test_create_bug_holdout_supplies_concrete_verify_and_judge_acceptance_criteria() -> None:
    """CT-PROMPT-AC の具体的な AC 構造を識別する。"""
    # Prompt の文意は holdout で評価し、ここでは task-memory が定める機械構造だけを扱う。
    prompt = _prompt("create-bug-issue-holdout")

    criteria = _acceptance_criteria(prompt)

    assert [criterion["kind"] for criterion in criteria].count("verify") >= 1
    assert [criterion["kind"] for criterion in criteria].count("judge") >= 1
    assert all(
        _PLACEHOLDER.search(criterion["condition"] + criterion["evidence"]) is None
        for criterion in criteria
    )
    assert all(
        criterion["kind"] != "verify"
        or (
            criterion["evidence"].startswith("`")
            and criterion["evidence"].endswith("`")
            and criterion["evidence"] != "``"
        )
        for criterion in criteria
    )


def test_create_bug_holdout_declares_executable_python_gh_issue_create_command() -> None:
    """CT-PROMPT-GH の実行可能な完了コマンドを識別する。"""
    prompt = _prompt("create-bug-issue-holdout")

    command = _matching_command(prompt, ["python3", "bin/gh", "issue", "create"])

    assert command == [
        "python3",
        "bin/gh",
        "issue",
        "create",
        "--title",
        "設定読込失敗で起動できない",
        "--label",
        "bug",
        "--body-file",
        "issue-body.md",
    ], f"unexpected command tokens: {command!r}"
    assert "--repo" not in command
    assert all(".meta-harness/issue-create-call.json" not in token for token in command)


def test_create_bug_holdout_declares_completion_contract() -> None:
    """CT-PROMPT-BUG-DONE の完了契約を識別する。"""
    prompt = _prompt("create-bug-issue-holdout")

    # 短い文言を実際の prompt 本文に結び付け、完了契約の退行を防ぐ。
    assert "AskUserQuestion is unavailable" in prompt, f"missing input contract: {prompt!r}"
    assert "proceed directly to issue creation" in prompt, f"missing proceed contract: {prompt!r}"
    assert "preparing the body alone is not complete" in prompt, (
        f"missing artifact completion contract: {prompt!r}"
    )


def test_fix_formal_holdout_declares_executable_python_gh_issue_view_command() -> None:
    """CT-PROMPT-FFG の fixture 呼び出しコマンドを識別する。"""
    prompt = _prompt("fix-formal-greeting-feature-holdout")

    command = _matching_command(prompt, ["python3", "bin/gh", "issue", "view", "305"])

    json_fields = _option_value(command, "--json").split(",")
    assert len(command) == 7
    assert len(json_fields) == 5
    assert set(json_fields) == {"number", "title", "body", "labels", "assignees"}


def test_fix_formal_holdout_declares_completion_contract() -> None:
    """CT-PROMPT-FFG-DONE の完了契約を識別する。"""
    prompt = _prompt("fix-formal-greeting-feature-holdout")

    # 短い文言を実際の prompt 本文に結び付け、完了契約の退行を防ぐ。
    assert "AskUserQuestion is unavailable" in prompt, f"missing input contract: {prompt!r}"
    assert "not a substitute" in prompt, f"missing invocation contract: {prompt!r}"
    assert "exit 126" in prompt, f"missing noexec contract: {prompt!r}"
    assert "python3" in prompt, f"missing Python invocation contract: {prompt!r}"
