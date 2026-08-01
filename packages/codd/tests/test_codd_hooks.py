"""codd hooks（scan-postedit / validate-precommit）のテスト（Issue #95）。

評価セット対応: docs/evaluation/codd.md §4.2 EV-59〜EV-65。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from tests.module_loader import REPO_ROOT, load_module

HOOKS_DIR = REPO_ROOT / "packages" / "codd" / "hooks"
CORE_HOOKS_DIR = REPO_ROOT / "packages" / "core" / "hooks"

# codd hooks は `hook_common` を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む。
# 環境変数の有無に関わらずモジュール import が解決できるよう、直接 sys.path にも足す
# （test_plan_gate.py と同じパターン）。
if str(CORE_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_HOOKS_DIR))

cc = load_module("codd_common", "packages/codd/lib/codd_common.py")
cli = load_module("codd_cli", "packages/codd/scripts/codd.py")
scan_hook = load_module("codd_scan_postedit", "packages/codd/hooks/codd-scan-postedit.py")
validate_hook = load_module(
    "codd_validate_precommit", "packages/codd/hooks/codd-validate-precommit.py"
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _run_hook(
    script_name: str, payload: dict[str, Any], project_dir: Path
) -> subprocess.CompletedProcess[str]:
    env = {**__import__("os").environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / script_name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=str(project_dir),
        check=False,
    )


def _run_hook_raw_stdin(
    script_name: str, raw_input: str, project_dir: Path
) -> subprocess.CompletedProcess[str]:
    env = {**__import__("os").environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / script_name)],
        input=raw_input,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(project_dir),
        check=False,
    )


def _config_path(project_dir: Path) -> Path:
    return project_dir / ".claude" / "config" / "codd" / "codd.yaml"


def _graph_path(project_dir: Path) -> Path:
    return project_dir / ".claude" / "codd" / "graph.jsonl"


def _codd_config_dict(
    *,
    enabled: bool = True,
    scope_include: list[str] | None = None,
    scope_exclude: list[str] | None = None,
    scan_on_edit: bool = False,
    validate_on_commit: str = "warn",
    include_hooks_section: bool = True,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": enabled,
        "scope": {
            "include": scope_include if scope_include is not None else ["docs/**/*.md"],
            "exclude": scope_exclude if scope_exclude is not None else [],
        },
        # unknown kind/relation 検査が誤って error を混入させないよう、
        # validate 系テストで使う語彙は明示しておく（test_codd_cli.py の BASE_CONFIG と同じ発想）。
        "kinds": ["requirement", "design", "adr", "plan", "rule", "instruction"],
        "relations": ["derives_from", "refines", "implements", "references", "supersedes"],
        "roots": ["requirement", "instruction"],
    }
    if include_hooks_section:
        data["hooks"] = {"scan_on_edit": scan_on_edit, "validate_on_commit": validate_on_commit}
    return data


def _write_codd_config(project_dir: Path, data: dict[str, Any]) -> Path:
    path = _config_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _write_raw_codd_config(project_dir: Path, text: str) -> Path:
    path = _config_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write(project_dir: Path, rel: str, content: str) -> Path:
    path = project_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _git_init(project_dir: Path) -> None:
    """`project_dir` を git working tree として初期化する（index スナップショット検証用、Issue #338）。

    validate-precommit hook は `git commit` 実行前に **index** の内容を検証するため
    （working tree ではない）、validate hook の e2e テストは実 git リポジトリを必要とする。
    実 commit は行わない（`write-tree` / `checkout-index` は index のみを参照するため不要）。
    """
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True, capture_output=True)


def _git_add_all(project_dir: Path) -> None:
    """`project_dir` 配下の全ファイルを index にステージする（実 commit はしない、Issue #338）。"""
    subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True, capture_output=True)


