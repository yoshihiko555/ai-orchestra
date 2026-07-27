"""CODD 整合性レイヤーの共通ライブラリ。

フロントマター parser・グラフモデル・config ローダーを提供する。
hook ではなく純粋なライブラリ（CLI `scripts/codd.py` から import される）。
"""

from __future__ import annotations

import math
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
    # Issue #98: コード⇔ドキュメントのトレーサビリティ（code/test ノード）。
    "code": DEFAULT_STATUSES,
    "test": DEFAULT_STATUSES,
}


def valid_statuses(kind: str) -> list[str]:
    """kind に対応する status 語彙を返す（未知 kind は空リスト）。"""
    return STATUS_BY_KIND.get(kind, [])


# node_id の `<kind>:<file-slug>` プレフィックス（設計 4.3 の表と一致）。
# requirement のみ "req" に略記され、他の kind はそのままの名前を使う。
NODE_ID_PREFIX_BY_KIND: dict[str, str] = {
    "requirement": "req",
    "design": "design",
    "adr": "adr",
    "plan": "plan",
    "rule": "rule",
    "instruction": "instruction",
    # Issue #98: コード⇔ドキュメントのトレーサビリティ（code/test ノード）。
    "code": "code",
    "test": "test",
}


def node_id_prefix(node_id: str) -> str | None:
    """node_id の `:` より前のプレフィックスを返す（EV-12: `<kind>:<file-slug>` 形式検証）。

    コロンが無い、コロンが複数個ある（余分なセパレータ）、または
    プレフィックス/スラッグのどちらかが空の場合は None
    （`<kind>:<file-slug>` 形式はコロンがちょうど 1 個であることを要求する）。
    """
    if node_id.count(":") != 1:
        return None
    prefix, _, slug = node_id.partition(":")
    if not prefix or not slug:
        return None
    return prefix


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
    """depends_on の 1 エントリ（参照先 node_id + 関係種別）。

    confidence はリンクの信頼度（Issue #98）。doc frontmatter で宣言された既存の
    リンクは人手レビュー済みの確定宣言として既定値 1.0。コード注釈から抽出された
    リンクは `codd_code` 側でより低い値（config の `inline_confidence`）を設定する。
    """

    id: str
    relation: str
    confidence: float = 1.0


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


def _clamp_unit_float(value: float, default: float) -> float:
    """confidence 値を有限な [0, 1] にクランプする（Issue #98 レビュー対応）。

    NaN/Inf のような非有限値は範囲判定・重み計算が破綻するため既定値へフォールバックする。
    範囲外の有限値（負値・1超）は境界へクランプする（例: -0.1 -> 0.0）。
    """
    if not math.isfinite(value):
        return default
    return min(1.0, max(0.0, value))


def _reject_bool_as_number(value: Any) -> Any:
    """YAML の bool を数値設定として受理しない（Issue #98 レビュー対応）。

    Python の `bool` は `int` のサブクラスのため、`float(False) == 0.0` /
    `float(True) == 1.0` が例外を投げずに黙って通ってしまう。
    `inline_confidence: false` のような設定ミスがそのまま confidence=0.0（全エッジ
    重みゼロで一斉 Gray 化）になるのを防ぐため、bool は明示的に拒否し、呼び出し側の
    `except (TypeError, ValueError)` でフォールバックさせる。
    """
    if isinstance(value, bool):
        raise TypeError(f"bool は数値設定として使用できません: {value!r}")
    return value


def _as_finite_int(value: Any, field_name: str) -> int:
    """impact 設定の整数値（`max_hops` / `corroboration_min_origins`）を検証する。

    YAML の `.inf`（`float("inf")`）のような非有限値をそのまま `int()` に渡すと
    `OverflowError`（`ValueError` のサブクラスではない）を送出し、`main()` の
    `except (TypeError, ValueError)` を素通りして未整形のトレースバックになる
    （P1 レビュー対応）。ここで非有限値を明示的に拒否し、万一 `int()` が
    `OverflowError` を送出した場合も `ValueError` へ正規化する。
    """
    checked = _reject_bool_as_number(value)
    if isinstance(checked, float) and not math.isfinite(checked):
        raise ValueError(f"{field_name} は有限の数値である必要があります（got {checked!r}）")
    try:
        return int(checked)
    except OverflowError as exc:
        raise ValueError(f"{field_name} を整数に変換できません（got {checked!r}）") from exc


