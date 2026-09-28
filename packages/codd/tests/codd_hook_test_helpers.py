"""codd hooks テスト（test_codd_hooks / test_codd_index_snapshot / test_codd_commit_args）の
共通ヘルパー（Issue #349 で test_codd_hooks.py から分割）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from tests.module_loader import REPO_ROOT

HOOKS_DIR = REPO_ROOT / "packages" / "codd" / "hooks"
CORE_HOOKS_DIR = REPO_ROOT / "packages" / "core" / "hooks"


# codd hooks は `hook_common` を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む。
# 環境変数の有無に関わらずモジュール import が解決できるよう、直接 sys.path にも足す
# （test_plan_gate.py と同じパターン）。
if str(CORE_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_HOOKS_DIR))


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