def _doc(node_id: str, kind: str = "design", deps: list[tuple[str, str]] | None = None) -> str:
    lines = ["---", "codd:", f"  node_id: {node_id}", f"  kind: {kind}", "  status: draft"]
    if deps:
        lines.append("  depends_on:")
        for dep_id, relation in deps:
            lines.append(f"    - id: {dep_id}")
            lines.append(f"      relation: {relation}")
    lines += ["---", "", "# 本文", ""]
    return "\n".join(lines)


# doc: validate すると dangling error（未知の参照先）を1件生成する。
_DANGLING_DOC = _doc("design:d", deps=[("req:missing", "derives_from")])
# doc: validate してもエラー無し。
_CLEAN_DOC = _doc("design:clean")


def _read_graph_node_ids(project_dir: Path) -> set[str]:
    graph_path = _graph_path(project_dir)
    if not graph_path.is_file():
        return set()
    lines = graph_path.read_text(encoding="utf-8").splitlines()
    return {json.loads(line)["node_id"] for line in lines if line.strip()}


# ---------------------------------------------------------------------------
# scan hook: fail-safe（EV-61 / EV-60）
# ---------------------------------------------------------------------------


class TestScanHookFailSafe:
    def test_noop_when_codd_not_initialized(self, tmp_path: Path) -> None:
        """EV-61: `.claude/config/codd/codd.yaml` 不在なら exit 0・無出力・graph 変化なし。"""
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert not _graph_path(tmp_path).is_file()

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        """EV-61: `enabled: false` なら scan_on_edit: true でも exit 0 no-op。"""
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(enabled=False, scan_on_edit=True))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert not _graph_path(tmp_path).is_file()

    def test_noop_when_hooks_section_missing_defaults_to_false(self, tmp_path: Path) -> None:
        """EV-60: 旧 codd.yaml（`hooks:` セクション無し）はデフォルト false で後方互換。"""
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(include_hooks_section=False))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert not _graph_path(tmp_path).is_file()

    def test_noop_when_file_path_missing(self, tmp_path: Path) -> None:
        """EV-61: tool_input.file_path 欠落は exit 0。"""
        _write_codd_config(tmp_path, _codd_config_dict(scan_on_edit=True))
        payload = {"cwd": str(tmp_path), "tool_name": "Edit", "tool_input": {}}
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_noop_when_stdin_is_invalid_json(self, tmp_path: Path) -> None:
        """EV-61: 不正 JSON stdin は exit 0（read_hook_input の fail-safe）。"""
        _write_codd_config(tmp_path, _codd_config_dict(scan_on_edit=True))
        result = _run_hook_raw_stdin("codd-scan-postedit.py", "not valid json{", tmp_path)
        assert result.returncode == 0

    def test_exception_fallback_when_config_yaml_is_malformed(self, tmp_path: Path) -> None:
        """EV-61: config ロード時の例外も safe_hook_execution で exit 0 に収束する。"""
        _write_raw_codd_config(tmp_path, "enabled: [unterminated\n")
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert "Hook error" in result.stderr


# ---------------------------------------------------------------------------
# scan hook: scope 判定（EV-62）
# ---------------------------------------------------------------------------