def _as_confidence(value: Any) -> float:
    """depends_on エントリの confidence を正規化する（未指定 / 不正値は既定 1.0）。

    bool（`confidence: false` 等）も不正値として扱い、既定 1.0 にフォールバックする
    （`_reject_bool_as_number` 参照）。
    """
    if value is None:
        return 1.0
    try:
        parsed = float(_reject_bool_as_number(value))
    except (TypeError, ValueError):
        return 1.0
    return _clamp_unit_float(parsed, 1.0)


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
                        confidence=_as_confidence(entry.get("confidence")),
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
# impact 分析（Issue #94 / Phase 2 / 信頼度3帯域）
# ---------------------------------------------------------------------------
#
# 変更ノードの下流（depends_on で依存している側）を逆引きで辿り、各下流ノードを
# Green（自動更新可）/ Amber（要確認）/ Gray（参考）に分類する。
#
# 信頼度スコアは「宣言された relation の強度 × グラフ距離の減衰」で算出する。
# CODD は依存宣言を frontmatter に限定する（ADR-026 D3）ため、証拠源は relation 種別と
# 距離のみ。codd-dev の Noisy-OR / エビデンス種別分類はコード静的解析由来の多様な証拠を
# 確率合成する設計であり、本レイヤーには適用しない（証拠源が無い）。
#
# 取り込んだ補正（codd-dev 思想の借用）:
# - Corroboration rule: Green は「直接の強依存（1 hop・強 relation）= 事実」か、
#   「complementary な複数起点で裏付け（origins >= corroboration_min_origins）」のみ許す。
#   多段単一経路（推論的）は Amber 上限に留める。
# - co_changed cap: 下流ノード自身が同一 diff で変更済みなら Amber 上限にフラグ表示する
#   （testimony cap 相当。スコアは下げず破壊的変更を Gray に隠さない）。

# 信頼度帯域の正準値。
BAND_GREEN = "green"
BAND_AMBER = "amber"
BAND_GRAY = "gray"

DEFAULT_RELATION_WEIGHTS: dict[str, float] = {
    "derives_from": 1.0,
    "refines": 1.0,
    "implements": 1.0,
    "supersedes": 0.6,
    "references": 0.3,
}
DEFAULT_DECAY = 0.5
DEFAULT_MAX_HOPS = 6
DEFAULT_GREEN_THRESHOLD = 0.8
DEFAULT_AMBER_THRESHOLD = 0.4
DEFAULT_STRONG_RELATION_MIN = 1.0
DEFAULT_CORROBORATION_MIN_ORIGINS = 2
DEFAULT_EVIDENCE_BONUS = 0.05
# 未知 relation のフォールバック重み（弱依存扱い）。
UNKNOWN_RELATION_WEIGHT = 0.3


