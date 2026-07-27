#!/usr/bin/env python3
"""CODD CLI: scan / validate / graph。

`orchex run codd codd -- <subcommand>` から、または直接実行する。
プロジェクトルート（cwd）を基準に scope を走査し、依存グラフの構築・整合性検証・
可視化を行う。設定は `.claude/config/codd/codd.yaml`（+ local 上書き）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# lib/ を import パスへ追加（scripts/ と lib/ は同一パッケージ配下）。
_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import codd_code as cx  # noqa: E402
import codd_common as cc  # noqa: E402

DEFAULT_CONFIG_PATH = Path(".claude/config/codd/codd.yaml")


# ---------------------------------------------------------------------------
# ノード収集（scan / validate / graph で共有）
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """走査結果。グラフ・収集ノード・frontmatter 欠落ファイル・注釈エラーを保持する。"""

    graph: cc.CoddGraph
    nodes: list[cc.CoddNode]
    missing_frontmatter: list[str]
    # Issue #98 レビュー対応: 値の無いコード注釈（例: `codd:implements` のみ）を
    # 黙って依存から除外せず、validate 側の malformed_annotation 検査として報告する。
    malformed_annotations: list[str]


def _glob_relpaths(root: Path, patterns: list[str]) -> set[str]:
    """patterns（glob）にマッチするファイルの相対 posix パス集合を返す。

    ``../*.py`` のようなパターンは ``Path.glob`` がそのまま解決してしまい、
    プロジェクトルート外のファイルを走査対象に含めてしまう（Issue #98 レビュー対応）。
    解決後のパスが root 配下かを検証し、root 外へ解決されたものは黙って除外する。

    格納する相対パスは、シンボリックリンクをたどらないレキシカル正規化
    （``os.path.normpath``）で ``..`` セグメントだけを畳み込んで求める。
    ``../proj/src/foo.py``（root == proj）のように root 内へ戻ってくるパターンは
    containment 判定こそ通るが、素朴に ``path.relative_to(root)`` すると
    ``".."`` を含む別名の文字列として集合に入り、通常パターン（``src/foo.py``）
    で見つかる同一ファイルと重複ノード化してしまう（Issue #98 レビュー対応）。

    root 内部のシンボリックリンクにマッチした場合は、リンクの解決先ではなく
    論理パス（リンク自体のパス）をそのまま保持する。``Path.resolve()`` は
    root 配下か否かの安全性チェックのみに使い、実際に登録する相対パスの計算には
    使わない（レビュー対応: symlink の解決先パスを登録すると、`git diff` が
    返すリンク自体のパスと `path_to_id` が一致しなくなる）。

    ``src/**.py`` のような不正な再帰 glob（``**`` がパスセグメント全体を占めていない）
    は Python 3.12+ の ``Path.glob()`` が ``ValueError`` を送出する。この呼び出しは
    `main()` の設定読み込み用例外ハンドラより後（scan/validate/impact の走査時）に
    実行されるため、そのまま伝播させるとトレースバックで CLI が終了してしまう。
    ここで捕捉し、パターンを含む分かりやすい ``ValueError`` に変換して再送出することで、
    `main()` 側の設定エラーハンドラ（scan/validate/impact 呼び出し全体を包む try/except）
    が整形済みメッセージとして表示できるようにする（Issue #98 レビュー対応: 8巡目）。
    """
    matched: set[str] = set()
    resolved_root = root.resolve()
    for pattern in patterns:
        try:
            candidates = list(root.glob(pattern))
        except ValueError as exc:
            msg = f"scope の glob パターンが不正です: {pattern!r} ({exc})"
            raise ValueError(msg) from exc
        for path in candidates:
            if not path.is_file():
                continue
            resolved_path = path.resolve()
            if not resolved_path.is_relative_to(resolved_root):
                continue
            normalized_path = Path(os.path.normpath(path))
            if not normalized_path.is_relative_to(root):
                continue
            matched.add(normalized_path.relative_to(root).as_posix())
    return matched


def collect_files(root: Path, config: cc.CoddConfig) -> list[Path]:
    """include glob から exclude を差し引いた対象ファイル一覧を返す。

    exclude も include と同じ ``Path.glob`` で解決するため、``docs/**/*.md`` の
    ような再帰 glob を exclude に書いても期待どおり除外される。
    """
    included = _glob_relpaths(root, config.include)
    excluded = _glob_relpaths(root, config.exclude)
    return [root / rel for rel in sorted(included - excluded)]


def collect_code_files(root: Path, config: cc.CoddConfig) -> list[Path]:
    """``code_scope.include``（Issue #98 / opt-in）内のソースファイル一覧を返す。

    既定は空リストのため、未設定プロジェクトでは常に空（既存挙動への影響ゼロ）。
    """
    if not config.code_include:
        return []
    included = _glob_relpaths(root, config.code_include)
    excluded = _glob_relpaths(root, config.code_exclude)
    return [root / rel for rel in sorted(included - excluded)]


def _read_source_text(path: Path) -> str | None:
    """ソースファイルをテキストとして読む。

    Python ファイル（``.py``）は PEP 263 の宣言済みエンコーディング（先頭2行の
    ``# -*- coding: ... -*-`` cookie または BOM）を尊重する（`tokenize.detect_encoding`）。
    固定 UTF-8 のままだと、Latin-1 等の coding cookie を持つ有効な Python ファイルが
    `UnicodeDecodeError` になってしまう。それ以外の対応言語（TS/JS/Go 等）は UTF-8 固定。

    復号に失敗した場合は None を返し、呼び出し側は注釈なしとして黙ってスキップする
    （UTF-16 で保存された TS や不正な coding cookie を持つ Python ファイルで
    scan/validate/impact 全体を落とさない。`_decode_ref_source` と同じ規約。
    Issue #98 レビュー対応）。
    """
    if path.suffix != ".py":
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    try:
        with path.open("rb") as fh:
            encoding, _ = tokenize.detect_encoding(fh.readline)
        return path.read_text(encoding=encoding)
    except (OSError, SyntaxError, LookupError, UnicodeDecodeError):
        return None


def scan_code_nodes(root: Path, config: cc.CoddConfig) -> tuple[list[cc.CoddNode], list[str]]:
    """``code_scope`` 内から code/test ノードを抽出する（Issue #98）。

    doc scope と異なり、`codd:` 注釈が無いファイルは missing_frontmatter として
    扱わず黙ってスキップする（コードベース全体へのフロントマター強制はしない、
    opt-in の軽量記法のため）。2 つ目の戻り値は値の無い依存注釈のエラー一覧。
    """
    nodes: list[cc.CoddNode] = []
    errors: list[str] = []
    for path in collect_code_files(root, config):
        rel = path.relative_to(root).as_posix()
        # 未対応拡張子（画像等）は読み込み前に除外する。混在ディレクトリを指す
        # code_scope glob（例: `src/**/*`）でも対応外ファイルを UTF-8 テキストとして
        # 復号しようとしない（Issue #98 レビュー対応）。
        if not cx.is_supported_suffix(rel):
            continue
        text = _read_source_text(path)
        if text is None:
            continue  # 復号失敗（UTF-16 等）は注釈なしとして黙ってスキップする
        node, node_errors = cx.extract_code_node(rel, text, config.inline_confidence)
        if node is not None:
            nodes.append(node)
        errors.extend(node_errors)
    return nodes, errors


def scan_project(root: Path, config: cc.CoddConfig) -> ScanResult:
    """scope（doc）+ code_scope（Issue #98 / opt-in）を走査してグラフを構築する。"""
    nodes: list[cc.CoddNode] = []
    missing: list[str] = []
    for path in collect_files(root, config):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        codd_block = cc.parse_codd_frontmatter(text)
        if codd_block is None:
            missing.append(rel)
            continue
        nodes.append(cc.build_node(codd_block, rel))
    code_nodes, malformed_annotations = scan_code_nodes(root, config)
    nodes.extend(code_nodes)
    graph = cc.build_graph(nodes)
    return ScanResult(
        graph=graph,
        nodes=nodes,
        missing_frontmatter=missing,
        malformed_annotations=malformed_annotations,
    )