class TestScanHookScope:
    def test_scan_runs_and_rebuilds_graph_when_in_scope(self, tmp_path: Path) -> None:
        """EV-62: scope 内ファイル編集で `codd scan` が走り graph.jsonl が再構築される。"""
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(scan_on_edit=True))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert _read_graph_node_ids(tmp_path) == {"design:clean"}

    def test_scan_fast_exits_when_out_of_scope(self, tmp_path: Path) -> None:
        """EV-62: scope 外ファイル編集は fast-exit し graph は作られない。"""
        _write(tmp_path, "notes/other.md", "# not in scope\n")
        _write_codd_config(tmp_path, _codd_config_dict(scan_on_edit=True))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "notes" / "other.md")},
        }
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert not _graph_path(tmp_path).is_file()

    def test_scope_matching_is_consistent_with_scan_cli(self, tmp_path: Path) -> None:
        """EV-62: scope 判定（hook 側 helper）が scan 本体（codd.py）と同じ解釈になる。

        `docs/**/*.md` は 0 階層（`docs/x.md`）にもマッチする（Path.glob と同じ意味論）。
        """
        _write(tmp_path, "docs/x.md", "0-level in scope")
        _write(tmp_path, "guides/y.md", "out of scope")
        config = cc.CoddConfig.from_dict(_codd_config_dict())

        in_scope_target = tmp_path / "docs" / "x.md"
        out_scope_target = tmp_path / "guides" / "y.md"

        assert cc.path_in_scan_scope(tmp_path, in_scope_target, config) is True
        assert cc.path_in_scan_scope(tmp_path, out_scope_target, config) is False

        collected = set(cli.collect_files(tmp_path, config))
        assert in_scope_target in collected
        assert out_scope_target not in collected


# ---------------------------------------------------------------------------
# `[!seq]` 否定文字クラスのパリティ（Medium-3 / codd-review High-1 回帰防止）
#
# hook 側の単一パス判定（`cc.path_in_scan_scope`）と scan 本体（`cli.collect_files`
# / 実際の `Path.glob`）とで、glob の `[!seq]`（否定）の解釈が一致することを確認する。
# ---------------------------------------------------------------------------


class TestNegatedCharClassParity:
    def test_include_negated_bracket_excludes_and_includes_expected_files(
        self, tmp_path: Path
    ) -> None:
        """`docs/[!_]*.md` は `_draft.md` を除外し `final.md` にマッチする（両エンジン一致）。"""
        _write(tmp_path, "docs/_draft.md", "excluded by [!_]")
        _write(tmp_path, "docs/final.md", _CLEAN_DOC)
        config = cc.CoddConfig.from_dict(
            _codd_config_dict(scope_include=["docs/[!_]*.md"], scope_exclude=[])
        )

        draft = tmp_path / "docs" / "_draft.md"
        final = tmp_path / "docs" / "final.md"

        # hook 側 helper（単一パス判定）
        assert cc.path_in_scan_scope(tmp_path, draft, config) is False
        assert cc.path_in_scan_scope(tmp_path, final, config) is True

        # scan 本体（列挙型・Path.glob ベース）
        collected = set(cli.collect_files(tmp_path, config))
        assert draft not in collected
        assert final in collected

        # Python 標準ライブラリの Path.glob 自体の意味論とも一致すること
        globbed = set(tmp_path.glob("docs/[!_]*.md"))
        assert draft not in globbed
        assert final in globbed

    def test_exclude_with_negated_bracket_matches_scan_scope(self, tmp_path: Path) -> None:
        """scope.exclude 側に `[!seq]` を含むケースでも hook 側 helper と scan 本体が一致する。"""
        _write(tmp_path, "docs/keep_a.md", _CLEAN_DOC)
        _write(tmp_path, "docs/skip_b.md", "excluded via exclude [!seq]")
        config = cc.CoddConfig.from_dict(
            _codd_config_dict(
                scope_include=["docs/*.md"],
                scope_exclude=["docs/[!k]*.md"],
            )
        )

        keep = tmp_path / "docs" / "keep_a.md"
        skip = tmp_path / "docs" / "skip_b.md"

        assert cc.path_in_scan_scope(tmp_path, keep, config) is True
        assert cc.path_in_scan_scope(tmp_path, skip, config) is False

        collected = set(cli.collect_files(tmp_path, config))
        assert keep in collected
        assert skip not in collected

    def test_code_scope_combination_matches_scan_code_files(self, tmp_path: Path) -> None:
        """`code_scope.include` との組み合わせでも scope 判定が scan 本体と一致する。

        `docs/` にマッチしないファイルでも `code_scope`（`src/[!_]*.py`）に
        マッチすれば scan 対象になる（doc scope + code_scope の合成判定）。
        """
        _write(tmp_path, "docs/design.md", _CLEAN_DOC)
        _write(tmp_path, "src/mod.py", "# codd:implements design:clean\n")
        _write(tmp_path, "src/_private.py", "# codd:implements design:clean\n")
        data = _codd_config_dict()
        data["code_scope"] = {"include": ["src/[!_]*.py"], "exclude": []}
        config = cc.CoddConfig.from_dict(data)

        public_module = tmp_path / "src" / "mod.py"
        private_module = tmp_path / "src" / "_private.py"

        assert cc.path_in_scan_scope(tmp_path, public_module, config) is True
        assert cc.path_in_scan_scope(tmp_path, private_module, config) is False

        collected_code = set(cli.collect_code_files(tmp_path, config))
        assert public_module in collected_code
        assert private_module not in collected_code


