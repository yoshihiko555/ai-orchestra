"""codd.py CLI（scan / graph / validate）の unit test。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest.mock
from pathlib import Path

import pytest

from tests.module_loader import load_module

cc = load_module("codd_common", "packages/codd/lib/codd_common.py")
cli = load_module("codd_cli", "packages/codd/scripts/codd.py")


BASE_CONFIG = {
    "enabled": True,
    "scope": {"include": ["docs/**/*.md"], "exclude": []},
    # code/test は Issue #98（コード⇔ドキュメントのトレーサビリティ）で追加された kind。
    # code_scope 自体は既定で空（opt-in）のため、この語彙追加だけでは既存テストに影響しない。
    "kinds": ["requirement", "design", "adr", "plan", "rule", "instruction", "code", "test"],
    "relations": ["derives_from", "refines", "implements", "references", "supersedes"],
    "roots": ["requirement", "instruction"],
    "graph_store": {"format": "jsonl", "path": ".claude/codd/graph.jsonl"},
    "checks": {
        "dangling": "error",
        "duplicate": "error",
        "cycle": "error",
        "unknown": "error",
        "missing_frontmatter": "warning",
        "orphan": "warning",
        "drift": "warning",
    },
}

# `path_in_scope()` / `path_in_code_scope()` は削除済みファイル向けの純粋パス判定
# であり、実ファイルアクセスをしない（レキシカルな正規化のみ）。tmp_path を使わない
# テストでは、この固定ダミー root を使う（Issue #98 レビュー対応: codd.py:880）。
_ROOT = Path("/repo")


def _config(**overrides) -> object:
    data = {**BASE_CONFIG, **overrides}
    return cc.CoddConfig.from_dict(data)


def _doc(
    node_id: str,
    kind: str,
    status: str = "draft",
    deps: list[tuple[str, str]] | None = None,
) -> str:
    lines = ["---", "codd:", f"  node_id: {node_id}", f"  kind: {kind}", f"  status: {status}"]
    if deps:
        lines.append("  depends_on:")
        for dep_id, relation in deps:
            lines.append(f"    - id: {dep_id}")
            lines.append(f"      relation: {relation}")
    lines += ["---", "", "# 本文", ""]
    return "\n".join(lines)


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# scan / graph 永続化
# ---------------------------------------------------------------------------


def test_scan_project_builds_graph(tmp_path) -> None:
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    assert len(result.nodes) == 2
    assert result.graph.has("req:r")
    assert result.graph.incoming_count("req:r") == 1
    assert result.missing_frontmatter == []


def test_scan_records_missing_frontmatter(tmp_path) -> None:
    _write(tmp_path, "docs/has.md", _doc("design:d", "design"))
    _write(tmp_path, "docs/none.md", "# フロントマター無し\n")
    result = cli.scan_project(tmp_path, _config())
    assert result.missing_frontmatter == ["docs/none.md"]


def test_scan_respects_exclude(tmp_path) -> None:
    _write(tmp_path, "docs/keep.md", _doc("design:k", "design"))
    _write(tmp_path, "docs/skip.md", _doc("design:s", "design"))
    config = _config(scope={"include": ["docs/**/*.md"], "exclude": ["docs/skip.md"]})
    result = cli.scan_project(tmp_path, config)
    ids = {n.node_id for n in result.nodes}
    assert ids == {"design:k"}


def test_scan_respects_recursive_exclude_glob(tmp_path) -> None:
    # exclude も Path.glob で解決するため、`**` 再帰 glob が直下・ネスト両方を除外する。
    _write(tmp_path, "docs/a.md", _doc("design:a", "design"))
    _write(tmp_path, "docs/sub/b.md", _doc("design:b", "design"))
    _write(tmp_path, "guides/c.md", _doc("design:c", "design"))
    config = _config(
        scope={"include": ["docs/**/*.md", "guides/**/*.md"], "exclude": ["docs/**/*.md"]}
    )
    result = cli.scan_project(tmp_path, config)
    ids = {n.node_id for n in result.nodes}
    assert ids == {"design:c"}


def test_write_graph_jsonl_roundtrip(tmp_path) -> None:
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    out = tmp_path / ".claude/codd/graph.jsonl"
    cli.write_graph_jsonl(result, out)
    records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert records[0]["node_id"] == "design:d"
    assert records[0]["depends_on"] == [{"id": "req:r", "relation": "derives_from"}]


# ---------------------------------------------------------------------------
# validate 検査
# ---------------------------------------------------------------------------


def _checks(result, config, root) -> dict[str, list]:
    findings = cli.run_checks(result, config, root)
    grouped: dict[str, list] = {}
    for f in findings:
        grouped.setdefault(f.check, []).append(f)
    return grouped


def test_validate_clean(tmp_path) -> None:
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    findings = cli.run_checks(result, _config(), tmp_path)
    errors = [f for f in findings if f.level == cc.LEVEL_ERROR]
    assert errors == []


def test_validate_dangling(tmp_path) -> None:
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:missing", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["dangling"]) == 1
    assert grouped["dangling"][0].level == cc.LEVEL_ERROR


def test_validate_duplicate(tmp_path) -> None:
    _write(tmp_path, "docs/a.md", _doc("design:dup", "design"))
    _write(tmp_path, "docs/b.md", _doc("design:dup", "design"))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["duplicate"]) == 1


def test_validate_cycle(tmp_path) -> None:
    _write(tmp_path, "docs/a.md", _doc("design:a", "design", deps=[("design:b", "refines")]))
    _write(tmp_path, "docs/b.md", _doc("design:b", "design", deps=[("design:a", "refines")]))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["cycle"]) == 1


def test_validate_unknown_kind_and_relation_and_status(tmp_path) -> None:
    _write(tmp_path, "docs/k.md", _doc("x:k", "widget"))
    _write(
        tmp_path,
        "docs/r.md",
        _doc("design:r", "design", status="bogus", deps=[("x:k", "weird_rel")]),
    )
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped["unknown"])
    assert "widget" in messages
    assert "weird_rel" in messages
    assert "bogus" in messages


def test_validate_unknown_flags_empty_status(tmp_path) -> None:
    # status: 欠落（空）も unknown error 扱い（5 プロパティ必須）。
    doc = "---\ncodd:\n  node_id: design:s\n  kind: design\n---\n# body\n"
    _write(tmp_path, "docs/s.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "status" in messages


def test_validate_unknown_flags_empty_node_id(tmp_path) -> None:
    doc = "---\ncodd:\n  kind: design\n  status: draft\n---\n# body\n"
    _write(tmp_path, "docs/n.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "node_id" in messages


def test_validate_unknown_flags_node_id_without_colon(tmp_path) -> None:
    # EV-12: node_id は `<kind>:<file-slug>` 形式。コロンが無ければ unknown error。
    doc = (
        "---\ncodd:\n  node_id: designwithoutcolon\n  kind: design\n  status: draft\n---\n# body\n"
    )
    _write(tmp_path, "docs/s.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "designwithoutcolon" in messages
    assert "コロンが無いか複数ある" in messages


def test_validate_unknown_flags_node_id_with_extra_separator(tmp_path) -> None:
    # EV-12: node_id にコロンが複数個ある（余分なセパレータ）場合も unknown error。
    # `partition(":")` は先頭コロンで区切ってしまい "design" を prefix として誤って
    # 受理する回帰があったため、明示的にこのケースを検証する。
    doc = "---\ncodd:\n  node_id: design:foo:bar\n  kind: design\n  status: draft\n---\n# body\n"
    _write(tmp_path, "docs/s.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "design:foo:bar" in messages
    assert "コロンが無いか複数ある" in messages


def test_validate_unknown_flags_kind_node_id_prefix_mismatch(tmp_path) -> None:
    # EV-12: kind は正しい語彙だが node_id のプレフィックスが kind と対応しない
    # （例: kind=requirement なのに node_id は "design:" プレフィックス）。
    _write(tmp_path, "docs/req.md", _doc("design:mismatched", "requirement"))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "design:mismatched" in messages
    assert "不一致" in messages


def test_validate_accepts_requirement_abbreviated_req_prefix(tmp_path) -> None:
    # EV-12: requirement kind は "req" プレフィックスへ略記される（設計 4.3 の表）。
    # これは不一致ではなく正常系として通ること。
    _write(tmp_path, "docs/req.md", _doc("req:coherence-guardrail", "requirement"))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert grouped.get("unknown", []) == []


def test_validate_node_id_without_colon_reports_single_finding(tmp_path) -> None:
    # Codex レビュー反映: 形式不正（コロン無し）と kind プレフィックス不一致を
    # 二重報告しない（1 ノードにつき unknown finding は 1 件のみ）。
    doc = (
        "---\ncodd:\n  node_id: designwithoutcolon\n  kind: design\n  status: draft\n---\n# body\n"
    )
    _write(tmp_path, "docs/s.md", doc)
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    node_id_findings = [f for f in grouped.get("unknown", []) if "designwithoutcolon" in f.message]
    assert len(node_id_findings) == 1


def test_validate_flags_node_id_with_empty_prefix_or_slug(tmp_path) -> None:
    # EV-12: プレフィックス側・スラッグ側どちらが空でも `<kind>:<file-slug>` 形式でない。
    _write(
        tmp_path,
        "docs/a.md",
        '---\ncodd:\n  node_id: ":a"\n  kind: design\n  status: draft\n---\n# body\n',
    )
    _write(
        tmp_path,
        "docs/b.md",
        '---\ncodd:\n  node_id: "design:"\n  kind: design\n  status: draft\n---\n# body\n',
    )
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    messages = [f.message for f in grouped.get("unknown", [])]
    assert sum("コロンが無いか複数ある" in m for m in messages) == 2


def test_validate_unmapped_custom_kind_skips_prefix_check(tmp_path) -> None:
    # Codex レビュー反映: config.kinds に NODE_ID_PREFIX_BY_KIND 未定義の独自 kind を
    # 追加しても、プレフィックス照合をスキップし誤検知（false positive）しない。
    _write(tmp_path, "docs/e.md", _doc("eval:e", "eval", status="draft"))
    config = _config(kinds=[*BASE_CONFIG["kinds"], "eval"])
    result = cli.scan_project(tmp_path, config)
    grouped = _checks(result, config, tmp_path)
    messages = " ".join(f.message for f in grouped.get("unknown", []))
    assert "プレフィックス" not in messages  # 未マッピング kind は照合スキップ（誤検知しない）


def test_validate_missing_frontmatter_warning(tmp_path) -> None:
    _write(tmp_path, "docs/none.md", "# no frontmatter\n")
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["missing_frontmatter"]) == 1
    assert grouped["missing_frontmatter"][0].level == cc.LEVEL_WARNING


def test_validate_orphan_warning_excludes_roots(tmp_path) -> None:
    # requirement は roots なので孤立でも除外。design は orphan として検出される。
    _write(tmp_path, "docs/root.md", _doc("req:root", "requirement"))
    _write(tmp_path, "docs/lonely.md", _doc("design:lonely", "design"))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    orphans = grouped.get("orphan", [])
    assert len(orphans) == 1
    assert orphans[0].level == cc.LEVEL_WARNING
    assert "docs/lonely.md" in orphans[0].message


def test_validate_drift_warning_via_mtime(tmp_path) -> None:
    # tmp_path は git 管理外 → commit_time は mtime にフォールバック。
    down = _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    up = _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    # 上流 (req) を下流 (design) より新しくする → drift
    os.utime(down, (1000, 1000))
    os.utime(up, (2000, 2000))
    result = cli.scan_project(tmp_path, _config())
    grouped = _checks(result, _config(), tmp_path)
    assert len(grouped["drift"]) == 1
    assert grouped["drift"][0].level == cc.LEVEL_WARNING


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def _commit_at(root: Path, message: str, date: str) -> None:
    """author/committer date を明示指定してコミットする。

    git のコミット時刻は秒単位のため、同一テスト内の連続コミットが同じ %ct に
    なりうる。drift 判定のテストで確実に前後関係を区別するために使う。
    """
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )


def test_commit_time_clean_uses_git_dirty_uses_mtime(tmp_path) -> None:
    # クリーン追跡ファイルは git コミット時刻、未コミット編集のあるファイルは mtime。
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "tester")
    target = _write(tmp_path, "docs/x.md", _doc("design:x", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    future = 4102444800.0  # 2100-01-01。mtime を未来へ置き、コミット時刻と区別する
    os.utime(target, (future, future))
    # クリーン: コミット時刻が返る（mtime ではない）
    assert cli.commit_time(tmp_path, "docs/x.md") != future

    # 編集して dirty にする → mtime が返る（drift を取りこぼさない）
    target.write_text(_doc("design:x", "design") + "\nedit\n", encoding="utf-8")
    os.utime(target, (future, future))
    assert cli.commit_time(tmp_path, "docs/x.md") == future


def test_batch_commit_times_matches_commit_time_for_clean_dirty_and_untracked(tmp_path) -> None:
    # 一括版（`batch_commit_times`）はノードごとに `commit_time()` を呼ぶのと同じ
    # 判定規約（クリーンな追跡ファイルはコミット時刻、dirty・未追跡は mtime）を
    # 維持する必要がある。1,000 ノード規模の git プロセス起動数を削減する最適化
    # のためのバッチ化であり、結果が変わってはならない（Issue #98 レビュー対応）。
    _init_repo(tmp_path)
    clean = _write(tmp_path, "docs/clean.md", "# clean\n")
    dirty = _write(tmp_path, "docs/dirty.md", "# dirty\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    dirty.write_text("# dirty edited\n", encoding="utf-8")
    _write(tmp_path, "docs/untracked.md", "# untracked\n")

    rel_paths = ["docs/clean.md", "docs/dirty.md", "docs/untracked.md"]
    batched = cli.batch_commit_times(tmp_path, rel_paths)
    expected = {rel: cli.commit_time(tmp_path, rel) for rel in rel_paths}

    assert batched == expected
    assert batched["docs/clean.md"] != clean.stat().st_mtime  # クリーンはコミット時刻
    assert batched["docs/dirty.md"] == dirty.stat().st_mtime  # dirty は mtime


def test_batch_commit_times_empty_input_returns_empty_dict(tmp_path) -> None:
    assert cli.batch_commit_times(tmp_path, []) == {}


def test_batch_commit_times_handles_non_ascii_paths(tmp_path) -> None:
    # `core.quotePath`（既定 true）の下で `git log --name-only` を素朴にパースすると
    # 非 ASCII パスが 8 進エスケープ付きで引用され、`rel_paths` のキーと一致せず
    # 常に mtime フォールバックへ落ちる（P1 レビュー対応: codd.py:350）。
    _init_repo(tmp_path)
    clean = _write(tmp_path, "docs/日本語.md", "# clean\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    rel_paths = ["docs/日本語.md"]
    batched = cli.batch_commit_times(tmp_path, rel_paths)

    assert batched == {rel: cli.commit_time(tmp_path, rel) for rel in rel_paths}
    # クリーンな追跡ファイルはコミット時刻が返り、mtime フォールバックにはならない。
    assert batched["docs/日本語.md"] != clean.stat().st_mtime


def test_log_commit_times_handles_pathspec_magic_like_filename(tmp_path) -> None:
    # git にノードパスを素朴な `--` pathspec として渡すと、`:(bad.md` のような
    # 先頭が `:(` のファイル名（正当なファイル名だが pathspec magic 構文と衝突する）
    # 1件だけで `fatal: Invalid pathspec magic` になり、`_git_output_bytes` が None を
    # 返して呼び出し全体が空 dict にフォールバックしていた。`:(literal)` を各パスへ
    # 前置し、pathspec magic として解釈させないことで回避する
    # （レビュー対応: 8巡目 codd.py:418）。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/normal.md", "# normal\n")
    _write(tmp_path, ":(bad.md", "# bad\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    times = cli._log_commit_times(tmp_path, ["docs/normal.md", ":(bad.md"])

    assert "docs/normal.md" in times
    assert ":(bad.md" in times


def test_batch_commit_times_survives_pathspec_magic_like_filename(tmp_path) -> None:
    # `_log_commit_times()` が literal pathspec 化されていないと、`:(bad.md` の
    # ような1ファイルの存在だけで一括 `git log` 全体が失敗し、同じバッチ内の
    # 他の clean node（docs/normal.md）まで commit time ではなく working-tree
    # mtime で比較されてしまう（drift 判定が不安定になる。レビュー対応: 8巡目
    # codd.py:418）。
    _init_repo(tmp_path)
    normal = _write(tmp_path, "docs/normal.md", "# normal\n")
    _write(tmp_path, ":(bad.md", "# bad\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    batched = cli.batch_commit_times(tmp_path, ["docs/normal.md", ":(bad.md"])

    assert batched["docs/normal.md"] != normal.stat().st_mtime
    assert batched["docs/normal.md"] == cli.commit_time(tmp_path, "docs/normal.md")


def test_log_commit_times_splits_pathspecs_across_multiple_git_log_batches(
    tmp_path, monkeypatch
) -> None:
    # 数万ノード規模の code_scope では、全パスを 1 回の `git log` の argv に展開すると
    # OS の ARG_MAX を超えて `subprocess.run()` が `OSError` になり、
    # `_git_output_bytes` がそれを None に変換するため、`_log_commit_times()` は
    # 空 dict を返してしまう（`batch_commit_times()` は全 clean node を working-tree
    # mtime へ黙ってフォールバックし、checkout 時刻ベースの誤った drift 判定を招く）。
    # pathspec を安全なサイズで複数バッチに分割し、各バッチの時刻を統合する必要が
    # ある（Issue #98 レビュー対応: 11巡目 codd.py:440）。実際に ARG_MAX を超えさせず
    # に検証するため、1 バッチあたりの許容パス件数を 1 へ下げ、`git log` が複数回
    # 呼ばれ、かつ両方のファイルの commit time が正しく取得できることを確認する。
    monkeypatch.setattr(cli, "_GIT_ARG_BATCH_MAX_COUNT", 1)
    monkeypatch.setattr(cli, "_GIT_ARG_BATCH_MAX_BYTES", 100_000)

    _init_repo(tmp_path)
    a = _write(tmp_path, "docs/a.md", "# a\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add a")
    b = _write(tmp_path, "docs/b.md", "# b\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add b")

    log_call_count = 0
    real_run = subprocess.run

    def _counting_run(args, **kwargs):
        nonlocal log_call_count
        if args[0] == "git" and args[1] == "log":
            log_call_count += 1
        return real_run(args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", _counting_run)

    result = cli.batch_commit_times(tmp_path, ["docs/a.md", "docs/b.md"])

    assert log_call_count == 2  # 1 パスずつ、2 回の `git log` に分割された
    assert result["docs/a.md"] != a.stat().st_mtime
    assert result["docs/b.md"] != b.stat().st_mtime


def test_batch_pathspecs_splits_on_byte_and_count_limits(monkeypatch) -> None:
    # `_batch_pathspecs()` はバイトサイズ上限・件数上限のいずれかを超える手前で
    # 新しいバッチを開始する（Issue #98 レビュー対応: 11巡目 codd.py:440）。
    monkeypatch.setattr(cli, "_GIT_ARG_BATCH_MAX_BYTES", 10)
    monkeypatch.setattr(cli, "_GIT_ARG_BATCH_MAX_COUNT", 4000)

    batches = cli._batch_pathspecs(["aaaa", "bbbb", "cccc"])  # 各 5 バイト（NUL 込み）

    assert batches == [["aaaa", "bbbb"], ["cccc"]]


def test_ref_blob_mode_handles_pathspec_magic_like_filename(tmp_path) -> None:
    # `git ls-tree` にファイル名を素朴な `--` pathspec として渡すと、`:(bad.md` の
    # ような先頭が `:(` のファイル名（正当なファイル名だが pathspec magic 構文と
    # 衝突する）で `fatal: Invalid pathspec magic` になり、`_ref_blob_mode()` は
    # 「存在しない」扱いで None を返してしまう（`_log_commit_times` と同種の問題。
    # Issue #98 レビュー対応: 11巡目 codd.py:1135）。`:(literal)` を前置し、
    # pathspec magic として解釈させないことで回避する。
    _init_repo(tmp_path)
    _write(tmp_path, ":(bad.md", "# bad\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    assert cli._ref_blob_mode(tmp_path, "HEAD", ":(bad.md") == "100644"


def test_git_output_bytes_keep_partial_on_error_returns_partial_stdout(
    monkeypatch, tmp_path
) -> None:
    # 部分履歴 clone（shallow clone 等）では、対象パスの無制限 `git log` が最新
    # timestamp を stdout に出力した後、古い履歴（欠けた tree）の走査中に nonzero
    # で終了することがある。`keep_partial_on_error=True` は stdout が空でなければ
    # nonzero 終了でも取得できた分を返す。既定（False）は従来どおり破棄する
    # （レビュー対応: codd.py:394）。
    def _fake_run(args, cwd, capture_output, check):
        return subprocess.CompletedProcess(
            args, returncode=128, stdout=b"partial-bytes", stderr=b"fatal: bad object deadbeef"
        )

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    assert cli._git_output_bytes(tmp_path, ["log"], keep_partial_on_error=True) == b"partial-bytes"
    assert cli._git_output_bytes(tmp_path, ["log"]) is None


def test_log_commit_times_uses_partial_output_on_nonzero_git_log_exit(
    monkeypatch, tmp_path
) -> None:
    # `_log_commit_times()` は `keep_partial_on_error=True` で `git log` を呼ぶため、
    # 部分履歴 clone で nonzero 終了しても、取得できた分の timestamp を使う（0 件
    # 扱いで mtime へ全面フォールバックしない。レビュー対応: codd.py:394）。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/a.md", "# a\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    real_run = subprocess.run

    def _fake_run(args, cwd, capture_output, check):
        # 実際の git 出力はそのまま使い、returncode だけ shallow clone 相当に壊す。
        completed = real_run(args, cwd=cwd, capture_output=capture_output, check=False)
        return subprocess.CompletedProcess(
            args, returncode=128, stdout=completed.stdout, stderr=b"fatal: bad object deadbeef"
        )

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    times = cli._log_commit_times(tmp_path, ["docs/a.md"])

    assert "docs/a.md" in times
    assert times["docs/a.md"] > 0


def test_batch_commit_times_excludes_dirty_paths_outside_root_prefix(tmp_path) -> None:
    # `--root` がサブディレクトリを指す場合、`_dirty_paths()` はリポジトリ全体の
    # dirty パスを返す。prefix（`sub/`）配下に無いパス（例: リポジトリ直下の
    # `docs/clean.md`）が prefix 除去前のまま素通りすると、prefix 除去後に偶然
    # root 内ノードと同じ相対名になり、clean な `sub/docs/clean.md` まで誤って
    # dirty 扱いされてしまっていた（レビュー対応: codd.py:442）。
    repo_root = tmp_path
    _init_repo(repo_root)
    sub_root = repo_root / "sub"
    clean = _write(repo_root, "sub/docs/clean.md", "# clean (sub)\n")
    _write(repo_root, "docs/clean.md", "# clean (top-level, will be dirty)\n")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-m", "init")
    # root 外（prefix 外）の同名相対パスだけを dirty にする。sub 側は commit のまま。
    (repo_root / "docs/clean.md").write_text("# edited\n", encoding="utf-8")

    batched = cli.batch_commit_times(sub_root, ["docs/clean.md"])

    # sub/docs/clean.md 自体は clean なのでコミット時刻が返るべき（mtime ではない）。
    assert batched["docs/clean.md"] != clean.stat().st_mtime


def test_batch_commit_times_normalizes_paths_when_root_is_not_git_root(tmp_path) -> None:
    # `--root` が git リポジトリルート以外（サブディレクトリ）を指す場合も、
    # `_dirty_paths()` / `_log_commit_times()` が返すリポジトリルート相対のパスを
    # `--root` 相対へ正規化してからキャッシュに載せる必要がある。正規化しないと
    # クリーンな追跡ファイルでもキャッシュが常にミスし、mtime フォールバックへ
    # 落ちてしまう（Minor レビュー対応: codd.py:393）。
    repo_root = tmp_path
    _init_repo(repo_root)
    sub_root = repo_root / "sub"
    clean = _write(repo_root, "sub/docs/clean.md", "# clean\n")
    _write(repo_root, "sub/docs/dirty.md", "# dirty\n")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-m", "init")
    (repo_root / "sub/docs/dirty.md").write_text("# dirty edited\n", encoding="utf-8")

    rel_paths = ["docs/clean.md", "docs/dirty.md"]
    batched = cli.batch_commit_times(sub_root, rel_paths)

    assert batched["docs/clean.md"] != clean.stat().st_mtime  # クリーンはコミット時刻
    assert batched["docs/dirty.md"] == (repo_root / "sub/docs/dirty.md").stat().st_mtime


def test_validate_drift_warning_via_git_commit_times(tmp_path) -> None:
    # `_check_drift` は `batch_commit_times()` 経由で一括取得したコミット時刻を使う
    # （ノードごとの個別 git 呼び出しを廃止。Issue #98 レビュー対応）。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init design+req")
    # 上流 (req) だけを新しいコミットで更新 → コミット時刻ベースで drift 検出。
    # git のコミット時刻は秒単位のため、同一テスト内の連続コミットが同じ %ct に
    # なりうる。committer date を明示的に未来へずらして確実に区別する。
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement") + "\nupdated\n")
    _git(tmp_path, "add", "-A")
    future_date = "@4102444800 +0000"  # 2100-01-01（epoch 秒 + タイムゾーン表記）
    subprocess.run(
        ["git", "commit", "-m", "update req"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": future_date,
            "GIT_COMMITTER_DATE": future_date,
        },
    )

    result = cli.scan_project(tmp_path, _config())
    findings = cli.run_checks(result, _config(), tmp_path)
    drift = [f for f in findings if f.check == "drift"]

    assert len(drift) == 1
    assert drift[0].level == cc.LEVEL_WARNING


def test_validate_drift_uses_symlink_target_commit_time_not_alias(tmp_path) -> None:
    # `_check_drift` は symlink alias ノードの更新時刻に alias 自体の最終コミット
    # 時刻ではなく、リンクチェーンの実体（最終ターゲット）の最終コミット時刻を使う
    # 必要がある。scan はリンク先の内容を dereference して注釈を読むため、alias 自体
    # は git 上変更されなくても、リンク先の内容だけが後続コミットで更新されることが
    # ある。alias の古い時刻のまま比較すると drift の誤検知（false positive）が起きる
    # （Issue #98 レビュー対応: 10巡目 codd.py:666）。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(tmp_path, "shared/mod.py", _py(["codd:implements req:r"]))
    (tmp_path / "aliases").mkdir()
    (tmp_path / "aliases" / "mod.py").symlink_to(Path("../shared/mod.py"))
    config = _config(code_scope={"include": ["aliases/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    # 上流 (req) を先に更新する。
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement") + "\nupdated\n")
    _git(tmp_path, "add", "-A")
    _commit_at(tmp_path, "update req", "@4102444800 +0000")  # 2100-01-01

    # alias（aliases/mod.py）自体は変更せず、リンク先（shared/mod.py）の内容だけを
    # 上流より後のコミットで追従更新する。
    _write(tmp_path, "shared/mod.py", _py(["codd:implements req:r"], body="print('v2')\n"))
    _git(tmp_path, "add", "-A")
    _commit_at(tmp_path, "catch up alias target", "@4102531200 +0000")  # 2100-01-02

    result = cli.scan_project(tmp_path, config)
    findings = cli.run_checks(result, config, tmp_path)
    drift = [f for f in findings if f.check == "drift"]

    # alias はリンク先の実体経由で上流より新しく更新済みなので drift は検出されない
    # はず。alias 自体のコミット時刻（init 時点）を使う旧実装では誤って drift が
    # 報告されていた。
    assert drift == []


def test_validate_drift_uses_alias_relink_time_not_only_target(tmp_path) -> None:
    # `_check_drift` は最終ターゲットの時刻だけでなく alias 自体の更新時刻も考慮する
    # 必要がある。alias を上流変更後に既存の古いターゲットへ張り替えた場合、
    # ターゲット自体の内容は変わっていなくても、alias の再結線コミットは「upstream
    # の変更を踏まえて張り替えた（＝追従済み）」ことを意味する。最終ターゲットの
    # 古いコミット時刻だけで比較すると誤って drift warning になる
    # （Issue #98 レビュー対応: 11巡目 codd.py:675）。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(tmp_path, "shared/mod.py", _py(["codd:implements req:r"]))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init req+target")

    # 上流 (req) を更新する。ターゲット（shared/mod.py）はこの時点でまだ古いまま。
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement") + "\nupdated\n")
    _git(tmp_path, "add", "-A")
    _commit_at(tmp_path, "update req", "@4102444800 +0000")  # 2100-01-01

    # 上流変更を確認した上で、alias を（内容更新済みの）既存ターゲットへ張る。
    # alias 自体の作成コミットは上流更新より後だが、ターゲット（shared/mod.py）の
    # 最終コミット時刻は init 時点のまま（上流更新より前）。
    (tmp_path / "aliases").mkdir()
    (tmp_path / "aliases" / "mod.py").symlink_to(Path("../shared/mod.py"))
    config = _config(code_scope={"include": ["aliases/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _commit_at(tmp_path, "point alias at existing target", "@4102531200 +0000")  # 2100-01-02

    result = cli.scan_project(tmp_path, config)
    findings = cli.run_checks(result, config, tmp_path)
    drift = [f for f in findings if f.check == "drift"]

    # alias 自体の張り替えコミットが上流更新より新しいので drift は検出されない
    # はず。最終ターゲットの時刻のみを使う旧実装では誤って drift が報告されていた。
    assert drift == []


def test_checks_off_level_suppresses(tmp_path) -> None:
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:missing", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    config = _config(checks={**BASE_CONFIG["checks"], "dangling": "off"})
    findings = cli.run_checks(result, config, tmp_path)
    assert all(f.check != "dangling" for f in findings)


def test_validate_exit_codes(tmp_path) -> None:
    # error あり → 1
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:missing", "derives_from")]),
    )
    assert cli.cmd_validate(tmp_path, _config()) == 1
    # error 無し（warning のみ）→ 0
    (tmp_path / "docs/design.md").unlink()
    _write(tmp_path, "docs/lonely.md", _doc("design:lonely", "design"))
    assert cli.cmd_validate(tmp_path, _config()) == 0


# ---------------------------------------------------------------------------
# impact 分析（compute_impact: pure ロジック）
# ---------------------------------------------------------------------------


def _node(node_id: str, kind: str = "design", deps: list[tuple[str, str]] | None = None):
    return cc.CoddNode(
        node_id=node_id,
        kind=kind,
        status="draft",
        depends_on=tuple(cc.Dependency(id=i, relation=r) for i, r in (deps or [])),
        owner=None,
        path=f"docs/{node_id.replace(':', '_')}.md",
    )


def _impact_cfg(**overrides):
    return cc.ImpactConfig.from_dict(overrides)


def _by_id(impacted) -> dict:
    return {n.node_id: n for n in impacted}


def test_band_for_score_boundaries() -> None:
    # EV-15: score>=0.8 は Green、>=0.4 は Amber、それ未満は Gray（境界値そのもの）。
    cfg = _impact_cfg()
    assert cc._band_for_score(0.8, cfg) == cc.BAND_GREEN  # ちょうど green_threshold
    assert cc._band_for_score(0.7999, cfg) == cc.BAND_AMBER  # green 未満は amber
    assert cc._band_for_score(0.4, cfg) == cc.BAND_AMBER  # ちょうど amber_threshold
    assert cc._band_for_score(0.3999, cfg) == cc.BAND_GRAY  # amber 未満は gray


def test_impact_direct_strong_is_green() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "derives_from")])]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:r"}, _impact_cfg()))
    assert set(impacted) == {"design:d"}
    node = impacted["design:d"]
    assert node.band == cc.BAND_GREEN
    assert node.score == 1.0
    assert node.min_hops == 1
    assert node.origins == ["req:r"]
    assert node.co_changed is False


def test_impact_references_is_gray() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "references")])]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:r"}, _impact_cfg()))
    assert impacted["design:d"].band == cc.BAND_GRAY
    assert impacted["design:d"].score == 0.3


def test_impact_two_hop_strong_is_amber() -> None:
    graph = cc.build_graph(
        [
            _node("req:r", "requirement"),
            _node("design:d", deps=[("req:r", "derives_from")]),
            _node("plan:p", "plan", deps=[("design:d", "implements")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:r"}, _impact_cfg()))
    assert impacted["design:d"].band == cc.BAND_GREEN  # 1 hop
    assert impacted["plan:p"].band == cc.BAND_AMBER  # 2 hop → 0.5
    assert impacted["plan:p"].score == 0.5
    assert impacted["plan:p"].min_hops == 2


def test_impact_supersedes_direct_is_amber() -> None:
    graph = cc.build_graph(
        [
            _node("adr:old", "adr"),
            _node("adr:new", "adr", deps=[("adr:old", "supersedes")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"adr:old"}, _impact_cfg()))
    assert impacted["adr:new"].band == cc.BAND_AMBER
    assert impacted["adr:new"].score == 0.6


def test_impact_co_changed_caps_at_amber() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "derives_from")])]
    )
    # design 自身も変更済み → 直接強依存で Green になるはずが Amber 上限へ。
    impacted = _by_id(cc.compute_impact(graph, {"req:r", "design:d"}, _impact_cfg()))
    node = impacted["design:d"]
    assert node.co_changed is True
    assert node.band == cc.BAND_AMBER
    assert node.score == 1.0  # スコア自体は下げない（破壊変更を Gray に隠さない）


def test_impact_corroboration_downgrades_uncorroborated_green() -> None:
    # decay=1.0 で 2-hop でも score 1.0（推論的 Green 候補）を作る。
    cfg = _impact_cfg(decay=1.0)
    graph = cc.build_graph(
        [
            _node("req:a", "requirement"),
            _node("design:m", deps=[("req:a", "derives_from")]),
            _node("plan:leaf", "plan", deps=[("design:m", "implements")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:a"}, cfg))
    # 単一起点・直接事実でない 2-hop は Green に上げず Amber へ降格。
    assert impacted["plan:leaf"].score == 1.0
    assert impacted["plan:leaf"].band == cc.BAND_AMBER
    # 直接強依存の design:m は Green のまま。
    assert impacted["design:m"].band == cc.BAND_GREEN


def test_impact_corroboration_allows_multi_origin_green() -> None:
    cfg = _impact_cfg(decay=1.0)
    graph = cc.build_graph(
        [
            _node("req:a", "requirement"),
            _node("req:b", "requirement"),
            _node("design:m", deps=[("req:a", "derives_from"), ("req:b", "derives_from")]),
            _node("plan:leaf", "plan", deps=[("design:m", "implements")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:a", "req:b"}, cfg))
    leaf = impacted["plan:leaf"]
    # 2 起点が裏付ける → Corroboration を満たし Green を許可。
    assert sorted(leaf.origins) == ["req:a", "req:b"]
    assert leaf.band == cc.BAND_GREEN


def test_impact_cycle_safe() -> None:
    # a <-> b の相互依存。無限ループせず下流を一度だけ列挙する。
    graph = cc.build_graph(
        [
            _node("design:a", deps=[("design:b", "refines")]),
            _node("design:b", deps=[("design:a", "refines")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"design:a"}, _impact_cfg()))
    assert "design:b" in impacted
    # 起点 a は自身の下流ではない（サイクルでも origin は影響先に含めない）。
    assert "design:a" not in impacted


def test_impact_empty_changed_ids() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "derives_from")])]
    )
    assert cc.compute_impact(graph, set(), _impact_cfg()) == []


def test_impact_origin_not_in_graph() -> None:
    # 変更ノードがグラフ未登録（scope 外の変更など）でも例外を出さず空を返す。
    graph = cc.build_graph([_node("design:d", deps=[("req:r", "derives_from")])])
    assert cc.compute_impact(graph, {"req:nonexistent"}, _impact_cfg()) == []


def test_impact_respects_max_hops() -> None:
    graph = cc.build_graph(
        [
            _node("req:a", "requirement"),
            _node("design:b", deps=[("req:a", "derives_from")]),
            _node("design:c", deps=[("design:b", "refines")]),
            _node("plan:d", "plan", deps=[("design:c", "implements")]),
        ]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:a"}, _impact_cfg(max_hops=2)))
    assert "design:b" in impacted  # 1 hop
    assert "design:c" in impacted  # 2 hop
    assert "plan:d" not in impacted  # 3 hop → 打ち切り


def test_impact_unknown_relation_uses_fallback_weight() -> None:
    graph = cc.build_graph(
        [_node("req:r", "requirement"), _node("design:d", deps=[("req:r", "weird")])]
    )
    impacted = _by_id(cc.compute_impact(graph, {"req:r"}, _impact_cfg()))
    # 未知 relation は弱依存フォールバック（0.3）→ Gray。
    assert impacted["design:d"].score == cc.UNKNOWN_RELATION_WEIGHT
    assert impacted["design:d"].band == cc.BAND_GRAY


# ---------------------------------------------------------------------------
# impact 分析（CLI: git 連携）
# ---------------------------------------------------------------------------


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")


def test_compute_impact_result_maps_diff_to_downstream(tmp_path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    # 上流 req を未コミット編集 → diff HEAD に出る
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement") + "\n変更\n")

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert result.changed_ids == ["req:r"]
    impacted = _by_id(result.impacted)
    assert impacted["design:d"].band == cc.BAND_GREEN


def test_compute_impact_result_reports_deleted_upstream(tmp_path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "docs/req.md").unlink()  # 上流削除（未コミット）

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert "docs/req.md" in result.deleted_upstream
    assert result.changed_ids == []  # 削除されたファイルはノードに残らない
    assert result.impacted == []  # 削除は deleted_upstream に分離され impacted には出ない


def test_compute_impact_result_reports_deleted_upstream_with_pathspec_magic_like_filename(
    tmp_path,
) -> None:
    # 削除された CODD ノードのファイル名が `:(bad.md` のような pathspec magic
    # 接頭辞と衝突する場合でも `deleted_upstream` を検出できる必要がある。
    # `_ref_blob_mode()`（`_old_node_id_at_ref` が使う `git ls-tree`）が
    # `:(literal)` 化されていないと `Invalid pathspec magic` で失敗し、旧 node_id
    # を復元できず検出漏れになる（Issue #98 レビュー対応: 11巡目 codd.py:1135）。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/:(bad.md", _doc("design:bad", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "docs/:(bad.md").unlink()  # 上流削除（未コミット）

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert "docs/:(bad.md" in result.deleted_upstream


def test_deleted_upstream_excludes_out_of_scope_md(tmp_path) -> None:
    # scope は docs/**/*.md。スコープ外の README.md 削除は dangling 注意に含めない。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(tmp_path, "README.md", "# readme\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "docs/req.md").unlink()
    (tmp_path / "README.md").unlink()

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert "docs/req.md" in result.deleted_upstream
    assert "README.md" not in result.deleted_upstream  # スコープ外を誤検出しない
    assert result.impacted == []  # 削除は deleted_upstream に分離され impacted には出ない


def test_path_in_scope() -> None:
    config = _config(scope={"include": ["docs/**/*.md"], "exclude": ["docs/adr/_template.md"]})
    assert cli.path_in_scope(_ROOT, "docs/x.md", config) is True
    assert cli.path_in_scope(_ROOT, "docs/sub/y.md", config) is True
    assert cli.path_in_scope(_ROOT, "README.md", config) is False  # スコープ外
    assert cli.path_in_scope(_ROOT, "docs/x.txt", config) is False  # 拡張子不一致
    assert cli.path_in_scope(_ROOT, "docs/adr/_template.md", config) is False  # exclude


def test_path_in_scope_single_star_is_segment_aware() -> None:
    # 単層 glob (dir/*.md) は 1 セグメントのみ。サブディレクトリを跨いではならない。
    config = _config(scope={"include": [".claude/rules/*.md"], "exclude": []})
    assert cli.path_in_scope(_ROOT, ".claude/rules/foo.md", config) is True
    assert cli.path_in_scope(_ROOT, ".claude/rules/sub/deep.md", config) is False  # 単層を跨がない


def test_path_in_scope_character_class_matches_like_path_glob() -> None:
    # `[ab]` のような glob 文字クラスは、通常走査（collect_files の Path.glob）と
    # 削除後 impact 判定（_scope_pattern_to_regex）とで解釈が一致しなければならない
    # （Issue #98 レビュー対応）。以前は `[` `]` をリテラルエスケープしており、
    # `docs/[ab].md` が `a.md` / `b.md` にマッチしなかった。
    config = _config(scope={"include": ["docs/[ab].md"], "exclude": []})
    assert cli.path_in_scope(_ROOT, "docs/a.md", config) is True
    assert cli.path_in_scope(_ROOT, "docs/b.md", config) is True
    assert cli.path_in_scope(_ROOT, "docs/c.md", config) is False


def test_path_in_scope_character_class_negation() -> None:
    config = _config(scope={"include": ["docs/[!ab].md"], "exclude": []})
    assert cli.path_in_scope(_ROOT, "docs/c.md", config) is True
    assert cli.path_in_scope(_ROOT, "docs/a.md", config) is False


def test_path_in_scope_character_class_range() -> None:
    config = _config(scope={"include": ["docs/[a-c].md"], "exclude": []})
    assert cli.path_in_scope(_ROOT, "docs/b.md", config) is True
    assert cli.path_in_scope(_ROOT, "docs/d.md", config) is False


def test_scope_pattern_unterminated_bracket_is_literal() -> None:
    # 閉じ ] が無い場合は fnmatch と同様リテラル [ として扱う（クラッシュしない）。
    regex = cli._scope_pattern_to_regex("docs/[abc.md")
    assert regex.fullmatch("docs/[abc.md") is not None
    assert regex.fullmatch("docs/a.md") is None


def test_scope_pattern_invalid_char_range_is_safe_non_match() -> None:
    # `[z-a]` は逆順の不正な範囲。`_scope_pattern_to_regex()` は re.compile が
    # re.error を送出した場合に備え、常に非マッチの正規表現へフォールバックする
    # （クラッシュしない。Issue #98 レビュー対応）。現行 CPython（3.12+）の
    # `fnmatch.translate()` 自体もこの種の不正な範囲を検出して常に非マッチの
    # パターンへ変換するため、Path.glob 経由の collect_files() 側でも同様に
    # クラッシュせず非マッチになる（下記
    # test_collect_files_invalid_char_range_pattern_is_safe_non_match で検証）。
    regex = cli._scope_pattern_to_regex("docs/[z-a].md")
    assert regex.fullmatch("docs/a.md") is None
    assert regex.fullmatch("docs/z.md") is None
    assert (
        cli.path_in_scope(
            _ROOT, "docs/a.md", _config(scope={"include": ["docs/[z-a].md"], "exclude": []})
        )
        is False
    )


def test_collect_files_invalid_char_range_pattern_is_safe_non_match(tmp_path) -> None:
    # `_scope_pattern_to_regex()`（削除後 impact 判定側）だけでなく、通常走査
    # （collect_files の Path.glob 経由）でも `[z-a]` のような不正な文字範囲が
    # クラッシュせず非マッチになることを実ファイルで検証する（レビュー対応:
    # 「Path.glob と同様非マッチ」という前提が両実装で整合しているかの確認）。
    _write(tmp_path, "docs/a.md", "# a\n")
    _write(tmp_path, "docs/z.md", "# z\n")
    config = _config(scope={"include": ["docs/[z-a].md"], "exclude": []})

    collected = {p.relative_to(tmp_path).as_posix() for p in cli.collect_files(tmp_path, config)}

    assert collected == set()


def test_scope_pattern_partial_invalid_char_range_keeps_valid_literal() -> None:
    # `[ab-a]` は `b-a` という逆順の不正範囲を含む一方、リテラル `a` も含む。
    # CPython 3.12+ の `fnmatch.translate()`（Path.glob の内部実装）はこの不正範囲
    # だけを除去し、残った有効なリテラル文字（`a`）は保持する。以前は
    # `_scope_pattern_to_regex()` の `re.compile` が `re.error` を送出した際、
    # パターン全体を常時非マッチへフォールバックしていたため、ファイルが存在する
    # 間は（Path.glob 経由で）scan 対象になる一方、削除後の `path_in_scope()` では
    # 対象外となり、消失した上流ノードの警告を取りこぼしていた
    # （レビュー対応: 8巡目 codd.py:859 P3）。
    regex = cli._scope_pattern_to_regex("docs/[ab-a].md")
    assert regex.fullmatch("docs/a.md") is not None
    assert regex.fullmatch("docs/b.md") is None
    assert (
        cli.path_in_scope(
            _ROOT, "docs/a.md", _config(scope={"include": ["docs/[ab-a].md"], "exclude": []})
        )
        is True
    )


def test_collect_files_partial_invalid_char_range_matches_valid_literal(tmp_path) -> None:
    # 実ファイルでも Path.glob（collect_files）と path_in_scope（削除後判定）の
    # 解釈が一致することを確認する（fnmatch と同じ「不正範囲だけ除去」規約）。
    _write(tmp_path, "docs/a.md", "# a\n")
    _write(tmp_path, "docs/b.md", "# b\n")
    config = _config(scope={"include": ["docs/[ab-a].md"], "exclude": []})

    collected = {p.relative_to(tmp_path).as_posix() for p in cli.collect_files(tmp_path, config)}

    assert collected == {"docs/a.md"}
    assert cli.path_in_scope(tmp_path, "docs/a.md", config) is True
    assert cli.path_in_scope(tmp_path, "docs/b.md", config) is False


def test_path_in_scope_character_class_matches_path_glob_behavior(tmp_path) -> None:
    # 通常走査（Path.glob 経由の collect_files）と削除後判定（_matches_scope_pattern）
    # の解釈が一致することを実ファイルで確認する。
    _write(tmp_path, "docs/a.md", "# a\n")
    _write(tmp_path, "docs/c.md", "# c\n")
    config = _config(scope={"include": ["docs/[ab].md"], "exclude": []})

    collected = {p.relative_to(tmp_path).as_posix() for p in cli.collect_files(tmp_path, config)}

    assert collected == {"docs/a.md"}
    assert cli.path_in_scope(tmp_path, "docs/a.md", config) is True
    assert cli.path_in_scope(tmp_path, "docs/c.md", config) is False


def test_impact_config_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"decay": 2.0})  # (0, 1] 外
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"max_hops": 0})  # 1 未満
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"green_threshold": 0.3, "amber_threshold": 0.5})  # 帯域逆転
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"green_threshold": 1.5})  # [0, 1] 外
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"amber_threshold": -0.1})  # [0, 1] 外
    with pytest.raises(ValueError):
        cc.ImpactConfig.from_dict({"corroboration_min_origins": 0})  # 1 未満


def test_cmd_impact_json_output(tmp_path, capsys) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement") + "\nx\n")

    assert cli.cmd_impact(tmp_path, _config(), "HEAD", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ref"] == "HEAD"
    assert payload["changed_nodes"] == ["req:r"]
    entry = {e["node_id"]: e for e in payload["impacted"]}["design:d"]
    assert entry["band"] == cc.BAND_GREEN
    assert entry["origins"] == ["req:r"]


def test_compute_impact_result_reports_deleted_code_upstream(tmp_path) -> None:
    # 注釈付きコードファイルの削除も dangling 化の可能性として deleted_upstream に
    # 検出される（Issue #98 レビュー対応。以前は doc scope のみ対象だった）。
    _init_repo(tmp_path)
    _write(tmp_path, "src/mod.py", _py(["codd:node_id code:mod"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "src/mod.py").unlink()  # コード側の上流削除（未コミット）

    result = cli.compute_impact_result(tmp_path, config, "HEAD")
    assert "src/mod.py" in result.deleted_upstream


def test_compute_impact_result_recovers_upstream_when_code_symlink_target_deleted(
    tmp_path,
) -> None:
    # `aliases/core.py -> ../shared/core.py` のような symlink で、リンク先
    # （shared/core.py。scope 外）だけが削除されると alias 自体は git 上
    # 変更されないため `git diff` の changed/deleted どちらにも現れない。alias は
    # 破損 symlink になり通常走査（is_file() 判定）から静かに落ちるため、alias が
    # 保持していた旧コードノードの消失が deleted_upstream に警告されていなかった
    # （レビュー対応: 8巡目 codd.py:77）。
    _init_repo(tmp_path)
    _write(tmp_path, "shared/core.py", _py(["codd:node_id code:core"]))
    (tmp_path / "aliases").mkdir()
    (tmp_path / "aliases" / "core.py").symlink_to(Path("../shared/core.py"))
    config = _config(code_scope={"include": ["aliases/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    (tmp_path / "shared" / "core.py").unlink()  # ターゲットのみ削除（alias 自体は未変更）

    result = cli.compute_impact_result(tmp_path, config, "HEAD")

    assert "aliases/core.py" in result.deleted_upstream


def test_compute_impact_result_recovers_upstream_when_code_symlink_target_node_id_changes(
    tmp_path,
) -> None:
    # `aliases/core.py -> ../shared/core.py` のような symlink で、code_scope には
    # alias（aliases/core.py）だけが含まれ、リンク先（shared/core.py）自体は scope 外
    # というケース。リンク先の node_id を変更（または注釈を削除）しても、alias 自体の
    # パスは git 上変更されないため `git diff` には現れず、リンク先パスは
    # path_in_code_scope() で scope 外と判定されて素通りしてしまい、旧 node_id の消失が
    # deleted_upstream に検出されなかった（Issue #98 レビュー対応: 10巡目 codd.py:1285）。
    _init_repo(tmp_path)
    _write(tmp_path, "shared/core.py", _py(["codd:node_id code:core"]))
    (tmp_path / "aliases").mkdir()
    (tmp_path / "aliases" / "core.py").symlink_to(Path("../shared/core.py"))
    config = _config(code_scope={"include": ["aliases/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    # ターゲットは削除せず node_id だけ差し替える（alias 自体は未変更のまま）。
    _write(tmp_path, "shared/core.py", _py(["codd:node_id code:core-v2"]))

    result = cli.compute_impact_result(tmp_path, config, "HEAD")

    assert "aliases/core.py" in result.deleted_upstream


def test_broken_code_symlink_relpaths_empty_when_code_scope_unset(tmp_path) -> None:
    # code_scope.include が空（既定）なら、壊れた symlink があっても検出しない
    # （opt-in・既存挙動への影響ゼロ）。
    (tmp_path / "aliases").mkdir()
    (tmp_path / "aliases" / "broken.py").symlink_to(Path("../missing.py"))

    assert cli._broken_code_symlink_relpaths(tmp_path, _config()) == set()


def test_old_node_id_at_ref_skips_git_show_for_unsupported_extension(tmp_path, monkeypatch) -> None:
    # working tree 側の scan_code_nodes と同様、削除済み blob も `git show` で読む前に
    # 拡張子チェックする（未対応拡張子の blob を無駄に読み込まない。Issue #98 レビュー対応）。
    _init_repo(tmp_path)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    config = _config(code_scope={"include": ["assets/**/*"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    calls: list[list[str]] = []
    original = cli._git_output_bytes

    def _spy(root: Path, args: list[str]) -> bytes | None:
        calls.append(args)
        return original(root, args)

    monkeypatch.setattr(cli, "_git_output_bytes", _spy)

    old_node_id = cli._old_node_id_at_ref(tmp_path, "HEAD", "assets/logo.png", config, is_code=True)

    assert old_node_id is None
    assert calls == []  # `git show` は呼ばれない（拡張子チェックで先に弾く）


def test_compute_impact_result_detects_dangling_when_code_annotation_removed(tmp_path) -> None:
    # ファイルは残ったまま `codd:` 注釈を削除した場合も、旧コードノードの消失を
    # 検出できるべき（削除ではなく変更のため、以前は deleted_paths のみ見ていて
    # 検出されなかった。Issue #98 レビュー対応）。
    _init_repo(tmp_path)
    _write(tmp_path, "src/mod.py", _py(["codd:node_id code:mod"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _write(tmp_path, "src/mod.py", "pass\n")  # 注釈を削除（ファイル自体は残す）

    result = cli.compute_impact_result(tmp_path, config, "HEAD")

    assert "src/mod.py" in result.deleted_upstream


def test_compute_impact_result_detects_dangling_when_code_node_id_changed(tmp_path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/mod.py", _py(["codd:node_id code:old-slug"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _write(tmp_path, "src/mod.py", _py(["codd:node_id code:new-slug"]))

    result = cli.compute_impact_result(tmp_path, config, "HEAD")

    assert "src/mod.py" in result.deleted_upstream
    assert "code:new-slug" in result.changed_ids


def test_compute_impact_result_excludes_changed_code_file_when_node_id_unchanged(tmp_path) -> None:
    # node_id が変わらない通常の変更は dangling ではない（誤検出しない）。
    _init_repo(tmp_path)
    _write(tmp_path, "src/mod.py", _py(["codd:node_id code:mod"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _write(tmp_path, "src/mod.py", _py(["codd:node_id code:mod", "codd:owner ai-orchestra"]))

    result = cli.compute_impact_result(tmp_path, config, "HEAD")

    assert "src/mod.py" not in result.deleted_upstream
    assert "code:mod" in result.changed_ids


def test_rename_keeps_node_out_of_deleted_upstream(tmp_path) -> None:
    # rename で node_id が新パスに引き継がれた上流は dangling 警告に出さない。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _git(tmp_path, "mv", "docs/req.md", "docs/req2.md")  # node_id は req:r のまま移動

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")
    assert "docs/req.md" not in result.deleted_upstream  # 移動しただけ → dangling ではない
    assert result.deleted_upstream == []
    assert "req:r" in result.changed_ids  # 移動先は changed 扱い → 下流が影響を受ける


def test_compute_impact_result_raises_on_invalid_ref(tmp_path) -> None:
    # 無効な ref / git 失敗を空の成功結果にせず、明示的にエラーへ昇格させる。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    with pytest.raises(cli.ImpactError):
        cli.compute_impact_result(tmp_path, _config(), "no-such-ref")


def test_cmd_impact_returns_nonzero_on_invalid_ref(tmp_path, capsys) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    assert cli.cmd_impact(tmp_path, _config(), "no-such-ref", as_json=False) == 2
    assert "ERROR" in capsys.readouterr().err


def test_main_reports_config_error_instead_of_traceback(tmp_path, capsys) -> None:
    # scope.include に非文字列（数値）を書いた不正設定は CoddConfig.from_dict の
    # _as_glob_list() で ValueError になる。main() はこれをトレースバックではなく
    # `[codd] ERROR:` として整形すべき（Issue #98 レビュー対応）。
    config_path = tmp_path / ".claude/config/codd/codd.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("scope:\n  include: 42\n", encoding="utf-8")

    exit_code = cli.main(["--root", str(tmp_path), "--config", str(config_path), "scan"])

    assert exit_code == 2
    assert "[codd] ERROR:" in capsys.readouterr().err


def test_main_reports_config_error_for_bool_impact_field(tmp_path, capsys) -> None:
    # impact.* に bool を渡すと _reject_bool_as_number が TypeError で拒否する。
    # main() はこれも ValueError と同様に設定エラーとして扱う。
    config_path = tmp_path / ".claude/config/codd/codd.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("impact:\n  decay: true\n", encoding="utf-8")

    exit_code = cli.main(["--root", str(tmp_path), "--config", str(config_path), "scan"])

    assert exit_code == 2
    assert "[codd] ERROR:" in capsys.readouterr().err


def test_main_reports_config_error_for_non_mapping_code_scope(tmp_path, capsys) -> None:
    # `code_scope: oops`（文字列）は `.get()` で AttributeError になり main() の
    # (TypeError, ValueError) ハンドラを素通りしていた（Issue #98 レビュー対応）。
    config_path = tmp_path / ".claude/config/codd/codd.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("code_scope: oops\n", encoding="utf-8")

    exit_code = cli.main(["--root", str(tmp_path), "--config", str(config_path), "scan"])

    assert exit_code == 2
    assert "[codd] ERROR:" in capsys.readouterr().err


def test_glob_relpaths_converts_glob_value_error_to_descriptive_message(
    tmp_path, monkeypatch
) -> None:
    # `src/**.py` のような不正な再帰 glob（`**` がパスセグメント全体を占めていない）は
    # Python 3.12+ の `Path.glob()` が `ValueError` を送出する。この呼び出しは
    # `main()` の設定読み込み用例外ハンドラより後（scan/validate/impact の走査時）に
    # 実行されるため、`_glob_relpaths()` が捕捉してパターンを含む分かりやすい
    # `ValueError` に変換する（Issue #98 レビュー対応: 8巡目）。実行中の Python
    # バージョンによっては当該パターンで例外が出ないことがあるため、`Path.glob` を
    # 差し替えて確実に再現する。
    real_glob = Path.glob

    def _fake_glob(self, pattern, *args, **kwargs):
        if pattern == "src/**.py":
            raise ValueError("Invalid pattern: '**' can only be an entire path component")
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", _fake_glob)

    with pytest.raises(ValueError, match=r"src/\*\*\.py"):
        cli._glob_relpaths(tmp_path, ["src/**.py"])


def test_glob_relpaths_converts_absolute_pattern_not_implemented_error_to_descriptive_message(
    tmp_path, monkeypatch
) -> None:
    # `/workspace/project/src/**/*.py` のような絶対パスの glob パターンは
    # Python 3.13+ の `Path.glob()` が `NotImplementedError`（"Non-relative patterns
    # are unsupported"）を送出する。`ValueError` しか捕捉していないと、この呼び出しは
    # `main()` の設定読み込み用例外ハンドラより後（scan/validate/impact の走査時）に
    # 実行されるためトレースバックで CLI が終了してしまう。`_glob_relpaths()` が
    # `NotImplementedError` も捕捉してパターンを含む分かりやすい `ValueError` に
    # 変換する必要がある（Issue #98 レビュー対応: 11巡目 codd.py:88）。実行中の
    # Python バージョンによっては当該パターンで例外が出ないことがあるため、
    # `Path.glob` を差し替えて確実に再現する。
    real_glob = Path.glob

    def _fake_glob(self, pattern, *args, **kwargs):
        if pattern == "/workspace/project/src/**/*.py":
            raise NotImplementedError("Non-relative patterns are unsupported")
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", _fake_glob)

    with pytest.raises(ValueError, match=r"/workspace/project/src/\*\*/\*\.py"):
        cli._glob_relpaths(tmp_path, ["/workspace/project/src/**/*.py"])


def test_main_reports_config_error_for_absolute_glob_pattern(tmp_path, monkeypatch, capsys) -> None:
    # `_glob_relpaths()` が変換した `NotImplementedError` 由来の `ValueError` を、
    # scan 実行時（load_config 完了後）でも main() がトレースバックではなく
    # `[codd] ERROR:` として整形して終了することを確認する（Issue #98 レビュー対応:
    # 11巡目 codd.py:88）。
    real_glob = Path.glob

    def _fake_glob(self, pattern, *args, **kwargs):
        if pattern == "/workspace/project/src/**/*.py":
            raise NotImplementedError("Non-relative patterns are unsupported")
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", _fake_glob)

    config_path = tmp_path / ".claude/config/codd/codd.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        'scope:\n  include: ["/workspace/project/src/**/*.py"]\n  exclude: []\n', encoding="utf-8"
    )

    exit_code = cli.main(["--root", str(tmp_path), "--config", str(config_path), "scan"])

    assert exit_code == 2
    assert "[codd] ERROR:" in capsys.readouterr().err


def test_main_reports_config_error_for_invalid_recursive_glob(
    tmp_path, monkeypatch, capsys
) -> None:
    # `_glob_relpaths()` が変換した ValueError を、scan 実行時（load_config 完了後）
    # でも main() がトレースバックではなく `[codd] ERROR:` として整形して終了する
    # ことを確認する（Issue #98 レビュー対応: 8巡目）。
    real_glob = Path.glob

    def _fake_glob(self, pattern, *args, **kwargs):
        if pattern == "src/**.py":
            raise ValueError("Invalid pattern: '**' can only be an entire path component")
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", _fake_glob)

    config_path = tmp_path / ".claude/config/codd/codd.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('scope:\n  include: ["src/**.py"]\n  exclude: []\n', encoding="utf-8")

    exit_code = cli.main(["--root", str(tmp_path), "--config", str(config_path), "scan"])

    assert exit_code == 2
    assert "[codd] ERROR:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# code_scope（コード⇔ドキュメントのトレーサビリティ、Issue #98）
# ---------------------------------------------------------------------------


def _py(annotation_lines: list[str], body: str = "pass\n") -> str:
    """コード注釈付き Python ソース断片を組み立てる（module docstring 形式）。"""
    lines = ['"""'] + annotation_lines + ['"""', "", body]
    return "\n".join(lines)


def test_collect_code_files_empty_by_default(tmp_path) -> None:
    # code_scope 未設定（既定）ではソースファイルが存在しても走査対象ゼロ（opt-in）。
    _write(tmp_path, "src/mod.py", _py(["codd:implements design:x"]))
    assert cli.collect_code_files(tmp_path, _config()) == []


def test_collect_code_files_default_exclude_globs_match_nested_files(tmp_path) -> None:
    # `**/.venv/**` の末尾を `/**/*` にしないと、Python 3.12 の Path.glob() では
    # ディレクトリのみが返り、`_glob_relpaths` の is_file() フィルタで実質無効になる
    # （Issue #98 レビュー対応）。既定 exclude と同じパターンで検証する。
    _write(tmp_path, "src/mod.py", _py(["codd:implements design:x"]))
    _write(tmp_path, "src/.venv/lib/pkg.py", _py(["codd:implements design:y"]))
    _write(tmp_path, "src/__pycache__/mod.cpython-312.pyc", "binary")
    config = _config(
        code_scope={
            "include": ["src/**/*.py", "src/**/*.pyc"],
            "exclude": ["**/__pycache__/**/*", "**/.venv/**/*"],
        }
    )
    files = {p.relative_to(tmp_path).as_posix() for p in cli.collect_code_files(tmp_path, config)}
    assert files == {"src/mod.py"}


def test_scan_code_nodes_skips_files_without_annotation(tmp_path) -> None:
    _write(tmp_path, "src/plain.py", "def f():\n    pass\n")
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    nodes, errors = cli.scan_code_nodes(tmp_path, config)
    assert nodes == []
    assert errors == []


def test_scan_code_node_malformed_annotation_reports_error(tmp_path) -> None:
    # 値の無い依存注釈（`codd:implements` のみ）は依存から黙って除外せず、
    # malformed_annotation として validate のエラーに乗る（Issue #98 レビュー対応）。
    _write(tmp_path, "src/mod.py", _py(["codd:implements"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    result = cli.scan_project(tmp_path, config)
    assert result.malformed_annotations != []
    grouped = _checks(result, config, tmp_path)
    assert len(grouped["malformed_annotation"]) == 1
    assert "src/mod.py" in grouped["malformed_annotation"][0].message


def test_scan_code_nodes_supports_pep263_coding_cookie(tmp_path) -> None:
    # PEP 263 の coding cookie 付き Python ファイルは宣言エンコーディングで読む
    # （固定 UTF-8 のままだと Latin-1 等で UnicodeDecodeError になる）。
    path = tmp_path / "src" / "legacy.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = '# -*- coding: latin-1 -*-\n"""\ncodd:implements design:d\nnote: café\n"""\n'
    path.write_bytes(content.encode("latin-1"))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    nodes, errors = cli.scan_code_nodes(tmp_path, config)
    assert errors == []
    assert len(nodes) == 1
    assert nodes[0].depends_on[0].id == "design:d"


def test_read_source_text_returns_none_for_undecodable_ts_file(tmp_path) -> None:
    # UTF-16 で保存された .ts 等は working tree 側の _read_source_text が無防備だと
    # UnicodeDecodeError で scan 全体を落としてしまう。復号失敗は None を返し、
    # 呼び出し側で黙ってスキップする（_decode_ref_source と同じ規約。Issue #98 レビュー対応）。
    path = tmp_path / "src" / "legacy.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("codd:implements design:d".encode("utf-16"))
    assert cli._read_source_text(path) is None


def test_read_source_text_returns_none_for_bogus_coding_cookie(tmp_path) -> None:
    # 不正な coding cookie は tokenize.detect_encoding が SyntaxError/LookupError を
    # 投げる。working tree 側でもこれを握りつぶし None を返す。
    path = tmp_path / "src" / "bogus.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# -*- coding: bogus-encoding-name -*-\npass\n")
    assert cli._read_source_text(path) is None


def test_scan_code_nodes_skips_undecodable_ts_file_without_crashing(tmp_path) -> None:
    # scan_code_nodes（scan/validate/impact の共通経路）は復号不能ファイルを
    # スキップし、他の正常ファイルは通常どおり抽出する。
    path = tmp_path / "src" / "legacy.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("// codd:implements design:d".encode("utf-16"))
    _write(tmp_path, "src/mod.ts", "// codd:implements design:d\n")
    config = _config(code_scope={"include": ["src/**/*.ts"], "exclude": []})

    nodes, errors = cli.scan_code_nodes(tmp_path, config)

    assert errors == []
    assert [n.node_id for n in nodes] == ["code:mod"]


def test_scan_code_nodes_skips_python_file_with_bogus_coding_cookie(tmp_path) -> None:
    path = tmp_path / "src" / "bogus.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# -*- coding: bogus-encoding-name -*-\npass\n")
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})

    nodes, errors = cli.scan_code_nodes(tmp_path, config)

    assert nodes == []
    assert errors == []


def test_scan_project_merges_code_nodes_into_doc_graph(tmp_path) -> None:
    _write(tmp_path, "docs/design.md", _doc("design:d", "design"))
    _write(
        tmp_path,
        "src/mod.py",
        _py(["codd:implements design:d"]),
    )
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    result = cli.scan_project(tmp_path, config)
    ids = {n.node_id for n in result.nodes}
    assert ids == {"design:d", "code:mod"}
    assert result.graph.incoming_count("design:d") == 1
    # code_scope はコードファイル未走査の doc missing_frontmatter とは独立（無関係の doc に影響しない）。
    assert result.missing_frontmatter == []


def test_scan_code_node_dangling_dependency_is_validated(tmp_path) -> None:
    # code ノードも既存の dangling 検査にそのまま乗る（特別扱い不要）。
    _write(tmp_path, "src/mod.py", _py(["codd:implements design:missing"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    result = cli.scan_project(tmp_path, config)
    grouped = _checks(result, config, tmp_path)
    assert len(grouped["dangling"]) == 1
    assert "src/mod.py" in grouped["dangling"][0].message


def test_scan_code_node_confidence_defaults_to_config_value(tmp_path) -> None:
    _write(tmp_path, "src/mod.py", _py(["codd:implements design:d"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    result = cli.scan_project(tmp_path, config)
    node = next(n for n in result.nodes if n.node_id == "code:mod")
    assert node.depends_on[0].confidence == cc.DEFAULT_INLINE_CONFIDENCE


def test_scan_code_node_confidence_configurable(tmp_path) -> None:
    _write(tmp_path, "src/mod.py", _py(["codd:implements design:d"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []}, inline_confidence=0.3)
    result = cli.scan_project(tmp_path, config)
    node = next(n for n in result.nodes if n.node_id == "code:mod")
    assert node.depends_on[0].confidence == 0.3


def test_write_graph_jsonl_omits_default_confidence(tmp_path) -> None:
    _write(
        tmp_path,
        "docs/design.md",
        _doc("design:d", "design", deps=[("req:r", "derives_from")]),
    )
    result = cli.scan_project(tmp_path, _config())
    out = tmp_path / ".claude/codd/graph.jsonl"
    cli.write_graph_jsonl(result, out)
    records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert records[0]["depends_on"] == [{"id": "req:r", "relation": "derives_from"}]


def test_write_graph_jsonl_includes_non_default_confidence(tmp_path) -> None:
    _write(tmp_path, "src/mod.py", _py(["codd:implements design:d"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    result = cli.scan_project(tmp_path, config)
    out = tmp_path / ".claude/codd/graph.jsonl"
    cli.write_graph_jsonl(result, out)
    records = {r["node_id"]: r for r in (json.loads(line) for line in out.read_text().splitlines())}
    dep = records["code:mod"]["depends_on"][0]
    assert dep["confidence"] == cc.DEFAULT_INLINE_CONFIDENCE


def test_impact_weighs_code_link_by_confidence(tmp_path) -> None:
    # code ノードの低信頼リンクは、同じ relation の doc リンクより impact スコアが低くなる。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/design.md", _doc("design:d", "design"))
    _write(tmp_path, "src/mod.py", _py(["codd:implements design:d"]))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _write(tmp_path, "docs/design.md", _doc("design:d", "design") + "\nx\n")

    result = cli.compute_impact_result(tmp_path, config, "HEAD")
    entry = next(n for n in result.impacted if n.node_id == "code:mod")
    assert entry.score == pytest.approx(cc.DEFAULT_INLINE_CONFIDENCE)
    assert entry.band == cc.BAND_AMBER  # 0.7 は green_threshold(0.8) 未満


def test_scan_code_nodes_skips_unsupported_extension_without_reading(tmp_path) -> None:
    # code_scope の混在 glob（例: `assets/**/*`）が画像等の対応外ファイルにマッチしても、
    # UTF-8 テキストとして読み込もうとしない（不正バイト列で UnicodeDecodeError に
    # なる／文字化けするのを防ぐ。Issue #98 レビュー対応）。
    _write(tmp_path, "assets/logo.png", "placeholder")
    (tmp_path / "assets/logo.png").write_bytes(b"\xff\xd8\xff\xe0\x00\x10not-utf8\xff\xfe")
    _write(tmp_path, "assets/mod.py", _py(["codd:implements design:d"]))
    config = _config(code_scope={"include": ["assets/**/*"], "exclude": []})

    nodes, errors = cli.scan_code_nodes(tmp_path, config)

    assert errors == []
    assert [n.node_id for n in nodes] == ["code:mod"]


def test_collect_code_files_excludes_paths_resolving_outside_root(tmp_path) -> None:
    # `../*.py` のような glob はプロジェクトルート外のファイルを解決してしまう
    # ため、root 配下に収まらないパスは除外する（Issue #98 レビュー対応）。
    root = tmp_path / "proj"
    root.mkdir()
    _write(tmp_path, "outside.py", _py(["codd:implements design:x"]))
    _write(root, "src/mod.py", _py(["codd:implements design:y"]))
    config = _config(code_scope={"include": ["../*.py", "src/**/*.py"], "exclude": []})

    files = {p.relative_to(root).as_posix() for p in cli.collect_code_files(root, config)}

    assert files == {"src/mod.py"}


def test_collect_code_files_normalizes_glob_paths_that_return_into_root(tmp_path) -> None:
    # `../proj/src/**/*.py`（root == proj）は containment 判定こそ通るが、正規化せず
    # `path.relative_to(root)` すると "../proj/..." という別名の文字列で集合に入り、
    # 通常パターン（`src/**/*.py`）で見つかる同一ファイルと重複ノード化してしまう
    # （Issue #98 レビュー対応）。root からの相対に正規化して単一エントリになるべき。
    root = tmp_path / "proj"
    root.mkdir()
    _write(root, "src/mod.py", _py(["codd:implements design:y"]))
    config = _config(code_scope={"include": ["../proj/src/**/*.py", "src/**/*.py"], "exclude": []})

    files = {p.relative_to(root).as_posix() for p in cli.collect_code_files(root, config)}

    assert files == {"src/mod.py"}


def test_collect_files_preserves_symlink_logical_path(tmp_path) -> None:
    # scope.include が root 内部の symlink にマッチした場合、解決先のパスではなく
    # symlink 自体の論理パスを登録する必要がある。解決先を登録すると `git diff` が
    # 返す symlink 自体のパスと `path_to_id` が一致せず、リンクの変更が impact 分析
    # から欠落する（P2 レビュー対応: codd.py:74）。root 外への解決を拒否する
    # 安全性チェックはこれまでどおり維持する。
    root = tmp_path
    _write(root, "shared/actual.md", "# actual\n")
    (root / "docs").mkdir()
    (root / "docs" / "link.md").symlink_to(root / "shared" / "actual.md")
    config = _config(scope={"include": ["docs/*.md"], "exclude": []})

    files = {p.relative_to(root).as_posix() for p in cli.collect_files(root, config)}

    assert files == {"docs/link.md"}


def test_collect_files_rejects_symlink_resolving_outside_root(tmp_path) -> None:
    # symlink のターゲットが root 外へ解決される場合は、論理パス保持の対象にせず
    # 従来どおり安全に除外する。
    root = tmp_path / "proj"
    root.mkdir()
    _write(tmp_path, "outside/actual.md", "# outside\n")
    (root / "docs").mkdir()
    (root / "docs" / "link.md").symlink_to(tmp_path / "outside" / "actual.md")
    config = _config(scope={"include": ["docs/*.md"], "exclude": []})

    files = {p.relative_to(root).as_posix() for p in cli.collect_files(root, config)}

    assert files == set()


def test_compute_impact_result_detects_change_via_symlink_target(tmp_path) -> None:
    # scan は symlink（working tree の node.path）を dereference してリンク先の
    # 内容を注釈として読むため、リンク先だけを変更した場合も `git diff` はリンク先
    # のパスを返す。node.path（symlink 自身）しか見ないと変更が検出されない
    # （レビュー対応: codd.py:84 scenario 1: リンク先だけを変更）。
    _init_repo(tmp_path)
    _write(tmp_path, "shared/actual.md", _doc("design:d", "design"))
    (tmp_path / "docs").mkdir()
    # 相対 symlink（portable な書き方）。git はこの相対パス文字列をそのまま
    # symlink blob として保存するため、ref 側の dereference（
    # `_resolve_ref_symlink_target`）はこの表現を前提にしている。
    (tmp_path / "docs" / "link.md").symlink_to(Path("../shared/actual.md"))
    _write(
        tmp_path,
        "docs/downstream.md",
        _doc("design:down", "design", deps=[("design:d", "derives_from")]),
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    # symlink 自体は変更せず、リンク先（root 外の shared/actual.md）だけを編集する。
    (tmp_path / "shared" / "actual.md").write_text(
        _doc("design:d", "design") + "\n変更\n", encoding="utf-8"
    )

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")

    assert "design:d" in result.changed_ids
    impacted = _by_id(result.impacted)
    assert impacted["design:down"].band == cc.BAND_GREEN


def test_compute_impact_result_detects_change_via_root_internal_absolute_symlink_target(
    tmp_path,
) -> None:
    # symlink のリンク先が root 内の絶対パス（例: `<root>/shared/mod.py`）の場合、
    # scan 側の containment 検査（`_glob_relpaths`）はこの alias をノードとして
    # 登録するのに対し、リンク先解決側（`_symlink_target_relpath`）が絶対パスを
    # 一律 None にすると、リンク先だけを変更しても alias の node_id が
    # `changed_ids` に入らず `codd impact` が下流影響を取りこぼす
    # （Issue #98 レビュー対応: 11巡目 codd.py:1103）。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/req.md", _doc("req:r", "requirement"))
    _write(tmp_path, "shared/mod.py", _py(["codd:implements req:r"]))
    (tmp_path / "aliases").mkdir()
    (tmp_path / "aliases" / "mod.py").symlink_to(tmp_path / "shared" / "mod.py")
    config = _config(code_scope={"include": ["aliases/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    # alias 自体は変更せず、リンク先（root 内の絶対パス経由）だけを編集する。
    (tmp_path / "shared" / "mod.py").write_text(
        _py(["codd:implements req:r"], body="print('v2')\n"), encoding="utf-8"
    )

    result = cli.compute_impact_result(tmp_path, config, "HEAD")

    assert "code:mod" in result.changed_ids


def test_compute_impact_result_handles_multiple_symlinks_to_same_target(
    tmp_path, monkeypatch
) -> None:
    # symlink_target_to_id が `target -> node_id` の 1 対 1 dict だと、docs/a.md と
    # docs/b.md が同じリンク先を指す場合に後勝ちで片方の node_id しか changed_ids に
    # 載らず、リンク先変更時に他方のノードが影響分析（impacted）から漏れる
    # （レビュー対応: 7巡目 codd.py:1137）。target -> node_id は多対一（list）で
    # 保持すべきことを、両ノードの node_id が異なるケースで検証する。
    #
    # 実ファイルの symlink は dereference した内容から node_id を読むため、同一
    # ターゲットを指す 2つの symlink は必ず同じ node_id になり、1対1 dict でも
    # 情報は失われない（バグを再現できない）。そのため `_symlink_target_relpath`
    # を差し替え、docs/a.md と docs/b.md が異なる node_id を持ったまま同一
    # ターゲット（shared/actual.md）を指す状況を作る。
    _init_repo(tmp_path)
    _write(tmp_path, "docs/a.md", _doc("design:a", "design"))
    _write(tmp_path, "docs/b.md", _doc("design:b", "design"))
    _write(
        tmp_path,
        "docs/down_a.md",
        _doc("design:down-a", "design", deps=[("design:a", "derives_from")]),
    )
    _write(
        tmp_path,
        "docs/down_b.md",
        _doc("design:down-b", "design", deps=[("design:b", "derives_from")]),
    )
    _write(tmp_path, "shared/actual.md", "# actual\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "shared" / "actual.md").write_text("# actual\n変更\n", encoding="utf-8")

    shared_target = {"docs/a.md", "docs/b.md"}
    real_symlink_target_relpath = cli._symlink_target_relpath

    def _fake_symlink_target_relpath(root: Path, rel: str) -> str | None:
        if rel in shared_target:
            return "shared/actual.md"
        return real_symlink_target_relpath(root, rel)

    monkeypatch.setattr(cli, "_symlink_target_relpath", _fake_symlink_target_relpath)

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")

    assert "design:a" in result.changed_ids
    assert "design:b" in result.changed_ids
    impacted = _by_id(result.impacted)
    assert impacted["design:down-a"].band == cc.BAND_GREEN
    assert impacted["design:down-b"].band == cc.BAND_GREEN


def test_compute_impact_result_recovers_symlink_node_id_after_deletion(tmp_path) -> None:
    # ref 側の rel が symlink の場合、`git show <ref>:<rel>` は symlink blob の中身
    # （リンク先パス文字列）をそのまま返す。dereference せず frontmatter として解析
    # すると旧 node_id を復元できず、symlink 削除による dangling 化を見逃していた
    # （レビュー対応: codd.py:84 scenario 2: alias 削除時の旧 node_id 復元）。
    _init_repo(tmp_path)
    _write(tmp_path, "shared/actual.md", _doc("design:d", "design"))
    (tmp_path / "docs").mkdir()
    # 相対 symlink（portable な書き方）。git はこの相対パス文字列をそのまま
    # symlink blob として保存するため、ref 側の dereference（
    # `_resolve_ref_symlink_target`）はこの表現を前提にしている。
    (tmp_path / "docs" / "link.md").symlink_to(Path("../shared/actual.md"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "docs" / "link.md").unlink()  # symlink 自体を削除（ターゲットは残す）

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")

    assert "docs/link.md" in result.deleted_upstream


def test_compute_impact_result_recovers_symlink_node_id_with_whitespace_target(
    tmp_path,
) -> None:
    # symlink ターゲット文字列の先頭/末尾の空白は、意味を持つファイル名の一部
    # （例: 末尾に空白のあるファイル名）でありうる。ref 側の dereference
    # （`_resolve_ref_symlink_target`）が `strip()` すると working tree
    # （`os.readlink` ベースの `_symlink_target_relpath` は空白を保持する）とは
    # 別パスに解決されてしまい、alias 削除時に旧 node_id を復元できなくなる
    # （レビュー対応: 8巡目 codd.py:1037）。
    _init_repo(tmp_path)
    _write(tmp_path, "shared/actual.md ", _doc("design:d", "design"))  # ファイル名末尾に空白
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "link.md").symlink_to(Path("../shared/actual.md "))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "docs" / "link.md").unlink()  # symlink 自体を削除（ターゲットは残す）

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")

    assert "docs/link.md" in result.deleted_upstream


def test_compute_impact_result_detects_change_via_intermediate_symlink_hop(
    tmp_path,
) -> None:
    # `api/alias.py -> ../links/current.py -> ../v1.py` のような中継 symlink チェーンで
    # 中間リンク（links/current.py）だけを別ターゲットへ retarget した場合、`git diff`
    # は中間リンクのパスを返す。最終ターゲットだけを追跡すると alias（api/alias.py）の
    # 変更が検出できない（レビュー対応: 8巡目 codd.py:1009）。
    _init_repo(tmp_path)
    _write(tmp_path, "v1.py", _py(["codd:node_id code:v1"]))
    _write(tmp_path, "v2.py", _py(["codd:node_id code:v2"]))
    (tmp_path / "links").mkdir()
    (tmp_path / "links" / "current.py").symlink_to(Path("../v1.py"))
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "alias.py").symlink_to(Path("../links/current.py"))
    # alias.py だけを code_scope に含める（links/current.py・v1.py・v2.py は scope 外
    # のままにし、alias.py 1 ノードのみを走査してテストを単純化する）。
    config = _config(code_scope={"include": ["api/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    # alias.py 自体は変更せず、中間リンク（links/current.py）だけを retarget する。
    (tmp_path / "links" / "current.py").unlink()
    (tmp_path / "links" / "current.py").symlink_to(Path("../v2.py"))

    result = cli.compute_impact_result(tmp_path, config, "HEAD")

    assert "code:v2" in result.changed_ids


def test_symlink_target_relpath_rejects_absolute_link_text(tmp_path) -> None:
    # symlink のリンク先が絶対パス（例: `docs/link.md -> /etc/hosts`）だと
    # `posixpath.join(dirname, link_text)` は join 元を無視して絶対パスを
    # そのまま返すため、`..` 始まりチェックだけでは root 外判定をすり抜けて
    # しまう（9巡目レビュー対応: codd.py:1089）。working tree 側
    # `_symlink_target_relpath` は絶対パスを早期 None で拒否する必要がある。
    root = tmp_path
    (root / "docs").mkdir()
    (root / "docs" / "link.md").symlink_to("/etc/hosts")

    assert cli._symlink_target_relpath(root, "docs/link.md") is None


def test_symlink_target_relpath_tracks_root_internal_absolute_link_text(tmp_path) -> None:
    # symlink のリンク先が root 内の絶対パス（例: `/project/shared/mod.py`）の場合、
    # scan 側の containment 検査（`_glob_relpaths`）はこの alias をノードとして登録
    # するのに対し、リンク先解決側が絶対パスを一律 None にすると、リンク先だけを
    # 変更してもチェーン追跡（drift 判定・impact 分析）に反映されない
    # （11巡目レビュー対応: codd.py:1103）。root 内の絶対パスは root 相対へ変換して
    # 追跡する必要がある。
    root = tmp_path
    (root / "aliases").mkdir()
    (root / "shared").mkdir()
    (root / "aliases" / "mod.py").symlink_to(root / "shared" / "mod.py")

    assert cli._symlink_target_relpath(root, "aliases/mod.py") == "shared/mod.py"


def test_resolve_ref_symlink_target_rejects_absolute_link_text(tmp_path) -> None:
    # ref 側（git symlink blob の内容）が絶対パスの場合も working tree 側と
    # 同一規約を適用する必要がある（9巡目レビュー対応: codd.py:1089）。root 外の
    # 絶対パス（例: `/etc/hosts`）は引き続き None（片方だけの修正は許されない。
    # working tree 側の判定と食い違うと、alias 削除時の旧 node_id 復元シナリオの
    # 整合が崩れる）。
    assert cli._resolve_ref_symlink_target(tmp_path, "docs/link.md", "/etc/hosts") is None


def test_resolve_ref_symlink_target_tracks_root_internal_absolute_link_text(tmp_path) -> None:
    # root 内の絶対パス（例: `<root>/shared/mod.py`）を指す ref 側 symlink は、
    # working tree 側 `_symlink_target_relpath` と同様に root 相対へ変換して
    # 追跡する必要がある（11巡目レビュー対応: codd.py:1103）。
    abs_target = str(tmp_path / "shared" / "mod.py")

    assert (
        cli._resolve_ref_symlink_target(tmp_path, "aliases/mod.py", abs_target) == "shared/mod.py"
    )


def test_compute_impact_result_ignores_absolute_ref_symlink_target(tmp_path) -> None:
    # ref 側の symlink が root 外の絶対パスを指す場合、root 外ファイルの内容を旧
    # node_id として誤って復元・dangling 判定に取り込んではいけない（9巡目レビュー
    # 対応: codd.py:1089）。修正前は `combined` が絶対パスのまま `..` 始まりでない
    # ため root 外判定をすり抜け、そのパスで `git show` を試みてしまっていた。
    # root 内の絶対パスは 11巡目レビュー対応（codd.py:1103）で正しく root 相対へ
    # 変換して追跡されるようになったため、この検証には genuinely root 外となる
    # パス（root = tmp_path のサブディレクトリ、ターゲットは兄弟ディレクトリ）を
    # 使う必要がある。
    root = tmp_path / "proj"
    root.mkdir()
    _write(tmp_path, "outside/actual.md", _doc("design:d", "design"))
    (root / "docs").mkdir()
    (root / "docs" / "link.md").symlink_to(tmp_path / "outside" / "actual.md")
    _init_repo(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    (root / "docs" / "link.md").unlink()  # symlink 自体を削除（ターゲットは残す）

    result = cli.compute_impact_result(root, _config(), "HEAD")

    assert "docs/link.md" not in result.deleted_upstream


def test_compute_impact_result_recovers_dangling_design_via_root_internal_absolute_ref_symlink(
    tmp_path,
) -> None:
    # ref 側の symlink が root 内の絶対パスを指す場合は、working tree 側
    # `_symlink_target_relpath` と同じ規約で root 相対へ変換して追跡し、旧 node_id
    # を正しく復元できる必要がある（11巡目レビュー対応: codd.py:1103）。ターゲット
    # 自身は scope 外（docs/**/*.md に含まれない）のため、alias（docs/link.md）を
    # 削除すると design:d はグラフから完全に失われ、dangling として検出されるべき。
    _init_repo(tmp_path)
    _write(tmp_path, "shared/actual.md", _doc("design:d", "design"))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "link.md").symlink_to(tmp_path / "shared" / "actual.md")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "docs" / "link.md").unlink()  # symlink 自体を削除（ターゲットは残す）

    result = cli.compute_impact_result(tmp_path, _config(), "HEAD")

    assert "docs/link.md" in result.deleted_upstream


def test_compute_impact_result_reports_deleted_code_upstream_with_pep263_encoding(
    tmp_path,
) -> None:
    # 削除された Python ファイルが Latin-1 の coding cookie を宣言していても、
    # ref 側の内容を UTF-8 固定で復号せず PEP 263 に従う（working tree 側の
    # `_read_source_text` と同じ規約。Issue #98 レビュー対応）。
    _init_repo(tmp_path)
    path = tmp_path / "src" / "legacy.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = '# -*- coding: latin-1 -*-\n"""\ncodd:node_id code:legacy\nnote: café\n"""\n'
    path.write_bytes(content.encode("latin-1"))
    config = _config(code_scope={"include": ["src/**/*.py"], "exclude": []})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    path.unlink()

    result = cli.compute_impact_result(tmp_path, config, "HEAD")

    assert "src/legacy.py" in result.deleted_upstream


# ---------------------------------------------------------------------------
# diff_changed_paths（非 ASCII パス / quotePath 対応）
# ---------------------------------------------------------------------------


def test_diff_changed_paths_detects_non_ascii_modification(tmp_path) -> None:
    # core.quotePath=true（既定）を明示設定し、-z パースが quote/エスケープの影響を
    # 受けないことを再現性を持って確認する。
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    target = _write(tmp_path, "docs/日本語ドキュメント.md", _doc("design:jp", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    target.write_text(_doc("design:jp", "design") + "\n変更\n", encoding="utf-8")

    changed, deleted = cli.diff_changed_paths(tmp_path, "HEAD")
    assert "docs/日本語ドキュメント.md" in changed
    assert deleted == set()


def test_diff_changed_paths_detects_non_ascii_rename(tmp_path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    _write(tmp_path, "docs/旧名前.md", _doc("design:jp", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _git(tmp_path, "mv", "docs/旧名前.md", "docs/新しい名前.md")

    changed, deleted = cli.diff_changed_paths(tmp_path, "HEAD")
    assert "docs/旧名前.md" in deleted
    assert "docs/新しい名前.md" in changed


def test_diff_changed_paths_detects_non_ascii_deletion(tmp_path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.quotePath", "true")
    target = _write(tmp_path, "docs/削除予定.md", _doc("design:jp", "design"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    target.unlink()

    changed, deleted = cli.diff_changed_paths(tmp_path, "HEAD")
    assert "docs/削除予定.md" in deleted
    assert "docs/削除予定.md" not in changed


# ---------------------------------------------------------------------------
# EV-19: 依存宣言の正本はフロントマター1箇所のみ（外部サイドカーの二重管理否定）
# ---------------------------------------------------------------------------


def test_codd_config_has_no_external_links_field() -> None:
    # EV-19: 依存宣言の正本はフロントマター1箇所のみ。config スキーマに doc_links
    # 等の外部依存宣言ファイルを指すフィールドが存在しないことを確認する
    # （「存在しないこと」の裏付け）。
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(cc.CoddConfig)}
    forbidden = {"doc_links", "links_file", "dependencies_file", "deps_path", "links_path"}
    assert field_names.isdisjoint(forbidden)


def test_scan_ignores_external_doc_links_sidecar(tmp_path) -> None:
    # EV-19: scope 内に外部の依存宣言サイドカー（doc_links.yaml 等）を置いても、
    # scan はそれを一切読まず、フロントマターに書かれた依存のみをグラフに反映する。
    _write(tmp_path, "docs/design.md", _doc("design:d", "design"))
    _write(tmp_path, "docs/doc_links.yaml", "design:d:\n  - req:phantom\n")
    config = _config(scope={"include": ["docs/**/*.md"], "exclude": []})
    result = cli.scan_project(tmp_path, config)
    node = result.graph.nodes["design:d"]
    # サイドカー由来の依存（req:phantom）は取り込まれない。
    assert node.depends_on == ()
    assert not result.graph.has("req:phantom")


# ---------------------------------------------------------------------------
# EV-22: 壊れた入力・存在しない scope でもクラッシュしない
# ---------------------------------------------------------------------------


def test_scan_survives_malformed_frontmatter_without_crash(tmp_path) -> None:
    # 壊れた YAML（閉じられていない配列）はクラッシュせず missing_frontmatter 扱い。
    _write(tmp_path, "docs/broken.md", "---\ncodd: [unclosed\n---\n# body\n")
    result = cli.scan_project(tmp_path, _config())
    assert result.nodes == []
    assert result.missing_frontmatter == ["docs/broken.md"]
    assert cli.cmd_validate(tmp_path, _config()) == 0


def test_cmd_validate_survives_missing_root_directory(tmp_path) -> None:
    # 存在しない --root パスでもクラッシュせず、対象 0 件として正常終了する。
    missing_root = tmp_path / "does-not-exist"
    assert cli.cmd_validate(missing_root, _config()) == 0


# ---------------------------------------------------------------------------
# EV-23: graph.jsonl の書き込み失敗が既存グラフを破損させない（atomic write）
# ---------------------------------------------------------------------------


def test_write_graph_jsonl_failure_preserves_existing_graph(tmp_path, monkeypatch) -> None:
    out = tmp_path / ".claude/codd/graph.jsonl"
    out.parent.mkdir(parents=True)
    existing_content = '{"node_id": "design:existing"}\n'
    out.write_text(existing_content, encoding="utf-8")

    _write(tmp_path, "docs/design.md", _doc("design:d", "design"))
    result = cli.scan_project(tmp_path, _config())

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError):
        cli.write_graph_jsonl(result, out)

    # 書き込み失敗前の既存グラフがそのまま残る（半端な内容で壊れていない）。
    assert out.read_text(encoding="utf-8") == existing_content
    # temp ファイルも後始末される（失敗後に残骸を残さない）。
    assert list(out.parent.glob(f".{out.name}.*")) == []


def test_write_graph_jsonl_uses_unique_tmp_name_for_concurrent_writes(tmp_path) -> None:
    # Codex レビュー反映（Issue #128）: 固定 temp ファイル名（`graph.jsonl.tmp`）だと
    # 2 つの並行 `codd scan` が同じ temp ファイルを共有し破壊し合う。
    # `tempfile.mkstemp` による一意生成を検証するため、書き込み中に temp ファイル名を
    # 収集し、2 回連続実行しても名前が重複しないことを確認する。
    out = tmp_path / ".claude/codd/graph.jsonl"
    _write(tmp_path, "docs/design.md", _doc("design:d", "design"))
    result = cli.scan_project(tmp_path, _config())

    seen_tmp_names: list[str] = []
    original_mkstemp = tempfile.mkstemp

    def _spy_mkstemp(*args, **kwargs):
        fd, name = original_mkstemp(*args, **kwargs)
        seen_tmp_names.append(name)
        return fd, name

    with unittest.mock.patch("tempfile.mkstemp", side_effect=_spy_mkstemp):
        cli.write_graph_jsonl(result, out)
        cli.write_graph_jsonl(result, out)

    assert len(seen_tmp_names) == 2
    assert seen_tmp_names[0] != seen_tmp_names[1]
    # temp ファイルは出力先と同じディレクトリに作られる（rename の atomicity のため）。
    for name in seen_tmp_names:
        assert Path(name).parent == out.parent
    # 実行後は正常な最終ファイルのみが残り、temp ファイルは残らない。
    assert list(out.parent.glob(f".{out.name}.*")) == []


# ---------------------------------------------------------------------------
# EV-13（should）: AI Orchestra 自身のドキュメントに対する dogfood validate
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_codd_validate_dogfoods_ai_orchestra_docs_with_zero_errors() -> None:
    # EV-13: AI Orchestra 自身のドキュメント群に対して validate を実行すると
    # error 0 で通る（ドッグフード）。warning（drift/orphan/missing_frontmatter 等）
    # は許容する。
    config_path = _REPO_ROOT / ".claude" / "config" / "codd" / "codd.yaml"
    if not config_path.exists():
        pytest.skip("codd config が未導入のためスキップ")
    config = cc.load_config(config_path)
    result = cli.scan_project(_REPO_ROOT, config)
    findings = cli.run_checks(result, config, _REPO_ROOT)
    errors = [f for f in findings if f.level == cc.LEVEL_ERROR]
    assert errors == [], "\n".join(f"{f.check}: {f.message}" for f in errors)


# ---------------------------------------------------------------------------
# EV-21: `orchex run codd codd -- <subcommand>` の後方互換サブプロセス起動
# ---------------------------------------------------------------------------

_ORCHESTRA_MANAGER = _REPO_ROOT / "scripts" / "orchestra-manager.py"

_MINIMAL_CODD_YAML = """\
enabled: true
scope:
  include: ["docs/**/*.md"]
  exclude: []
