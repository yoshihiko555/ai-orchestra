"""codex-suggestions hooks のユニットテスト。"""

from __future__ import annotations

import io
import json

import pytest

from tests.module_loader import load_module

codex_write = load_module(
    "check_codex_write", "packages/codex-suggestions/hooks/check-codex-before-write.py"
)
codex_plan = load_module(
    "check_codex_plan", "packages/codex-suggestions/hooks/check-codex-after-plan.py"
)


# ======================================================================
# check-codex-before-write.py
# ======================================================================


class TestValidateInput:
    """validate_input のテスト。"""

    def test_valid(self):
        """正常な入力。"""
        assert codex_write.validate_input("src/main.py", "content") is True

    def test_empty_path(self):
        """空パスは False。"""
        assert codex_write.validate_input("", "content") is False

    def test_path_too_long(self):
        """パスが長すぎる場合は False。"""
        assert codex_write.validate_input("x" * 5000, "content") is False

    def test_content_too_long(self):
        """コンテンツが長すぎる場合は False。"""
        assert codex_write.validate_input("file.py", "x" * 1_100_000) is False

    def test_path_traversal(self):
        """パストラバーサルは False。"""
        assert codex_write.validate_input("../etc/passwd", "content") is False
        assert codex_write.validate_input("src/../../secret", "content") is False