# ---------------------------------------------------------------------------
# グラフの JSONL 永続化
# ---------------------------------------------------------------------------


def _dependency_to_record(dep: cc.Dependency) -> dict[str, Any]:
    """Dependency を JSONL の depends_on エントリへ変換する。

    confidence は既定値 1.0（doc frontmatter 由来）のときは省略し、既存の
    JSONL 出力（doc のみのグラフ）をバイト互換に保つ。1.0 未満（コード注釈
    由来。Issue #98）の場合のみ明示する。
    """
    record: dict[str, Any] = {"id": dep.id, "relation": dep.relation}
    if dep.confidence != 1.0:
        record["confidence"] = dep.confidence
    return record


def node_to_record(node: cc.CoddNode) -> dict[str, Any]:
    """CoddNode を JSONL 1 行分の dict に変換する。"""
    return {
        "node_id": node.node_id,
        "kind": node.kind,
        "status": node.status,
        "owner": node.owner,
        "path": node.path,
        "depends_on": [_dependency_to_record(dep) for dep in node.depends_on],
    }


def write_graph_jsonl(result: ScanResult, output_path: Path) -> None:
    """グラフを JSONL として書き出す（1 ノード 1 行）。

    EV-23: 一時ファイルへ書いてから rename する atomic write にし、書き込み失敗
    （中断・ディスク容量不足等）が既存の `graph.jsonl` を壊れた/半端な内容で
    上書きしないようにする（rename は同一ファイルシステム内で不可分）。

    temp ファイル名は `tempfile.mkstemp` で出力先と同一ディレクトリに一意生成する
    （固定名だと並行 `codd scan` 実行同士が同じ temp ファイルを共有し破壊し合うため）。
    rename の atomicity を保つため、同一ファイルシステム＝同ディレクトリに置く。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(node_to_record(node), ensure_ascii=False) for node in result.nodes]
    content = "\n".join(lines) + ("\n" if lines else "")
    fd, tmp_name = tempfile.mkstemp(
        dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_path, output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# drift 用の時刻ソース（設計 4.5 / H-3）
# ---------------------------------------------------------------------------


def _git_output(root: Path, args: list[str]) -> str | None:
    """git コマンドを実行し stdout を返す。失敗時は None。

    非 ASCII パス（日本語ファイル名等）を正しく扱うため、デコードは明示的に
    UTF-8 を指定する（未指定だと locale 依存の `getpreferredencoding()` になり、
    環境によっては非 ASCII パスのデコードに失敗し得るため）。
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _git_output_bytes(
    root: Path, args: list[str], *, keep_partial_on_error: bool = False
) -> bytes | None:
    """git コマンドを実行し stdout を生バイト列で返す。失敗時は None。

    ``git show <ref>:<rel>`` でコミット時点の Python ソースを取得する際に使う
    （`_old_node_id_at_ref` / Issue #98 レビュー対応）。working tree 側の
    `_read_source_text` と同じく PEP 263 宣言済みエンコーディングを尊重するには、
    先に UTF-8 固定でデコードしない生バイト列が必要なため、`_git_output` とは別に
    エンコーディング指定なしで実行する。

    ``keep_partial_on_error``: 部分履歴 clone（shallow clone 等）では、対象パスの
    無制限 `git log` が最新 timestamp を stdout に出力した後、古い履歴（欠けた
    tree）の走査中に nonzero で終了することがある。既定（False）では従来どおり
    nonzero 終了時に stdout を丸ごと捨てて None を返すが、`_log_commit_times()`
    のように「取得できた分だけでも使いたい」呼び出しでは True を指定し、stdout が
    空でなければ nonzero 終了でもそれを返す（レビュー対応: codd.py:394）。
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        if keep_partial_on_error and completed.stdout:
            return completed.stdout
        return None
    return completed.stdout


def commit_time(root: Path, rel_path: str) -> float:
    """最終更新時刻（epoch 秒）。

    クリーンな追跡ファイルは最終コミット時刻（`git log -1 --format=%ct`）を、
    未追跡・未コミット編集のあるファイルは mtime を用いる。git は mtime を
    履歴保持しないため、編集中ファイルではコミット時刻が古いままになり drift を
    取りこぼす。これを避けるため dirty 判定で mtime に切り替える（H-3 / Codex P2）。
    """
    status = _git_output(root, ["status", "--porcelain", "--", rel_path])
    is_clean_tracked = status is not None and not status.strip()
    if is_clean_tracked:
        out = _git_output(root, ["log", "-1", "--format=%ct", "--", rel_path])
        if out and out.strip():
            try:
                return float(out.strip())
            except ValueError:
                pass
    try:
        return (root / rel_path).stat().st_mtime
    except OSError:
        return 0.0  # 取得不能なら最古扱い（drift の誤検知を防ぐ）


def _dirty_paths(root: Path) -> set[str] | None:
    """``git status --porcelain -z`` からリポジトリ全体の dirty パス集合を返す。

    未追跡・未コミット編集のパス（rename の旧パス・新パス両方を含む）を 1 回の
    プロセス起動でまとめて取得する。`commit_time()` をノードごとに個別実行すると
    パスごとに `git status` を起動してしまい、ノード数に比例して遅くなる
    （1,000 ノード規模で顕著。Issue #98 レビュー対応）。
    失敗時（git 実行エラー等）は None（呼び出し側は全パスを dirty 扱いにする）。
    """
    out = _git_output_bytes(root, ["status", "--porcelain", "-z"])
    if out is None:
        return None
    dirty: set[str] = set()
    tokens = out.decode("utf-8", errors="surrogateescape").split("\0")
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record:
            continue
        # `XY PATH`。X/Y いずれかが R（rename）/ C（copy）なら、次トークンが
        # rename/copy 元パス（NUL 区切りで追加）になる（`git status --porcelain -z` 規約）。
        status_code, path = record[:2], record[3:]
        dirty.add(path)
        if status_code[0] in ("R", "C") or status_code[1:2] in ("R", "C"):
            if index >= len(tokens):
                break
            dirty.add(tokens[index])
            index += 1
    return dirty


def _repo_root_prefix(root: Path) -> str:
    """root からリポジトリルートまでの相対パス prefix を返す（末尾 `/` 付き、または空文字）。

    `git status --porcelain` / `git log --name-only` が返すパス表記は常に
    リポジトリルート相対だが、`batch_commit_times()` に渡される `rel_paths` は
    `--root` 相対である。root が git リポジトリルートでない場合、両者のキーが
    一致せず時刻キャッシュが常にミスし、クリーンな追跡ファイルでも mtime
    フォールバックに落ちて drift 検出が変わってしまう（レビュー対応: `--root` が
    git リポジトリルート以外を指すケース）。`git rev-parse --show-prefix` の
    取得に失敗した場合は空文字（prefix なし = 従来どおり root == repo root を
    前提にした挙動）を返す。
    """
    out = _git_output(root, ["rev-parse", "--show-prefix"])
    if out is None:
        return ""
    return out.strip()


def _strip_repo_prefix(path: str, prefix: str) -> str | None:
    """リポジトリルート相対の path から prefix を取り除き、root 相対に正規化する。

    ``git status --porcelain`` はリポジトリ全体の dirty パスを返すため、``--root``
    が git リポジトリのサブディレクトリを指す場合、prefix 配下に無いパスが混ざる。
    以前は prefix 外のパスもそのまま素通ししていたため、root 外の dirty ファイルが
    偶然 root 内ノードと同じ相対名を持つと、clean なノードまで dirty と誤認されて
    commit time ではなく mtime が使われてしまっていた（レビュー対応: codd.py:442）。
    prefix 配下に無い path は None を返し、呼び出し側で除外させる。
    """
    if not prefix:
        return path
    if not path.startswith(prefix):
        return None
    return path[len(prefix) :]


def _log_commit_times(root: Path, rel_paths: list[str]) -> dict[str, float]:
    """rel_paths の最終コミット時刻を 1 回の `git log` でまとめて取得する。

    ``-z`` を付けずに ``--name-only`` を使うと、`core.quotePath`（既定 true）に
    より非 ASCII パスが 8 進エスケープ付きで引用されて出力され、`rel_paths` の
    キーと一致しなくなる（P1 レビュー対応: 非 ASCII パスで `batch_commit_times()`
    が常に mtime フォールバックへ落ちる）。``-z`` はコミットごとの区切りと
    パス区切りの両方を NUL にし、パスの引用も無効化するため、生バイト列を NUL で
    分割して構造的にパースする: 各コミットのレコードは
    ``\\0<ct>\\0\\n<path1>\\0<path2>\\0...`` という形式になり、空文字列トークンが
    次コミットの開始（=次のタイムスタンプトークン）を示す（レビュー対応: 素朴な
    行分割だとコミットヘッダ行をパスとして誤取得しうる問題も併せて解消）。新しい
    コミットから走査するため、各パスについて最初に出現した時刻が最終コミット
    時刻になる（パスごとに `git log -1 --format=%ct -- <path>` を呼ぶのと同じ
    結果。rename の追跡先切替は `--follow` 非使用のため元の `commit_time()` と
    同じく行わない。Issue #98 レビュー対応）。
    """
    if not rel_paths:
        return {}
    # ``:(literal)`` を各パスへ前置し、pathspec magic として解釈させない。前置しないと
    # ``:(bad.md`` のような（先頭が ``:(`` から始まる）正当なファイル名 1 件だけで
    # `git log` が `fatal: Invalid pathspec magic` を送出し一括呼び出し全体が失敗する。
    # `_log_commit_times` が呼び出し元（`batch_commit_times`）へ空 dict を返すため、
    # 該当ファイルだけでなく同じバッチ内の全 clean node が commit time ではなく
    # working-tree mtime で比較されてしまう（drift 判定が不安定になる。Issue #98
    # レビュー対応: 8巡目）。``:(literal)`` は先頭の magic 指定子だけを解釈し、以降の
    # 文字列（``:(bad.md`` 自体を含む）を常にリテラルとして扱う。
    literal_pathspecs = [f":(literal){p}" for p in rel_paths]
    out = _git_output_bytes(
        root,
        ["log", "-z", "--name-only", "--format=%x00%ct", "--", *literal_pathspecs],
        keep_partial_on_error=True,
    )
    if not out:
        return {}
    tokens = out.decode("utf-8", errors="surrogateescape").split("\0")
    times: dict[str, float] = {}
    current_time: float | None = None
    awaiting_timestamp = False
    first_path_in_record = False
    for token in tokens:
        if token == "":
            awaiting_timestamp = True
            continue
        if awaiting_timestamp:
            try:
                current_time = float(token)
            except ValueError:
                current_time = None
            awaiting_timestamp = False
            first_path_in_record = True
            continue
        path = token[1:] if first_path_in_record and token.startswith("\n") else token
        first_path_in_record = False
        if not path or current_time is None or path in times:
            continue
        times[path] = current_time
    return times


def batch_commit_times(root: Path, rel_paths: list[str]) -> dict[str, float]:
    """rel_paths の最終更新時刻（epoch 秒）を一括取得する（`commit_time()` のバッチ版）。

    validate の drift 検査はノードごとに `commit_time()` を呼んでいたため、git
    プロセスをノード数に比例して起動していた（1,000 ノード規模で著しく遅い。
    Issue #98 レビュー対応）。dirty 判定を 1 回の `git status`、コミット時刻を
    1 回の `git log` にまとめ、各パスの判定規約（dirty/未追跡/履歴なしは mtime、
    クリーンな追跡ファイルは最終コミット時刻）は `commit_time()` と同一に保つ。

    `_dirty_paths()` / `_log_commit_times()` が返すパスはリポジトリルート相対の
    ままなので、`--root` が git リポジトリルートでない場合は `_repo_root_prefix()`
    で求めた prefix を使って `rel_paths`（`--root` 相対）へ正規化してから
    突き合わせる（レビュー対応）。

    `_dirty_paths()` はリポジトリ全体（root 外を含む）の dirty パスを返すため、
    prefix 配下に無いパスは正規化せず破棄する（`_strip_repo_prefix()` が None を
    返す）。破棄せずそのまま残すと、root 外の dirty ファイルが root 内ノードと
    偶然同じ相対名を持った場合に、clean なノードまで dirty と誤認してしまう
    （レビュー対応: codd.py:442）。
    """
    if not rel_paths:
        return {}
    prefix = _repo_root_prefix(root)
    dirty = _dirty_paths(root)
    if dirty is not None and prefix:
        dirty = {stripped for p in dirty if (stripped := _strip_repo_prefix(p, prefix)) is not None}
    clean_paths = [p for p in rel_paths if dirty is not None and p not in dirty]
    commit_times = _log_commit_times(root, clean_paths)
    if prefix:
        commit_times = {
            stripped: t
            for p, t in commit_times.items()
            if (stripped := _strip_repo_prefix(p, prefix)) is not None
        }
    result: dict[str, float] = {}
    for rel in rel_paths:
        commit_ct = commit_times.get(rel)
        if commit_ct is not None:
            result[rel] = commit_ct
            continue
        try:
            result[rel] = (root / rel).stat().st_mtime
        except OSError:
            result[rel] = 0.0  # 取得不能なら最古扱い（drift の誤検知を防ぐ）
    return result


# ---------------------------------------------------------------------------
# validate（設計 4.5）
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """検査結果 1 件。"""

    check: str
    level: str
    message: str


def _check_dangling(result: ScanResult) -> list[Finding]:
    findings: list[Finding] = []
    for node in result.nodes:
        for dep in node.depends_on:
            if not result.graph.has(dep.id):
                findings.append(
                    Finding(
                        "dangling",
                        cc.LEVEL_ERROR,
                        f"{node.path}: depends_on '{dep.id}' が存在しない",
                    )
                )
    return findings


def _check_duplicate(result: ScanResult) -> list[Finding]:
    return [
        Finding(
            "duplicate",
            cc.LEVEL_ERROR,
            f"node_id '{node_id}' が複数定義: {', '.join(paths)}",
        )
        for node_id, paths in result.graph.duplicate_paths.items()
    ]


def _check_cycle(result: ScanResult) -> list[Finding]:
    return [
        Finding("cycle", cc.LEVEL_ERROR, f"循環依存: {' -> '.join(cycle)}")
        for cycle in result.graph.find_cycles()
    ]


def _check_unknown(result: ScanResult, config: cc.CoddConfig) -> list[Finding]:
    findings: list[Finding] = []
    for node in result.nodes:
        if not node.node_id:
            findings.append(Finding("unknown", cc.LEVEL_ERROR, f"{node.path}: node_id が空"))
        elif cc.node_id_prefix(node.node_id) is None:
            # EV-12: node_id は `<kind>:<file-slug>` 形式（コロンがちょうど 1 個）である必要がある。
            # コロン無し／複数（余分なセパレータ）はどちらも不正。
            findings.append(
                Finding(
                    "unknown",
                    cc.LEVEL_ERROR,
                    f"{node.path}: node_id '{node.node_id}' が"
                    " '<kind>:<file-slug>' 形式でない（コロンが無いか複数ある）",
                )
            )
        if node.kind not in config.kinds:
            findings.append(
                Finding("unknown", cc.LEVEL_ERROR, f"{node.path}: 未定義 kind '{node.kind}'")
            )
        else:
            statuses = cc.valid_statuses(node.kind)
            # 空 status（status: 欠落）も不正扱い（5 プロパティ必須）。
            if node.status not in statuses:
                findings.append(
                    Finding(
                        "unknown",
                        cc.LEVEL_ERROR,
                        f"{node.path}: kind '{node.kind}' に不正な status '{node.status}'",
                    )
                )
            # EV-12: node_id プレフィックスが declare された kind と対応しているか
            # （設計 4.3 の表: requirement は "req" に略記、他は kind 名と同一）。
            expected_prefix = cc.NODE_ID_PREFIX_BY_KIND.get(node.kind)
            actual_prefix = cc.node_id_prefix(node.node_id)
            if expected_prefix and actual_prefix and actual_prefix != expected_prefix:
                findings.append(
                    Finding(
                        "unknown",
                        cc.LEVEL_ERROR,
                        f"{node.path}: node_id '{node.node_id}' のプレフィックス"
                        f" '{actual_prefix}' が kind '{node.kind}' の想定プレフィックス"
                        f" '{expected_prefix}:' と不一致",
                    )
                )
        for dep in node.depends_on:
            if dep.relation not in config.relations:
                findings.append(
                    Finding(
                        "unknown",
                        cc.LEVEL_ERROR,
                        f"{node.path}: 未定義 relation '{dep.relation}'",
                    )
                )
    return findings


def _check_missing_frontmatter(result: ScanResult) -> list[Finding]:
    return [
        Finding(
            "missing_frontmatter",
            cc.LEVEL_WARNING,
            f"{path}: scope 内だが codd フロントマターが無い",
        )
        for path in result.missing_frontmatter
    ]


def _check_malformed_annotation(result: ScanResult) -> list[Finding]:
    """値の無いコード注釈（Issue #98 レビュー対応）。

    `codd:implements` のように relation 名だけで参照先 value が無い注釈は、
    `codd_code._entries_to_node` が依存として黙って除外せずエラーメッセージ化する。
    ここではそれを error 相当の Finding として報告する（unknown/dangling 検査と
    同様、依存宣言の書き漏れを検出可能にする）。
    """
    return [
        Finding("malformed_annotation", cc.LEVEL_ERROR, message)
        for message in result.malformed_annotations
    ]


def _check_orphan(result: ScanResult, config: cc.CoddConfig) -> list[Finding]:
    findings: list[Finding] = []
    for node in result.nodes:
        if node.kind in config.roots:
            continue
        has_outgoing = len(node.depends_on) > 0
        has_incoming = result.graph.incoming_count(node.node_id) > 0
        if not has_outgoing and not has_incoming:
            findings.append(
                Finding("orphan", cc.LEVEL_WARNING, f"{node.path}: 孤立ノード（参照ゼロ）")
            )
    return findings


def _check_drift(result: ScanResult, root: Path) -> list[Finding]:
    """drift 検査。ノードの最終更新時刻は `batch_commit_times()` で一括取得する。

    ノードごとに `commit_time()` を呼ぶと、1,000 ノード規模で git プロセスを
    ノード数に比例して起動してしまい著しく遅い（Issue #98 レビュー対応）。
    """
    findings: list[Finding] = []
    time_cache = batch_commit_times(root, [node.path for node in result.nodes])

    def time_of(rel: str) -> float:
        return time_cache.get(rel, 0.0)

    for node in result.nodes:
        downstream_time = time_of(node.path)
        for dep in node.depends_on:
            upstream = result.graph.nodes.get(dep.id)
            if upstream is None:
                continue
            if time_of(upstream.path) > downstream_time:
                findings.append(
                    Finding(
                        "drift",
                        cc.LEVEL_WARNING,
                        f"{node.path}: 上流 '{dep.id}' が下流より新しい（追従漏れの疑い）",
                    )
                )
    return findings


def run_checks(result: ScanResult, config: cc.CoddConfig, root: Path) -> list[Finding]:
    """全検査を実行し、config の level（off は除外）を適用した結果を返す。"""
    raw: list[Finding] = []
    raw.extend(_check_dangling(result))
    raw.extend(_check_duplicate(result))
    raw.extend(_check_cycle(result))
    raw.extend(_check_unknown(result, config))
    raw.extend(_check_missing_frontmatter(result))
    raw.extend(_check_malformed_annotation(result))
    raw.extend(_check_orphan(result, config))
    # drift は git/stat 呼び出しを伴うため、off のときは実行自体をスキップする。
    if config.checks.get("drift", cc.LEVEL_WARNING) != cc.LEVEL_OFF:
        raw.extend(_check_drift(result, root))

    applied: list[Finding] = []
    for finding in raw:
        level = config.checks.get(finding.check, finding.level)
        if level == cc.LEVEL_OFF:
            continue
        applied.append(Finding(finding.check, level, finding.message))
    return applied


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------


def cmd_scan(root: Path, config: cc.CoddConfig) -> int:
    result = scan_project(root, config)
    output_path = root / config.graph_path
    write_graph_jsonl(result, output_path)
    print(
        f"[codd scan] nodes={len(result.nodes)} "
        f"missing_frontmatter={len(result.missing_frontmatter)} "
        f"-> {config.graph_path}"
    )
    return 0


def cmd_graph(root: Path, config: cc.CoddConfig) -> int:
    result = scan_project(root, config)
    if not result.nodes:
        print("[codd graph] ノードがありません")
        return 0
    for node in result.nodes:
        print(f"{node.node_id} ({node.kind}/{node.status})")
        for dep in node.depends_on:
            mark = "" if result.graph.has(dep.id) else "  [missing]"
            print(f"  --{dep.relation}--> {dep.id}{mark}")
    return 0


def cmd_validate(root: Path, config: cc.CoddConfig) -> int:
    result = scan_project(root, config)
    findings = run_checks(result, config, root)
    errors = [f for f in findings if f.level == cc.LEVEL_ERROR]
    warnings = [f for f in findings if f.level == cc.LEVEL_WARNING]

    for finding in errors:
        print(f"ERROR  [{finding.check}] {finding.message}")
    for finding in warnings:
        print(f"WARN   [{finding.check}] {finding.message}")

    print(f"[codd validate] errors={len(errors)} warnings={len(warnings)}")
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# impact（Issue #94 / 信頼度3帯域）
# ---------------------------------------------------------------------------


class ImpactError(RuntimeError):
    """impact 分析が前提条件を満たせず続行できないことを示す。"""


@dataclass
class ImpactResult:
    """impact 分析の結果。影響先・変更ノード・削除上流をまとめる。"""

    ref: str
    changed_ids: list[str]
    impacted: list[cc.ImpactedNode]
    deleted_upstream: list[str]


def diff_changed_paths(root: Path, ref: str) -> tuple[set[str], set[str]]:
    """``git diff --name-status -z <ref>`` から変更パスと削除パスを返す。

    返り値は (changed, deleted)。rename は旧パスを deleted・新パスを changed に振る。
    copy は新パスのみ changed に加える（旧パスは変更されていないため changed/deleted
    いずれにも入れない）。

    ``-z`` を付けず改行区切りでパースすると、``core.quotePath``（既定 true）により
    非 ASCII パス（日本語ファイル名等）が ``"docs/\\346\\227\\245..."`` のように
    quote + 8進エスケープされ、``path_to_id`` との突合に失敗して検出漏れになる。
    ``-z`` は quotePath の影響を受けず NUL 区切りで raw パスを返すため、これを避ける。
    NUL 区切りの各レコードは、R/C（rename/copy）ステータスのみ
    ``status\\0old_path\\0new_path`` の3フィールド、それ以外は ``status\\0path`` の
    2フィールドになる。

    パスはリポジトリルート相対の posix。``--root`` が git ルートと一致する前提
    （drift 検査と同じ運用、設計 4.5 H-3 と整合）。
    """
    out = _git_output(root, ["diff", "--name-status", "-z", ref])
    if out is None:
        msg = f"git diff --name-status {ref!r} に失敗しました（無効な ref または git 実行エラー）"
        raise ImpactError(msg)
    changed: set[str] = set()
    deleted: set[str] = set()
    tokens = out.split("\0")
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue  # 末尾の NUL による空トークン
        if status.startswith(("R", "C")):
            if index + 1 >= len(tokens):
                break  # 出力が途中で切れている（想定外）
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
            if status.startswith("R"):
                deleted.add(old_path)
            changed.add(new_path)
        elif status.startswith("D"):
            if index >= len(tokens):
                break
            deleted.add(tokens[index])
            index += 1
        else:
            if index >= len(tokens):
                break
            changed.add(tokens[index])
            index += 1
    return changed, deleted


def _scope_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """scope glob を segment-aware な正規表現へ変換する。

    - ``*`` / ``?`` は 1 セグメント内（``/`` を跨がない）でマッチする
    - ``**`` は 0 個以上のディレクトリセグメントにマッチする（例: ``docs/**/*.md``）
    - ``[seq]`` / ``[!seq]`` は文字クラスとして解釈する（``Path.glob`` と同じ fnmatch 規約。
      閉じ ``]`` が無い場合はリテラル ``[`` として扱う。Issue #98 レビュー対応）
    - メタ文字を含まないパターンは完全一致

    ``Path.glob`` はファイルシステムを走査するため削除済みパスには使えない。
    純粋なパス文字列の判定として正規表現に落とす。文字クラスをここでもリテラル
    エスケープしてしまうと、通常走査（``collect_files`` の ``Path.glob``）と
    削除後 impact 判定（本関数）とで同じ glob の解釈が食い違ってしまう。
    """
    out: list[str] = []
    index, length = 0, len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                index += 2
                if index < length and pattern[index] == "/":
                    index += 1
                    out.append("(?:[^/]+/)*")  # 0 個以上のディレクトリセグメント
                else:
                    out.append(".*")  # 末尾の ** は残り全部にマッチ
            else:
                out.append("[^/]*")  # 単層: / を跨がない
                index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            end = _find_char_class_end(pattern, index)
            if end is None:
                out.append(re.escape(char))  # 閉じ ] が無い → リテラル [
                index += 1
            else:
                out.append(_char_class_to_regex(pattern[index + 1 : end]))
                index = end + 1
        else:
            out.append(re.escape(char))
            index += 1
    try:
        return re.compile("".join(out))
    except re.error:
        # `_char_class_to_regex()` が fnmatch と同じ正規化（不正範囲の除去）を
        # 行うようになったため、文字クラス起因の re.error は通常発生しない
        # （Issue #98 レビュー対応: 8巡目 P3）。ここに到達するのは文字クラス以外の
        # 未知の要因によるものだけのはずだが、クラッシュではなく「常に非マッチ」
        # として安全に扱う防御的フォールバックとして残す。
        return re.compile(r"(?!)")


def _find_char_class_end(pattern: str, start: int) -> int | None:
    """``pattern[start]`` が ``[`` の文字クラスの閉じ ``]`` の index を返す。

    fnmatch と同じ規約: ``[!...`` の直後、または ``[...`` の直後に来る最初の
    ``]`` はクラスの終端ではなくリテラル文字として扱う（例: ``[]]`` は ``]`` 1文字）。
    閉じ ``]`` が見つからない場合は None（呼び出し側でリテラル ``[`` として扱う）。
    """
    j = start + 1
    length = len(pattern)
    if j < length and pattern[j] == "!":
        j += 1
    if j < length and pattern[j] == "]":
        j += 1
    while j < length and pattern[j] != "]":
        j += 1
    return j if j < length else None


_RE_SETOPS_SUB = re.compile(r"([&~|])").sub


def _char_class_to_regex(stuff: str) -> str:
    """glob の文字クラス中身（``[`` と ``]`` の間）を regex 文字クラスへ変換する。

    ``!`` 先頭の否定を regex の ``^`` に変換し、regex 側で特別な意味を持つ
    先頭 ``^`` / バックスラッシュ / 集合演算子（``&`` ``~`` ``|``）はリテラルとして
    エスケープする。

    不正な文字範囲（``lo > hi``。例: ``[ab-a]`` の ``b-a``）は CPython
    ``fnmatch.translate()`` と同一のアルゴリズムで、範囲部分だけを除去し他の
    リテラル文字は保持する（``[ab-a]`` → リテラル ``a`` にマッチ）。以前は
    `_scope_pattern_to_regex()` 側で `re.compile` が `re.error` を送出した際、
    パターン全体を常時非マッチ（``(?!)``）にフォールバックしていたため、
    `collect_files`（`Path.glob` ベース、fnmatch 相当）とここ（削除済みファイル向け
    純粋パス判定）とで有効/無効ファイルの扱いが食い違い、ファイル削除後の
    `path_in_scope()` 判定で消失した上流ノードの警告を取りこぼしていた
    （Issue #98 レビュー対応: 8巡目 P3）。クラス全体が空になった場合（例: 単体の
    ``[z-a]``）のみ ``(?!)``（常時非マッチ）、``[!z-a]`` のように否定の空範囲は
    ``.``（任意の1文字にマッチ）にする（いずれも fnmatch と同じ規約）。
    """
    if "-" not in stuff:
        body = stuff.replace("\\", "\\\\")
    else:
        chunks: list[str] = []
        i = 0
        length = len(stuff)
        k = 2 if stuff.startswith("!") else 1
        while True:
            k = stuff.find("-", k, length)
            if k < 0:
                break
            chunks.append(stuff[i:k])
            i = k + 1
            k = k + 3
        chunk = stuff[i:length]
        if chunk:
            chunks.append(chunk)
        else:
            chunks[-1] += "-"
        # 不正な範囲（lo > hi）を除去する（fnmatch.translate と同じ規約）。
        for idx in range(len(chunks) - 1, 0, -1):
            if chunks[idx - 1][-1] > chunks[idx][0]:
                chunks[idx - 1] = chunks[idx - 1][:-1] + chunks[idx][1:]
                del chunks[idx]
        body = "-".join(c.replace("\\", "\\\\").replace("-", "\\-") for c in chunks)
    if not body:
        return "(?!)"  # 空クラス（範囲除去の結果、有効な文字が残らない）は常時非マッチ
    if body == "!":
        return "."  # 否定の空クラス（`[!lo-hi]` で lo > hi）は任意の1文字にマッチ
    body = _RE_SETOPS_SUB(r"\\\1", body)
    if body[0] == "!":
        body = "^" + body[1:]
    elif body[0] in ("^", "["):
        body = "\\" + body
    return f"[{body}]"


def _normalize_scope_pattern(root: Path, pattern: str) -> str | None:
    """scope glob パターンを root 相対の正規化形へレキシカルに畳み込む。

    ``../proj/src/**/*.py``（root の basename が ``proj``）のように、一度 root の
    外へ出て同じ root 内へ戻ってくるパターンは、通常走査（``_glob_relpaths()``）側
    では ``root.glob(pattern)`` の実ファイル解決 + ``os.path.normpath`` によって
    ``src/**/*.py`` に畳み込まれ、scan 対象になる。一方こちらの純粋パス判定
    （削除済みファイル向けの ``_matches_scope_pattern``）はパターン文字列を素朴に
    regex 化するだけだったため、`` ../proj/src/mod.py`` という別名の非マッチ扱いに
    なり、scan と impact 判定の間で解釈が食い違っていた（レビュー対応: codd.py:880）。

    ``root / pattern`` を ``os.path.normpath`` でレキシカルに畳み込み（ファイル
    システムへはアクセスしない。削除済みファイルは実体が無いため）、root 配下に
    収まっていれば root 相対の正規化パターンを返す。root の外（または root 自体）
    を指す場合は None（マッチ対象なし。``_glob_relpaths()`` が root 外を黙って
    除外するのと同じ扱い）。
    """
    combined = os.path.normpath(str(root / pattern))
    root_str = os.path.normpath(str(root))
    if combined == root_str:
        return None
    prefix = root_str + os.sep
    if not combined.startswith(prefix):
        return None
    return combined[len(prefix) :].replace(os.sep, "/")


def _matches_scope_pattern(root: Path, rel: str, pattern: str) -> bool:
    """rel が scope glob にマッチするか（削除済みファイル向けの純粋パス判定）。"""
    normalized = _normalize_scope_pattern(root, pattern)
    if normalized is None:
        return False
    return _scope_pattern_to_regex(normalized).fullmatch(rel) is not None


def path_in_scope(root: Path, rel: str, config: cc.CoddConfig) -> bool:
    """rel が codd scope（include − exclude）に属するか判定する。"""
    if not any(_matches_scope_pattern(root, rel, pat) for pat in config.include):
        return False
    return not any(_matches_scope_pattern(root, rel, pat) for pat in config.exclude)


def path_in_code_scope(root: Path, rel: str, config: cc.CoddConfig) -> bool:
    """rel が codd code_scope（include − exclude, Issue #98）に属するか判定する。

    ``config.code_include`` の既定は空リストのため、未設定プロジェクトでは常に False
    （既存挙動への影響ゼロ）。
    """
    if not config.code_include:
        return False
    if not any(_matches_scope_pattern(root, rel, pat) for pat in config.code_include):
        return False
    return not any(_matches_scope_pattern(root, rel, pat) for pat in config.code_exclude)


def _warn_if_not_git_root(root: Path) -> None:
    """root が git のトップレベルと異なる場合、無音の空結果を避けるため警告する。"""
    out = _git_output(root, ["rev-parse", "--show-toplevel"])
    if out is None:
        return
    toplevel = Path(out.strip())
    if toplevel.resolve() != root.resolve():
        print(
            f"[codd impact] WARN: --root ({root}) が git トップレベル ({toplevel}) と不一致。"
            "変更パスの突合に失敗し空結果になる可能性があります。",
            file=sys.stderr,
        )


def _decode_ref_source(rel: str, data: bytes) -> str | None:
    """ref 時点のソースの生バイト列を復号する（working tree と同じ PEP 263 規約）。

    working tree 側の `_read_source_text` と同様、Python（``.py``）は宣言済み
    エンコーディング（coding cookie / BOM）を尊重する。固定 UTF-8 のままだと、
    Latin-1 等で書かれた削除済み Python ファイルの impact 判定が
    `UnicodeDecodeError` で落ちてしまう（Issue #98 レビュー対応）。復号に失敗した
    場合は None（呼び出し側は CODD ノードでなかった扱いにする）。
    """
    if not rel.endswith(".py"):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (SyntaxError, LookupError, UnicodeDecodeError):
        return None


# scan は working tree の symlink を dereference して注釈を読む（`Path.open()` が
# 自動的にリンク先の内容を読み込む）。ref 時点の内容取得（`git show`）・working tree
# の変更検出（`compute_impact_result`）でも同じモデルに揃えないと、走査結果と impact
# 判定が食い違う（レビュー対応: codd.py:84）。
# - working tree: symlink 経由で登録されたノードの変更検出は、リンク先の相対パスも
#   `changed_paths` の突合対象に含める（`_symlink_target_relpath` / scenario 1: リンク先
#   だけを変更）。
# - ref 側: symlink を辿らず `git show ref:<path>` を素朴に呼ぶと symlink blob の中身
#   （リンク先パス文字列）が返ってしまい、注釈ではなくパス文字列を Python として構文解析
#   しようとして失敗する。`git ls-tree` でモード（120000 = symlink）を判定し、symlink
#   ならリンク先へ辿ってから内容を取得する（`_git_show_bytes_dereferencing_symlink` /
#   scenario 2: alias 削除時の旧 node_id 復元）。
_MAX_SYMLINK_HOPS = 8


def _symlink_target_relpath(root: Path, rel: str) -> str | None:
    """rel（working tree, root 相対）が symlink なら、直接のリンク先（1 hop 先）の
    root 相対パスを返す。

    `Path.resolve()` のようにチェーン全体を一気に最終ターゲットへ解決するのではなく、
    `os.readlink` でこの symlink 自身が指す先だけを読む。``api/alias.py ->
    ../links/current.py -> ../v1.py`` のような中継 symlink チェーンで中間リンク
    （``links/current.py``）だけが変更された場合、`git diff` は中間リンクのパスを
    返すため、最終ターゲット（``v1.py``）だけを追跡すると変更を検出できない
    （レビュー対応: 8巡目 codd.py:1009）。呼び出し側（`compute_impact_result`）が
    この関数を繰り返し呼んで各 hop を辿ることで、チェーン上の全パスを変更検出
    対象に含められる。

    symlink でない、または解決先が root の外（`_glob_relpaths` と同じ安全策）の
    場合は None。
    """
    path = root / rel
    if not path.is_symlink():
        return None
    try:
        link_text = os.readlink(path)
    except OSError:
        return None
    combined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link_text))
    if combined == os.pardir or combined.startswith(f"{os.pardir}/"):
        return None
    return combined


def _symlink_chain_relpaths(root: Path, rel: str) -> list[str]:
    """rel（working tree, root 相対）が symlink チェーンの起点なら、辿れる各 hop の
    root 相対パスを順に返す（Issue #98 レビュー対応: 8巡目）。

    ``alias.py -> links/current.py -> v1.py`` の場合、
    ``["links/current.py", "v1.py"]`` を返す。symlink でない場合は空リスト。
    循環 symlink 対策として `_MAX_SYMLINK_HOPS` で打ち切る。
    """
    hops: list[str] = []
    current = rel
    for _ in range(_MAX_SYMLINK_HOPS):
        target = _symlink_target_relpath(root, current)
        if target is None:
            break
        hops.append(target)
        current = target
    return hops


def _ref_blob_mode(root: Path, ref: str, rel: str) -> str | None:
    """ref 時点での rel の git tree エントリのモードを返す（例: ``120000`` = symlink）。

    `git show` はファイル内容しか返さず symlink かどうかを区別できないため、
    `git ls-tree` でモードを判定する。取得できない場合（存在しない等）は None。
    """
    out = _git_output(root, ["ls-tree", ref, "--", rel])
    if not out or not out.strip():
        return None
    first_line = out.splitlines()[0]
    parts = first_line.split(maxsplit=1)
    return parts[0] if parts else None


def _resolve_ref_symlink_target(rel: str, link_text: str) -> str | None:
    """ref 時点の symlink（rel）が指す先を、rel と同じ root からの相対パスへ解決する。

    root の外へ解決される場合は None（`_glob_relpaths` の root 外除外と同じ安全策）。

    ``link_text`` は git blob の内容をそのまま渡すこと（``strip()`` しない）。
    先頭/末尾に有意な空白を含むファイル名（例: ``" target.py"``）を指す symlink は
    strip すると working tree（`os.readlink` で空白を保持する `_symlink_target_relpath`）
    とは別のパスに解決されてしまい、alias 削除時に旧 node_id を復元できなくなる
    （レビュー対応: 8巡目 codd.py:1037）。
    """
    combined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link_text))
    if combined == os.pardir or combined.startswith(f"{os.pardir}/"):
        return None
    return combined


def _git_show_bytes_dereferencing_symlink(root: Path, ref: str, rel: str) -> bytes | None:
    """ref 時点の rel の内容を、working tree の scan と同じく symlink をたどって取得する。

    working tree 側（`_read_source_text` 経由の scan）は `Path.open()` が symlink を
    自動的に dereference するのに対し、`git show <ref>:<rel>` は symlink blob の中身
    （リンク先パス文字列）をそのまま返してしまう。`git ls-tree` のモードで symlink を
    判定しながらリンク先を辿ることで、working tree と同じ内容を取得する
    （レビュー対応: codd.py:84）。循環 symlink 対策として最大 hop 数で打ち切る。
    """
    current_rel = rel
    for _ in range(_MAX_SYMLINK_HOPS):
        mode = _ref_blob_mode(root, ref, current_rel)
        if mode is None:
            return None  # ref 側に存在しない
        if mode != "120000":
            return _git_output_bytes(root, ["show", f"{ref}:{current_rel}"])
        link_bytes = _git_output_bytes(root, ["show", f"{ref}:{current_rel}"])
        if link_bytes is None:
            return None
        link_text = link_bytes.decode("utf-8", errors="surrogateescape")
        target = _resolve_ref_symlink_target(current_rel, link_text)
        if target is None:
            return None
        current_rel = target
    return None  # symlink チェーンが深すぎる（循環の可能性）→ 安全側で諦める


def _old_node_id_at_ref(
    root: Path, ref: str, rel: str, config: cc.CoddConfig, *, is_code: bool
) -> str | None:
    """ref 時点の rel の内容から旧 node_id を回収する（doc frontmatter / コード注釈の両対応）。

    ref 側に存在しない、または CODD ノードでなかった場合は None。symlink（rel 自体、
    または途中の中継先）は working tree の scan と同じく dereference して内容を読む
    （`_git_show_bytes_dereferencing_symlink` / レビュー対応: codd.py:84）。
    """
    if is_code:
        # working tree 側（scan_code_nodes）と同様、未対応拡張子（画像等）は
        # `git show` で読み込む前に除外する（Issue #98 レビュー対応）。
        if not cx.is_supported_suffix(rel):
            return None
        data = _git_show_bytes_dereferencing_symlink(root, ref, rel)
        if data is None:
            return None  # ref 側に存在しない（新規追加→削除等）→ dangling 化しない
        text = _decode_ref_source(rel, data)
        if text is None:
            return None
        node, _errors = cx.extract_code_node(rel, text, config.inline_confidence)
        return node.node_id if node is not None else None
    data = _git_show_bytes_dereferencing_symlink(root, ref, rel)
    if data is None:
        return None  # ref 側に存在しない（新規追加→削除等）→ dangling 化しない
    try:
        out = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    codd = cc.parse_codd_frontmatter(out)
    old_id = str((codd or {}).get("node_id") or "").strip()
    return old_id or None


def _is_dangling_deletion(
    root: Path, ref: str, rel: str, graph: cc.CoddGraph, config: cc.CoddConfig, *, is_code: bool
) -> bool:
    """rel の削除/改名で旧 node_id が現グラフから消えたか（dangling 化の可能性）。

    rename（``R old new``）では old を deleted として受け取るが、node_id が新パスへ
    引き継がれていれば現グラフに残るため dangling ではない。ref 側の旧内容（doc
    frontmatter または Issue #98 のコード注釈）から node_id を回収し、現グラフに
    存在しない場合のみ dangling 候補とする。
    """
    old_id = _old_node_id_at_ref(root, ref, rel, config, is_code=is_code)
    if not old_id:
        return False  # CODD ノードでなかった → 依存元にならず dangling 化しない
    return not graph.has(old_id)


def _broken_code_symlink_relpaths(root: Path, config: cc.CoddConfig) -> set[str]:
    """code_scope 内で壊れた symlink（ターゲット不在）の root 相対パス集合を返す。

    ``aliases/core.py -> ../shared/core.py`` のような symlink で、リンク先の
    ``shared/core.py`` だけが削除されると、alias 自体は git 上変更されていないため
    `git diff` の changed/deleted どちらにも現れない。一方 `collect_code_files`
    （通常走査）は `is_file()` で判定するため、破損した alias は走査対象から静かに
    落ち、alias が保持していた旧コードノードの消失が `deleted_upstream` に警告されない
    （Issue #98 レビュー対応: 8巡目）。ここでは `is_file()` の代わりに「symlink かつ
    存在しない（broken）」を条件に候補を集め、`path_in_code_scope()` で scope
    membership（exclude 込み）を確認する。

    `config.code_include` が空（未設定）の場合は空集合（既存挙動への影響ゼロ）。
    """
    if not config.code_include:
        return set()
    resolved_root = root.resolve()
    candidates: set[str] = set()
    for pattern in config.code_include:
        for path in root.glob(pattern):
            if not path.is_symlink() or path.exists():
                continue  # symlink でない、またはリンク切れでない（正常）
            resolved_path = path.resolve()
            if not resolved_path.is_relative_to(resolved_root):
                continue
            normalized_path = Path(os.path.normpath(path))
            if not normalized_path.is_relative_to(root):
                continue
            candidates.add(normalized_path.relative_to(root).as_posix())
    return {rel for rel in candidates if path_in_code_scope(root, rel, config)}


def compute_impact_result(root: Path, config: cc.CoddConfig, ref: str) -> ImpactResult:
    """scan → diff 突合 → 影響分析までを行い ImpactResult を返す。"""
    _warn_if_not_git_root(root)
    result = scan_project(root, config)
    changed_paths, deleted_paths = diff_changed_paths(root, ref)

    path_to_id = {node.path: node.node_id for node in result.nodes}
    changed_ids = {path_to_id[p] for p in changed_paths if p in path_to_id}
    # scan は symlink（working tree の node.path）を dereference してリンク先の内容を
    # 注釈として読むため、リンク先だけを変更した場合も `git diff` はリンク先のパスを
    # 返す。node.path（symlink 自身）しか見ないと変更が検出されないため、リンク先の
    # root 相対パスも突合対象に加える（レビュー対応: codd.py:84）。
    # 複数の symlink ノードが同一ターゲットを指すことがあるため target -> node_id は
    # 多対一（list）で保持する。1対1 dict だと後勝ちで一部の symlink ノードが
    # changed_ids から漏れ、リンク先変更時の影響分析から欠落する（レビュー対応: 7巡目）。
    # ``alias.py -> links/current.py -> v1.py`` のような中継 symlink チェーンでは、
    # 最終ターゲット（v1.py）だけでなくチェーン上の全 hop（links/current.py も）を
    # 変更検出対象に含める。中間リンクだけが変更された場合 `git diff` は中間リンクの
    # パスを返すため、最終ターゲットしか見ないと変更が検出できない
    # （レビュー対応: 8巡目 codd.py:1009）。
    symlink_target_to_ids: dict[str, list[str]] = {}
    for node in result.nodes:
        for hop in _symlink_chain_relpaths(root, node.path):
            symlink_target_to_ids.setdefault(hop, []).append(node.node_id)
    changed_ids |= {node_id for p in changed_paths for node_id in symlink_target_to_ids.get(p, [])}

    # 削除された scope 内ドキュメント / code_scope 内コード（Issue #98 レビュー対応。
    # 注釈付きコードファイル削除も下流の dangling 化を検出対象にする）。
    # 削除済みファイルは working tree に無いため、純粋パス判定でスコープ membership を見る。
    # rename は old を deleted に含むが、node_id が現グラフに残るものは除外する（誤警告防止）。
    deleted_upstream_candidates = {
        p
        for p in deleted_paths
        if (
            path_in_scope(root, p, config)
            and _is_dangling_deletion(root, ref, p, result.graph, config, is_code=False)
        )
        or (
            path_in_code_scope(root, p, config)
            and _is_dangling_deletion(root, ref, p, result.graph, config, is_code=True)
        )
    }
    # ファイル自体は残っていても `codd:` 注釈の削除や node_id 変更で旧コード
    # ノードが消失するケースも同様に dangling 化しうる（Issue #98 レビュー対応）。
    # changed_paths（削除されていない）の code_scope ファイルについても ref 側の
    # 旧注釈を読み戻し、現グラフから消えていれば同じ集合に加える。
    deleted_upstream_candidates |= {
        p
        for p in changed_paths
        if path_in_code_scope(root, p, config)
        and _is_dangling_deletion(root, ref, p, result.graph, config, is_code=True)
    }
    # code_scope 内の symlink alias 自体は変更されていなくても、リンク先だけが
    # 削除されると working tree 上で壊れた symlink になり、`git diff` の
    # changed/deleted どちらにも現れないまま旧コードノードが消失しうる
    # （Issue #98 レビュー対応: 8巡目 codd.py:77）。
    deleted_upstream_candidates |= {
        p
        for p in _broken_code_symlink_relpaths(root, config)
        if _is_dangling_deletion(root, ref, p, result.graph, config, is_code=True)
    }
    deleted_upstream = sorted(deleted_upstream_candidates)

    impacted = cc.compute_impact(result.graph, changed_ids, config.impact)
    impacted.sort(key=lambda n: (_BAND_ORDER.get(n.band, 9), -n.score, n.node_id))
    return ImpactResult(
        ref=ref,
        changed_ids=sorted(changed_ids),
        impacted=impacted,
        deleted_upstream=deleted_upstream,
    )


_BAND_ORDER = {cc.BAND_GREEN: 0, cc.BAND_AMBER: 1, cc.BAND_GRAY: 2}
_BAND_LABEL = {
    cc.BAND_GREEN: "Green（自動更新可）",
    cc.BAND_AMBER: "Amber（要確認）",
    cc.BAND_GRAY: "Gray（参考）",
}


def _impact_to_json(result: ImpactResult) -> dict[str, Any]:
    return {
        "ref": result.ref,
        "changed_nodes": result.changed_ids,
        "impacted": [
            {
                "node_id": node.node_id,
                "path": node.path,
                "band": node.band,
                "score": node.score,
                "origins": node.origins,
                "min_hops": node.min_hops,
                "co_changed": node.co_changed,
            }
            for node in result.impacted
        ],
        "deleted_upstream": result.deleted_upstream,
    }


def print_impact_text(result: ImpactResult) -> None:
    counts = {band: 0 for band in (cc.BAND_GREEN, cc.BAND_AMBER, cc.BAND_GRAY)}
    for node in result.impacted:
        counts[node.band] += 1
    print(
        f"[codd impact] ref={result.ref} changed_nodes={len(result.changed_ids)} "
        f"impacted={len(result.impacted)} "
        f"(green={counts[cc.BAND_GREEN]} amber={counts[cc.BAND_AMBER]} "
        f"gray={counts[cc.BAND_GRAY]})"
    )
    for band in (cc.BAND_GREEN, cc.BAND_AMBER, cc.BAND_GRAY):
        rows = [n for n in result.impacted if n.band == band]
        if not rows:
            continue
        print(f"\n## {_BAND_LABEL[band]} ({len(rows)})")
        for node in rows:
            origins = ", ".join(node.origins) if node.origins else "-"
            flag = " [co_changed]" if node.co_changed else ""
            print(
                f"- {node.node_id}  {node.path}  score={node.score:.2f} "
                f"hops={node.min_hops}  via {origins}{flag}"
            )
    if result.deleted_upstream:
        # ファイル削除だけでなく、ファイルは残っていても `codd:` 注釈の削除・
        # node_id 変更で旧コードノードが消失したケースも含む（Issue #98 レビュー対応）。
        print(f"\n## 消失した上流ノード（dangling 注意, {len(result.deleted_upstream)}）")
        for path in result.deleted_upstream:
            print(f"- {path}  — `/codd-validate` で dangling を確認")


def cmd_impact(root: Path, config: cc.CoddConfig, ref: str, as_json: bool) -> int:
    try:
        result = compute_impact_result(root, config, ref)
    except ImpactError as exc:
        print(f"[codd impact] ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(_impact_to_json(result), ensure_ascii=False, indent=2))
    else:
        print_impact_text(result)
    return 0


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codd", description="CODD 整合性 CLI")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="config パス（既定: .claude/config/codd/codd.yaml）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="プロジェクトルート（既定: カレントディレクトリ）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="依存グラフを構築し JSONL 出力")
    sub.add_parser("graph", help="依存グラフをテキスト表示")
    sub.add_parser("validate", help="整合性を検証")
    impact = sub.add_parser("impact", help="変更 diff から下流影響を信頼度帯域で分類")
    impact.add_argument(
        "--diff",
        default="HEAD",
        help="比較対象の git ref（既定: HEAD。例: origin/main, HEAD~1）",
    )
    impact.add_argument(
        "--json",
        action="store_true",
        help="JSON で出力（既定はテキスト）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        config = cc.load_config(root / args.config)
        if not config.enabled:
            print("[codd] disabled（config の enabled: false）")
            return 0

        if args.command == "impact":
            return cmd_impact(root, config, args.diff, args.json)

        handlers = {"scan": cmd_scan, "graph": cmd_graph, "validate": cmd_validate}
        return handlers[args.command](root, config)
    except (TypeError, ValueError) as exc:
        # scope.include / code_scope.include 等の設定検証エラー（ValueError）や、
        # impact.* に bool を渡した際の型エラー（TypeError。`_reject_bool_as_number`
        # 参照）を、トレースバックではなく CLI の設定エラーとして整形する
        # （Issue #98 レビュー対応）。`src/**.py` のような不正な再帰 glob は
        # `cc.load_config` 完了後（scan/validate/impact の走査時）に `_glob_relpaths`
        # から ValueError が送出されるため、コマンド実行全体を同じ try に含めて
        # 捕捉する（Issue #98 レビュー対応: 8巡目）。
        print(f"[codd] ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