kinds: [requirement, design, adr, plan, rule, instruction]
relations: [derives_from, refines, implements, references, supersedes]
roots: [requirement, instruction]
graph_store:
  format: jsonl
  path: ".claude/codd/graph.jsonl"
checks:
  dangling: error
  duplicate: error
  cycle: error
  unknown: error
  missing_frontmatter: warning
  orphan: warning
  drift: warning
"""


def _run_orchex_codd(project: Path, *subcommand_args: str) -> subprocess.CompletedProcess[str]:
    """`orchex run codd codd -- <subcommand>` 相当のサブプロセス起動。"""
    import sys

    cmd = [
        sys.executable,
        str(_ORCHESTRA_MANAGER),
        "run",
        "codd",
        "codd",
        "--project",
        str(project),
        "--",
        *subcommand_args,
    ]
    env = {**os.environ, "AI_ORCHESTRA_DIR": str(_REPO_ROOT)}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)


def test_orchex_run_codd_scan_and_validate_backward_compatible(tmp_path) -> None:
    # EV-21: scan / validate は `orchex run codd codd -- <subcommand>` として
    # サブプロセス起動・引数パースが後方互換に動作する。
    project = tmp_path / "project"
    _write(project, "docs/req.md", _doc("req:r", "requirement"))
    config_dir = project / ".claude" / "config" / "codd"
    config_dir.mkdir(parents=True)
    (config_dir / "codd.yaml").write_text(_MINIMAL_CODD_YAML, encoding="utf-8")

    scan_result = _run_orchex_codd(project, "scan")
    assert scan_result.returncode == 0, scan_result.stderr
    assert "[codd scan]" in scan_result.stdout
    assert (project / ".claude" / "codd" / "graph.jsonl").exists()

    validate_result = _run_orchex_codd(project, "validate")
    assert validate_result.returncode == 0, validate_result.stderr
    assert "[codd validate]" in validate_result.stdout
