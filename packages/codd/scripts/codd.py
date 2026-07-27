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
    """
    matched: set[str] = set()
    resolved_root = root.resolve()
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            if not path.resolve().is_relative_to(resolved_root):
                continue
            matched.add(path.relative_to(root).as_posix())
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


def _read_source_text(path: Path) -> str:
    """ソースファイルをテキストとして読む。

    Python ファイル（``.py``）は PEP 263 の宣言済みエンコーディング（先頭2行の
    ``# -*- coding: ... -*-`` cookie または BOM）を尊重する（`tokenize.detect_encoding`）。
    固定 UTF-8 のままだと、Latin-1 等の coding cookie を持つ有効な Python ファイルが
    `UnicodeDecodeError` になってしまう。それ以外の対応言語（TS/JS/Go 等）は UTF-8 固定。
    """
    if path.suffix != ".py":
        return path.read_text(encoding="utf-8")
    with path.open("rb") as fh:
        encoding, _ = tokenize.detect_encoding(fh.readline)
    return path.read_text(encoding=encoding)


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


def _git_output_bytes(root: Path, args: list[str]) -> bytes | None:
    """git コマンドを実行し stdout を生バイト列で返す。失敗時は None。

    ``git show <ref>:<rel>`` でコミット時点の Python ソースを取得する際に使う
    （`_old_node_id_at_ref` / Issue #98 レビュー対応）。working tree 側の
    `_read_source_text` と同じく PEP 263 宣言済みエンコーディングを尊重するには、
    先に UTF-8 固定でデコードしない生バイト列が必要なため、`_git_output` とは別に
    エンコーディング指定なしで実行する。
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
    findings: list[Finding] = []
    time_cache: dict[str, float] = {}

    def time_of(rel: str) -> float:
        if rel not in time_cache:
            time_cache[rel] = commit_time(root, rel)
        return time_cache[rel]

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
    - メタ文字を含まないパターンは完全一致

    ``Path.glob`` はファイルシステムを走査するため削除済みパスには使えない。
    純粋なパス文字列の判定として正規表現に落とす。
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
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile("".join(out))


def _matches_scope_pattern(rel: str, pattern: str) -> bool:
    """rel が scope glob にマッチするか（削除済みファイル向けの純粋パス判定）。"""
    return _scope_pattern_to_regex(pattern).fullmatch(rel) is not None


def path_in_scope(rel: str, config: cc.CoddConfig) -> bool:
    """rel が codd scope（include − exclude）に属するか判定する。"""
    if not any(_matches_scope_pattern(rel, pat) for pat in config.include):
        return False
    return not any(_matches_scope_pattern(rel, pat) for pat in config.exclude)


def path_in_code_scope(rel: str, config: cc.CoddConfig) -> bool:
    """rel が codd code_scope（include − exclude, Issue #98）に属するか判定する。

    ``config.code_include`` の既定は空リストのため、未設定プロジェクトでは常に False
    （既存挙動への影響ゼロ）。
    """
    if not config.code_include:
        return False
    if not any(_matches_scope_pattern(rel, pat) for pat in config.code_include):
        return False
    return not any(_matches_scope_pattern(rel, pat) for pat in config.code_exclude)


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


def _old_node_id_at_ref(
    root: Path, ref: str, rel: str, config: cc.CoddConfig, *, is_code: bool
) -> str | None:
    """ref 時点の rel の内容から旧 node_id を回収する（doc frontmatter / コード注釈の両対応）。

    ref 側に存在しない、または CODD ノードでなかった場合は None。
    """
    if is_code:
        data = _git_output_bytes(root, ["show", f"{ref}:{rel}"])
        if data is None:
            return None  # ref 側に存在しない（新規追加→削除等）→ dangling 化しない
        text = _decode_ref_source(rel, data)
        if text is None:
            return None
        node, _errors = cx.extract_code_node(rel, text, config.inline_confidence)
        return node.node_id if node is not None else None
    out = _git_output(root, ["show", f"{ref}:{rel}"])
    if out is None:
        return None  # ref 側に存在しない（新規追加→削除等）→ dangling 化しない
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


def compute_impact_result(root: Path, config: cc.CoddConfig, ref: str) -> ImpactResult:
    """scan → diff 突合 → 影響分析までを行い ImpactResult を返す。"""
    _warn_if_not_git_root(root)
    result = scan_project(root, config)
    changed_paths, deleted_paths = diff_changed_paths(root, ref)

    path_to_id = {node.path: node.node_id for node in result.nodes}
    changed_ids = {path_to_id[p] for p in changed_paths if p in path_to_id}

    # 削除された scope 内ドキュメント / code_scope 内コード（Issue #98 レビュー対応。
    # 注釈付きコードファイル削除も下流の dangling 化を検出対象にする）。
    # 削除済みファイルは working tree に無いため、純粋パス判定でスコープ membership を見る。
    # rename は old を deleted に含むが、node_id が現グラフに残るものは除外する（誤警告防止）。
    deleted_upstream = sorted(
        p
        for p in deleted_paths
        if (
            path_in_scope(p, config)
            and _is_dangling_deletion(root, ref, p, result.graph, config, is_code=False)
        )
        or (
            path_in_code_scope(p, config)
            and _is_dangling_deletion(root, ref, p, result.graph, config, is_code=True)
        )
    )

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
        print(f"\n## 削除された上流（dangling 注意, {len(result.deleted_upstream)}）")
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
    config = cc.load_config(root / args.config)
    if not config.enabled:
        print("[codd] disabled（config の enabled: false）")
        return 0

    if args.command == "impact":
        return cmd_impact(root, config, args.diff, args.json)

    handlers = {"scan": cmd_scan, "graph": cmd_graph, "validate": cmd_validate}
    return handlers[args.command](root, config)


if __name__ == "__main__":
    sys.exit(main())
