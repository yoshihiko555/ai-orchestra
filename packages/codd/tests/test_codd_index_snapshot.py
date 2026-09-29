"""codd validate-precommit hook の index スナップショット構築・validate 実行のテスト。

対象: packages/codd/lib/codd_index_snapshot.py（Issue #349 で hook から分割）。
評価セット対応: docs/evaluation/codd.md §4.2 の index スナップショット関連
（EV-68〜EV-84、EV-87〜EV-89、EV-92、EV-94〜EV-96）。
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

helpers = load_module("codd_hook_test_helpers", "packages/codd/tests/codd_hook_test_helpers.py")
_run_hook = helpers._run_hook
_codd_config_dict = helpers._codd_config_dict
_write_codd_config = helpers._write_codd_config
_write = helpers._write
_git_init = helpers._git_init
_git_add_all = helpers._git_add_all
_git_config_identity = helpers._git_config_identity
_git_commit_at = helpers._git_commit_at
_git_stage_unmerged_conflict = helpers._git_stage_unmerged_conflict
_doc = helpers._doc
_DANGLING_DOC = helpers._DANGLING_DOC
_CLEAN_DOC = helpers._CLEAN_DOC

from hook_common import sanitized_git_env  # noqa: E402

commit_args = load_module("codd_commit_args", "packages/codd/lib/codd_commit_args.py")
cc = load_module("codd_common", "packages/codd/lib/codd_common.py")
snapshot = load_module("codd_index_snapshot", "packages/codd/lib/codd_index_snapshot.py")

CODD_CLI = str(REPO_ROOT / "packages" / "codd" / "scripts" / "codd.py")


def _run_validate(root: str, **kwargs: Any) -> tuple[int, str, str] | None:
    """hook と同じ引数（codd CLI パス・サニタイズ済み env）で `run_validate` を呼ぶ。"""
    return snapshot.run_validate(root, codd_cli=CODD_CLI, git_env=sanitized_git_env(), **kwargs)


def _resolve_git_dir_for_test(root: str) -> str:
    """テスト用に `root` の絶対 git-dir を解決する（新しい deadline を使う、失敗しない前提）。"""
    deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
    git_dir = snapshot._resolve_absolute_git_dir(root, sanitized_git_env(), deadline)
    assert git_dir is not None
    return git_dir


class TestScopeLimitedIndexSnapshot:
    """Issue #349 の EV-97 scope 限定展開と EV-98 symlink fallback を検証する。"""

    @staticmethod
    def _build_and_list_files(root: Path, scope_patterns: list[str] | None) -> list[str]:
        """snapshot のファイル一覧を返し、候補 index と展開先を必ず削除する。"""
        git_dir = _resolve_git_dir_for_test(str(root))
        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        built, diagnostic = snapshot._build_index_snapshot(
            str(root),
            git_dir,
            sanitized_git_env(),
            deadline,
            scope_patterns=scope_patterns,
        )
        assert built is not None, diagnostic
        try:
            return sorted(
                path.relative_to(built.snapshot_dir).as_posix()
                for path in Path(built.snapshot_dir).rglob("*")
                if path.is_file()
            )
        finally:
            shutil.rmtree(built.snapshot_dir, ignore_errors=True)
            Path(built.candidate_index_path).unlink(missing_ok=True)

    @staticmethod
    def _stage_monorepo(tmp_path: Path) -> Path:
        """project 内外にファイルがある staged リポジトリを作る。"""
        _git_init(tmp_path)
        project = tmp_path / "apps" / "foo"
        _write(project, "docs/a.md", "# A\n")
        _write(project, "src/x.txt", "x\n")
        _write(tmp_path, "other/b.md", "# B\n")
        _git_add_all(tmp_path)
        return project

    def test_monorepo_scope_checks_out_only_project_match(self, tmp_path: Path) -> None:
        """project 相対の ls-files パスを repo prefix 付きで展開する。"""
        project = self._stage_monorepo(tmp_path)
        assert self._build_and_list_files(project, ["docs/**/*.md"]) == ["apps/foo/docs/a.md"]

    def test_project_symlink_expands_full_index(self, tmp_path: Path) -> None:
        """project 内の symlink は scope 外ファイルも含む全件展開に戻す。"""
        project = self._stage_monorepo(tmp_path)
        (project / "link.md").symlink_to("docs/a.md")
        _git_add_all(tmp_path)
        files = self._build_and_list_files(project, ["docs/**/*.md"])
        assert "apps/foo/src/x.txt" in files
        assert "other/b.md" in files

    def test_case_insensitive_superset_includes_path(self, tmp_path: Path) -> None:
        """ワイルドカードを含まないセグメントは大小文字違いも候補に含める。"""
        project = self._stage_monorepo(tmp_path)
        assert self._build_and_list_files(project, ["Docs/*.md"]) == ["apps/foo/docs/a.md"]

    def test_negated_char_class_keeps_case_sensitive_match(self, tmp_path: Path) -> None:
        """否定文字クラスは大文字小文字を区別して照合し、一致を狭めない（Issue #349 レビュー）。

        パターン全体を `re.IGNORECASE` で照合すると `[!a-z]` が `R` を除外し、`Path.glob` が
        一致させる `docs/README.md` を展開し損ねていた。
        """
        project = self._stage_monorepo(tmp_path)
        _write(project, "docs/README.md", "# R\n")
        _git_add_all(tmp_path)
        files = self._build_and_list_files(project, ["docs/[!a-z]*.md"])
        assert files == ["apps/foo/docs/README.md"]

    def test_non_ascii_pattern_falls_back_to_full_checkout(self, tmp_path: Path) -> None:
        """非 ASCII 文字を含むパターンは全件展開に戻す（EV-98。Unicode 正規化形の差の回避）。"""
        project = self._stage_monorepo(tmp_path)
        files = self._build_and_list_files(project, ["café/*.md"])
        assert "apps/foo/src/x.txt" in files
        assert "other/b.md" in files

    def test_pattern_conversion_failure_falls_back_to_full_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """パターン変換の例外は全件展開へ戻し、候補 index を残さない。"""
        project = self._stage_monorepo(tmp_path)

        def broken_matcher(root: str, patterns: list[str]) -> None:
            raise re.error("simulated")

        monkeypatch.setattr(snapshot, "_scope_matcher", broken_matcher)
        files = self._build_and_list_files(project, ["docs/**/*.md"])
        assert "other/b.md" in files

    def test_project_prefix_with_space_is_expanded(self, tmp_path: Path) -> None:
        """空白を含む project prefix でも repo root 相対のパスで展開する（EV-82 との組み合わせ）。"""
        _git_init(tmp_path)
        project = tmp_path / "apps" / "my proj"
        _write(project, "docs/a.md", "# A\n")
        _write(project, "src/x.txt", "x\n")
        _git_add_all(tmp_path)
        files = self._build_and_list_files(project, ["docs/**/*.md"])
        assert files == ["apps/my proj/docs/a.md"]

    def test_none_scope_keeps_full_checkout(self, tmp_path: Path) -> None:
        """scope 未指定の直接呼び出しは従来通り全件展開する。"""
        project = self._stage_monorepo(tmp_path)
        files = self._build_and_list_files(project, None)
        assert "apps/foo/src/x.txt" in files
        assert "other/b.md" in files

    def test_ls_files_failure_falls_back_to_full_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ls-files の失敗時も snapshot を構築し全件展開する。"""
        project = self._stage_monorepo(tmp_path)
        real_run = snapshot.subprocess.run

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            if cmd[:2] == ["git", "ls-files"]:
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"boom")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
        files = self._build_and_list_files(project, ["docs/**/*.md"])
        assert "apps/foo/src/x.txt" in files
        assert "other/b.md" in files

    def test_empty_selection_checks_out_nothing(self, tmp_path: Path) -> None:
        """一致エントリがなくても空 snapshot を成功として返す。"""
        project = self._stage_monorepo(tmp_path)
        assert self._build_and_list_files(project, ["nomatch/**/*.xyz"]) == []

    def test_gitlink_is_excluded_from_selection(self, tmp_path: Path) -> None:
        """gitlink は glob に一致しても通常ファイルとして展開しない。"""
        project = self._stage_monorepo(tmp_path)
        subprocess.run(
            [
                "git",
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{'a' * 40},apps/foo/docs/sub",
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        files = self._build_and_list_files(project, ["docs/**"])
        assert files == ["apps/foo/docs/a.md"]

    def test_validate_result_matches_full_checkout(self, tmp_path: Path) -> None:
        """scope 外・project 外のファイルがあるモノレポでも、全件展開と結果が一致する（EV-97）。"""
        for name, document, expected in (
            ("dangling", _DANGLING_DOC, 1),
            ("clean", _CLEAN_DOC, 0),
        ):
            repo = tmp_path / name
            project = repo / "apps" / "foo"
            repo.mkdir()
            _git_init(repo)
            _write(project, "docs/a.md", document)
            _write(project, "notes/out-of-scope.md", _DANGLING_DOC)
            _write(repo, "other/docs/outside.md", _DANGLING_DOC)
            _write_codd_config(project, _codd_config_dict())
            _git_add_all(repo)
            full = _run_validate(str(project))
            limited = _run_validate(str(project), scope_patterns=["docs/**/*.md"])
            assert full is not None
            assert limited is not None
            assert full[0] == limited[0] == expected
            assert limited[1] == full[1]

    def test_case_only_path_collision_falls_back_to_full_checkout(self, tmp_path: Path) -> None:
        """大文字小文字だけが異なるパスが index にあれば全件展開に戻し、結果を一致させる（EV-98）。

        APFS では全件展開時に `Docs/` と `docs/` が同じ物理ディレクトリへまとまり、ワイルド
        カードセグメント（`D*`）の `Path.glob` が両方を見つける。パス文字列で選ぶと
        `docs/y.md` の壊れた依存を見逃していた（Issue #349 レビュー指摘）。
        """
        _git_init(tmp_path)
        _write(tmp_path, "Docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict(scope_include=["D*/*.md"]))
        _git_add_all(tmp_path)
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=tmp_path,
            input=_DANGLING_DOC,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},docs/y.md"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        full = _run_validate(str(tmp_path))
        limited = _run_validate(str(tmp_path), scope_patterns=["D*/*.md"])
        assert full is not None
        assert limited is not None
        assert limited == full
        assert snapshot._has_folding_collision(["Docs/x.md", "docs/y.md"])
        assert not snapshot._has_folding_collision(["Docs/x.md", "Docs/y.md"])

    def test_symlink_node_validate_result_matches_full_checkout(self, tmp_path: Path) -> None:
        """scope 外を指す symlink ノードがあっても全件展開と結果が一致する（EV-98）。

        `docs/alias.md -> ../shared/real.md` の実体は scope 外にある。symlink 時の全件展開に
        戻らないと実体が snapshot に無く alias ノードが消え、`docs/b.md` の依存が false
        dangling になる。
        """
        _git_init(tmp_path)
        _write(tmp_path, "shared/real.md", _doc("design:real"))
        _write(tmp_path, "docs/b.md", _doc("design:b", deps=[("design:real", "derives_from")]))
        (tmp_path / "docs" / "alias.md").symlink_to(Path("..") / "shared" / "real.md")
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        full = _run_validate(str(tmp_path))
        limited = _run_validate(str(tmp_path), scope_patterns=["docs/**/*.md"])
        assert full is not None
        assert limited is not None
        assert full[0] == limited[0] == 0
        assert limited[1] == full[1]


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
        """`git write-tree` の TimeoutExpired は fail-safe で `(None, diagnostic)`。"""
        _git_init(tmp_path)
        git_dir = _resolve_git_dir_for_test(str(tmp_path))
        real_run = snapshot.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "write-tree"]:
                raise snapshot.subprocess.TimeoutExpired(cmd=cmd, timeout=1)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        built, diagnostic = snapshot._build_index_snapshot(
            str(tmp_path), git_dir, sanitized_git_env(), deadline
        )
        assert built is None
        assert "git write-tree failed" in diagnostic

    def test_checkout_index_oserror_is_fail_safe(self, tmp_path: Path, monkeypatch) -> None:
        """`git checkout-index` の OSError は fail-safe で `(None, diagnostic)`。"""
        _git_init(tmp_path)
        git_dir = _resolve_git_dir_for_test(str(tmp_path))
        real_run = snapshot.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if "checkout-index" in cmd:
                raise OSError("boom")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        built, diagnostic = snapshot._build_index_snapshot(
            str(tmp_path), git_dir, sanitized_git_env(), deadline
        )
        assert built is None
        assert "git checkout-index failed" in diagnostic

    def test_resolve_git_dir_failure_is_fail_safe(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """絶対 git-dir を解決できない場合、run_validate は fail-safe で None を返す。"""
        _git_init(tmp_path)
        monkeypatch.setattr(snapshot, "_resolve_absolute_git_dir", lambda root, env, deadline: None)
        outcome = _run_validate(str(tmp_path))
        assert outcome is None
        assert "git rev-parse --git-dir failed" in capsys.readouterr().err

    def test_resolve_prefix_failure_is_fail_safe(self, tmp_path: Path, monkeypatch) -> None:
        """prefix を解決できない場合も snapshot 構築失敗として fail-safe になる（反復3）。"""
        _git_init(tmp_path)
        git_dir = _resolve_git_dir_for_test(str(tmp_path))
        monkeypatch.setattr(snapshot, "_resolve_repo_prefix", lambda root, env, deadline: None)
        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        built, diagnostic = snapshot._build_index_snapshot(
            str(tmp_path), git_dir, sanitized_git_env(), deadline
        )
        assert built is None
        assert diagnostic == "git rev-parse --show-prefix failed"

    def test_run_validate_subprocess_timeout_is_fail_safe(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`codd validate` サブプロセス自体の TimeoutExpired も fail-safe（`run_validate`）。"""
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        real_run = snapshot.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if len(cmd) > 1 and str(cmd[1]).endswith("codd.py"):
                raise snapshot.subprocess.TimeoutExpired(cmd=cmd, timeout=1)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
        outcome = _run_validate(str(tmp_path))
        assert outcome is None