# ---------------------------------------------------------------------------
# scan hook: サブプロセス失敗時の通知（Medium-1 / codd-review）
# ---------------------------------------------------------------------------


class TestScanHookSubprocessFailureNotification:
    def test_scan_subprocess_failure_emits_stderr_notice_and_exits_zero(
        self, tmp_path: Path
    ) -> None:
        """`codd scan` が非ゼロ終了しても hook は exit 0 かつ stderr へ 1 行通知する。

        `/nonexistent/*.py`（絶対パスパターン）は hook 側の単一パス判定
        （`path_in_scan_scope`）では re.error を起こさず fast-exit しないが、実際の
        `codd scan` サブプロセスは `collect_files` の `Path.glob` 呼び出しで
        `NotImplementedError`（"Non-relative patterns are unsupported"）を捕捉した
        `ValueError` により非ゼロ終了する（`main()` の設定エラーハンドラ経由）。
        """
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        config_data = _codd_config_dict(
            scope_include=["docs/**/*.md", "/nonexistent/*.py"], scan_on_edit=True
        )
        _write_codd_config(tmp_path, config_data)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert "[codd] scan がエラー終了しました" in result.stderr
        assert not _graph_path(tmp_path).is_file()


# ---------------------------------------------------------------------------
# validate hook: `git commit` 検出（EV-63、直接 import で regex を検証）
# ---------------------------------------------------------------------------


class TestValidateHookCommitDetection:
    def test_detects_plain_git_commit(self) -> None:
        assert validate_hook._looks_like_git_commit('git commit -m "msg"') is True

    def test_detects_dash_capital_c_global_option(self) -> None:
        assert validate_hook._looks_like_git_commit("git -C /repo commit -m msg") is True

    def test_detects_dash_c_key_value_global_option(self) -> None:
        assert (
            validate_hook._looks_like_git_commit('git -c user.name=x -c user.email=y commit -m "m"')
            is True
        )

    def test_does_not_detect_git_log(self) -> None:
        assert validate_hook._looks_like_git_commit("git log --oneline") is False

    def test_does_not_detect_unrelated_command(self) -> None:
        assert validate_hook._looks_like_git_commit("ls -la") is False


# ---------------------------------------------------------------------------
# validate hook: fail-safe（EV-61 / EV-64）
# ---------------------------------------------------------------------------