class TestShouldSuggestCodex:
    """should_suggest_codex のテスト。"""

    def test_simple_edit_skipped(self):
        """SIMPLE_EDIT_PATTERNS のファイルはスキップ。"""
        assert codex_write.should_suggest_codex("README.md")[0] is False
        assert codex_write.should_suggest_codex("CHANGELOG.md")[0] is False
        assert codex_write.should_suggest_codex(".gitignore")[0] is False
        assert codex_write.should_suggest_codex("requirements.txt")[0] is False

    def test_design_file_path(self):
        """DESIGN.md は設計文書として提案する。"""
        result, reason = codex_write.should_suggest_codex("DESIGN.md")
        assert result is True
        assert "DESIGN.md" in reason

    @pytest.mark.parametrize(
        "file_path",
        [
            "core/engine.py",
            "settings/config.py",
            "models/user.py",
            "src/new_module.py",
            "tools/base_service.py",
            "tools/abstract_factory.py",
            "docs/designer_notes.md",
            "docs/architecture_notes.md",
            "docs/MY_DESIGN.md",
            "docs/DESIGN.md.backup",
            "docs/schema_notes.json",
            "docs/interfaces_backup/types.ts",
            "docs/contracts_backup/types.ts",
        ],
    )
    def test_generic_or_partial_path_does_not_suggest(self, file_path):
        """汎用語や設計名の部分一致だけでは提案しない。"""
        assert codex_write.should_suggest_codex(file_path)[0] is False

    @pytest.mark.parametrize(
        "file_path",
        [
            "docs/DESIGN.md",
            "docs/ARCHITECTURE.md",
            "docs/design/overview.md",
            "docs/architecture/overview.md",
            "schema/user.json",
            "schemas/user.json",
            "interfaces/engine.ts",
            "contracts/service.py",
        ],
    )
    def test_exact_design_path_suggests(self, file_path):
        """設計文書の basename または設計用パス構成要素が一致すれば提案する。"""
        should_suggest, reason = codex_write.should_suggest_codex(file_path)
        assert should_suggest is True
        assert reason

    def test_architecture_in_path(self):
        """architecture を含むパスは True。"""
        result, reason = codex_write.should_suggest_codex("docs/architecture/overview.md")
        assert result is True

    @pytest.mark.parametrize("file_path", ["normal.py", "src/new_module.py"])
    @pytest.mark.parametrize("length", [250, 600, 100_000])
    def test_content_length_alone_does_not_suggest(self, file_path, length):
        """src 配下でも本文の長さだけでは提案しない。"""
        assert codex_write.should_suggest_codex(file_path, "x" * length)[0] is False

    def test_maximum_length_newline_only_content_does_not_suggest(self):
        """本文上限まで改行だけでも、契約宣言と誤判定せず処理できる。"""
        content = "\n" * codex_write.MAX_CONTENT_LENGTH
        assert codex_write.validate_input("src/engine.py", content) is True
        assert codex_write.should_suggest_codex(
            "src/engine.py", content, tool_name="Edit", old_content=content
        ) == (False, "")

    @pytest.mark.parametrize(
        "content",
        [
            "class MyClass:\n    pass",
            "@dataclass\nclass User:\n    name: str",
            "class User(TypedDict):\n    name: str",
            "from abc import ABC, abstractmethod",
            "from typing import Protocol",
            "def __init__(self):\n    pass",
            "# interface Engine {}",
            "// abstract class Engine {}",
            'description = "interface Engine {}"',
            "class Engine(ProtocolFactory):\n    pass",
            "class Engine(ABCFactory):\n    pass",
        ],
    )
    def test_non_declaration_content_does_not_suggest(self, content):
        """通常クラス・import・コメント・文字列中の設計語だけでは提案しない。"""
        assert codex_write.should_suggest_codex("src/engine.py", content)[0] is False

    @pytest.mark.parametrize(
        "file_path", ["src/engine.ts", "src/engine.tsx", "src/engine.js", "src/engine.jsx"]
    )
    @pytest.mark.parametrize(
        "content",
        ["interface Engine { run(): void; }", "export interface ButtonProps { label: string; }"],
    )
    def test_interface_addition_or_removal_in_normal_source_does_not_suggest(
        self, file_path, content
    ):
        """通常 src の interface は名前によらず汎用の契約シグナルにしない。"""
        assert codex_write.should_suggest_codex(
            file_path, content, tool_name="Edit", old_content=""
        ) == (False, "")
        assert codex_write.should_suggest_codex(
            file_path, "", tool_name="Edit", old_content=content
        ) == (False, "")

    @pytest.mark.parametrize("file_path", ["interfaces/engine.ts", "contracts/button.js"])
    @pytest.mark.parametrize(
        "content",
        ["interface Engine { run(): void; }", "export interface ButtonProps { label: string; }"],
    )
    def test_interface_on_design_path_still_suggests(self, file_path, content):
        """interface の内容ではなく設計用パスの条件で提案する。"""
        should_suggest, reason = codex_write.should_suggest_codex(file_path, content)
        assert should_suggest is True
        assert reason

    @pytest.mark.parametrize(
        ("file_path", "content"),
        [
            ("src/engine.ts", "abstract class Engine { abstract run(): void; }"),
            ("src/Engine.java", "public interface Engine { void run(); }"),
            ("src/Engine.java", "public interface Engine<T> { T run(); }"),
            ("src/engine.py", "class Engine(Protocol):\n    def run(self): ..."),
            ("src/engine.py", "class Engine(ABC):\n    pass"),
            ("src/engine.py", "class Engine:\n    @abstractmethod\n    def run(self): ..."),
        ],
    )
    def test_contract_declaration_addition_or_removal_suggests(self, file_path, content):
        """契約宣言の追加と削除をどちらも提案する。"""
        added, add_reason = codex_write.should_suggest_codex(
            file_path, content, tool_name="Edit", old_content=""
        )
        removed, remove_reason = codex_write.should_suggest_codex(
            file_path, "", tool_name="Edit", old_content=content
        )
        assert added is True
        assert add_reason
        assert removed is True
        assert remove_reason

    @pytest.mark.parametrize(
        ("old_content", "content"),
        [
            ("interface Engine { old(): void; }", "interface Engine { run(): void; }"),
            ("abstract class Engine { old() {} }", "abstract class Engine { run() {} }"),
            ("class Engine(Protocol):\n    old: int", "class Engine(Protocol):\n    value: str"),
            ("class Engine(ABC):\n    old: int", "class Engine(ABC):\n    value: str"),
            (
                "    @abstractmethod\n    def old(self): ...",
                "    @abstractmethod\n    def run(self): ...",
            ),
        ],
    )
    def test_unchanged_declaration_with_body_edit_does_not_suggest(self, old_content, content):
        """Edit の前後で宣言が同じなら本文変更だけでは提案しない。"""
        assert (
            codex_write.should_suggest_codex(
                "src/engine.py", content, tool_name="Edit", old_content=old_content
            )[0]
            is False
        )

    def test_java_interface_body_edit_does_not_suggest(self):
        """Java の interface もヘッダが同じなら本文変更を提案しない。"""
        assert codex_write.should_suggest_codex(
            "src/Engine.java",
            "public interface Engine { void run(); }",
            tool_name="Edit",
            old_content="public interface Engine { void stop(); }",
        ) == (False, "")

    def test_contract_declaration_replacement_suggests(self):
        """契約宣言自体が変わる Edit は提案する。"""
        assert (
            codex_write.should_suggest_codex(
                "src/engine.py",
                "class NewEngine(Protocol):\n    pass",
                tool_name="Edit",
                old_content="class OldEngine(Protocol):\n    pass",
            )[0]
            is True
        )

    def test_design_path_suggests_even_when_declaration_is_unchanged(self):
        """設計パスの提案条件は宣言の差分判定とは独立する。"""
        assert (
            codex_write.should_suggest_codex(
                "contracts/engine.py",
                "class Engine(Protocol):\n    value: str",
                tool_name="Edit",
                old_content="class Engine(Protocol):\n    old: int",
            )[0]
            is True
        )

    @pytest.mark.parametrize("file_path", ["design/README.md", "contracts/CHANGELOG.md"])
    def test_simple_edit_exclusion_precedes_design_signals(self, file_path):
        """README・CHANGELOG は設計パスや契約宣言があっても除外する。"""
        assert (
            codex_write.should_suggest_codex(file_path, "class Engine(Protocol):\n    pass")[0]
            is False
        )

    def test_src_file_short_content(self):
        """src/ 内でも短いコンテンツは False。"""
        result, _ = codex_write.should_suggest_codex("src/tiny.py", "pass")
        assert result is False

    def test_plain_file_no_content(self):
        """通常のパスでコンテンツなしは False。"""
        result, _ = codex_write.should_suggest_codex("utils/helper.py")
        assert result is False

    def test_plain_file_short_content(self):
        """通常のパスで短いコンテンツは False。"""
        result, _ = codex_write.should_suggest_codex("utils/helper.py", "x = 1")
        assert result is False


