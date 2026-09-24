from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from tests.module_loader import load_module

lint_on_save = load_module("lint_on_save", "packages/quality-gates/hooks/lint-on-save.py")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/main.py", "python"),
        ("frontend/app.ts", "javascript"),
        ("docs/guide.md", "prettier"),
        ("config/settings.yaml", "prettier"),
        ("cmd/main.go", "go"),
        ("src/lib.rs", "rust"),
    ],
)
def test_get_file_kind_detects_supported_extensions(path: str, expected: str) -> None:
    assert lint_on_save.get_file_kind(path) == expected


def test_get_file_kind_detects_shell_script_by_shebang(tmp_path: Path) -> None:
    script = tmp_path / "deploy"
    script.write_text("#!/bin/bash\necho hello\n", encoding="utf-8")

    assert lint_on_save.get_file_kind(str(script)) == "shell"


def test_get_file_kind_returns_none_for_unsupported_file() -> None:
    assert lint_on_save.get_file_kind("notes.txt") is None


def test_build_lint_steps_for_python() -> None:
    steps = lint_on_save.build_lint_steps("app/main.py")
    assert [step["name"] for step in steps] == ["ruff format", "ruff check"]


def test_build_lint_steps_for_typescript() -> None:
    steps = lint_on_save.build_lint_steps("frontend/app.ts")
    assert [step["name"] for step in steps] == ["biome check", "prettier", "eslint"]


def test_build_lint_steps_for_shell() -> None:
    steps = lint_on_save.build_lint_steps("scripts/deploy.sh")
    assert [step["name"] for step in steps] == ["shfmt", "shellcheck"]


