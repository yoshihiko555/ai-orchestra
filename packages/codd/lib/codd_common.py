"""CODD 整合性レイヤーの共通ライブラリ。

フロントマター parser・グラフモデル・config ローダーを提供する。
hook ではなく純粋なライブラリ（CLI `scripts/codd.py` から import される）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# 語彙定義（設計 4.2 / 4.4）
# ---------------------------------------------------------------------------

# kind ごとの status 語彙。config ではなくコードで一元管理する。
ADR_STATUSES = ["proposed", "accepted", "rejected", "superseded", "deprecated"]
DEFAULT_STATUSES = ["draft", "active", "deprecated"]

STATUS_BY_KIND: dict[str, list[str]] = {
    "adr": ADR_STATUSES,
    "requirement": DEFAULT_STATUSES,
    "design": DEFAULT_STATUSES,
    "plan": DEFAULT_STATUSES,
    "rule": DEFAULT_STATUSES,
    "instruction": DEFAULT_STATUSES,
}


def valid_statuses(kind: str) -> list[str]:
    """kind に対応する status 語彙を返す（未知 kind は空リスト）。"""
    return STATUS_BY_KIND.get(kind, [])


# ---------------------------------------------------------------------------
# フロントマター parser（設計 4.2 / M-1）
# ---------------------------------------------------------------------------

FRONTMATTER_DELIMITER = "---"


def extract_frontmatter_block(text: str) -> str | None:
    """ドキュメント先頭の YAML frontmatter ブロックのみを抽出する。

    先頭行が `---` の場合に限り、次の `---` までを返す。
    本文中のコードブロック内 `---` や YAML 例は対象外（先頭ブロックのみ）。
    frontmatter が無い / 閉じられていない場合は None。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_DELIMITER:
            return "\n".join(lines[1:index])
    return None


def parse_codd_frontmatter(text: str) -> dict[str, Any] | None:
    """先頭 frontmatter の `codd:` ブロックを dict で返す。

    frontmatter 自体が無い / `codd:` キーが無い / 値が dict でない場合は None。
    """
    block = extract_frontmatter_block(text)
    if block is None:
        return None
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    codd = data.get("codd")
    if not isinstance(codd, dict):
        return None
    return codd


# ---------------------------------------------------------------------------
# グラフモデル（設計 4.3 / 1ファイル=1ノード）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dependency:
    """depends_on の 1 エントリ（参照先 node_id + 関係種別）。"""

    id: str
    relation: str


@dataclass(frozen=True)
class CoddNode:
    """1 ドキュメント = 1 ノード。"""

    node_id: str
    kind: str
    status: str
    depends_on: tuple[Dependency, ...]
    owner: str | None
    path: str  # プロジェクトルートからの相対パス


def _as_text(value: Any) -> str:
    """YAML 値を文字列へ正規化する。`None`（YAML の `null`）は空文字にする。

    `str(None)` が `"None"` になり架空の ID/語彙としてグラフに混入するのを防ぐ。
    """
    return "" if value is None else str(value)


def build_node(codd: dict[str, Any], path: str) -> CoddNode:
    """`codd:` ブロック dict から CoddNode を構築する。

    欠落フィールドは空文字 / None で補い、検証は validate 側に委ねる
    （ここで例外を投げず、unknown / missing 検査で扱えるようにする）。
    """
    raw_deps = codd.get("depends_on") or []
    deps: list[Dependency] = []
    if isinstance(raw_deps, list):
        for entry in raw_deps:
            if isinstance(entry, dict):
                deps.append(
                    Dependency(
                        id=_as_text(entry.get("id")),
                        relation=_as_text(entry.get("relation")),
                    )
                )
    owner = codd.get("owner")
    return CoddNode(
        node_id=_as_text(codd.get("node_id")),
        kind=_as_text(codd.get("kind")),
        status=_as_text(codd.get("status")),
        depends_on=tuple(deps),
        owner=str(owner) if owner is not None else None,
        path=path,
    )