class TestBuildCodexCommand:
    """_build_codex_command のテスト。"""

    def test_defaults(self):
        """デフォルト値でコマンドを構築。

        フォールバック既定値は hook_common の SSOT 定数を参照するため、
        モデル名を literal で固定せず定数と照合する（モデル変更で壊れない）。
        DEFAULT_CODEX_FLAGS は空文字（`--full-auto` は codex exec が未知引数として
        拒否するためフォールバックから除去済み）。空文字定数は `in` が常に真になり
        検証が無意味化するため、ここでは「無効フラグを含まない」ことを確認する。
        """
        # 空文字定数では `in` が常に真になり検証が無意味化するため非空をガード
        assert codex_write.DEFAULT_CODEX_MODEL
        assert codex_write.DEFAULT_CODEX_FLAGS == ""
        result = codex_write._build_codex_command({})
        assert codex_write.DEFAULT_CODEX_MODEL in result
        assert codex_write.DEFAULT_CODEX_SANDBOX_ANALYSIS in result
        assert "--full-auto" not in result
        assert "  " not in result
        assert "< /dev/null" in result

    def test_custom_config(self):
        """カスタム設定でコマンドを構築。"""
        config = {
            "codex": {
                "model": "o3-pro",
                "sandbox": {"analysis": "workspace-write"},
                "flags": "--quiet",
            }
        }
        result = codex_write._build_codex_command(config)
        assert "o3-pro" in result
        assert "workspace-write" in result
        assert "--quiet" in result