def test_run_step_skips_missing_tool_errors(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    responses = iter(
        [
            Result(1, stderr="npm ERR! could not determine executable to run"),
            Result(0, stdout="formatted"),
        ]
    )

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return next(responses)

    monkeypatch.setattr(lint_on_save.subprocess, "run", fake_run)

    result = lint_on_save.run_step(
        {
            "name": "prettier",
            "commands": [["npm", "exec", "--", "prettier", "--write", "file.ts"], ["prettier"]],
        },
        ".",
    )

    assert result == {"name": "prettier", "success": True, "output": "formatted"}
    assert len(calls) == 2


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.parametrize(
    "missing",
    [
        # pnpm exec: package.json の無いリポジトリ
        _Result(1, stdout="ERR_PNPM_RECURSIVE_EXEC_NO_PACKAGE  No package found in this workspace"),
        # npm exec --no / npx --no-install: ツール未導入
        _Result(
            1,
            stderr='npm error npx canceled due to missing packages and no YES option: ["prettier"]',
        ),
        # --offline で registry 情報のキャッシュも無い
        _Result(
            1,
            stderr="npm error code ENOTCACHED\nnpm error request to https://registry.npmjs.org/"
            "prettier failed: cache mode is 'only-if-cached'",
        ),
        # npm 7〜9 は理由を付けず canceled だけを出す
        _Result(1, stderr="npm ERR! canceled\n\nnpm ERR! A complete log of this run can be found"),
        # yarn: stdout に案内、stderr に未導入の理由
        _Result(
            1,
            stdout="yarn run v1.22.22\ninfo Visit https://yarnpkg.com/en/docs/cli/run",
            stderr='error Couldn\'t find a package.json file in "/repo/docs"',
        ),
    ],
    ids=[
        "pnpm-no-package",
        "npm-no-install",
        "npm-offline-not-cached",
        "npm9-canceled",
        "yarn-reason-on-stderr",
    ],
)
def test_run_step_falls_back_to_next_launcher_when_tool_missing(
    monkeypatch, missing: _Result
) -> None:
    # 先頭候補の「未導入」を失敗として報告すると、後ろにある導入済みの
    # prettier（PATH 上のグローバル版など）に届かず毎回エラーになる。
    responses = iter([missing, _Result(0, stdout="formatted")])
    monkeypatch.setattr(lint_on_save.subprocess, "run", lambda cmd, **kwargs: next(responses))

    result = lint_on_save.run_step(
        {"name": "prettier", "commands": [["pnpm", "exec", "prettier"], ["prettier"]]},
        ".",
    )

    assert result == {"name": "prettier", "success": True, "output": "formatted"}


@pytest.mark.parametrize(
    "failure",
    [
        _Result(2, stderr="[error] doc.md: SyntaxError: Unexpected token"),
        # canceled を含むだけの行は未導入扱いしない（npm 7〜9 の 1 行と完全一致のみ）
        _Result(2, stderr="[error] doc.md: request canceled by plugin"),
        # 別々のストリームの断片を組み合わせて「command "x" not found」とみなさない
        _Result(2, stdout='Running command "lint"', stderr='[error] rule "semi" not found'),
    ],
    ids=["syntax-error", "partial-canceled", "fragments-across-streams"],
)
def test_run_step_reports_real_tool_failure(monkeypatch, failure: _Result) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _Result:
        calls.append(cmd)
        return failure

    monkeypatch.setattr(lint_on_save.subprocess, "run", fake_run)

    result = lint_on_save.run_step(
        {"name": "prettier", "commands": [["pnpm", "exec", "prettier"], ["prettier"]]},
        ".",
    )

    assert result is not None
    assert result["success"] is False
    assert failure.stderr.strip() in result["output"]
    assert len(calls) == 1


def test_run_step_reports_stderr_even_when_stdout_has_banner(monkeypatch) -> None:
    # yarn は stdout に案内を出すため、stdout だけを表示すると失敗理由が隠れる。
    failure = _Result(1, stdout="yarn run v1.22.22", stderr="error doc.md: invalid config")
    monkeypatch.setattr(lint_on_save.subprocess, "run", lambda cmd, **kwargs: failure)

    result = lint_on_save.run_step({"name": "prettier", "commands": [["yarn", "prettier"]]}, ".")

    assert result is not None
    assert "error doc.md: invalid config" in result["output"]


def test_node_tool_commands_never_install_implicitly() -> None:
    # hook の stdin は TTY ではないため、npm exec は --no が無いと --yes とみなし
    # 未導入のツールを名前だけで最新版取得する（`biome` は同名の別パッケージ）。
    # --offline は、未導入の判断前に registry へ問い合わせてオフラインで
    # hook の timeout を超えるのを防ぐ。
    commands = lint_on_save.node_tool_commands("prettier", "--write", "doc.md")

    assert ["npm", "exec", "--no", "--offline", "--", "prettier", "--write", "doc.md"] in commands
    assert ["npx", "--no-install", "--offline", "prettier", "--write", "doc.md"] in commands
    assert all(cmd[:2] != ["npm", "exec"] or "--no" in cmd for cmd in commands)


def test_run_step_falls_back_on_timeout(monkeypatch) -> None:
    """EV-14: 15秒タイムアウト（subprocess.TimeoutExpired）でも次候補にフォールバックする。"""
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if cmd[0] == "pnpm":
            raise lint_on_save.subprocess.TimeoutExpired(cmd=cmd, timeout=15)
        return Result(0, stdout="formatted")

    monkeypatch.setattr(lint_on_save.subprocess, "run", fake_run)

    result = lint_on_save.run_step(
        {
            "name": "prettier",
            "commands": [
                ["pnpm", "exec", "prettier", "--write", "file.ts"],
                ["npm", "exec", "--", "prettier", "--write", "file.ts"],
            ],
        },
        ".",
    )

    assert result == {"name": "prettier", "success": True, "output": "formatted"}
    assert len(calls) == 2


def test_main_skips_unsupported_files(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "packages/quality-gates/manifest.txt"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""


def test_main_reports_lint_result(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        lint_on_save,
        "run_lint_commands",
        lambda _: [{"name": "ruff format", "success": True, "output": "1 file reformatted"}],
    )

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Lint OK]" in context
    assert "ruff format: 1 file reformatted" in context


def test_main_stays_silent_when_no_launcher_has_the_tool(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 整形ツールが無い環境で編集のたびにエラーを出さない（EV-14）。
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "docs/guide.md"}}
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    def fake_run(cmd: list[str], **kwargs: object) -> _Result:
        if cmd[0] == "pnpm":
            return _Result(1, stdout="ERR_PNPM_RECURSIVE_EXEC_NO_PACKAGE  No package found here")
        if cmd[0] in ("npm", "npx"):
            return _Result(1, stderr="npm error npx canceled due to missing packages")
        if cmd[0] == "yarn":
            return _Result(1, stdout="yarn run v1.22.22", stderr="error Command not found")
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(lint_on_save.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# EV-21: quality_gate.enabled 遵守
# ---------------------------------------------------------------------------


def test_main_no_op_when_quality_gate_disabled(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """quality_gate.enabled=false のときは formatter/linter 実行を含む全動作を行わない。"""
    payload = {
        "tool_name": "Write",
        "cwd": "/project",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        lint_on_save,
        "load_package_config",
        lambda *_args: {"features": {"quality_gate": {"enabled": False}}},
    )
    called = {"ran": False}

    def _fail_if_called(_file_path):  # type: ignore[no-untyped-def]
        called["ran"] = True
        return []

    monkeypatch.setattr(lint_on_save, "run_lint_commands", _fail_if_called)

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    assert called["ran"] is False
    assert capsys.readouterr().out == ""


def test_main_normalizes_subdirectory_cwd_before_loading_config(monkeypatch) -> None:
    """Claude Code がリポジトリのサブディレクトリ（例: packages/foo）から
    起動された場合でも、project_dir を .claude/ を持つ最寄りの親へ正規化
    してから audit-flags.json を読み込むことを確認する（Issue #134 レビュー
    指摘: 従来は data.cwd がそのまま渡され、プロジェクト固有の設定・
    ローカル上書きが見つからなくなっていた）。"""
    payload = {
        "tool_name": "Write",
        "cwd": "/repo/packages/foo",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(lint_on_save, "find_project_root", lambda start_dir: "/repo")

    captured: dict[str, str] = {}

    def _fake_load_package_config(_package, _filename, project_dir):  # type: ignore[no-untyped-def]
        captured["project_dir"] = project_dir
        return {"features": {"quality_gate": {"enabled": False}}}

    monkeypatch.setattr(lint_on_save, "load_package_config", _fake_load_package_config)

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    assert captured["project_dir"] == "/repo"


# ---------------------------------------------------------------------------
# EV-22: additionalContext の秘匿情報マスキング
# ---------------------------------------------------------------------------


def test_main_masks_secrets_in_lint_output(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "tool_name": "Write",
        "cwd": "/project",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        lint_on_save,
        "load_package_config",
        lambda *_args: {"features": {"quality_gate": {"enabled": True}}},
    )
    monkeypatch.setattr(
        lint_on_save,
        "run_lint_commands",
        lambda _: [
            {
                "name": "ruff check",
                "success": False,
                "output": "config error: api_key=sk-1234567890abcdefghijklmno",
            }
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "sk-1234567890abcdefghijklmno" not in context
    assert "[REDACTED]" in context


# ---------------------------------------------------------------------------
# EV-10: main() の fail-open（例外捕捉 → stderr ログ + exit 0）
# ---------------------------------------------------------------------------


def test_main_fails_open_on_unexpected_exception(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {
        "tool_name": "Write",
        "cwd": "/project",
        "tool_input": {"file_path": "packages/quality-gates/hooks/lint-on-save.py"},
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(lint_on_save, "load_package_config", _raise)

    with pytest.raises(SystemExit) as exc_info:
        lint_on_save.main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Hook error" in captured.err
    assert "boom" in captured.err
