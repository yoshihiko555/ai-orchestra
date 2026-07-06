---
codd:
  node_id: "req:meta-harness"
  kind: requirement
  status: draft
  depends_on: []
  owner: ai-orchestra
---

# Meta-Harness（ハーネス最適化基盤）要件定義

**作成日**: 2026-07-06
**ステータス**: draft
**起点**: arXiv:2603.28052 "Meta-Harness: End-to-End Optimization of Model Harnesses"（Stanford/KRAFTON/MIT, 2026-03）
**関連**: `design:meta-harness`（`docs/design/meta-harness.md`）, `adr:ADR-20260706-030`

## 1. 背景

arXiv:2603.28052 は、LLM を取り巻く「ハーネス」（プロンプト足場・メモリ・検索・ツールオーケストレーション）
が最終性能を最大 6 倍左右することを示した。既存の自動プロンプト最適化（OPRO/TextGrad/GEPA/ACE）は
フィードバックをスコアや要約に圧縮しすぎており、失敗原因を診断できない。論文の Meta-Harness は
「ハーネス最適化プロセス自体のハーネス」であり、(1) 候補ハーネス群（population）、(2) 全候補のソース・
スコア・生実行トレースを永続保存する filesystem store、(3) 過去成果物を選択的に検査して新候補を提案する
agentic proposer、(4) evaluator、のループで品質 vs コストの Pareto frontier を返す。ablation では
「生トレースへのフルアクセス」が決定的（フルアクセス 50.0 vs スコアのみ 34.6）であることが示されている。

AI Orchestra 自身も `.claude/` 配下の skills / rules / hooks / config、facet 合成、外部 CLI ハーネス
（Codex/Antigravity）という形で独自のハーネスを持つが、その改善は現状すべて手作業のチューニングに
依存している。

## 2. 課題

| #   | 課題                                                                                                                                      |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| P-1 | ハーネス変更（facet/config の調整）の効果を再現可能に計測する手段がない                                                                   |
| P-2 | 実行トレースが 3 系統（skill-evolution metrics / fail-logs / codex-harness runs）に分断され、セッション横断のフルトレース永続ストアがない |
| P-3 | skill-evolution のオフライン反復改善層（Issue #139）が未実装で、改善ループが回っていない                                                  |
| P-4 | ハーネス候補を並行保持・比較する population の概念がなく、単一系譜の逐次変更しかできない                                                  |

## 3. スコープ (In/Out)

### 3.1 In Scope

- Claude Code ハーネス（facet ソース由来の skills/rules、allowlist された config キー）を第一対象とする
  候補管理・評価・提案・昇格の全ループ設計
- 全候補の来歴・生トレースを保存する永続ストア（filesystem store）
- 品質（二軸 judge）× コスト（トークン/ツール数/時間）の多目的評価と Pareto frontier 算出
- skill-evolution オフライン層（未実装）への委譲による責務統合

### 3.2 Out of Scope

| 項目                                            | 理由                                                          |
| ----------------------------------------------- | ------------------------------------------------------------- |
| 外部 CLI（Codex/Antigravity）ハーネスの最適化   | 将来拡張。まず Claude Code ハーネスで基盤を検証する           |
| モデル重み自体の最適化                          | 本機構はハーネス（足場）側の最適化に限定                      |
| 評価の完全自動化（rubric_judge 以上の自動判定） | LLM 評価には分散があり、人間レビューを完全代替しない          |
| facet への完全自動昇格（無人）                  | 品質ガバナンス上、当面は人間承認ゲート必須（ADR-027 D3 踏襲） |

## 4. 機能要件（FT）

| ID    | 要件                                                                                                                             | 優先   |
| ----- | -------------------------------------------------------------------------------------------------------------------------------- | ------ |
| FT-01 | `.claude/meta-harness/` に永続ストアを初期化・維持できる（SessionEnd クリーンアップ対象外、gitignore 対象）                      | must   |
| FT-02 | 候補ハーネス（宣言的オーバーレイ + 来歴メタデータ）を immutable に登録できる                                                     | must   |
| FT-03 | 隔離環境（一時 worktree 等）に overlay を適用し、固定シナリオを実行してフルトレースを redaction 付きで保存できる                 | must   |
| FT-04 | 二軸 judge（critical ゲート + 自己申告減点）とリソースコストで候補をスコアリングし ledger に記録できる                           | must   |
| FT-05 | ledger から品質 vs コストの Pareto frontier レポートを生成できる                                                                 | must   |
| FT-06 | proposer（Claude Code サブエージェント）が store を選択的に検査し、1 イテレーション 1 候補のオーバーレイと仮説ノートを生成できる | must   |
| FT-07 | budget・停止条件・発散/過学習/コストガード付きの自動探索ループを実行できる                                                       | should |
| FT-08 | 勝者オーバーレイを PR ベース（facet build / context build / context sync 経由）で反映できる。人間承認必須                        | must   |
| FT-09 | skill-evolution オフライン層からの `target=skill` 委譲を受け付ける I/F を持つ                                                    | must   |
| FT-10 | `docs/evaluation/<pkg>.checks.yaml` sidecar により must 観点を段階的に oracle 化できる                                           | should |

## 5. 非機能要件

| ID    | 要件                                                                                             |
| ----- | ------------------------------------------------------------------------------------------------ |
| NF-01 | 改善反映には人間承認ゲートを必須とし、無人での破壊的変更を行わない（ADR-027 NF-03 踏襲）         |
| NF-02 | 全トレースに codex-harness 同等以上の secret/PII redaction を適用する                            |
| NF-03 | 探索ループはコスト上限・反復上限のハード制約を持つ                                               |
| NF-04 | 既存 CLI コマンド・config キーの後方互換性を維持する                                             |
| NF-05 | store の retention・圧縮（jsonl.gz）方針を持ち、無制限肥大化を防ぐ                               |
| NF-06 | evaluator・シナリオは overlay 適用範囲外に固定し、改竄防止のため hash を run metadata に記録する |
| NF-07 | run metadata に実行環境（worktree・project root・source_commit）を必須記録し、混線を防ぐ         |

## 6. 受け入れ基準

- 現行ハーネス（baseline 候補）を候補として登録できる。
- baseline 候補をシナリオスイートで評価し、フルトレース（redaction 済み）が `.claude/meta-harness/runs/` に再現可能に保存される。
- ledger.jsonl と frontier.json が baseline 候補の評価結果から再生成可能な形で得られる。
- Phase 1（scaffold + store + register + evaluate + ledger + frontier）が proposer・promotion なしで独立に完結し、価値（現行ハーネスの再現可能な計測）を提供する。
- 評価・提案・昇格のいずれの段階でも、人間承認なしに facet/config の実ファイルが変更されない。