class TestIndexSnapshotErrorPathHardening:
    """index スナップショットの異常系（Issue #349）。

    評価セット対応: EV-94（config コピーの部分書き込み）、EV-95（subprocess 出力の復号失敗・
    一時ファイル作成失敗）、EV-96（mtime 正規化の symlink 非追従・権限エラー）。git-dir を
    1 回だけ解決することも確認する。
    """

    @pytest.mark.parametrize(
        "case",
        [
            "checkout-index",
            "write-tree",
            "show-prefix",
            "git add -u",
            "codd validate",
            "rev-parse",
        ],
    )
    def test_unicode_decode_error_is_fail_safe_and_cleans_temporary_paths(
        self, tmp_path: Path, monkeypatch, case: str
    ) -> None:
        """subprocess 出力の復号失敗も fail-safe に倒れ、一時ファイルを残さない（EV-95）。"""
        _git_init(tmp_path)
        if case in {"git add -u", "codd validate", "rev-parse"}:
            _write(tmp_path, "docs/x.md", _CLEAN_DOC)
            _write_codd_config(tmp_path, _codd_config_dict())
            _git_add_all(tmp_path)
        if case in {"checkout-index", "write-tree", "show-prefix", "git add -u"}:
            git_dir = _resolve_git_dir_for_test(str(tmp_path))

        tmp_root = Path(tempfile.gettempdir())
        patterns = (
            "codd-candidate-index-*",
            "codd-index-snapshot-*",
            "codd-commit-a-index-*",
        )

        def temp_entries() -> set[Path]:
            return {path for pattern in patterns for path in tmp_root.glob(pattern)}

        before = temp_entries()
        real_run = snapshot.subprocess.run
        targeted = False

        def fake_run(cmd, *args, **kwargs):
            nonlocal targeted
            matches = {
                "checkout-index": "checkout-index" in cmd,
                "write-tree": cmd[:2] == ["git", "write-tree"],
                "git add -u": cmd[:3] == ["git", "add", "-u"],
                "codd validate": len(cmd) > 1 and str(cmd[1]).endswith("codd.py"),
                "show-prefix": cmd[:2] == ["git", "rev-parse"] and "--show-prefix" in cmd,
                "rev-parse": cmd[:2] == ["git", "rev-parse"] and "--git-dir" in cmd,
            }
            if matches[case]:
                targeted = True
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
        if case in {"checkout-index", "write-tree", "show-prefix"}:
            built, diagnostic = snapshot._build_index_snapshot(
                str(tmp_path),
                git_dir,
                sanitized_git_env(),
                snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS),
            )
            assert built is None
            assert diagnostic
        elif case == "git add -u":
            tmp_index_path, diagnostic = snapshot._build_commit_all_index_file(
                str(tmp_path),
                git_dir,
                sanitized_git_env(),
                snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS),
            )
            assert tmp_index_path is None
            assert diagnostic
        else:
            assert _run_validate(str(tmp_path)) is None

        assert targeted
        assert temp_entries() == before

    def test_normalize_mtimes_does_not_follow_directory_symlinks(self, tmp_path: Path) -> None:
        """mtime 正規化はディレクトリへの symlink を辿らず、symlink 自体だけを正規化する（EV-96）。

        Python 3.11〜3.14 の `Path.rglob` は symlink ディレクトリへ再帰しないため、現行実装の
        まま snapshot 外（`outside/victim.txt`）の mtime は変わらない。この振る舞いを固定する
        回帰テスト（Issue #349 の調査で、rglob が symlink を辿るという前提は誤りと確認済み）。
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim.txt"
        victim.write_text("victim\n", encoding="utf-8")
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        file_path = snap_dir / "file.txt"
        file_path.write_text("file\n", encoding="utf-8")
        link_dir = snap_dir / "linkdir"
        link_dir.symlink_to(Path("..") / "outside", target_is_directory=True)
        link_file = snap_dir / "linkfile"
        link_file.symlink_to("file.txt")
        old_mtime = 1_000_000
        os.utime(victim, (old_mtime, old_mtime))
        os.utime(file_path, (old_mtime, old_mtime))
        os.utime(link_dir, (old_mtime, old_mtime), follow_symlinks=False)
        os.utime(link_file, (old_mtime, old_mtime), follow_symlinks=False)

        snapshot._normalize_snapshot_mtimes(str(snap_dir), snapshot._Deadline(60))

        assert int(victim.stat().st_mtime) == old_mtime
        assert file_path.stat().st_mtime > old_mtime + 1000
        assert os.lstat(link_dir).st_mtime > old_mtime + 1000
        assert os.lstat(link_file).st_mtime > old_mtime + 1000

    def test_copy_no_follow_removes_partially_written_dest_on_enospc(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """書き込み途中で ENOSPC になっても、部分的に書かれた `dest` を残さない（EV-94）。"""
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")
        dest = tmp_path / "dest.bin"
        real_fdopen = snapshot.os.fdopen

        class PartialWriter:
            def __init__(self, file: BinaryIO) -> None:
                self._file = file

            def __enter__(self) -> PartialWriter:
                return self

            def __exit__(self, *_args: object) -> None:
                self._file.close()

            def write(self, data: bytes) -> None:
                self._file.write(data[:1])
                raise OSError(errno.ENOSPC, "No space left on device")

        def failing_fdopen(fd: int, mode: str) -> PartialWriter:
            return PartialWriter(real_fdopen(fd, mode))

        monkeypatch.setattr(snapshot.os, "fdopen", failing_fdopen)

        assert snapshot._copy_no_follow(src, dest) is False
        assert not dest.exists()

    def test_normalize_mtimes_ignores_symlink_to_unreadable_location(self, tmp_path: Path) -> None:
        """権限のない場所を指す symlink があっても例外を送出しない（EV-96、Issue #349）。

        Python 3.12 以前の `Path.is_dir()` は symlink を辿った stat の EACCES を再送出する。
        判定前に `is_symlink()` で分岐しないと、例外が snapshot・候補 index の cleanup 前に
        漏れていた（3.13 以降は `is_dir()` が握りつぶすため、この回帰は 3.12 で検出される）。
        """
        locked = tmp_path / "locked"
        (locked / "inner").mkdir(parents=True)
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        file_path = snap_dir / "file.txt"
        file_path.write_text("file\n", encoding="utf-8")
        link = snap_dir / "link"
        link.symlink_to(locked / "inner")
        old_mtime = 1_000_000
        os.utime(file_path, (old_mtime, old_mtime))
        os.utime(link, (old_mtime, old_mtime), follow_symlinks=False)
        locked.chmod(0o000)
        try:
            snapshot._normalize_snapshot_mtimes(str(snap_dir), snapshot._Deadline(60))
        finally:
            locked.chmod(0o700)

        assert file_path.stat().st_mtime > old_mtime + 1000
        assert os.lstat(link).st_mtime > old_mtime + 1000

    @pytest.mark.parametrize("target", ["candidate", "commit-all"])
    def test_mkstemp_failure_returns_diagnostic(
        self, tmp_path: Path, monkeypatch, target: str
    ) -> None:
        """一時 index の作成（`tempfile.mkstemp`）の OSError も診断へ収束させる（EV-95）。"""
        _git_init(tmp_path)
        git_dir = _resolve_git_dir_for_test(str(tmp_path))

        def fail_mkstemp(*, prefix: str) -> tuple[int, str]:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(snapshot.tempfile, "mkstemp", fail_mkstemp)
        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        if target == "candidate":
            path, diagnostic = snapshot._prepare_candidate_index(git_dir, None, deadline)
        else:
            path, diagnostic = snapshot._build_commit_all_index_file(
                str(tmp_path), git_dir, sanitized_git_env(), deadline
            )
        assert path is None
        assert "tempfile.mkstemp failed" in diagnostic

    def test_git_dir_is_resolved_once_for_commit_all(self, tmp_path: Path, monkeypatch) -> None:
        """`-a/--all` 再現時も git-dir の解決は 1 回だけ（共有 deadline を無駄に消費しない）。"""
        _git_init(tmp_path)
        _git_config_identity(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        _git_commit_at(tmp_path, "init")
        real_resolve = snapshot._resolve_absolute_git_dir
        call_count = 0

        def counting_resolve(root, env, deadline):
            nonlocal call_count
            call_count += 1
            return real_resolve(root, env, deadline)

        monkeypatch.setattr(snapshot, "_resolve_absolute_git_dir", counting_resolve)
        outcome = _run_validate(str(tmp_path), simulate_commit_all=True)
        assert outcome is not None
        assert call_count == 1


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

        snapshot._safe_copy_config(src, dest, snapshot_dir)

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

        result = snapshot._copy_no_follow(src_dir, dest)

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

        git_dir = _resolve_git_dir_for_test(str(tmp_path))
        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        tmp_index_path, diagnostic = snapshot._build_commit_all_index_file(
            str(tmp_path), git_dir, sanitized_git_env(), deadline
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

        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        candidate_path, diagnostic = snapshot._prepare_candidate_index(
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
        git_dir = _resolve_git_dir_for_test(str(tmp_path))

        def _raise_copyfile(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated ENOSPC")

        monkeypatch.setattr(snapshot.shutil, "copyfile", _raise_copyfile)

        before = set(Path(tempfile.gettempdir()).glob("codd-commit-a-index-*"))
        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        tmp_index_path, diagnostic = snapshot._build_commit_all_index_file(
            str(tmp_path), git_dir, sanitized_git_env(), deadline
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

        outcome = _run_validate(str(tmp_path))
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

        snapshot._normalize_snapshot_mtimes(str(snapshot_dir), snapshot._Deadline(-1.0))

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
        git_dir = _resolve_git_dir_for_test(str(tmp_path))

        tmp_root = Path(tempfile.gettempdir())
        before = set(tmp_root.glob("codd-candidate-index-*"))
        candidate_paths: list[Path] = []
        original_prepare = snapshot._prepare_candidate_index

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

        monkeypatch.setattr(snapshot, "_prepare_candidate_index", capture_candidate_index)
        monkeypatch.setattr(snapshot.tempfile, "mkdtemp", fail_mkdtemp)
        try:
            built, diagnostic = snapshot._build_index_snapshot(
                str(tmp_path),
                git_dir,
                sanitized_git_env(),
                snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS),
            )
        finally:
            after = set(tmp_root.glob("codd-candidate-index-*"))
            for candidate_path in candidate_paths:
                if candidate_path in after - before:
                    candidate_path.unlink(missing_ok=True)

        assert built is None
        assert "mkdtemp" in diagnostic
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

        def boom(root: str, project_dir: str, snapshot_dir: str) -> None:
            raise OSError("boom: simulated ENOSPC")

        monkeypatch.setattr(snapshot, "_materialize_config", boom)

        tmp_root = Path(tempfile.gettempdir())
        before = set(tmp_root.glob("codd-index-snapshot-*")) | set(
            tmp_root.glob("codd-candidate-index-*")
        )
        with pytest.raises(OSError):
            _run_validate(str(tmp_path))
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
        deadline = snapshot._Deadline(0.05)
        assert deadline.remaining_seconds() > 0
        assert not deadline.expired()
        time.sleep(0.1)
        assert deadline.remaining_seconds() == 0.0
        assert deadline.expired()

    def test_build_index_snapshot_fails_safe_when_deadline_already_expired(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        git_dir = _resolve_git_dir_for_test(str(tmp_path))
        deadline = snapshot._Deadline(0.0)
        time.sleep(0.01)
        built, diagnostic = snapshot._build_index_snapshot(
            str(tmp_path), git_dir, sanitized_git_env(), deadline
        )
        assert built is None
        assert "timeout budget" in diagnostic

    def test_run_validate_fails_safe_when_shared_budget_is_too_small(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`HOOK_TIMEOUT_BUDGET_SECONDS` を極小化すると write-tree 到達前に予算切れで
        fail-safe になる。この定数・deadline 共有機構自体を revert すると
        `monkeypatch.setattr` が未定義属性で失敗し、修正の有無を判別できる。
        """
        _git_init(tmp_path)
        _write(tmp_path, "docs/x.md", _CLEAN_DOC)
        _write_codd_config(tmp_path, _codd_config_dict())
        _git_add_all(tmp_path)
        monkeypatch.setattr(snapshot, "HOOK_TIMEOUT_BUDGET_SECONDS", 0.0)
        outcome = _run_validate(str(tmp_path))
        assert outcome is None
        # 予算切れは git-dir 解決の失敗ではなく予算超過として診断する（Issue #349）
        stderr = capsys.readouterr().err
        assert "タイムアウト予算" in stderr
        assert "git rev-parse --git-dir failed" not in stderr


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
        real_run = snapshot.subprocess.run
        captured_env: dict[str, str] = {}

        def fake_run(cmd, *args, **kwargs):
            if len(cmd) > 1 and str(cmd[1]).endswith("codd.py"):
                captured_env.update(kwargs.get("env") or {})
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
        outcome = _run_validate(str(tmp_path))
        assert outcome is not None
        assert captured_env.get("GIT_OPTIONAL_LOCKS") == "0"


class TestValidateHookRepoPrefixLeadingWhitespace:
    """`_resolve_repo_prefix` は末尾の改行のみを取り除き、有効な先頭空白を保持する
    （E-1: Issue #338 反復4 bot レビュー対応）。
    """

    def test_leading_space_in_project_root_dirname_is_preserved(self, tmp_path: Path) -> None:
        repo_root = tmp_path
        project_dir = repo_root / " apps" / "foo"
        _git_init(repo_root)
        _write(project_dir, "docs/x.md", _CLEAN_DOC)
        deadline = snapshot._Deadline(snapshot.HOOK_TIMEOUT_BUDGET_SECONDS)
        prefix = snapshot._resolve_repo_prefix(str(project_dir), sanitized_git_env(), deadline)
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