class TestCodexWriteMain:
    """check-codex-before-write main() のテスト。"""

    def test_codex_disabled_exits(self, monkeypatch, capsys):
        """Codex 無効時は提案なしで exit(0)。"""
        monkeypatch.setattr(codex_write, "has_project_config", lambda *a, **kw: True)
        monkeypatch.setattr(codex_write, "is_cli_enabled", lambda tool, config, **kw: False)
        monkeypatch.setattr(codex_write, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": "DESIGN.md", "content": "設計内容"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_write.main()

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_invalid_input_exits(self, monkeypatch):
        """不正な入力は exit(0)。"""
        monkeypatch.setattr(codex_write, "has_project_config", lambda *a, **kw: True)
        monkeypatch.setattr(codex_write, "is_cli_enabled", lambda *a, **kw: True)
        monkeypatch.setattr(codex_write, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": "../etc/passwd", "content": "hack"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_write.main()

    def test_suggestion_output(self, monkeypatch, capsys):
        """提案がある場合、hookSpecificOutput を出力。"""
        monkeypatch.setattr(codex_write, "has_project_config", lambda *a, **kw: True)
        monkeypatch.setattr(codex_write, "is_cli_enabled", lambda *a, **kw: True)
        monkeypatch.setattr(codex_write, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "Write",
                        "tool_input": {
                            "file_path": "contracts/engine.py",
                            "content": "class Engine(Protocol): pass",
                        },
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_write.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Codex Suggestion" in output["hookSpecificOutput"]["additionalContext"]


# ======================================================================
# check-codex-after-plan.py
# ======================================================================


class TestIsPlanAgentTask:
    """is_plan_agent_task のテスト。"""

    def test_planner_subagent(self):
        """subagent_type=planner は True。"""
        assert codex_plan.is_plan_agent_task({"subagent_type": "planner"}) is True

    def test_plan_subagent(self):
        """subagent_type=plan は True。"""
        assert codex_plan.is_plan_agent_task({"subagent_type": "plan"}) is True

    def test_case_insensitive(self):
        """大文字小文字を区別しない。"""
        assert codex_plan.is_plan_agent_task({"subagent_type": "Planner"}) is True

    def test_non_plan_agent(self):
        """plan 以外は False（発火条件は subagent_type のみ、Issue #452）。"""
        assert codex_plan.is_plan_agent_task({"subagent_type": "researcher"}) is False

    def test_plan_keyword_in_prompt_japanese_no_longer_matches(self):
        """日本語の計画キーワードが prompt に含まれていても、subagent_type が
        plan/planner でなければ False（Issue #452: prompt 部分一致は
        "Plans.md" 等で誤発火するため廃止。暫定仕様は EV-17 参照）。
        """
        assert (
            codex_plan.is_plan_agent_task({"subagent_type": "other", "prompt": "実装計画を立てて"})
            is False
        )
        assert (
            codex_plan.is_plan_agent_task({"subagent_type": "other", "prompt": "プランを作成"})
            is False
        )
        assert (
            codex_plan.is_plan_agent_task(
                {"subagent_type": "other", "prompt": "設計計画をまとめて"}
            )
            is False
        )
        assert (
            codex_plan.is_plan_agent_task({"subagent_type": "other", "prompt": "プランを考えて"})
            is False
        )

    def test_plan_keyword_in_prompt_english_no_longer_matches(self):
        """英語の plan キーワードが prompt に含まれていても False（Issue #452）。"""
        assert (
            codex_plan.is_plan_agent_task(
                {"subagent_type": "other", "prompt": "create implementation plan"}
            )
            is False
        )

    def test_no_plan_keywords(self):
        """キーワードなしは False。"""
        assert (
            codex_plan.is_plan_agent_task({"subagent_type": "other", "prompt": "implement feature"})
            is False
        )


class TestCodexPlanMain:
    """check-codex-after-plan main() のテスト。"""

    def test_non_agent_tool_exits(self, monkeypatch):
        """Agent/Task 以外は exit(0)。"""
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
        with pytest.raises(SystemExit, match="0"):
            codex_plan.main()

    def test_codex_disabled_exits(self, monkeypatch):
        """Codex 無効時は exit(0)。"""
        monkeypatch.setattr(codex_plan, "is_cli_enabled", lambda *a, **kw: False)
        monkeypatch.setattr(codex_plan, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "Agent",
                        "tool_input": {"subagent_type": "planner"},
                        "tool_response": "plan done",
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_plan.main()

    def test_successful_plan_outputs_suggestion(self, monkeypatch, capsys):
        """正常なプラン完了後に提案を出力。"""
        monkeypatch.setattr(codex_plan, "has_project_config", lambda *a, **kw: True)
        monkeypatch.setattr(codex_plan, "is_cli_enabled", lambda *a, **kw: True)
        monkeypatch.setattr(codex_plan, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "Agent",
                        "tool_input": {"subagent_type": "planner"},
                        "tool_response": "Plan created with 5 phases",
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            codex_plan.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Codex Review Suggestion" in output["hookSpecificOutput"]["additionalContext"]
