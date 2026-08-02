"""codd hooks（scan-postedit / validate-precommit）のテスト（Issue #95）。

評価セット対応: docs/evaluation/codd.md §4.2 EV-59〜EV-65。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
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


def _run_hook_with_path_prefix(
    script_name: str, payload: dict[str, Any], project_dir: Path, path_prefix: Path
) -> subprocess.CompletedProcess[str]:
    """`PATH` の先頭に `path_prefix` を差し込んで hook を実行する（Issue #338）。

    hook が起動する `codd` サブプロセスのインタプリタが、`PATH` 上の `python3` ではなく
    hook 自身のインタプリタ（`sys.executable`）で解決されることを検証するために使う。
    """
    os_module = __import__("os")
    env = {
        **os_module.environ,
        "AI_ORCHESTRA_DIR": str(REPO_ROOT),
        "PATH": f"{path_prefix}{os_module.pathsep}{os_module.environ['PATH']}",
    }
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / script_name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=str(project_dir),
        check=False,
    )


def _write_failing_python3_shim(bin_dir: Path) -> None:
    """常に失敗する `python3` を `bin_dir` に配置する（PATH 汚染の再現用）。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "python3"
    shim.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    shim.chmod(0o755)


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


def _git_config_identity(project_dir: Path) -> None:
    """テスト用の commit identity を設定する（反復2: 実 commit を伴うテストで必要）。"""
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=project_dir, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=project_dir, check=True)


def _git_commit_at(project_dir: Path, message: str, date: str | None = None) -> None:
    """実際に commit する（反復2: index スナップショット経由の drift 検査を実 git 履歴で
    検証するために使う。Issue #338 レビュー High 対応）。

    `date`（指定時）は author/committer date を明示指定する。git のコミット時刻は
    秒単位のため、同一テスト内の連続コミットが同じ `%ct` になりうる。drift 判定の
    前後関係を確実に区別するため、上流を意図的に未来日時でコミットする用途で使う
    （`test_codd_cli.py::_commit_at` と同じ発想）。
    """
    env = {**__import__("os").environ}
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=project_dir,
        check=True,
        capture_output=True,
        env=env,
    )


def _git_stage_unmerged_conflict(project_dir: Path, rel_path: str) -> None:
    """`rel_path` に unmerged（未解決コンフリクト）エントリを index へ直接注入する（反復2）。

    実際の merge conflict を起こさずとも、`git update-index --index-info` で stage
    1/2/3 のエントリを直接構築すれば同じ状態（stage 0 が存在しない unmerged path）を
    再現できる。`git write-tree` はこの状態で必ず失敗する
    （`error: <path>: unmerged (<stage>)` → `fatal: git-write-tree: error building trees`）。
    """
    blobs = {}
    for stage, content in (("1", "base\n"), ("2", "ours\n"), ("3", "theirs\n")):
        hashed = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=project_dir,
            input=content,
            text=True,
            check=True,
            capture_output=True,
        )
        blobs[stage] = hashed.stdout.strip()
    index_info = "\n".join(
        f"100644 {blobs[stage]} {stage}\t{rel_path}" for stage in ("1", "2", "3")
    )
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=project_dir,
        input=index_info,
        text=True,
        check=True,
        capture_output=True,
    )


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

    def test_block_mode_ignores_unstaged_working_tree_errors_when_index_is_clean(
        self, tmp_path: Path
    ) -> None:
        """index がクリーンなら、working tree だけの未ステージ変更（エラー含む）は無視する。

        E-4: Issue #338 反復4 bot レビュー対応。設定は `validate_on_commit="block"`
        であり、warn モードの挙動ではなく「block モードでも index がクリーンなら通す」
        ことを検証している（旧テスト名 `test_warn_mode_ignores_...` は実際の検証内容と
        不一致だった）。
        """
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


class TestHookInterpreterResolution:
    """EV-71: hook が起動する `codd` は `PATH` ではなく hook 自身の interpreter で走る。

    loop-harness の Checker は機械検証コマンドを `bash -lc`（ログインシェル）で実行するため、
    `PATH` 上の `python3` が hook を起動したインタプリタと別物になる環境がありうる
    （例: mise の python が使われずシステム python が先に解決される）。その場合に
    `codd` サブプロセスを `PATH` 経由の `python3` で起動すると、依存モジュール不足等で
    黙って失敗し、hook が「検査できたが指摘ゼロ」と誤認する。
    """

    def test_validate_hook_ignores_broken_python3_on_path(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        bin_dir = tmp_path / "fakebin"
        _write_failing_python3_shim(bin_dir)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook_with_path_prefix(
            "codd-validate-precommit.py", payload, tmp_path, bin_dir
        )
        assert result.returncode == 2  # 壊れた依存を検出して block できている
        assert "ブロック" in result.stderr

    def test_scan_hook_ignores_broken_python3_on_path(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(scan_on_edit=True))
        bin_dir = tmp_path / "fakebin"
        _write_failing_python3_shim(bin_dir)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")},
        }
        result = _run_hook_with_path_prefix("codd-scan-postedit.py", payload, tmp_path, bin_dir)
        assert result.returncode == 0
        assert _read_graph_node_ids(tmp_path) == {"design:clean"}