class TestValidateHookFailSafe:
    def test_noop_when_codd_not_initialized(self, tmp_path: Path) -> None:
        """EV-61: codd 未初期化なら exit 0 無出力。"""
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_fast_exits_on_non_commit_command_even_with_errors_in_block_mode(
        self, tmp_path: Path
    ) -> None:
        """EV-63: `git commit` を含まないコマンドは、error があっても block 化しない。"""
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "git log --oneline"},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        """EV-61: `enabled: false` なら block モード + error があっても exit 0 no-op。"""
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(enabled=False, validate_on_commit="block"))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0

    def test_exception_fallback_when_validate_on_commit_is_invalid(self, tmp_path: Path) -> None:
        """EV-64: hook 経路の不正値は ValueError → safe_hook_execution で exit 0 に倒れる。"""
        _write(tmp_path, "docs/d.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="bogus"))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert "Hook error" in result.stderr
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# validate hook: warn / block / off モード（EV-63 / EV-64）
# ---------------------------------------------------------------------------


class TestValidateHookModes:
    def test_warn_mode_emits_additional_context_and_exits_zero(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="warn"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "errors=1" in context

    def test_block_mode_blocks_commit_and_exits_two(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2
        assert result.stdout == ""
        assert "ブロック" in result.stderr

    def test_off_mode_string_is_noop_even_with_errors(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="off"))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_off_mode_bare_yaml_off_is_noop_even_with_errors(self, tmp_path: Path) -> None:
        """EV-64: bare `off`（YAML 1.1 で boolean False）も `"off"` と同じ扱いになる。"""
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_raw_codd_config(
            tmp_path,
            "enabled: true\n"
            "scope:\n"
            '  include: ["docs/**/*.md"]\n'
            "  exclude: []\n"
            "hooks:\n"
            "  scan_on_edit: false\n"
            "  validate_on_commit: off\n",
        )
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_no_output_when_no_errors(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write(tmp_path, "docs/clean.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="warn"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# validate hook: index スナップショット検証（Issue #338）
#
# `git commit` が実際にコミットするのは working tree ではなく git index の内容。
# hook は index のスナップショットに対して validate を実行するため、working tree
# だけの差分（未ステージの変更）は判定に影響しない。
# ---------------------------------------------------------------------------


class TestValidateHookIndexSnapshot:
    def test_block_mode_uses_staged_content_not_working_tree(self, tmp_path: Path) -> None:
        """壊れた依存を `git add` した後、working tree だけ修正しても index の内容で判定する。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        # working tree だけ正常なドキュメントへ書き換える（index はまだ壊れた内容のまま）
        _write(tmp_path, "docs/d.md", _CLEAN_DOC)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2  # index の壊れた内容で block される
        assert "ブロック" in result.stderr

    def test_warn_mode_ignores_unstaged_working_tree_errors(self, tmp_path: Path) -> None:
        """index がクリーンなら、working tree だけの未ステージ変更（エラー含む）は無視する。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/clean.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        # working tree だけ壊れた内容に書き換える（index はクリーンなまま）
        _write(tmp_path, "docs/clean.md", _DANGLING_DOC)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_noop_fail_safe_when_root_is_not_a_git_repository(self, tmp_path: Path) -> None:
        """git 管理下でない root では index スナップショットを構築できず fail-safe で通す。"""
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert "index スナップショット" in result.stderr


# ---------------------------------------------------------------------------
# validate hook: 複合コマンドの既知の制限注記（Issue #338）
# ---------------------------------------------------------------------------


class TestValidateHookCompoundCommandDetection:
    """`_has_preceding_command_segment` の直接 import 単体テスト。"""

    def test_detects_preceding_double_ampersand_segment(self) -> None:
        assert (
            validate_hook._has_preceding_command_segment('git add docs && git commit -m "m"')
            is True
        )

    def test_detects_preceding_semicolon_segment(self) -> None:
        assert (
            validate_hook._has_preceding_command_segment('git add docs; git commit -m "m"') is True
        )

    def test_no_preceding_segment_for_plain_commit(self) -> None:
        assert validate_hook._has_preceding_command_segment('git commit -m "msg"') is False

    def test_trailing_segment_after_commit_is_not_detected(self) -> None:
        """`git commit ... && git push` のような後続連結は検証対象外（False）。"""
        assert (
            validate_hook._has_preceding_command_segment('git commit -m "m" && git push') is False
        )


class TestValidateHookCompoundCommandNote:
    """複合コマンド検出時の warn/block メッセージへの注記 end-to-end テスト。"""

    def test_block_message_includes_compound_command_note(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git add docs && git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2
        assert "複合コマンド" in result.stderr

    def test_warn_message_includes_compound_command_note(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="warn"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git add docs && git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "複合コマンド" in context

    def test_plain_commit_message_has_no_compound_command_note(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2
        assert "複合コマンド" not in result.stderr


# ---------------------------------------------------------------------------
# normalize_validate_on_commit（EV-64、単体テスト）
# ---------------------------------------------------------------------------


class TestNormalizeValidateOnCommit:
    def test_bare_false_normalizes_to_off(self) -> None:
        assert cc.normalize_validate_on_commit(False) == "off"

    def test_off_string_case_insensitive(self) -> None:
        assert cc.normalize_validate_on_commit("OFF") == "off"
        assert cc.normalize_validate_on_commit("Off") == "off"

    def test_warn_and_block_pass_through(self) -> None:
        assert cc.normalize_validate_on_commit("warn") == "warn"
        assert cc.normalize_validate_on_commit("block") == "block"

    def test_invalid_value_raises_value_error(self) -> None:
        try:
            cc.normalize_validate_on_commit("bogus")
        except ValueError:
            return
        raise AssertionError("expected ValueError for invalid validate_on_commit")


# ---------------------------------------------------------------------------
# パフォーマンス（EV-65 / should）
# ---------------------------------------------------------------------------


class TestHookPerformance:
    def test_scan_hook_fast_exit_completes_quickly_when_not_initialized(
        self, tmp_path: Path
    ) -> None:
        """EV-65: 非該当ケース（codd 未初期化）の fast-exit は妥当な時間で完了する。

        CI 揺らぎを考慮し緩い上限（3秒）でアサートする。実測値はテスト実行報告を参照。
        """
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        start = time.monotonic()
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        elapsed = time.monotonic() - start
        assert result.returncode == 0
        assert elapsed < 3.0


# ---------------------------------------------------------------------------
# T1: validate hook の exit code 区別（bot レビュー対応, Issue #95 PR #337）
#
# `codd validate` の非ゼロ終了コードは 2 通り異なる意味を持つ:
#   - 1: 整合性エラーを検出（正常な validate 結果）→ warn/block 分岐へ
#   - 1 以外（例: 2 = 設定エラー）: validate 実行自体の失敗 → 常に非ブロック
# ---------------------------------------------------------------------------


class TestValidateHookExecutionFailureIsNonBlocking:
    def test_config_error_does_not_block_commit_even_in_block_mode(self, tmp_path: Path) -> None:
        """T1: `codd validate` の設定エラー（exit 2）は block モードでも commit をブロックしない。

        `/nonexistent/*.py`（絶対パスパターン）は `collect_files` の `Path.glob` 呼び出しで
        `NotImplementedError`（"Non-relative patterns are unsupported"）を捕捉した
        `ValueError` により `codd validate` サブプロセスが exit 2 で終了する
        （`TestScanHookSubprocessFailureNotification` と同じ原理の設定エラー）。
        """
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        config_data = _codd_config_dict(
            scope_include=["docs/**/*.md", "/nonexistent/*.py"], validate_on_commit="block"
        )
        _write_codd_config(tmp_path, config_data)
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert "[codd] validate の実行に失敗しました" in result.stderr

    def test_config_error_notifies_in_warn_mode_too(self, tmp_path: Path) -> None:
        """T1: warn モードでも実行失敗（exit 2）は additionalContext ではなく通知のみ。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        config_data = _codd_config_dict(
            scope_include=["docs/**/*.md", "/nonexistent/*.py"], validate_on_commit="warn"
        )
        _write_codd_config(tmp_path, config_data)
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert "[codd] validate の実行に失敗しました" in result.stderr


# ---------------------------------------------------------------------------
# T5: `git -C path` の検証 root 整合（bot レビュー対応, Issue #95 PR #337）
# ---------------------------------------------------------------------------


class TestValidateHookDashCExtraction:
    """`_extract_dash_c_paths` / `_is_guard_target_root` の直接 import 単体テスト。"""

    def test_extracts_single_dash_c_path(self) -> None:
        assert validate_hook._extract_dash_c_paths("git -C /repo commit -m msg") == ["/repo"]

    def test_extracts_multiple_dash_c_paths_in_order(self) -> None:
        assert validate_hook._extract_dash_c_paths("git -C /a -C b commit -m msg") == ["/a", "b"]

    def test_no_dash_c_returns_empty_list(self) -> None:
        assert validate_hook._extract_dash_c_paths('git commit -m "msg"') == []

    def test_guard_target_root_true_when_no_dash_c(self) -> None:
        assert validate_hook._is_guard_target_root("/proj", 'git commit -m "msg"') is True

    def test_guard_target_root_false_when_dash_c_points_elsewhere(self) -> None:
        assert validate_hook._is_guard_target_root("/proj", "git -C /elsewhere commit") is False

    def test_guard_target_root_true_when_dash_c_is_dot(self) -> None:
        assert validate_hook._is_guard_target_root("/proj", "git -C . commit") is True

    def test_guard_target_root_true_when_dash_c_is_root_absolute_path(self) -> None:
        assert validate_hook._is_guard_target_root("/proj", "git -C /proj commit") is True


class TestValidateHookDashCGuardTargetRoot:
    """hook サブプロセス経由での `-C` ガード対象整合の end-to-end テスト。"""

    def test_dash_c_pointing_elsewhere_skips_guard(self, tmp_path: Path) -> None:
        """T5: `-C` が hook root 以外を指す場合はガード対象外として exit 0 skip する。"""
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "git -C /elsewhere commit -m msg"},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_dash_c_dot_still_guards_and_blocks(self, tmp_path: Path) -> None:
        """`-C .` は root 自身を指すため従来通りガード対象のまま（block される）。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "git -C . commit -m msg"},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2

    def test_dash_c_absolute_root_still_guards_and_blocks(self, tmp_path: Path) -> None:
        """`-C <root の絶対パス>` は root 自身を指すため従来通りガード対象のまま。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": f"git -C {tmp_path} commit -m msg"},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2

    def test_without_dash_c_still_guards_as_before(self, tmp_path: Path) -> None:
        """`-C` を含まない従来どおりのコマンドは引き続きガード対象（回帰確認）。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# T6: scan_on_edit の厳密 bool 検証（bot レビュー対応, Issue #95 PR #337）
# ---------------------------------------------------------------------------


class TestScanOnEditStrictBool:
    def test_string_false_raises_value_error(self) -> None:
        """CLI/hook 共通ロード経路: `scan_on_edit: "false"`（文字列）は ValueError。"""
        try:
            cc.HooksConfig.from_dict({"scan_on_edit": "false"})
        except ValueError:
            return
        raise AssertionError("expected ValueError for string scan_on_edit")

    def test_mapping_value_raises_value_error(self) -> None:
        try:
            cc.HooksConfig.from_dict({"scan_on_edit": {"enabled": True}})
        except ValueError:
            return
        raise AssertionError("expected ValueError for mapping scan_on_edit")

    def test_true_bool_passes_through(self) -> None:
        assert cc.HooksConfig.from_dict({"scan_on_edit": True}).scan_on_edit is True

    def test_false_bool_passes_through(self) -> None:
        assert cc.HooksConfig.from_dict({"scan_on_edit": False}).scan_on_edit is False

    def test_missing_key_defaults_to_false(self) -> None:
        assert cc.HooksConfig.from_dict({}).scan_on_edit is False

    def test_cli_validate_exits_two_for_string_scan_on_edit(self, tmp_path: Path) -> None:
        """T6: CLI 経路（`codd validate` サブプロセス）は ValueError を exit 2 に整形する。"""
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_raw_codd_config(
            tmp_path,
            "enabled: true\n"
            "scope:\n"
            '  include: ["docs/**/*.md"]\n'
            "  exclude: []\n"
            "hooks:\n"
            '  scan_on_edit: "false"\n'
            "  validate_on_commit: warn\n",
        )
        codd_cli = REPO_ROOT / "packages" / "codd" / "scripts" / "codd.py"
        result = subprocess.run(
            [sys.executable, str(codd_cli), "validate"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2

    def test_hook_fail_safe_exits_zero_for_string_scan_on_edit(self, tmp_path: Path) -> None:
        """T6: hook 経路は `safe_hook_execution` により ValueError を exit 0 に収束する。"""
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_raw_codd_config(
            tmp_path,
            "enabled: true\n"
            "scope:\n"
            '  include: ["docs/**/*.md"]\n'
            "  exclude: []\n"
            "hooks:\n"
            '  scan_on_edit: "false"\n'
            "  validate_on_commit: warn\n",
        )
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        result = _run_hook("codd-scan-postedit.py", payload, tmp_path)
        assert result.returncode == 0
        assert "Hook error" in result.stderr


# ---------------------------------------------------------------------------
# T2: scope glob の root 相対正規化（bot レビュー対応, Issue #95 PR #337）
#
# `./docs/**/*.md` や `../<root名>/docs/**/*.md` のような正規化可能パターンは、
# scan 本体（`Path.glob` 経由の `collect_files`）は対象を収集するが、以前の単一
# パス判定（`path_matches_glob_scope` / `path_in_scan_scope`）は未正規化のまま
# regex 比較していたため常に非該当だった。`codd_common._normalize_scope_pattern()`
# へ一本化した後は両者が一致することを確認する。
# ---------------------------------------------------------------------------


class TestScopePatternRootRelativeNormalizationParity:
    def test_dot_slash_prefixed_pattern_matches_scan_scope(self, tmp_path: Path) -> None:
        """`./docs/**/*.md` は hook 側単一パス判定と scan 本体で一致する。"""
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        config = cc.CoddConfig.from_dict(
            _codd_config_dict(scope_include=["./docs/**/*.md"], scope_exclude=[])
        )
        target = tmp_path / "docs" / "x.md"

        assert cc.path_in_scan_scope(tmp_path, target, config) is True
        collected = set(cli.collect_files(tmp_path, config))
        assert target in collected

    def test_dot_slash_prefixed_exclude_pattern_matches_scan_scope(self, tmp_path: Path) -> None:
        """`./docs/skip.md`（正規化可能な exclude パターン）も同様に一致する。"""
        _write(tmp_path, "docs/keep.md", _CLEAN_DOC)
        _write(tmp_path, "docs/skip.md", "excluded via ./ prefixed exclude")
        config = cc.CoddConfig.from_dict(
            _codd_config_dict(scope_include=["docs/*.md"], scope_exclude=["./docs/skip.md"])
        )
        keep = tmp_path / "docs" / "keep.md"
        skip = tmp_path / "docs" / "skip.md"

        assert cc.path_in_scan_scope(tmp_path, keep, config) is True
        assert cc.path_in_scan_scope(tmp_path, skip, config) is False
        collected = set(cli.collect_files(tmp_path, config))
        assert keep in collected
        assert skip not in collected

    def test_parent_relative_pattern_folding_back_into_root_matches(self, tmp_path: Path) -> None:
        """`../<root名>/docs/**/*.md` のように root 外へ出て戻るパターンも一致する。"""
        root = tmp_path / "proj"
        root.mkdir()
        _write(root, "docs/x.md", _CLEAN_DOC)
        config = cc.CoddConfig.from_dict(
            _codd_config_dict(scope_include=["../proj/docs/**/*.md"], scope_exclude=[])
        )
        target = root / "docs" / "x.md"

        assert cc.path_in_scan_scope(root, target, config) is True
        collected = set(cli.collect_files(root, config))
        assert target in collected
