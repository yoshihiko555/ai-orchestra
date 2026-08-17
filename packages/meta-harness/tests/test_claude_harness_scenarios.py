"""claude-harness suite 拡充（ADR-20260817-052）のシナリオ・fixture テスト。

- assert-python-type-hints.py / assert-python-conventions.py の pass/fail 両方向
- assert-effective-config.py の pass/fail/tamper 両方向（train=flat 形式・holdout=keyed 形式）
- 実シナリオの setup: が書き出す config ペアの sha256 が assert-effective-config.py の
  既知テーブルと一致すること（setup: heredoc とハードコードされたハッシュのドリフト検出）
- suite 全体が schema 検証・train/graded 宣言の一貫性チェックを通ること
- graded[*].command が YAML から折り畳まれた形のまま、既知の正解実装に対して実際に
  通る（false negative がない）こと
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_claude_harness_scenarios",
    "packages/meta-harness/lib/evaluator.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "packages" / "meta-harness"
SCHEMA_DIR = PACKAGE_DIR / "schemas"
SCENARIO_DIR = PACKAGE_DIR / "scenarios" / "claude-harness"
FIXTURES_DIR = PACKAGE_DIR / "scenarios" / "fixtures"


def _fixture(name: str):
    relative_path = (FIXTURES_DIR / name).relative_to(REPO_ROOT)
    return load_module(f"claude_harness_fixture_{name.replace('-', '_')}", str(relative_path))


def _load_scenario(scenario_id: str) -> dict:
    path = SCENARIO_DIR / f"{scenario_id}.yaml"
    return ev.load_scenario(path, SCHEMA_DIR)


def _run_setup(scenario_id: str, tmp_path: Path) -> None:
    scenario = _load_scenario(scenario_id)
    for command in scenario["setup"]:
        result = subprocess.run(command, shell=True, cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, f"setup command failed for {scenario_id}: {result.stderr}"


# ---------------------------------------------------------------------------
# Suite-level structure
# ---------------------------------------------------------------------------


def test_claude_harness_suite_validates_and_declares_graded_consistently() -> None:
    paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, "claude-harness")
    scenarios = [ev.load_scenario(path, SCHEMA_DIR) for path in paths]

    by_id = {scenario["id"]: scenario for scenario in scenarios}
    assert by_id.keys() >= {
        "create-version-file",
        "summarize-readme",
        "implement-slug-util",
        "implement-retry-helper-holdout",
        "resolve-effective-config",
        "resolve-nested-override-holdout",
    }

    non_holdout = [s for s in scenarios if not s["holdout"]]
    holdout = [s for s in scenarios if s["holdout"]]
    assert len(holdout) >= 2
    for scenario in non_holdout:
        assert scenario.get("graded"), f"{scenario['id']} must declare graded (suite consistency)"
    for scenario in scenarios:
        assert all(item["oracle"] != "rubric_judge" for item in scenario.get("graded", []))


# ---------------------------------------------------------------------------
# assert-python-type-hints.py
# ---------------------------------------------------------------------------


class TestAssertPythonTypeHints:
    def test_passes_with_complete_annotations(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "ok.py").write_text(
            'def slugify(title: str) -> str:\n    """Docstring."""\n    return title.lower()\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "ok.py"])

    def test_fails_when_parameter_annotation_missing(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title) -> str:\n    return title.lower()\n", encoding="utf-8"
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="missing a type annotation"):
            fixture.main(["--file", "bad.py"])

    def test_fails_when_return_annotation_missing(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title: str):\n    return title.lower()\n", encoding="utf-8"
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="missing a return type annotation"):
            fixture.main(["--file", "bad.py"])

    def test_self_and_cls_are_exempt(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "ok.py").write_text(
            "class Foo:\n"
            "    def bar(self, name: str) -> str:\n"
            '        """Doc."""\n'
            "        return name\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "ok.py"])


# ---------------------------------------------------------------------------
# assert-python-conventions.py
# ---------------------------------------------------------------------------


class TestAssertPythonConventions:
    def test_docstring_check_fails_when_missing(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="missing a docstring"):
            fixture.main(["--file", "f.py", "--require-docstring"])

    def test_docstring_check_passes_when_present(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text('def f():\n    """Doc."""\n    return 1\n', encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "f.py", "--require-docstring"])

    def test_snake_case_fails_on_pascal_case_function_name(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text("def DoThing():\n    return 1\n", encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="not snake_case"):
            fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_snake_case_fails_on_single_char_variable(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text("def f():\n    x = 1\n    return x\n", encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="single character"):
            fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_snake_case_exempts_for_loop_targets(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def total(values):\n    result = 0\n    for x in values:\n        result += x\n    return result\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_max_function_lines_fails_when_exceeded(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-conventions.py")
        body = "\n".join(f"    y = y + {i}" for i in range(30))
        (tmp_path / "f.py").write_text(
            f"def f():\n    y = 0\n{body}\n    return y\n", encoding="utf-8"
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="exceeds the 20-line limit"):
            fixture.main(["--file", "f.py", "--max-function-lines", "20"])

    def test_max_function_lines_passes_within_limit(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text('def f():\n    """Doc."""\n    return 1\n', encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "f.py", "--max-function-lines", "20"])

    def test_max_nesting_depth_fails_on_deep_sequential_ifs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def validate(name):\n"
            "    if name:\n"
            "        if len(name) >= 3:\n"
            "            if len(name) <= 32:\n"
            "                return True\n"
            "    return False\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="nesting depth"):
            fixture.main(["--file", "f.py", "--max-nesting-depth", "2"])

    def test_max_nesting_depth_passes_with_early_return(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def validate(name):\n"
            "    if not name:\n"
            "        return False\n"
            "    if len(name) < 3:\n"
            "        return False\n"
            "    return True\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "f.py", "--max-nesting-depth", "2"])


# ---------------------------------------------------------------------------
# assert-effective-config.py
# ---------------------------------------------------------------------------


def _write_train_config_pair(tmp_path: Path) -> None:
    base = tmp_path / "sandbox" / "config" / "agent-routing" / "cli-tools.yaml"
    local = tmp_path / "sandbox" / "config" / "agent-routing" / "cli-tools.local.yaml"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        "codex:\n  model: harness-base-model\n  sandbox:\n    analysis: read-only\n",
        encoding="utf-8",
    )
    local.write_text("codex:\n  model: harness-local-override-model\n", encoding="utf-8")


class TestAssertEffectiveConfigFlat:
    def test_passes_with_correct_value_and_source(self, tmp_path: Path, monkeypatch) -> None:
        _write_train_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"value": "harness-local-override-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        fixture.main(
            [
                "--base",
                "sandbox/config/agent-routing/cli-tools.yaml",
                "--local",
                "sandbox/config/agent-routing/cli-tools.local.yaml",
                "--answer",
                ".meta-harness/config-answer.json",
                "--key-path",
                "codex.model",
                "--field",
                "value",
            ]
        )

    def test_fails_when_value_wrongly_prefers_base(self, tmp_path: Path, monkeypatch) -> None:
        _write_train_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"value": "harness-base-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.yaml"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        with pytest.raises(AssertionError, match="value"):
            fixture.main(
                [
                    "--base",
                    "sandbox/config/agent-routing/cli-tools.yaml",
                    "--local",
                    "sandbox/config/agent-routing/cli-tools.local.yaml",
                    "--answer",
                    ".meta-harness/config-answer.json",
                    "--key-path",
                    "codex.model",
                    "--field",
                    "value",
                ]
            )

    def test_fails_closed_when_config_pair_tampered(self, tmp_path: Path, monkeypatch) -> None:
        _write_train_config_pair(tmp_path)
        base = tmp_path / "sandbox" / "config" / "agent-routing" / "cli-tools.yaml"
        base.write_text("codex:\n  model: tampered-value\n", encoding="utf-8")
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"value": "harness-local-override-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        with pytest.raises(AssertionError, match="does not match any known-good fixture"):
            fixture.main(
                [
                    "--base",
                    "sandbox/config/agent-routing/cli-tools.yaml",
                    "--local",
                    "sandbox/config/agent-routing/cli-tools.local.yaml",
                    "--answer",
                    ".meta-harness/config-answer.json",
                    "--key-path",
                    "codex.model",
                    "--field",
                    "value",
                ]
            )


def _write_holdout_config_pair(tmp_path: Path) -> None:
    base = tmp_path / "sandbox" / "config" / "agent-routing" / "cli-tools.yaml"
    local = tmp_path / "sandbox" / "config" / "agent-routing" / "cli-tools.local.yaml"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        "codex:\n  model: harness-nested-base-model\n  sandbox:\n"
        "    analysis: read-only\n    implementation: workspace-write\n",
        encoding="utf-8",
    )
    local.write_text("codex:\n  model: harness-nested-local-model\n", encoding="utf-8")


class TestAssertEffectiveConfigKeyed:
    def test_passes_for_overridden_key(self, tmp_path: Path, monkeypatch) -> None:
        _write_holdout_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"codex.model": {"value": "harness-nested-local-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}, '
            '"codex.sandbox.analysis": {"value": "read-only", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.yaml"}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        fixture.main(
            [
                "--keyed",
                "--base",
                "sandbox/config/agent-routing/cli-tools.yaml",
                "--local",
                "sandbox/config/agent-routing/cli-tools.local.yaml",
                "--answer",
                ".meta-harness/config-answer.json",
                "--key-path",
                "codex.model",
                "--field",
                "both",
            ]
        )

    def test_passes_for_non_overridden_key(self, tmp_path: Path, monkeypatch) -> None:
        _write_holdout_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"codex.model": {"value": "harness-nested-local-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}, '
            '"codex.sandbox.analysis": {"value": "read-only", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.yaml"}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        fixture.main(
            [
                "--keyed",
                "--base",
                "sandbox/config/agent-routing/cli-tools.yaml",
                "--local",
                "sandbox/config/agent-routing/cli-tools.local.yaml",
                "--answer",
                ".meta-harness/config-answer.json",
                "--key-path",
                "codex.sandbox.analysis",
                "--field",
                "both",
            ]
        )

    def test_fails_on_over_generalized_non_overridden_key(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """候補が「local があるキーは全部 local が勝つ」と過度に一般化した場合に不合格に
        なることを検証する（誤誘導耐性の核心アサーション）。"""
        _write_holdout_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"codex.model": {"value": "harness-nested-local-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}, '
            '"codex.sandbox.analysis": {"value": "harness-nested-local-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        with pytest.raises(AssertionError, match="codex.sandbox.analysis"):
            fixture.main(
                [
                    "--keyed",
                    "--base",
                    "sandbox/config/agent-routing/cli-tools.yaml",
                    "--local",
                    "sandbox/config/agent-routing/cli-tools.local.yaml",
                    "--answer",
                    ".meta-harness/config-answer.json",
                    "--key-path",
                    "codex.sandbox.analysis",
                    "--field",
                    "both",
                ]
            )


# ---------------------------------------------------------------------------
# Drift detection: real scenario setup: bytes must match the fixture's known-good table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario_id", ["resolve-effective-config", "resolve-nested-override-holdout"]
)
def test_real_scenario_setup_bytes_match_known_config_pair_table(scenario_id: str) -> None:
    """`assert-effective-config.py` の `_KNOWN_CONFIG_PAIRS` は setup: heredoc の正確な
    バイト列に対する sha256 でキーイングされている。setup: を編集してもハードコードされた
    ハッシュを更新し忘れる drift を検出するため、実シナリオの setup: を実行して得た
    config ペアの sha256 が既知テーブルのキーであることを確認する（PR #266 流儀、
    `_real_issue_fixture_bytes` と同じパターン）。"""
    fixture = _fixture("assert-effective-config.py")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _run_setup(scenario_id, tmp_dir)
        base = tmp_dir / "sandbox" / "config" / "agent-routing" / "cli-tools.yaml"
        local = tmp_dir / "sandbox" / "config" / "agent-routing" / "cli-tools.local.yaml"
        assert base.is_file()
        assert local.is_file()
        import hashlib

        base_sha = hashlib.sha256(base.read_bytes()).hexdigest()
        local_sha = hashlib.sha256(local.read_bytes()).hexdigest()
        assert (base_sha, local_sha) in fixture._KNOWN_CONFIG_PAIRS, (
            f"{scenario_id}'s setup: bytes (base sha256={base_sha}, local sha256={local_sha}) "
            "are not a key in assert-effective-config.py's _KNOWN_CONFIG_PAIRS; the setup: "
            "heredoc and the hardcoded table have drifted apart"
        )


# ---------------------------------------------------------------------------
# YAML-extracted graded[*].command smoke test against known-good sample solutions
# ---------------------------------------------------------------------------


_SLUGIFY_SOLUTION = '''import re


def slugify(title: str) -> str:
    """Convert a title string into a URL-friendly slug."""
    lowered = title.lower()
    hyphenated = re.sub(r"\\s+", "-", lowered)
    collapsed = re.sub(r"-{2,}", "-", hyphenated)
    stripped = collapsed.strip("-")
    return stripped if stripped else "untitled"
'''

_VALIDATOR_SOLUTION = '''import re

_ALLOWED = re.compile(r"^[A-Za-z0-9_]+$")


def validate_username(name: str | None) -> bool:
    """Validate a username using early return."""
    if not name:
        return False
    if len(name) < 3:
        return False
    if len(name) > 32:
        return False
    return bool(_ALLOWED.match(name))
'''


def _run_graded_commands(
    scenario_id: str, tmp_path: Path, files: dict[str, str], *, extra_env: dict | None = None
) -> None:
    scenario = _load_scenario(scenario_id)
    for rel_path, content in files.items():
        full = tmp_path / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    # Point AI_ORCHESTRA_DIR at tmp_path (the candidate workspace where `files` were written),
    # overriding this test process's own AI_ORCHESTRA_DIR (the meta repo checkout). This matches
    # real harness semantics: AI_ORCHESTRA_DIR resolves the fixture's `project_root` to the
    # candidate's own worktree, not this meta repo.
    env = {"AI_ORCHESTRA_DIR": str(tmp_path)}
    if extra_env:
        env.update(extra_env)
    for item in scenario["graded"]:
        assert item["oracle"] == "command_exit", item
        # In the real harness, cwd is a repo checkout, so `packages/meta-harness/scenarios/
        # fixtures/...` resolves relative to it. This test's tmp_path is not a repo checkout,
        # so substitute in the real fixtures dir's absolute path before executing.
        command = item["command"].replace(
            "packages/meta-harness/scenarios/fixtures/", f"{FIXTURES_DIR}/"
        )
        result = subprocess.run(
            command,
            shell=True,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={**__import__("os").environ, **env},
        )
        assert result.returncode == 0, (
            f"{scenario_id}/{item['id']} failed against known-good solution:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )


def test_implement_slug_util_graded_commands_pass_against_known_good_solution(
    tmp_path: Path,
) -> None:
    _run_graded_commands("implement-slug-util", tmp_path, {"sandbox/slugify.py": _SLUGIFY_SOLUTION})


def test_implement_retry_helper_holdout_graded_commands_pass_against_known_good_solution(
    tmp_path: Path,
) -> None:
    _run_graded_commands(
        "implement-retry-helper-holdout",
        tmp_path,
        {"sandbox/validator.py": _VALIDATOR_SOLUTION},
    )


def test_resolve_effective_config_graded_commands_pass_against_known_good_answer(
    tmp_path: Path,
) -> None:
    _run_setup("resolve-effective-config", tmp_path)
    answer = tmp_path / ".meta-harness" / "config-answer.json"
    answer.write_text(
        '{"value": "harness-local-override-model", '
        '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}',
        encoding="utf-8",
    )
    _run_graded_commands("resolve-effective-config", tmp_path, {})


def test_resolve_nested_override_holdout_graded_commands_pass_against_known_good_answer(
    tmp_path: Path,
) -> None:
    _run_setup("resolve-nested-override-holdout", tmp_path)
    answer = tmp_path / ".meta-harness" / "config-answer.json"
    answer.write_text(
        '{"codex.model": {"value": "harness-nested-local-model", '
        '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}, '
        '"codex.sandbox.analysis": {"value": "read-only", '
        '"source_file": "sandbox/config/agent-routing/cli-tools.yaml"}}',
        encoding="utf-8",
    )
    _run_graded_commands("resolve-nested-override-holdout", tmp_path, {})


def test_create_version_file_graded_command_passes_against_known_good_solution(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=tmp_path,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _run_graded_commands("create-version-file", tmp_path, {"VERSION": head + "\n"})


def test_summarize_readme_graded_command_passes_within_word_limit() -> None:
    scenario = _load_scenario("summarize-readme")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "summary.md").write_text(("word " * 50).strip() + "\n", encoding="utf-8")
        item = scenario["graded"][0]
        result = subprocess.run(item["command"], shell=True, cwd=tmp_dir, capture_output=True)
        assert result.returncode == 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "summary.md").write_text(("word " * 250).strip() + "\n", encoding="utf-8")
        item = scenario["graded"][0]
        result = subprocess.run(item["command"], shell=True, cwd=tmp_dir, capture_output=True)
        assert result.returncode != 0


def test_all_claude_harness_scenario_yaml_files_are_valid_yaml() -> None:
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        with path.open(encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        assert doc["schema_version"] == "1.0"
        assert doc["target"] == "claude-harness"