class TestValidateHookIndexSnapshotDriftGitContext:
    """反復2（Issue #338 レビュー High 対応）: スナップショットへ実 git 履歴を伝播する。

    `codd validate` サブプロセスには `GIT_DIR`/`GIT_WORK_TREE` を渡すため、drift 検査
    （`_check_drift` / `batch_commit_times`）は checkout-index 時の mtime ではなく実際の
    commit 履歴で「上流が下流より新しい」を判定できる。既定の drift level は warning
    （commit をブロックしない）なので、`checks.drift: error` 昇格構成でも正しく機能する
    ことを block モードで確認する。

    **ファイル名の順序が本質**: `git checkout-index -a -f` は index 順（= パスの辞書順）に
    書き出すため、mtime フォールバックでの新旧は「辞書順で後のファイルほど新しい」に
    なる。上流を辞書順で**先**（= mtime が古い側）に置くことで、mtime フォールバックでは
    drift を検出できない状況を作る。この配置にしないと、`GIT_DIR`/`GIT_WORK_TREE` の
    伝播を無効化してもテストが pass してしまい、修正の有無を判別できない。
    """

    def test_drift_detected_via_actual_commit_history_not_checkout_mtime(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        # 上流 `a-req.md` を辞書順で下流 `z-design.md` より前に置く（クラス docstring 参照）
        _write(tmp_path, "docs/a-req.md", _doc("req:r", "requirement"))
        _write(
            tmp_path,
            "docs/z-design.md",
            _doc("design:d", "design", deps=[("req:r", "derives_from")]),
        )
        config_data = _codd_config_dict(scope_include=["docs/**/*.md"], validate_on_commit="block")
        config_data["checks"] = {"drift": "error"}
        _write_codd_config(tmp_path, config_data)
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init design+req")
        # 上流 (a-req) だけを未来日時の commit で更新する。checkout-index は上流を先に
        # 書き出すため、mtime ベースの比較では上流が「古い」ままとなり drift を検出
        # できない（反復1 の既知の欠陥）。実 git 履歴を見れば上流の方が新しいと判定できる。
        _write(tmp_path, "docs/a-req.md", _doc("req:r", "requirement") + "\nupdated\n")
        _git_add_all(tmp_path)
        future_date = "@4102444800 +0000"  # 2100-01-01
        _git_commit_at(tmp_path, "update req", date=future_date)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2  # drift が error 昇格されているため block される
        assert "ブロック" in result.stderr


class TestValidateHookIndexSnapshotFailSafeBranches:
    """反復2（Issue #338 レビュー Medium 対応）: 未テストだった fail-safe 分岐。

    index の unmerged エントリ、および `_build_index_snapshot` 内の subprocess
    timeout / OSError を個別に検証する。
    """

    def test_unmerged_index_entries_fail_write_tree_fail_safe(self, tmp_path: Path) -> None:
        """index に unmerged エントリがあると write-tree が失敗し fail-safe で通す（e2e）。"""
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        _git_stage_unmerged_conflict(tmp_path, "docs/conflict.md")
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert "index スナップショット" in result.stderr

    def test_write_tree_timeout_is_fail_safe(self, tmp_path: Path, monkeypatch) -> None:
        """`git write-tree` の TimeoutExpired は fail-safe で `(None, None, diagnostic)`。"""
        _git_init(tmp_path)
        real_run = validate_hook.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "write-tree"]:
                raise validate_hook.subprocess.TimeoutExpired(cmd=cmd, timeout=1)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(validate_hook.subprocess, "run", fake_run)
        deadline = validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS)
        snapshot_dir, git_dir, prefix, candidate_index_path, diagnostic = (
            validate_hook._build_index_snapshot(
                str(tmp_path), validate_hook.sanitized_git_env(), deadline
            )
        )
        assert snapshot_dir is None
        assert git_dir is None
        assert prefix is None
        assert candidate_index_path is None
        assert "git write-tree failed" in diagnostic

    def test_checkout_index_oserror_is_fail_safe(self, tmp_path: Path, monkeypatch) -> None:
        """`git checkout-index` の OSError は fail-safe で `(None, None, diagnostic)`。"""
        _git_init(tmp_path)
        real_run = validate_hook.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if "checkout-index" in cmd:
                raise OSError("boom")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(validate_hook.subprocess, "run", fake_run)
        deadline = validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS)
        snapshot_dir, git_dir, prefix, candidate_index_path, diagnostic = (
            validate_hook._build_index_snapshot(
                str(tmp_path), validate_hook.sanitized_git_env(), deadline
            )
        )
        assert snapshot_dir is None
        assert git_dir is None
        assert prefix is None
        assert candidate_index_path is None
        assert "git checkout-index failed" in diagnostic

    def test_resolve_git_dir_failure_is_fail_safe(self, tmp_path: Path, monkeypatch) -> None:
        """絶対 git-dir を解決できない場合も snapshot 構築失敗として fail-safe になる。"""
        _git_init(tmp_path)
        monkeypatch.setattr(
            validate_hook, "_resolve_absolute_git_dir", lambda root, env, deadline: None
        )
        deadline = validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS)
        snapshot_dir, git_dir, prefix, candidate_index_path, diagnostic = (
            validate_hook._build_index_snapshot(
                str(tmp_path), validate_hook.sanitized_git_env(), deadline
            )
        )
        assert snapshot_dir is None
        assert git_dir is None
        assert prefix is None
        assert candidate_index_path is None
        assert diagnostic == "git rev-parse --git-dir failed"

    def test_resolve_prefix_failure_is_fail_safe(self, tmp_path: Path, monkeypatch) -> None:
        """prefix を解決できない場合も snapshot 構築失敗として fail-safe になる（反復3）。"""
        _git_init(tmp_path)
        monkeypatch.setattr(validate_hook, "_resolve_repo_prefix", lambda root, env, deadline: None)
        deadline = validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS)
        snapshot_dir, git_dir, prefix, candidate_index_path, diagnostic = (
            validate_hook._build_index_snapshot(
                str(tmp_path), validate_hook.sanitized_git_env(), deadline
            )
        )
        assert snapshot_dir is None
        assert git_dir is None
        assert prefix is None
        assert candidate_index_path is None
        assert diagnostic == "git rev-parse --show-prefix failed"

    def test_run_validate_subprocess_timeout_is_fail_safe(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`codd validate` サブプロセス自体の TimeoutExpired も fail-safe（`_run_validate`）。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        monkeypatch.setattr(validate_hook, "_orchestra_dir", str(REPO_ROOT))
        real_run = validate_hook.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if len(cmd) > 1 and str(cmd[1]).endswith("codd.py"):
                raise validate_hook.subprocess.TimeoutExpired(cmd=cmd, timeout=1)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(validate_hook.subprocess, "run", fake_run)
        outcome = validate_hook._run_validate(str(tmp_path))
        assert outcome is None


# ---------------------------------------------------------------------------
# validate hook: モノレポ prefix / 実効設定 materialize（Issue #338 反復3）
# ---------------------------------------------------------------------------


class TestValidateHookMonorepoPrefixAndConfigMaterialization:
    """bot レビュー P1 対応: project root がリポジトリ直下でない構成（モノレポ）、
    および `codd.local.yaml`（未追跡）の実効設定反映を検証する。
    """

    def test_validate_uses_project_root_prefix_inside_snapshot(self, tmp_path: Path) -> None:
        """`checkout-index` はリポジトリ全体を展開するため、project root がサブディレクトリ
        （モノレポ）の場合は `snapshot_dir/<prefix>` を validate の cwd にする必要がある。
        prefix 解決が壊れていると snapshot 直下（誤った場所）で config/scope を探すため
        `codd` が対象を見つけられず、壊れた依存を誤って通してしまう。
        """
        repo_root = tmp_path
        project_dir = repo_root / "apps" / "foo"
        _git_init(repo_root)
        _write(project_dir, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(project_dir, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(repo_root)
        payload = {
            "cwd": str(project_dir),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, project_dir)
        assert result.returncode == 2  # サブディレクトリ root でも壊れた依存を検出できる
        assert "ブロック" in result.stderr

    def test_local_config_override_is_materialized_into_snapshot(self, tmp_path: Path) -> None:
        """`codd.local.yaml`（未追跡）の scope 上書きが snapshot 側の validate に反映される。

        base config の scope.include を空にして「何も検査しない」状態にし、
        `codd.local.yaml`（git add せず未追跡のまま置く）で scope.include を追加する。
        materialize が壊れていると local override が snapshot に届かず、base の
        空 scope のまま検査対象ゼロとなり block されない（誤って通してしまう）。
        """
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(
            tmp_path, _codd_config_dict(scope_include=[], validate_on_commit="block")
        )
        _git_add_all(tmp_path)
        # codd.local.yaml は同期対象外の未追跡ファイルとして "後から" 置く（git add しない）
        _write(
            tmp_path,
            ".claude/config/codd/codd.local.yaml",
            yaml.safe_dump({"scope": {"include": ["docs/**/*.md"], "exclude": []}}),
        )
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2  # local override の scope が反映され block される
        assert "ブロック" in result.stderr


# ---------------------------------------------------------------------------
# validate hook: config materialize の symlink 非追従化 / permission（Issue #338 反復4）
# ---------------------------------------------------------------------------


class TestValidateHookConfigMaterializeSymlinkSafety:
    """A-1: bot レビュー Critical 対応。

    index 側の config が snapshot 外への symlink（`checkout-index` 展開後の実体）である
    場合でも、materialize がそのリンク先を上書きしてはならない。
    """

    def test_materialize_does_not_follow_symlink_to_write_outside_snapshot(
        self, tmp_path: Path
    ) -> None:
        """index 側の codd.yaml が snapshot 外（victim）への symlink でも、victim の内容は
        上書きされない。working tree 側は通常ファイルのまま（`main()` の早期 exit を回避
        するため）にしておく。
        """
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)

        victim = tmp_path.parent / f"codd-victim-{tmp_path.name}.txt"
        victim.write_text("untouched\n", encoding="utf-8")

        # index 側の config path を、victim への symlink blob（mode 120000）に差し替える。
        # working tree 側の実ファイルは変更しないため、`main()` の
        # `config_path.is_file()` ゲートは通常ファイルとして通過する。
        rel_config = ".claude/config/codd/codd.yaml"
        hashed = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=tmp_path,
            input=str(victim),
            text=True,
            check=True,
            capture_output=True,
        )
        blob = hashed.stdout.strip()
        subprocess.run(
            ["git", "update-index", "--cacheinfo", f"120000,{blob},{rel_config}"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert victim.read_text(encoding="utf-8") == "untouched\n"

    def test_materialize_does_not_create_directories_through_ancestor_symlink(
        self, tmp_path: Path
    ) -> None:
        """index 側の `.claude` が snapshot 外への symlink でも、リンク先へ config 用の
        ディレクトリを作成しない。working tree 側の config は通常ファイルのままにする。
        """
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)

        outside_target = tmp_path.parent / f"codd-outside-{tmp_path.name}"
        outside_target.mkdir()
        subprocess.run(
            ["git", "rm", "-r", "--cached", ".claude"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        hashed = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=tmp_path,
            input=str(outside_target),
            text=True,
            check=True,
            capture_output=True,
        )
        blob = hashed.stdout.strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"120000,{blob},.claude"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert not (outside_target / "config" / "codd").exists()

    def test_safe_copy_config_helper_rejects_symlink_at_destination(self, tmp_path: Path) -> None:
        """`_safe_copy_config` 単体でも、dest が symlink なら追従せず安全に置き換える。"""
        snapshot_dir = tmp_path / "snapshot"
        snapshot_dir.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")
        dest = snapshot_dir / "codd.yaml"
        dest.symlink_to(victim)
        src = tmp_path / "source-codd.yaml"
        src.write_text("enabled: true\n", encoding="utf-8")

        validate_hook._safe_copy_config(src, dest, snapshot_dir)

        assert victim.read_text(encoding="utf-8") == "keep\n"
        assert not dest.is_symlink()
        assert dest.read_text(encoding="utf-8") == "enabled: true\n"

    def test_copy_no_follow_removes_empty_dest_on_write_failure(self, tmp_path: Path) -> None:
        """`os.open` 後に書き込みが失敗した場合、`_copy_no_follow` は作成済みの 0 バイト
        `dest` を削除してから False を返す（Issue #338 反復7 bot レビュー対応）。削除
        しないと `_safe_copy_config` は警告のみで継続するため、snapshot 上に空の
        `codd.yaml` が残り、`codd validate` が実 root とは異なる「設定あり」判定を
        してしまう。`src` をディレクトリにすることで `src.read_bytes()` に
        `IsADirectoryError`（`OSError` のサブクラス）を送出させ、書き込み失敗を再現する。
        """
        dest = tmp_path / "codd.yaml"
        src_dir = tmp_path / "not-a-file"
        src_dir.mkdir()

        result = validate_hook._copy_no_follow(src_dir, dest)

        assert result is False
        assert not dest.exists()


class TestValidateHookCandidateIndexPermissions:
    """A-2: bot レビュー Critical 対応。候補 index の一時ファイルは実 index の 0644
    permission を引き継がず 0600 を維持する。
    """

    def test_commit_all_candidate_index_file_keeps_owner_only_permissions(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        git_dir_out = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        real_index = tmp_path / git_dir_out / "index"
        real_index.chmod(0o644)  # 実 index の典型的な permission を明示的に再現する

        deadline = validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS)
        tmp_index_path, diagnostic = validate_hook._build_commit_all_index_file(
            str(tmp_path), validate_hook.sanitized_git_env(), deadline
        )
        assert tmp_index_path is not None, diagnostic
        mode = Path(tmp_index_path).stat().st_mode & 0o777
        Path(tmp_index_path).unlink(missing_ok=True)
        assert mode == 0o600

    def test_candidate_index_file_keeps_owner_only_permissions(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        git_dir_out = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        real_index = tmp_path / git_dir_out / "index"
        real_index.chmod(0o644)

        deadline = validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS)
        candidate_path, diagnostic = validate_hook._prepare_candidate_index(
            str((tmp_path / git_dir_out).resolve()), None, deadline
        )
        assert candidate_path is not None, diagnostic
        mode = Path(candidate_path).stat().st_mode & 0o777
        Path(candidate_path).unlink(missing_ok=True)
        assert mode == 0o600


class TestValidateHookCommitAllCopyFailureCleanup:
    """`_build_commit_all_index_file` は `mkstemp` 成功後のコピー失敗でも一時ファイルを
    残留させない（Issue #338 反復6: bot レビュー P2 対応）。
    """

    def test_copyfile_failure_does_not_leave_stray_temp_index_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")

        def _raise_copyfile(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated ENOSPC")

        monkeypatch.setattr(validate_hook.shutil, "copyfile", _raise_copyfile)

        before = set(Path(tempfile.gettempdir()).glob("codd-commit-a-index-*"))
        deadline = validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS)
        tmp_index_path, diagnostic = validate_hook._build_commit_all_index_file(
            str(tmp_path), validate_hook.sanitized_git_env(), deadline
        )
        after = set(Path(tempfile.gettempdir()).glob("codd-commit-a-index-*"))

        assert tmp_index_path is None
        assert diagnostic
        assert after == before


# ---------------------------------------------------------------------------
# validate hook: 実 index 不変・候補 index の validate 伝播（Issue #338 反復4）
# ---------------------------------------------------------------------------


class TestValidateHookRealIndexImmutability:
    """C-1: bot レビュー Critical 対応。`write-tree` は候補 index のコピーに対して
    実行され、実 index のバイト列は一切変化しない。
    """

    def test_real_index_bytes_are_unchanged_after_run_validate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        monkeypatch.setattr(validate_hook, "_orchestra_dir", str(REPO_ROOT))

        # commit 直後の index は cache-tree が有効なままのことが多く、これだと
        # `write-tree` を実 index に直接実行しても再計算が走らず判別できない。
        # 内容変更なしの再 `git add` で cache-tree エントリを invalidate してから
        # 「before」を記録する（`write-tree` が実 index へ書き戻す動作を確実に再現する）。
        _git_add_all(tmp_path)

        git_dir_out = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        real_index_path = tmp_path / git_dir_out / "index"
        before = real_index_path.read_bytes()

        outcome = validate_hook._run_validate(str(tmp_path))
        assert outcome is not None

        after = real_index_path.read_bytes()
        assert after == before  # write-tree は候補コピーに対してのみ実行される（C-1）


class TestValidateHookCandidateIndexPropagatedToValidate:
    """D-1: bot レビュー Critical 対応。`-a/--all` 候補 index が validate の
    `GIT_INDEX_FILE` として渡らないと、drift の `git status` が候補 snapshot を
    stale な実 index と比較し、実質的に変更なしの commit を誤って drift block する。
    """

    def test_dash_a_reverted_upstream_edit_is_not_false_positive_drift_blocked(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/a-upstream.md", _doc("req:r", "requirement"))
        _write(
            tmp_path,
            "docs/z-downstream.md",
            _doc("design:d", "design", deps=[("req:r", "derives_from")]),
        )
        config_data = _codd_config_dict(scope_include=["docs/**/*.md"], validate_on_commit="block")
        config_data["checks"] = {"drift": "error"}
        _write_codd_config(tmp_path, config_data)
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")

        # upstream を stage（変更）した後、working tree だけ HEAD 内容へ戻す
        # （index には古い stage 済み変更が残ったまま。実 working tree は HEAD と一致）。
        # `git checkout HEAD -- <path>` は index も一緒に戻してしまう（`MM` を再現できない）
        # ため、working tree だけをファイル書き込みで直接 HEAD 内容へ戻す。
        _write(tmp_path, "docs/a-upstream.md", _doc("req:r", "requirement") + "\ntemp edit\n")
        _git_add_all(tmp_path)
        _write(tmp_path, "docs/a-upstream.md", _doc("req:r", "requirement"))
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -am "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        # `-a` 候補 index（working tree 内容へ戻された upstream）が正しく validate へ
        # 伝播されれば、実質的に変更なしの commit であり drift は検出されない。
        assert result.returncode == 0
        assert result.stdout == ""


class TestValidateHookDriftIndependentOfCheckoutOrder:
    """D-2: bot レビュー High 対応。同一 commit で同時に stage された依存ノード間の
    drift 判定は、`checkout-index` の書き込み順（パス辞書順）に依存してはならない。
    """

    def test_simultaneously_staged_dependents_do_not_false_positive_from_checkout_order(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        # 下流（design）が辞書順で上流（requirement）より前に来るよう配置する。
        # 正規化しなければ checkout-index は a-design.md を先に書き出す（古い mtime）ため、
        # 後に書き出される z-req.md（新しい mtime）が「上流の方が新しい」偽 drift を生む。
        _write(
            tmp_path,
            "docs/a-design.md",
            _doc("design:d", "design", deps=[("req:r", "derives_from")]),
        )
        _write(tmp_path, "docs/z-req.md", _doc("req:r", "requirement"))
        config_data = _codd_config_dict(scope_include=["docs/**/*.md"], validate_on_commit="block")
        config_data["checks"] = {"drift": "error"}
        _write_codd_config(tmp_path, config_data)
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")

        # 両ノードを同一の変更として stage する（実 commit しない）。
        _write(
            tmp_path,
            "docs/a-design.md",
            _doc("design:d", "design", deps=[("req:r", "derives_from")]) + "\nupdated together\n",
        )
        _write(
            tmp_path,
            "docs/z-req.md",
            _doc("req:r", "requirement") + "\nupdated together\n",
        )
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0  # 同時 stage は checkout 順由来の偽 drift を生まない
        assert result.stdout == ""


class TestValidateHookNormalizeMtimesDeadline:
    """Issue #338 反復5: mtime 正規化も hook の共有 deadline 内に収める。"""

    def test_expired_deadline_leaves_all_file_mtimes_unchanged(
        self, tmp_path: Path, capsys
    ) -> None:
        snapshot_dir = tmp_path / "snapshot"
        snapshot_dir.mkdir()
        files = [snapshot_dir / name for name in ("a.md", "b.md", "c.md")]
        old_timestamp = 946684800.0
        for path in files:
            path.write_text(path.name, encoding="utf-8")
            os.utime(path, (old_timestamp, old_timestamp))
        before = {path: path.stat().st_mtime_ns for path in files}

        validate_hook._normalize_snapshot_mtimes(str(snapshot_dir), validate_hook._Deadline(-1.0))

        after = {path: path.stat().st_mtime_ns for path in files}
        assert after == before
        assert "mtime 正規化" in capsys.readouterr().err


class TestValidateHookMkdtempFailure:
    """Issue #338 反復5: snapshot directory 作成失敗を fail-safe に cleanup する。"""

    def test_mkdtemp_failure_returns_diagnostic_and_cleans_candidate_index(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")

        tmp_root = Path(tempfile.gettempdir())
        before = set(tmp_root.glob("codd-candidate-index-*"))
        candidate_paths: list[Path] = []
        original_prepare = validate_hook._prepare_candidate_index

        def capture_candidate_index(
            git_dir: str, index_file: str | None, deadline: Any
        ) -> tuple[str | None, str]:
            candidate_path, diagnostic = original_prepare(git_dir, index_file, deadline)
            if candidate_path is not None:
                candidate_paths.append(Path(candidate_path))
            return candidate_path, diagnostic

        def fail_mkdtemp(*, prefix: str) -> str:
            assert prefix == "codd-index-snapshot-"
            raise OSError("simulated mkdtemp failure")

        monkeypatch.setattr(validate_hook, "_prepare_candidate_index", capture_candidate_index)
        monkeypatch.setattr(validate_hook.tempfile, "mkdtemp", fail_mkdtemp)
        try:
            result = validate_hook._build_index_snapshot(
                str(tmp_path),
                validate_hook.sanitized_git_env(),
                validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS),
            )
        finally:
            after = set(tmp_root.glob("codd-candidate-index-*"))
            for candidate_path in candidate_paths:
                if candidate_path in after - before:
                    candidate_path.unlink(missing_ok=True)

        assert result[:4] == (None, None, None, None)
        assert "mkdtemp" in result[4]
        assert after == before


class TestValidateHookSnapshotCleanupOnMaterializeFailure:
    """E-2: bot レビュー High 対応。config materialize から validate 実行までの経路で
    例外が発生しても、snapshot / 候補 index が `/tmp` に残留しない。
    """

    def test_snapshot_and_candidate_index_are_cleaned_up_when_materialize_raises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        monkeypatch.setattr(validate_hook, "_orchestra_dir", str(REPO_ROOT))

        def boom(root: str, project_dir: str, snapshot_dir: str) -> None:
            raise OSError("boom: simulated ENOSPC")

        monkeypatch.setattr(validate_hook, "_materialize_config", boom)

        tmp_root = Path(tempfile.gettempdir())
        before = set(tmp_root.glob("codd-index-snapshot-*")) | set(
            tmp_root.glob("codd-candidate-index-*")
        )
        with pytest.raises(OSError):
            validate_hook._run_validate(str(tmp_path))
        after = set(tmp_root.glob("codd-index-snapshot-*")) | set(
            tmp_root.glob("codd-candidate-index-*")
        )
        assert after == before  # 例外発生時も finally で snapshot・候補 index を cleanup する


class TestValidateHookSkipWorktreeEntriesExpanded:
    """E-3: bot レビュー対応。sparse checkout で skip-worktree bit が付いたエントリも
    実際の commit tree 通りに snapshot へ展開される（`--ignore-skip-worktree-bits`）。
    """

    def test_skip_worktree_entry_is_still_expanded_into_snapshot(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/a-req.md", _doc("req:r", "requirement"))
        _write(
            tmp_path,
            "docs/z-design.md",
            _doc("design:d", "design", deps=[("req:r", "derives_from")]),
        )
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        # sparse checkout を模倣し、上流（依存先）に skip-worktree bit を立てる。
        subprocess.run(
            ["git", "update-index", "--skip-worktree", "docs/a-req.md"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        # 下流を新しい変更で stage する（内容自体は妥当）。
        _write(
            tmp_path,
            "docs/z-design.md",
            _doc("design:d", "design", deps=[("req:r", "derives_from")]) + "\nupdated\n",
        )
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        # skip-worktree の a-req.md が snapshot から欠落すると、z-design.md の依存先
        # `req:r` が dangling として誤って block される。展開されていれば通る。
        assert result.returncode == 0
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# validate hook: 共有 timeout budget / GIT_OPTIONAL_LOCKS（Issue #338 反復3）
# ---------------------------------------------------------------------------


class TestValidateHookSharedTimeoutBudget:
    """bot レビュー P2 対応: 全 subprocess で単一の deadline を共有する。"""

    def test_deadline_remaining_seconds_decreases_and_expires(self) -> None:
        deadline = validate_hook._Deadline(0.05)
        assert deadline.remaining_seconds() > 0
        assert not deadline.expired()
        time.sleep(0.1)
        assert deadline.remaining_seconds() == 0.0
        assert deadline.expired()

    def test_build_index_snapshot_fails_safe_when_deadline_already_expired(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        deadline = validate_hook._Deadline(0.0)
        time.sleep(0.01)
        snapshot_dir, git_dir, prefix, candidate_index_path, diagnostic = (
            validate_hook._build_index_snapshot(
                str(tmp_path), validate_hook.sanitized_git_env(), deadline
            )
        )
        assert snapshot_dir is None
        assert git_dir is None
        assert prefix is None
        assert candidate_index_path is None
        assert "timeout budget" in diagnostic

    def test_run_validate_fails_safe_when_shared_budget_is_too_small(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`HOOK_TIMEOUT_BUDGET_SECONDS` を極小化すると write-tree 到達前に予算切れで
        fail-safe になる。この定数・deadline 共有機構自体を revert すると
        `monkeypatch.setattr` が未定義属性で失敗し、修正の有無を判別できる。
        """
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        monkeypatch.setattr(validate_hook, "HOOK_TIMEOUT_BUDGET_SECONDS", 0.0)
        outcome = validate_hook._run_validate(str(tmp_path))
        assert outcome is None


class TestValidateHookGitOptionalLocksEnv:
    """bot レビュー P2 対応: drift 検査が実 index の stat cache を refresh・書き戻すのを
    防ぐため、`codd validate` サブプロセスへ `GIT_OPTIONAL_LOCKS=0` を渡す。
    """

    def test_codd_validate_subprocess_receives_git_optional_locks_zero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        monkeypatch.setattr(validate_hook, "_orchestra_dir", str(REPO_ROOT))
        real_run = validate_hook.subprocess.run
        captured_env: dict[str, str] = {}

        def fake_run(cmd, *args, **kwargs):
            if len(cmd) > 1 and str(cmd[1]).endswith("codd.py"):
                captured_env.update(kwargs.get("env") or {})
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(validate_hook.subprocess, "run", fake_run)
        outcome = validate_hook._run_validate(str(tmp_path))
        assert outcome is not None
        assert captured_env.get("GIT_OPTIONAL_LOCKS") == "0"


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
# validate hook: `git commit -a/--all` 候補ツリー再現（Issue #338 反復3、bot レビュー P1）
# ---------------------------------------------------------------------------


class TestValidateHookCommitAllClassification:
    """`_classify_commit_invocation` の単体テスト。"""

    def test_detects_dash_a_alone(self) -> None:
        assert validate_hook._classify_commit_invocation('git commit -a -m "msg"') == (
            True,
            False,
        )

    def test_detects_combined_dash_am(self) -> None:
        assert validate_hook._classify_commit_invocation('git commit -am "msg"') == (True, False)

    def test_detects_dash_dash_all(self) -> None:
        assert validate_hook._classify_commit_invocation('git commit --all -m "msg"') == (
            True,
            False,
        )

    def test_plain_commit_has_neither(self) -> None:
        assert validate_hook._classify_commit_invocation('git commit -m "msg"') == (False, False)

    def test_no_args_commit_has_neither(self) -> None:
        assert validate_hook._classify_commit_invocation("git commit") == (False, False)

    def test_dash_a_with_only_flag_is_unsupported(self) -> None:
        assert validate_hook._classify_commit_invocation(
            'git commit -a --only docs/x.md -m "msg"'
        ) == (True, True)

    def test_patch_mode_is_unsupported_without_all(self) -> None:
        assert validate_hook._classify_commit_invocation("git commit --patch") == (False, True)

    def test_pathspec_after_double_dash_is_unsupported(self) -> None:
        assert validate_hook._classify_commit_invocation('git commit -m "msg" -- docs/x.md') == (
            False,
            True,
        )

    def test_trailing_bare_pathspec_is_unsupported(self) -> None:
        assert validate_hook._classify_commit_invocation('git commit docs/x.md -m "msg"') == (
            False,
            True,
        )

    def test_dash_amfix_attached_message_value_is_not_misread_as_interactive(self) -> None:
        """`-amfix` は `-a` + `-m` の attached value `"fix"` であり、値中の `i` を
        `-i`（interactive）と誤認してはならない（B-1: bot レビュー Critical 対応）。"""
        assert validate_hook._classify_commit_invocation("git commit -amfix") == (True, False)

    def test_dash_ma_attached_value_is_not_misread_as_dash_a(self) -> None:
        """`-ma` は `-m` の attached value `"a"` であり、`--all` 相当ではない（B-1）。"""
        assert validate_hook._classify_commit_invocation("git commit -ma") == (False, False)

    def test_dash_u_bare_does_not_consume_following_dash_m_value(self) -> None:
        """`-u`（`--untracked-files`）は attached optional value のみを取り、次トークン
        （`-m` のフラグ自体）を値として飲み込んではならない（B-1 の回帰防止）。"""
        assert validate_hook._classify_commit_invocation('git commit -u -m "msg"') == (
            False,
            False,
        )

    def test_dash_s_keyid_with_letter_a_is_not_misread_as_dash_a(self) -> None:
        """`-Sabc1234` は `-S`（`--gpg-sign`）の attached value であり、`--all` 相当では
        ない（Issue #338 反復7 bot レビュー対応。keyid は 16 進表記が一般的で
        `a` は頻出するため、`S` を `_COMMIT_VALUE_SHORT_CHARS` から外すと value 中の
        `a` を `-a`/`--all` と誤認し、未ステージ変更を候補ツリーへ誤って含めてしまう）。
        """
        assert validate_hook._classify_commit_invocation("git commit -Sabc1234 -m x") == (
            False,
            False,
        )

    def test_dash_s_with_separate_token_is_not_consumed_as_keyid(self) -> None:
        """`-S abc`（値が別トークン）は git の挙動どおり `abc` を keyid として消費せず、
        pathspec 指定として扱う（`-S` は attached optional value のみを取るため）。"""
        assert validate_hook._classify_commit_invocation("git commit -S abc -m x") == (
            False,
            True,
        )

    def test_pathspec_from_file_with_equals_is_unsupported(self) -> None:
        """`--pathspec-from-file=<file>` は再現困難モードとして分類する（B-2）。"""
        assert validate_hook._classify_commit_invocation(
            'git commit -m "msg" --pathspec-from-file=/tmp/x.txt'
        ) == (False, True)

    def test_pathspec_from_file_separate_token_is_unsupported(self) -> None:
        """`--pathspec-from-file <file>`（値が別トークン）も再現困難モードとして分類する（B-2）。"""
        assert validate_hook._classify_commit_invocation(
            'git commit -m "msg" --pathspec-from-file /tmp/x.txt'
        ) == (False, True)

    def test_trailer_value_is_not_misread_as_pathspec(self) -> None:
        """`--trailer <value>` の値はパススペックと誤認されず、`-a` 候補ツリー再現が
        有効なままになる（Issue #338 反復6: bot レビュー P1 対応）。"""
        assert validate_hook._classify_commit_invocation(
            'git commit -a --trailer "Acked-by: dev" -m x'
        ) == (True, False)

    def test_no_all_after_dash_a_clears_all_flag(self) -> None:
        """`-a --no-all` は git の `-a, --[no-]all` 仕様どおり、後置の否定形が勝つため
        all 扱いにならない（Issue #338 反復6: bot レビュー P2 対応）。"""
        assert validate_hook._classify_commit_invocation("git commit -a --no-all -m x") == (
            False,
            False,
        )

    def test_no_all_before_dash_a_still_yields_all(self) -> None:
        """`--no-all -a`（順序が逆）では最後に現れた `-a` が勝ち、all 扱いになる
        （Issue #338 反復6: bot レビュー P2 対応の回帰防止）。"""
        assert validate_hook._classify_commit_invocation("git commit --no-all -a -m x") == (
            True,
            False,
        )


class TestValidateHookRepoPrefixLeadingWhitespace:
    """`_resolve_repo_prefix` は末尾の改行のみを取り除き、有効な先頭空白を保持する
    （E-1: Issue #338 反復4 bot レビュー対応）。
    """

    def test_leading_space_in_project_root_dirname_is_preserved(self, tmp_path: Path) -> None:
        repo_root = tmp_path
        project_dir = repo_root / " apps" / "foo"
        _git_init(repo_root)
        _write(project_dir, "docs/x.md", _CLEAN_DOC)
        deadline = validate_hook._Deadline(validate_hook.HOOK_TIMEOUT_BUDGET_SECONDS)
        prefix = validate_hook._resolve_repo_prefix(
            str(project_dir), validate_hook.sanitized_git_env(), deadline
        )
        assert prefix == " apps/foo/"


class TestValidateHookCommitAllReconstruction:
    """`git commit -a/--all` は index だけでなく working tree の追跡ファイル変更も
    候補ツリーに含める必要がある（working tree 近似の解消。Issue #338 反復3）。
    """

    def test_dash_am_detects_error_introduced_by_unstaged_tracked_modification(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/clean.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        # 追跡済みファイルを未ステージのまま壊れた内容へ書き換える（`git add` はしない）
        _write(tmp_path, "docs/clean.md", _DANGLING_DOC)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -am "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2  # -a により未ステージの追跡変更も候補ツリーに含まれる
        assert "ブロック" in result.stderr

    def test_plain_commit_without_dash_a_ignores_unstaged_tracked_modification(
        self, tmp_path: Path
    ) -> None:
        """比較対象: `-a` を指定しない場合は実 index（クリーン）のみを検証する。"""
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/clean.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        _write(tmp_path, "docs/clean.md", _DANGLING_DOC)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_dash_a_with_unsupported_marker_skips_reconstruction(self, tmp_path: Path) -> None:
        """`-a` と `--only` の併用は再現困難のため reconstruction を試みない。

        reconstruction を誤って試みると、未ステージの追跡変更が候補ツリーに含まれて
        しまい block されてしまう（このテストは fail する）。
        """
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/clean.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        _write(tmp_path, "docs/clean.md", _DANGLING_DOC)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -a --only docs/clean.md -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 0  # reconstruction されないため実 index（クリーン）のみ検証
        assert result.stdout == ""

    def test_unsupported_marker_note_appears_in_block_message(self, tmp_path: Path) -> None:
        """`--patch` 等の再現困難モードでは、index 由来のエラーでも注記が付く。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/d.md", _DANGLING_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(validate_on_commit="block"))
        _git_add_all(tmp_path)
        payload = {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit --patch -m "msg"'},
        }
        result = _run_hook("codd-validate-precommit.py", payload, tmp_path)
        assert result.returncode == 2
        assert "実際の commit tree と異なる可能性があります" in result.stderr


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