@dataclass
class CoddGraph:
    """ノード集合と依存エッジを保持するグラフ。

    duplicate_paths は同一 node_id が複数ファイルに現れたケースを記録する
    （duplicate 検査用。nodes には最初の出現を保持する）。
    """

    nodes: dict[str, CoddNode] = field(default_factory=dict)
    duplicate_paths: dict[str, list[str]] = field(default_factory=dict)
    # 逆引きエッジ: target node_id -> それを depends_on に持つ node_id 集合。
    incoming: dict[str, set[str]] = field(default_factory=dict)

    def add(self, node: CoddNode) -> None:
        """ノードを追加する。node_id 重複は duplicate_paths に記録する。"""
        if not node.node_id:
            return
        if node.node_id in self.nodes:
            existing = self.nodes[node.node_id]
            paths = self.duplicate_paths.setdefault(node.node_id, [existing.path])
            paths.append(node.path)
            return
        self.nodes[node.node_id] = node
        for dep in node.depends_on:
            if dep.id != node.node_id:
                self.incoming.setdefault(dep.id, set()).add(node.node_id)

    def has(self, node_id: str) -> bool:
        return node_id in self.nodes

    def incoming_count(self, node_id: str) -> int:
        """node_id を depends_on に持つ他ノードの数（逆引きマップで O(1)）。"""
        return len(self.incoming.get(node_id, ()))

    def find_cycles(self) -> list[list[str]]:
        """depends_on を辿って到達する循環を検出する（存在するエッジのみ）。

        各循環は node_id のリストとして返す（始点 == 終点で閉じる）。
        同一循環は 1 回だけ返す。再帰ではなく明示スタックで実装し、
        大規模グラフでも RecursionError を起こさない。
        """
        cycles: list[list[str]] = []
        seen_keys: set[frozenset[str]] = set()
        visited: set[str] = set()

        for start in self.nodes:
            if start in visited:
                continue
            # スタック要素: (node_id, 次に見る depends_on のインデックス)
            stack: list[list] = [[start, 0]]
            path: list[str] = []
            on_path: set[str] = set()
            while stack:
                node_id, index = stack[-1]
                if index == 0:
                    path.append(node_id)
                    on_path.add(node_id)
                deps = [
                    dep.id
                    for dep in self.nodes[node_id].depends_on
                    if dep.id in self.nodes  # dangling は cycle 検査では無視
                ]
                if index < len(deps):
                    stack[-1][1] = index + 1
                    target = deps[index]
                    if target in on_path:
                        cut = path.index(target)
                        cycle = path[cut:] + [target]
                        key = frozenset(cycle)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            cycles.append(cycle)
                    elif target not in visited:
                        stack.append([target, 0])
                else:
                    stack.pop()
                    path.pop()
                    on_path.discard(node_id)
                    visited.add(node_id)
        return cycles


def build_graph(nodes: list[CoddNode]) -> CoddGraph:
    """CoddNode のリストから CoddGraph を構築する。"""
    graph = CoddGraph()
    for node in nodes:
        graph.add(node)
    return graph


# ---------------------------------------------------------------------------
# config ローダー（設計 4.6 / config-loading ルール準拠）
# ---------------------------------------------------------------------------

DEFAULT_GRAPH_FORMAT = "jsonl"
DEFAULT_GRAPH_PATH = ".claude/codd/graph.jsonl"

# 検査レベルの正準値。
LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LEVEL_OFF = "off"


def normalize_check_level(value: Any) -> str:
    """検査レベル値を正準文字列に正規化する。

    YAML 1.1 は bare `off` を boolean False として読むため、
    False / "off"（大文字小文字無視）を等しく `off` に揃える。
    """
    if value is False:
        return LEVEL_OFF
    return str(value).strip().lower()


@dataclass
class CoddConfig:
    """codd.yaml（+ local 上書き）の実効設定。"""

    enabled: bool
    include: list[str]
    exclude: list[str]
    kinds: list[str]
    relations: list[str]
    roots: list[str]
    graph_format: str
    graph_path: str
    checks: dict[str, str]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoddConfig:
        scope = data.get("scope") or {}
        graph_store = data.get("graph_store") or {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            include=list(scope.get("include") or []),
            exclude=list(scope.get("exclude") or []),
            kinds=list(data.get("kinds") or []),
            relations=list(data.get("relations") or []),
            roots=list(data.get("roots") or []),
            graph_format=str(graph_store.get("format", DEFAULT_GRAPH_FORMAT)),
            graph_path=str(graph_store.get("path", DEFAULT_GRAPH_PATH)),
            checks={
                str(name): normalize_check_level(level)
                for name, level in (data.get("checks") or {}).items()
            },
            raw=data,
        )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """base に override を再帰マージする（dict は再帰、その他は置換）。"""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _local_path(config_path: Path) -> Path:
    """`codd.yaml` に対する `codd.local.yaml` のパスを返す。"""
    return config_path.with_name(f"{config_path.stem}.local{config_path.suffix}")


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    """YAML を dict として読む。構文エラーはパス付きの ValueError に変換する。"""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid CODD config YAML: {path}") from exc
    return loaded if isinstance(loaded, dict) else {}


def load_config(config_path: Path) -> CoddConfig:
    """base config を読み、`*.local.yaml` があれば上書きマージして返す。"""
    base: dict[str, Any] = {}
    if config_path.exists():
        base = _load_yaml_dict(config_path)

    local_path = _local_path(config_path)
    if local_path.exists():
        base = deep_merge(base, _load_yaml_dict(local_path))

    return CoddConfig.from_dict(base)
