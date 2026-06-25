#!/usr/bin/env python3
"""CODD CLI: scan / validate / graph。

`orchex run codd codd -- <subcommand>` から、または直接実行する。
プロジェクトルート（cwd）を基準に scope を走査し、依存グラフの構築・整合性検証・
可視化を行う。設定は `.claude/config/codd/codd.yaml`（+ local 上書き）。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# lib/ を import パスへ追加（scripts/ と lib/ は同一パッケージ配下）。
_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import codd_common as cc  # noqa: E402

DEFAULT_CONFIG_PATH = Path(".claude/config/codd/codd.yaml")


# ---------------------------------------------------------------------------
# ノード収集（scan / validate / graph で共有）
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """走査結果。グラフ・収集ノード・frontmatter 欠落ファイルを保持する。"""

    graph: cc.CoddGraph
    nodes: list[cc.CoddNode]
    missing_frontmatter: list[str]


def _matches_any(rel_path: str, patterns: list[str]) -> bool:
    """相対 posix パスがいずれかの glob にマッチするか。"""
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def collect_files(root: Path, config: cc.CoddConfig) -> list[Path]:
    """include glob から exclude を差し引いた対象ファイル一覧を返す。"""
    found: dict[str, Path] = {}
    for pattern in config.include:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if _matches_any(rel, config.exclude):
                continue
            found[rel] = path
    return [found[rel] for rel in sorted(found)]


def scan_project(root: Path, config: cc.CoddConfig) -> ScanResult:
    """scope を走査してグラフを構築する。"""
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
    graph = cc.build_graph(nodes)
    return ScanResult(graph=graph, nodes=nodes, missing_frontmatter=missing)


# ---------------------------------------------------------------------------
# グラフの JSONL 永続化
# ---------------------------------------------------------------------------


def node_to_record(node: cc.CoddNode) -> dict[str, Any]:
    """CoddNode を JSONL 1 行分の dict に変換する。"""
    return {
        "node_id": node.node_id,
        "kind": node.kind,
        "status": node.status,
        "owner": node.owner,
        "path": node.path,
        "depends_on": [{"id": dep.id, "relation": dep.relation} for dep in node.depends_on],
    }


def write_graph_jsonl(result: ScanResult, output_path: Path) -> None:
    """グラフを JSONL として書き出す（1 ノード 1 行）。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(node_to_record(node), ensure_ascii=False) for node in result.nodes]
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# ---------------------------------------------------------------------------
# drift 用の時刻ソース（設計 4.5 / H-3）
# ---------------------------------------------------------------------------


def commit_time(root: Path, rel_path: str) -> float:
    """最終コミット時刻（epoch 秒）。未コミットは mtime にフォールバック。"""
    try:
        completed = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", rel_path],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        out = completed.stdout.strip()
        if completed.returncode == 0 and out:
            return float(out)
    except (OSError, ValueError):
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    config = cc.load_config(root / args.config)
    if not config.enabled:
        print("[codd] disabled（config の enabled: false）")
        return 0

    handlers = {"scan": cmd_scan, "graph": cmd_graph, "validate": cmd_validate}
    return handlers[args.command](root, config)


if __name__ == "__main__":
    sys.exit(main())