@dataclass(frozen=True)
class ImpactConfig:
    """impact 分析の重み・閾値設定（codd.yaml の ``impact:`` ブロック）。"""

    relation_weights: dict[str, float]
    decay: float
    max_hops: int
    green_threshold: float
    amber_threshold: float
    strong_relation_min: float
    corroboration_min_origins: int
    evidence_bonus: float

    def __post_init__(self) -> None:
        """誤設定で無音の異常結果を返さないよう値域を検証する。"""
        if not 0.0 < self.decay <= 1.0:
            raise ValueError(f"impact.decay は (0, 1] の範囲（got {self.decay}）")
        if self.max_hops < 1:
            raise ValueError(f"impact.max_hops は 1 以上（got {self.max_hops}）")
        for name, value in (
            ("green_threshold", self.green_threshold),
            ("amber_threshold", self.amber_threshold),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"impact.{name} は [0, 1] の範囲（got {value}）")
        if self.corroboration_min_origins < 1:
            raise ValueError(
                f"impact.corroboration_min_origins は 1 以上（got {self.corroboration_min_origins}）"
            )
        if self.amber_threshold > self.green_threshold:
            raise ValueError(
                "impact.amber_threshold は green_threshold 以下である必要がある"
                f"（amber={self.amber_threshold} > green={self.green_threshold}）"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImpactConfig:
        weights = dict(DEFAULT_RELATION_WEIGHTS)
        # `relation_weights: []` のようなマッピング以外の値は `.items()` で
        # `AttributeError` になり、`main()` の `(TypeError, ValueError)` ハンドラを
        # 素通りして未整形のトレースバックになる（P1 レビュー対応）。`_as_mapping()`
        # で先に検証し、設定エラーとして整形させる。
        for name, value in _as_mapping(
            data.get("relation_weights"), "impact.relation_weights"
        ).items():
            try:
                weights[str(name)] = float(_reject_bool_as_number(value))
            except (TypeError, ValueError):
                continue
        # bool は int のサブクラスのため int()/float() が黙って通ってしまう
        # （例: `max_hops: true` -> `1`）。`_reject_bool_as_number` で明示的に拒否する
        # （Issue #98 レビュー対応）。範囲外の値は __post_init__ の検証に委ねる。
        return cls(
            relation_weights=weights,
            decay=float(_reject_bool_as_number(data.get("decay", DEFAULT_DECAY))),
            max_hops=_as_finite_int(data.get("max_hops", DEFAULT_MAX_HOPS), "impact.max_hops"),
            green_threshold=float(
                _reject_bool_as_number(data.get("green_threshold", DEFAULT_GREEN_THRESHOLD))
            ),
            amber_threshold=float(
                _reject_bool_as_number(data.get("amber_threshold", DEFAULT_AMBER_THRESHOLD))
            ),
            strong_relation_min=float(
                _reject_bool_as_number(data.get("strong_relation_min", DEFAULT_STRONG_RELATION_MIN))
            ),
            corroboration_min_origins=_as_finite_int(
                data.get("corroboration_min_origins", DEFAULT_CORROBORATION_MIN_ORIGINS),
                "impact.corroboration_min_origins",
            ),
            evidence_bonus=float(
                _reject_bool_as_number(data.get("evidence_bonus", DEFAULT_EVIDENCE_BONUS))
            ),
        )

    def weight_of(self, relation: str) -> float:
        """relation の重みを返す（未知は弱依存フォールバック）。"""
        return self.relation_weights.get(relation, UNKNOWN_RELATION_WEIGHT)


@dataclass
class ImpactedNode:
    """impact 分析で影響先と判定された 1 ノード。"""

    node_id: str
    path: str
    score: float
    band: str
    origins: list[str]  # この下流に到達した変更ノード（昇順）
    min_hops: int
    co_changed: bool  # 同一 diff で自身も変更されている


def build_edge_weights(graph: CoddGraph, config: ImpactConfig) -> dict[tuple[str, str], float]:
    """``(target_id, source_id) -> 重み`` のエッジ重みキャッシュを構築する。

    エッジ ``source depends_on target`` の重みは ``relation 重み × confidence``（Issue #98）。
    doc frontmatter 由来のリンクは confidence 既定 1.0 のため従来と同じ重みになる。コード注釈
    由来の低信頼リンクは、この掛け算で impact 分析への影響が比例して弱まる。source が target を
    複数 relation で参照する場合は最大重み（最良証拠）を採る。グラフ構築後は不変なので
    traversal 前に一度だけ計算し、hot path での depends_on 線形走査を避ける。
    """
    cache: dict[tuple[str, str], float] = {}
    for source_id, node in graph.nodes.items():
        for dep in node.depends_on:
            if dep.id == source_id:
                continue
            key = (dep.id, source_id)
            weight = config.weight_of(dep.relation) * dep.confidence
            cache[key] = max(cache.get(key, 0.0), weight)
    return cache


def _band_for_score(score: float, config: ImpactConfig) -> str:
    """スコアを帯域へ写像する（補正前の素の帯域）。"""
    if score >= config.green_threshold:
        return BAND_GREEN
    if score >= config.amber_threshold:
        return BAND_AMBER
    return BAND_GRAY


@dataclass
class _Accumulator:
    """1 下流ノードに対する経路探索の集計バケット。"""

    best_score: float = 0.0
    min_hops: int = 0
    has_direct_fact: bool = False
    # 到達した全変更起点（表示用 via）。
    origins: set[str] = field(default_factory=set)
    # amber 閾値以上の経路で到達した変更起点（Corroboration カウント用）。
    strong_origins: set[str] = field(default_factory=set)


def compute_impact(
    graph: CoddGraph,
    changed_ids: set[str],
    config: ImpactConfig,
) -> list[ImpactedNode]:
    """変更ノード集合から下流の影響先を列挙し信頼度帯域へ分類する。

    各変更起点から incoming（逆引き）エッジを単純パスで辿り（サイクル安全・
    ``max_hops`` で打ち切り）、``path_score = min(経路上の重み) × decay^(hops-1)`` を
    計算する。ノードごとに全経路・全起点の最良スコアを採り、補正を適用する。
    """
    acc: dict[str, _Accumulator] = {}
    weights = build_edge_weights(graph, config)

    for origin in changed_ids:
        if origin not in graph.nodes:
            continue
        # スタック要素: (現在ノード, hops, 経路上の最小重み, 訪問済み集合)
        stack: list[tuple[str, int, float, frozenset[str]]] = [
            (origin, 0, 1.0, frozenset({origin}))
        ]
        while stack:
            current, hops, min_w, visited = stack.pop()
            next_hops = hops + 1
            if next_hops > config.max_hops:
                continue
            for source_id in graph.incoming.get(current, ()):
                if source_id in visited:
                    continue  # 単純パスのみ（循環を辿らない）
                weight = weights.get((current, source_id), 0.0)
                new_min = min(min_w, weight)
                path_score = new_min * (config.decay ** (next_hops - 1))
                is_direct_fact = next_hops == 1 and weight >= config.strong_relation_min
                bucket = acc.setdefault(source_id, _Accumulator(min_hops=next_hops))
                if path_score > bucket.best_score:
                    bucket.best_score = path_score
                bucket.min_hops = min(bucket.min_hops, next_hops)
                bucket.has_direct_fact = bucket.has_direct_fact or is_direct_fact
                bucket.origins.add(origin)
                if path_score >= config.amber_threshold:
                    bucket.strong_origins.add(origin)
                stack.append((source_id, next_hops, new_min, visited | {source_id}))

    return [
        _finalize(node_id, bucket, graph, changed_ids, config) for node_id, bucket in acc.items()
    ]


def _finalize(
    node_id: str,
    bucket: _Accumulator,
    graph: CoddGraph,
    changed_ids: set[str],
    config: ImpactConfig,
) -> ImpactedNode:
    """集計バケットへ件数ボーナス・Corroboration・co_changed 補正を適用する。"""
    score = bucket.best_score
    strong_count = len(bucket.strong_origins)
    # 件数ボーナス: amber 以上の経路を持つ複数起点が裏付ける場合のみ加点（弱リンク水増し防止）。
    if strong_count >= 2:
        score = min(1.0, score + config.evidence_bonus * (strong_count - 1))

    band = _band_for_score(score, config)
    # Corroboration rule: Green は直接事実か、十分な起点数の裏付けが必要。
    if (
        band == BAND_GREEN
        and not bucket.has_direct_fact
        and strong_count < config.corroboration_min_origins
    ):
        band = BAND_AMBER

    # co_changed cap: 自身も変更済みなら Green を Amber に留める（フラグで可視化）。
    co_changed = node_id in changed_ids
    if co_changed and band == BAND_GREEN:
        band = BAND_AMBER

    node = graph.nodes.get(node_id)
    return ImpactedNode(
        node_id=node_id,
        path=node.path if node else "",
        score=round(score, 4),
        band=band,
        origins=sorted(bucket.origins),
        min_hops=bucket.min_hops,
        co_changed=co_changed,
    )


# ---------------------------------------------------------------------------
# config ローダー（設計 4.6 / config-loading ルール準拠）
# ---------------------------------------------------------------------------

DEFAULT_GRAPH_FORMAT = "jsonl"
DEFAULT_GRAPH_PATH = ".claude/codd/graph.jsonl"
# code/test 注釈（Issue #98）の既定信頼度。doc frontmatter（1.0）より低く、
# 1 行注釈という軽量な記法ゆえの不確実性を反映する。
DEFAULT_INLINE_CONFIDENCE = 0.7

# 検査レベルの正準値。
LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LEVEL_OFF = "off"
ALLOWED_CHECK_LEVELS = {LEVEL_ERROR, LEVEL_WARNING, LEVEL_OFF}


def normalize_check_level(value: Any) -> str:
    """検査レベル値を正準文字列に正規化する。

    YAML 1.1 は bare `off` を boolean False として読むため、
    False / "off"（大文字小文字無視）を等しく `off` に揃える。
    ``error`` / ``warning`` / ``off`` 以外の値（typo 等）は、Finding の level が
    どの集計カテゴリにも一致せず validate が無音の成功になる（CI ゲートのサイレント
    無効化）ことを防ぐため、ここで ValueError にする。
    """
    if value is False:
        return LEVEL_OFF
    level = str(value).strip().lower()
    if level not in ALLOWED_CHECK_LEVELS:
        raise ValueError(f"Invalid check level: {value!r} (allowed: error / warning / off)")
    return level


def _as_mapping(value: Any, field_name: str) -> dict[str, Any]:
    """``scope`` / ``code_scope`` / ``graph_store`` 等のサブセクションを検証する。

    YAML で ``scope: oops``（文字列）や ``scope: [a, b]``（リスト）のように mapping
    以外を書くと、後続の ``.get()`` 呼び出しが ``AttributeError`` になり、
    ``main()`` の ``(TypeError, ValueError)`` ハンドラを素通りして未整形の
    トレースバックが出てしまう（Issue #98 レビュー対応）。mapping 以外は
    ``ValueError`` にして設定エラーとして整形させる。
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise ValueError(f"{field_name} はマッピングである必要があります: {value!r}")


def _as_glob_list(value: Any, field_name: str) -> list[str]:
    """scope/code_scope の include・exclude を glob 文字列のリストへ正規化する。

    YAML でリスト記法を忘れて単一文字列（例: ``code_scope.include: "src/**/*.py"``）を
    書くと、素朴な ``list(value)`` では文字列が 1 文字ずつイテレートされ、無意味な
    glob（`s`, `r`, `c`, ...）として扱われてしまう（Issue #98 レビュー対応）。単一文字列は
    単要素リストとして扱い、リスト以外の型（数値・dict 等）は設定エラーとして拒否する。

    空文字列（``scope.include: ""``）は「対象なし」を表す既存設定との後方互換のため
    空リストとして扱う（P1 レビュー対応）。旧実装 ``list(value or [])`` では空文字列が
    偽値として ``[]`` になっていたが、単一文字列を単要素リスト化する本関数の変換を
    素朴に空文字列へも適用すると ``[""]`` になり、後続の ``Path.glob("")`` が
    ``ValueError: Unacceptable pattern: ''`` を送出して `main()` の設定ロード用
    ハンドラより後（走査時）に CLI がトレースバックで終了してしまう。

    リスト内の要素すべてが文字列であればこのチェックは通過するため、
    ``code_scope.include: [""]`` のようにリスト**内**の空文字列要素も同じ
    ``Path.glob("")`` の ``ValueError`` を引き起こしうる。単独の空文字列と同様
    「対象なし」として扱い、空文字列要素は結果から除去する（Issue #98 レビュー対応）。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ValueError(f"{field_name} の要素は全て文字列である必要があります: {value!r}")
        return [item for item in value if item != ""]
    raise ValueError(f"{field_name} は文字列またはリストである必要があります: {value!r}")


def _load_inline_confidence(data: dict[str, Any]) -> float:
    """codd.yaml の `inline_confidence` を有限な [0, 1] へ正規化する（Issue #98 レビュー対応）。

    `-0.1` のような範囲外の値や YAML の `.nan` がそのまま depends_on の confidence へ
    流れ込むと、impact のエッジ重み（relation 重み × confidence）が負値/NaN になり、
    誤って Gray 判定になったり JSONL の書き出しが壊れたりする（`json.dumps` は既定で
    NaN を非標準の `NaN` リテラルとして出力してしまうため）。未指定 / 不正値（bool を含む。
    `_reject_bool_as_number` 参照）は `DEFAULT_INLINE_CONFIDENCE` にフォールバックする。
    """
    raw = data.get("inline_confidence", DEFAULT_INLINE_CONFIDENCE)
    try:
        parsed = float(_reject_bool_as_number(raw))
    except (TypeError, ValueError):
        return DEFAULT_INLINE_CONFIDENCE
    return _clamp_unit_float(parsed, DEFAULT_INLINE_CONFIDENCE)


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
    impact: ImpactConfig
    # Issue #98: コード⇔ドキュメントのトレーサビリティ（opt-in・既定は空 = 挙動変化なし）。
    code_include: list[str]
    code_exclude: list[str]
    inline_confidence: float
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoddConfig:
        scope = _as_mapping(data.get("scope"), "scope")
        graph_store = _as_mapping(data.get("graph_store"), "graph_store")
        code_scope = _as_mapping(data.get("code_scope"), "code_scope")
        return cls(
            enabled=bool(data.get("enabled", True)),
            include=_as_glob_list(scope.get("include"), "scope.include"),
            exclude=_as_glob_list(scope.get("exclude"), "scope.exclude"),
            kinds=list(data.get("kinds") or []),
            relations=list(data.get("relations") or []),
            roots=list(data.get("roots") or []),
            graph_format=str(graph_store.get("format", DEFAULT_GRAPH_FORMAT)),
            graph_path=str(graph_store.get("path", DEFAULT_GRAPH_PATH)),
            # `checks: []` / `impact: []` のようなマッピング以外の値は `.items()` /
            # `.get()` で `AttributeError` になり、`main()` の
            # `(TypeError, ValueError)` ハンドラを素通りして未整形のトレースバックに
            # なる（P1 レビュー対応）。`_as_mapping()` で先に検証する。
            checks={
                str(name): normalize_check_level(level)
                for name, level in _as_mapping(data.get("checks"), "checks").items()
            },
            impact=ImpactConfig.from_dict(_as_mapping(data.get("impact"), "impact")),
            code_include=_as_glob_list(code_scope.get("include"), "code_scope.include"),
            code_exclude=_as_glob_list(code_scope.get("exclude"), "code_scope.exclude"),
            inline_confidence=_load_inline_confidence(data),
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
