"""claude-harness suite 拡充（ADR-20260817-052）のシナリオ・fixture テスト。

- assert-python-type-hints.py / assert-python-conventions.py の pass/fail 両方向
- assert-effective-config.py の pass/fail/tamper 両方向（train=flat 形式・holdout=keyed 形式）
- assert-function-behavior.py の pass/fail 両方向（正常実装 pass・SystemExit(0)/os._exit(0)
  による早期終了 fail・1ケースのみ不正 fail・ケース隔離検証、PR #381 第4巡レビュー対応）
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
        "implement-username-validator-holdout",
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
        fixture.main(["--file", "ok.py", "--function", "slugify"])

    def test_fails_when_parameter_annotation_missing(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title) -> str:\n    return title.lower()\n", encoding="utf-8"
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="missing a type annotation"):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_fails_when_return_annotation_missing(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title: str):\n    return title.lower()\n", encoding="utf-8"
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="missing a return type annotation"):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_self_and_cls_are_exempt(self, tmp_path: Path, monkeypatch) -> None:
        """`self`/`cls` params stay exempt from the annotation requirement even for a class
        method sitting alongside the module-level required function (PR #381 review (P1) moved
        the *existence* check to module-level defs only; the per-function hint-completeness
        loop below still walks every function, including class methods, so the self/cls
        exemption must keep working there)."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "ok.py").write_text(
            "def bar(name: str) -> str:\n"
            '    """Doc."""\n'
            "    return name\n\n\n"
            "class Foo:\n"
            "    def method(self, name: str) -> str:\n"
            '        """Doc."""\n'
            "        return name\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "ok.py", "--function", "bar"])

    def test_fails_when_required_function_is_not_a_def(self, tmp_path: Path, monkeypatch) -> None:
        """PR #381 review (P1): a candidate satisfying behavior via `slugify = lambda ...`
        instead of `def slugify(...)` must not vacuously pass the type-hints check just because
        `_iter_functions_with_context()` finds nothing to complain about."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "slugify = lambda title: title.lower()\n", encoding="utf-8"
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(
            AssertionError,
            match="required function 'slugify' is not the sole module-level def binding",
        ):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_fails_when_required_function_is_only_a_class_method_or_nested_def(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review (P1): a decoy class method (or nested def) named after the required
        function must not satisfy the existence check while the actual module attribute used at
        import time is an unannotated `slugify = lambda ...` -- `defined_names` previously came
        from `ast.walk()`, which finds class methods and nested defs alongside module-level
        ones, so such a decoy could vacuously pass."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "class Decoy:\n"
            "    def slugify(self, title: str) -> str:\n"
            '        """Doc."""\n'
            "        return title\n\n\n"
            "slugify = lambda title: title.lower()\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(
            AssertionError,
            match="required function 'slugify' is not the sole module-level def binding",
        ):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_fails_when_def_is_shadowed_by_later_lambda_reassignment(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review, round 3 (P1): a real `def slugify(title: str) -> str: ...` must not
        satisfy the existence check if a later top-level statement immediately rebinds the name
        to an unannotated `lambda` -- the behavior oracle runs the *final* module binding, not
        whichever `def` happened to appear first."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title: str) -> str:\n"
            '    """Doc."""\n'
            "    return title.lower()\n\n\n"
            "slugify = lambda title: title.lower()\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(
            AssertionError,
            match="required function 'slugify' is not the sole module-level def binding",
        ):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_passes_when_def_is_reassigned_to_itself_or_wrapped_by_a_later_def(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The final-binding check must not falsely reject a legitimate module structure where
        the required name's last top-level statement is still a `def` (e.g. a decorator-less
        re-`def` after an unrelated import), not every `def` followed by *any* later statement."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "ok.py").write_text(
            "import re\n\n\n"
            "def slugify(title: str) -> str:\n"
            '    """Doc."""\n'
            '    return re.sub(r"\\s+", "-", title.lower()).strip("-")\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "ok.py", "--function", "slugify"])

    def test_self_named_parameter_on_a_free_function_still_requires_annotation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review, round 3 (P1): naming a module-level (non-method) function's real
        parameter `self` must not exempt it from the annotation requirement -- the self/cls
        carve-out is for actual class methods only, identified by the function's immediate AST
        parent being a `ClassDef`, not by parameter name alone."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            'def slugify(self) -> str:\n    """Doc."""\n    return self.lower()\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="parameter 'self' is missing a type annotation"):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_fails_when_def_is_shadowed_by_a_branch_conditional_reassignment(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review, round 4 (P1): a real `def slugify(title: str) -> str: ...` must not
        satisfy the existence check if a later top-level `if` branch rebinds the name to an
        unannotated `lambda` -- `if`/`try`/`with` blocks are not separate scopes in Python, so a
        rebinding one branch deep still overrides the module-level name just as a straight-line
        reassignment would. The round-3 fix only scanned direct `tree.body` statements and missed
        this one construct deep; the round-4 fix walks the whole module AST instead of tracing
        control flow, so it should catch this without needing an `if`-specific carve-out."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title: str) -> str:\n"
            '    """Doc."""\n'
            "    return title.lower()\n\n\n"
            "if True:\n"
            "    slugify = lambda title: title.lower()\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(
            AssertionError, match="required function 'slugify' is not the sole module-level"
        ):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_fails_when_only_binding_is_a_for_loop_target(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The whole-module walk (PR #381 review, round 4) must also catch a `for` loop target
        rebinding the required name, not just `if`/`try`/`with` blocks and direct assignment."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title: str) -> str:\n"
            '    """Doc."""\n'
            "    return title.lower()\n\n\n"
            "for slugify in [slugify]:\n"
            "    pass\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(
            AssertionError, match="required function 'slugify' is not the sole module-level"
        ):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_fails_when_only_binding_is_a_match_case_capture(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Advisor follow-up (post round-4): a `match`/`case` capture pattern
        (`case slugify:`) rebinds the module-level name via `ast.MatchAs`, which the whole-module
        walk must also treat as a disqualifying binding, not just the constructs enumerated in
        the initial round-4 pass."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title: str) -> str:\n"
            '    """Doc."""\n'
            "    return title.lower()\n\n\n"
            "match 1:\n"
            "    case slugify:\n"
            "        pass\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(
            AssertionError, match="required function 'slugify' is not the sole module-level"
        ):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_fails_when_only_binding_is_a_type_alias_statement(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Advisor follow-up (post round-4): a `type slugify = ...` statement (Python 3.12+)
        rebinds the module-level name via `ast.TypeAlias`, which the whole-module walk must also
        treat as a disqualifying binding."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "def slugify(title: str) -> str:\n"
            '    """Doc."""\n'
            "    return title.lower()\n\n\n"
            "type slugify = str\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(
            AssertionError, match="required function 'slugify' is not the sole module-level"
        ):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_staticmethod_self_and_cls_are_not_exempt(self, tmp_path: Path, monkeypatch) -> None:
        """PR #381 review, round 4 (P2): unlike an ordinary instance method or `@classmethod`,
        `@staticmethod` never receives an implicitly-bound `self`/`cls` -- naming a static
        method's real parameter `self` must not exempt it from the annotation requirement."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "bad.py").write_text(
            "class Helper:\n"
            "    @staticmethod\n"
            "    def transform(self) -> str:\n"
            '        """Doc."""\n'
            "        return self.lower()\n\n\n"
            "def slugify(title: str) -> str:\n"
            '    """Doc."""\n'
            "    return Helper.transform(title)\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="parameter 'self' is missing a type annotation"):
            fixture.main(["--file", "bad.py", "--function", "slugify"])

    def test_classmethod_cls_is_still_exempt(self, tmp_path: Path, monkeypatch) -> None:
        """The round-4 `@staticmethod` carve-out must not overreach into `@classmethod`, which
        genuinely does receive an implicitly-bound `cls`."""
        fixture = _fixture("assert-python-type-hints.py")
        (tmp_path / "ok.py").write_text(
            "class Helper:\n"
            "    @classmethod\n"
            "    def transform(cls, title: str) -> str:\n"
            '        """Doc."""\n'
            "        return title.lower()\n\n\n"
            "def slugify(title: str) -> str:\n"
            '    """Doc."""\n'
            "    return Helper.transform(title)\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "ok.py", "--function", "slugify"])


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

    def test_snake_case_still_checks_for_loop_targets(self, tmp_path: Path, monkeypatch) -> None:
        """PR #381 review (P2): `coding-principles.md` defines no loop-variable carve-out for
        the "meaningful variable names" rule, so a single-character `for` target must still be
        flagged like any other single-character variable."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def total(values):\n    result = 0\n    for x in values:\n        result += x\n    return result\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="single character"):
            fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_snake_case_allows_module_level_upper_snake_case_constant(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review (P2): `coding-principles.md` requires UPPER_SNAKE_CASE for
        constants, so a module-level constant like `_WHITESPACE_RE` must not be flagged as a
        snake_case violation."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "import re\n\n"
            '_WHITESPACE_RE = re.compile(r"\\s+")\n\n\n'
            "def collapse(text):\n"
            '    return _WHITESPACE_RE.sub("-", text)\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_snake_case_still_fails_on_upper_snake_case_local_variable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The UPPER_SNAKE_CASE carve-out only applies to module-top-level assignments; a
        same-cased name assigned inside a function body is a regular local variable."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def f():\n    LOCAL_VALUE = 1\n    return LOCAL_VALUE\n", encoding="utf-8"
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="not snake_case"):
            fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_snake_case_still_flags_local_variable_shadowing_module_constant(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review (P2): a module-level `VALUE = 1` must not permanently mark the bare
        name "VALUE" as an already-checked constant; a later, unrelated function-local
        `VALUE = 2` is a different scope and must still be flagged."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "VALUE = 1\n\n\ndef f():\n    VALUE = 2\n    return VALUE\n", encoding="utf-8"
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="not snake_case"):
            fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_snake_case_allows_module_level_tuple_unpacked_constants(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review, round 3 (P2): `WHITESPACE_RE, HYPHEN_RE = re.compile(...),
        re.compile(...)` is a legitimate way to define two module constants in one statement.
        Only a bare `ast.Name` assign target was previously recognized as a module-level
        constant, so both names in a tuple-unpack were misreported as `snake_case` violations."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "import re\n\n"
            'WHITESPACE_RE, HYPHEN_RE = re.compile(r"\\s+"), re.compile(r"-+")\n\n\n'
            "def collapse(text):\n"
            '    return HYPHEN_RE.sub("-", WHITESPACE_RE.sub("-", text))\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_snake_case_checks_vararg_and_kwarg_parameter_names(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review, round 4 (P2): the parameter scan previously only walked
        `posonlyargs`/`args`/`kwonlyargs`, so a non-snake_case `*args`/`**kwargs` name (e.g.
        `*X`) never tripped the check even though `coding-principles.md`'s naming rule draws no
        exception for variadic parameters."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text("def f(*X, **Y):\n    return X, Y\n", encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="parameter 'X' is not a meaningful snake_case"):
            fixture.main(["--file", "f.py", "--require-snake-case"])

    def test_snake_case_passes_with_snake_case_vararg_and_kwarg(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def f(*args, **kwargs):\n    return args, kwargs\n", encoding="utf-8"
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

    def test_max_nesting_depth_counts_elif_chain_as_one_level(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review (P2): `ast` represents an `elif` chain as nested `If` nodes inside
        each other's `orelse`, indistinguishable in node type from an `if` written inside an
        explicit `else:` block. A shallow, functionally correct `if/elif/elif/else` chain must
        not be penalized as if each `elif` added a further nesting level."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def validate(name):\n"
            "    if not name:\n"
            "        return False\n"
            "    elif len(name) < 3:\n"
            "        return False\n"
            "    elif len(name) > 32:\n"
            "        return False\n"
            "    else:\n"
            "        return True\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--file", "f.py", "--max-nesting-depth", "1"])

    def test_max_nesting_depth_still_penalizes_if_nested_inside_explicit_else(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The `elif`-chain carve-out is scoped to true `elif` (same indentation column as the
        `if` it continues); an `if` written inside an explicit `else:` block is genuinely one
        level deeper and must still count as extra nesting."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def validate(name):\n"
            "    if not name:\n"
            "        return False\n"
            "    else:\n"
            "        if len(name) < 3:\n"
            "            return False\n"
            "    return True\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="nesting depth"):
            fixture.main(["--file", "f.py", "--max-nesting-depth", "1"])

    def test_max_nesting_depth_counts_nested_match_statements(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review, round 3 (P2): `_NESTING_NODES` previously omitted `ast.Match`, so a
        candidate could dodge the nesting-depth check entirely by writing an equivalently deep
        chain of nested `match` statements instead of sequential `if`s."""
        fixture = _fixture("assert-python-conventions.py")
        (tmp_path / "f.py").write_text(
            "def validate(name):\n"
            "    match name:\n"
            "        case str():\n"
            "            match len(name):\n"
            "                case n if n >= 3:\n"
            "                    match n:\n"
            "                        case m if m <= 32:\n"
            "                            return True\n"
            "    return False\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="nesting depth"):
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

    def test_passes_when_source_file_has_a_redundant_leading_dot_slash(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PR #381 review, round 3 (P2): the prompt only requires *some* correct relative path,
        so `./sandbox/config/agent-routing/cli-tools.local.yaml` names the same file as
        `sandbox/config/agent-routing/cli-tools.local.yaml` and must hash identically instead of
        being penalized for the redundant `./` prefix."""
        _write_train_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"value": "harness-local-override-model", '
            '"source_file": "./sandbox/config/agent-routing/cli-tools.local.yaml"}',
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
                "source_file",
            ]
        )

    def test_fails_when_source_file_has_a_trailing_slash(self, tmp_path: Path, monkeypatch) -> None:
        """PR #381 review, round 4 (P2): `posixpath.normpath()` maps a trailing-slash path onto
        the same normalized string as the real file (e.g.
        `sandbox/config/agent-routing/cli-tools.local.yaml/`), so without an explicit rejection
        this nonexistent, directory-shaped path would hash-match the real file's expected value.
        A trailing slash must be rejected before normalization, not silently collapsed away."""
        _write_train_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"value": "harness-local-override-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml/"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        with pytest.raises(AssertionError, match="trailing slash"):
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
                    "source_file",
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

    def test_fails_on_unexpected_extra_key(self, tmp_path: Path, monkeypatch) -> None:
        """PR #381 review (P2): an answer with a correct value/source_file plus an extra key
        must not pass just because the graded fields happen to be correct."""
        _write_train_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"value": "harness-local-override-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml", '
            '"unexpected": "accepted"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        with pytest.raises(AssertionError, match="key set"):
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

    def test_fails_on_missing_key(self, tmp_path: Path, monkeypatch) -> None:
        _write_train_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"value": "harness-local-override-model"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        with pytest.raises(AssertionError, match="key set"):
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

    def test_fails_on_unexpected_top_level_key(self, tmp_path: Path, monkeypatch) -> None:
        """PR #381 review (P2): the prompt specifies exactly the registered key_paths as the
        answer's top-level keys; an extra top-level key must fail even when both registered
        entries are otherwise correct."""
        _write_holdout_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"codex.model": {"value": "harness-nested-local-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml"}, '
            '"codex.sandbox.analysis": {"value": "read-only", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.yaml"}, '
            '"unexpected.key": {"value": "x", "source_file": "y"}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        with pytest.raises(AssertionError, match="top-level"):
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

    def test_fails_on_unexpected_entry_key(self, tmp_path: Path, monkeypatch) -> None:
        """An extra key inside a per-key_path entry (not just at the top level) must also
        fail."""
        _write_holdout_config_pair(tmp_path)
        answer = tmp_path / ".meta-harness" / "config-answer.json"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text(
            '{"codex.model": {"value": "harness-nested-local-model", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.local.yaml", '
            '"unexpected": "accepted"}, '
            '"codex.sandbox.analysis": {"value": "read-only", '
            '"source_file": "sandbox/config/agent-routing/cli-tools.yaml"}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture = _fixture("assert-effective-config.py")
        with pytest.raises(AssertionError, match="entry"):
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


# ---------------------------------------------------------------------------
# assert-function-behavior.py
# ---------------------------------------------------------------------------


class TestAssertFunctionBehavior:
    """PR #381 review, round 4 (P1): the behavior oracle must isolate each case in its own
    subprocess so a candidate function calling `SystemExit(0)`/`os._exit(0)` cannot short-circuit
    the whole check by terminating the shared oracle process with exit 0 (see the fixture's
    module docstring for the full design rationale)."""

    _OK_MODULE = 'def slugify(title: str) -> str:\n    return title.lower().replace(" ", "-")\n'
    _SYSTEM_EXIT_MODULE = (
        "def slugify(title: str) -> str:\n    raise SystemExit(0)\n    return title\n"
    )
    _OS_EXIT_MODULE = (
        "import os\n\n\ndef slugify(title: str) -> str:\n    os._exit(0)\n    return title\n"
    )
    _ONE_CASE_WRONG_MODULE = (
        "def slugify(title: str) -> str:\n"
        '    if title == "Hello World":\n'
        '        return "wrong"\n'
        '    return title.lower().replace(" ", "-")\n'
    )
    _CASES = (
        '[{"id": "basic", "args": ["Hello World"], "expected": "hello-world"}, '
        '{"id": "already-lower", "args": ["already-lower"], "expected": "already-lower"}]'
    )

    def test_passes_with_a_correct_implementation(self, tmp_path: Path, monkeypatch) -> None:
        fixture = _fixture("assert-function-behavior.py")
        (tmp_path / "mod.py").write_text(self._OK_MODULE, encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--module", "mod.py", "--function", "slugify", "--cases", self._CASES])

    def test_fails_when_function_calls_system_exit_zero(self, tmp_path: Path, monkeypatch) -> None:
        """A `raise SystemExit(0)` inside the candidate function must not vacuously pass just
        because the isolated subprocess it runs in also exits 0 -- the print of the result
        payload never executes, so there is nothing on stdout to satisfy this case."""
        fixture = _fixture("assert-function-behavior.py")
        (tmp_path / "mod.py").write_text(self._SYSTEM_EXIT_MODULE, encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="printed nothing"):
            fixture.main(["--module", "mod.py", "--function", "slugify", "--cases", self._CASES])

    def test_fails_when_function_calls_os_exit_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Same defense as `SystemExit(0)`, but for `os._exit(0)`, which bypasses even Python's
        own exception-based unwinding."""
        fixture = _fixture("assert-function-behavior.py")
        (tmp_path / "mod.py").write_text(self._OS_EXIT_MODULE, encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="printed nothing"):
            fixture.main(["--module", "mod.py", "--function", "slugify", "--cases", self._CASES])

    def test_fails_when_only_one_case_is_wrong(self, tmp_path: Path, monkeypatch) -> None:
        """A single wrong case among several correct ones must still fail the whole check, and
        the failure message must name the offending case."""
        fixture = _fixture("assert-function-behavior.py")
        (tmp_path / "mod.py").write_text(self._ONE_CASE_WRONG_MODULE, encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="basic"):
            fixture.main(["--module", "mod.py", "--function", "slugify", "--cases", self._CASES])

    def test_reads_cases_from_a_project_root_relative_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fixture = _fixture("assert-function-behavior.py")
        (tmp_path / "mod.py").write_text(self._OK_MODULE, encoding="utf-8")
        (tmp_path / "cases.json").write_text(self._CASES, encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        fixture.main(["--module", "mod.py", "--function", "slugify", "--cases", "cases.json"])

    def test_isolates_an_early_exit_in_one_case_from_a_later_correct_case(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Each case must run in its own subprocess: an early exit while evaluating the first
        case must not prevent a correct implementation's later case from ever being evaluated
        (and passing)."""
        fixture = _fixture("assert-function-behavior.py")
        module = (
            "def slugify(title: str) -> str:\n"
            '    if title == "Hello World":\n'
            "        raise SystemExit(0)\n"
            '    return title.lower().replace(" ", "-")\n'
        )
        (tmp_path / "mod.py").write_text(module, encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError) as excinfo:
            fixture.main(["--module", "mod.py", "--function", "slugify", "--cases", self._CASES])
        assert "basic" in str(excinfo.value)
        assert "already-lower" not in str(excinfo.value)

    def test_fails_when_bool_expected_is_satisfied_by_an_int(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Advisor follow-up (post round-4): comparing with Python `!=` lets `1 == True` and
        `0 == False` conflate an int result with a bool `expected`, silently passing a candidate
        that returns `1`/`0` instead of the required `True`/`False` -- a regression from the old
        oracle's strict `is True`/`is False` identity check. Comparison must be on the canonical
        JSON serialization instead, which distinguishes `true` from `1`."""
        fixture = _fixture("assert-function-behavior.py")
        module = "def validate_username(name: str) -> int:\n    return 1 if name else 0\n"
        (tmp_path / "mod.py").write_text(module, encoding="utf-8")
        cases = '[{"id": "truthy", "args": ["alice"], "expected": true}]'
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))
        with pytest.raises(AssertionError, match="truthy"):
            fixture.main(
                ["--module", "mod.py", "--function", "validate_username", "--cases", cases]
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
        assert (base_sha, local_sha) in fixture._KNOWN_ANSWER_HASHES, (
            f"{scenario_id}'s setup: bytes (base sha256={base_sha}, local sha256={local_sha}) "
            "are not a key in assert-effective-config.py's _KNOWN_ANSWER_HASHES; the setup: "
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


def test_implement_username_validator_holdout_graded_commands_pass_against_known_good_solution(
    tmp_path: Path,
) -> None:
    _run_graded_commands(
        "implement-username-validator-holdout",
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
