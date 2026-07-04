# design-flow 評価セット（スキルフロー）

**対象スキル群**: `design` / `preflight` / `startproject`（正本: `facets/instructions/{design,preflight,startproject}.md`, `facets/knowledge/design-review.md` ほか knowledge 6 本）
**単位**: スキルフロー（設計 → タスク分解 → 実装の一連の振る舞い）
**作成日**: 2026-07-04
**最終レビュー日**: 2026-07-04
**情報源**: facets/instructions/design.md, facets/instructions/preflight.md, facets/instructions/startproject.md, facets/knowledge/design-review.md, .claude/rules/skill-review-policy.md, .claude/rules/codd-frontmatter-policy.md, PR #144

> **パッケージ評価セットとの違い**: スキルは Markdown 指示書であり pytest で強制できない。
> この評価セットは「振る舞い仕様書」として機能し、テストコードとの突合（`evaluation-set-policy`
> ルールの MUST 手順)の対象外。検証手段は下記「検証方法」に従う。

## 1. フロー責務定義

設計フェーズの 3 スキルが `docs/` 配下の設計成果物を介して連続し、設計品質を二段品質ゲート（セルフチェック + 自動レビュー）で担保するフロー。`/design` は対話で要件定義・基本設計・詳細設計を進めて設計書を出力し、`/preflight` は設計要否を判定して必要なら `/design` へ誘導しつつタスク分解を行い、`/startproject` は設計書を入力として実装し、実装後に設計書との整合を検証する。起点は `/design` 先行・`/preflight` 起点のどちらでもよい。

### Non-Goals

- 設計内容そのものの正しさの保証（ゲートは検出装置であり、最終判断はユーザーの受け入れ確認）
- コード実装の品質担保（`/review` / `/tdd` / quality-gates パッケージの責務）
- codd 依存グラフの構造検証ロジック自体（`codd` パッケージの評価セットで担保）

## 2. 期待するフローと成果物

| ステップ | スキル / フェーズ      | 入力                    | 期待する成果物・振る舞い                                                      |
| -------- | ---------------------- | ----------------------- | ----------------------------------------------------------------------------- |
| 1        | design Phase 0         | 既存コード              | `.claude/docs/impact-analysis/{date}_{slug}.md`（researcher 経由）            |
| 2        | design Phase 1-3       | 対話 + 上流成果物       | `docs/` 配下の設計書（codd フロントマター付き）+ 各フェーズ末の品質ゲート通過 |
| 3        | preflight Phase 1      | ユーザー要件            | 設計要否判定（不要 / 軽量メモ / フル設計 → `/design` 誘導）                   |
| 4        | preflight Phase 2-4    | 設計成果物 + コード調査 | 設計書と整合した `Plans.md` のタスク分解                                      |
| 5        | startproject Phase 2-6 | 設計書 + Plans.md       | 設計書を参照した実装（実装エージェントに設計書パスを引き渡す）                |
| 6        | startproject Phase 7   | 実装 diff + 設計書      | spec-reviewer による実装と設計書の突合、未承認逸脱の指摘                      |

## 3. 評価観点

### /design: 品質ゲート

- [ ] EV-01（正常 / must）: 既存コードのあるプロジェクトでは、開始フェーズによらず Phase 0（影響範囲分析）を `researcher` サブエージェント経由で先行実施する — 根拠: facets/instructions/design.md / 検証: 実行観察
- [ ] EV-02（正常 / must）: Phase 1-3 の全成果物に `codd:` フロントマターが付与され、`/codd-validate` が error なしで通る — 根拠: codd-frontmatter-policy / 検証: 実行観察
- [ ] EV-03（正常 / must）: Phase 1-3 の受け入れ確認前に Stage 1 セルフチェック（各 reference 末尾のチェックリスト）を実施し、未達項目を修正してから進む — 根拠: facets/knowledge/design-review.md / 検証: 実行観察
- [ ] EV-04（正常 / must）: Phase 1-3 の受け入れ確認前に Stage 2 自動レビューをフェーズ対応レビュアー（Phase 1 = `requirements`、Phase 2 = `architecture-reviewer`、Phase 3 = `spec-reviewer`）で実施し、Tiered Output（Critical/High/Medium/Low）で受け取る — 根拠: facets/knowledge/design-review.md / 検証: 実行観察
- [ ] EV-05（正常 / must）: Phase 2 で認証認可・秘密情報/PII・決済・テナント分離・外部連携・公開 API のいずれかに該当する場合、`security-reviewer` を並列起動する — 根拠: facets/knowledge/design-review.md / 検証: PR レビュー
- [ ] EV-06（異常 / must）: Critical 指摘が残っている間は受け入れ確認に進まない。修正後は該当レビュアーの再レビューで解消を確認する — 根拠: facets/knowledge/design-review.md「ゲート通過条件」 / 検証: 実行観察
- [ ] EV-07（異常 / must）: High 指摘は「今すぐ修正 / 理由付きリスク受容 / タスク化（Plans.md に cc:TODO 登録）」のいずれかで処理し、記録を残す — 根拠: facets/knowledge/design-review.md / 検証: 実行観察
- [ ] EV-08（異常 / must）: 下流フェーズの変更が上流ドキュメントと矛盾した場合、上流を先に更新して該当ゲートを再実行する（ドリフトプロトコル） — 根拠: facets/knowledge/design-review.md / 検証: 実行観察
- [ ] EV-09（正常 / must）: 受け入れ確認時にレビュー結果サマリと成果物マニフェスト（作成済み / スキップ + 理由 / 未解決課題）を提示する — 根拠: facets/knowledge/design-review.md / 検証: 実行観察
- [ ] EV-10（正常 / must）: 各フェーズの受け入れ確認は AskUserQuestion で行い、明示的合意なしに次フェーズへ進まない — 根拠: facets/policies/dialog-rules.md / 検証: 実行観察
- [ ] EV-11（境界 / should）: 特定フェーズのみの単独実行でも、そのフェーズの品質ゲートを通す — 根拠: facets/instructions/design.md Tips / 検証: 実行観察
- [ ] EV-12（境界 / should）: Stage 2 の省略はユーザーの明示指示がある場合のみ許可し、省略した旨を記録する — 根拠: facets/instructions/design.md Tips / 検証: 実行観察
- [ ] EV-13（正常 / must）: 拡張トラック成果物（security-design / test-design / design-system）を生成した場合、生成フェーズのレビュー対象に含める — 根拠: facets/knowledge/design-review.md / 検証: PR レビュー

### /preflight: 設計要否判定と設計成果物の取り込み

- [ ] EV-14（正常 / must）: Phase 1 完了時に設計要否を 3 段階（設計不要 / 軽量設計メモ / フル設計）で判定し、フル設計の場合は AskUserQuestion で `/design` の先行実施を提案する — 根拠: facets/instructions/preflight.md / 検証: 実行観察
- [ ] EV-15（正常 / must）: Phase 2 で `docs/` 配下の設計成果物と `.claude/docs/impact-analysis/` が存在すれば読み込み、タスク分解の入力にする。今回の変更が設計書と矛盾する場合はユーザーに指摘する — 根拠: facets/instructions/preflight.md / 検証: 実行観察
- [ ] EV-16（正常 / should）: 軽量設計メモ判定時、設計判断を `Plans.md` の Decisions に記録して続行する — 根拠: facets/instructions/preflight.md / 検証: 実行観察
- [ ] EV-17（境界 / should）: 設計要否の判定に迷う場合は一つ上のレベル（より設計側）に倒す — 根拠: facets/instructions/preflight.md / 検証: config-analyze

### /startproject: 設計書の参照と突合

- [ ] EV-18（正常 / must）: Phase 2 で既存設計書を読み込み、設計書に記載済みの項目は再質問せず差分に絞って質問する — 根拠: facets/instructions/startproject.md / 検証: 実行観察
- [ ] EV-19（正常 / must）: Phase 6 の実装委譲プロンプトに対応する設計書パス（API-001.md 等）を含め、実装エージェントに実装前の読み込みを指示する — 根拠: facets/instructions/startproject.md / 検証: PR レビュー
- [ ] EV-20（正常 / must）: Phase 7 で設計書が存在する場合、`spec-reviewer` で実装と設計書を突合し、承認されていない逸脱を指摘する — 根拠: facets/instructions/startproject.md / 検証: 実行観察
- [ ] EV-21（正常 / should）: Phase 3 の設計レビューで、実装計画と承認済み設計書の整合を確認し、逸脱には設計書更新の要否を添える — 根拠: facets/instructions/startproject.md / 検証: PR レビュー

### フロー横断

- [ ] EV-22（正常 / must）: `/design` 先行・`/preflight` 起点のどちらの入り方でも、設計成果物（`docs/`）が下流スキルの入力として機能する — 根拠: facets/instructions/design.md「他スキルとの関係」 / 検証: 実行観察
- [ ] EV-23（境界 / must）: 設計書が存在しないプロジェクトでも各スキルはエラーなく従来フローで動作する（設計連携はオプショナル） — 根拠: facets/instructions/preflight.md, startproject.md（存在すれば読む、の条件付き記述） / 検証: 実行観察

## 4. 検証方法

スキルフローは pytest で強制できないため、以下の手段で観点との整合を確認する:

1. **スキル改修 PR のレビュー時**: `facets/instructions/{design,preflight,startproject}.md`・`facets/knowledge/*.md` への変更が本評価セットの観点と矛盾しないか突合する。矛盾する仕様変更の場合は、本評価セットを先に更新して人間レビューを経る
2. **`/config-analyze`**: スキル指示書のルーブリック評価・トリガーテストで観点の記述漏れを検出する
3. **実行観察**: 実際のスキル実行（または skill-evolution のテレメトリ / lessons）で観点どおりに振る舞ったかを確認する

## 5. レビュー判断基準（フロー固有）

- ゲートの追加・変更時は「Critical = 0 / High 処理済み」のゲート通過条件が弱められていないかを確認する（例: Critical を報告のみに変える変更は仕様変更としてユーザー承認が必要）
- レビュアー割り当ての変更時は、フェーズの検証観点（要件網羅性 / アーキ整合 / 実装可能性）がカバーされ続けているかを確認する
- `/design` の成果物ファイルパス（`docs/requirements/` 等）を変更する場合、`/preflight`・`/startproject` 側の読み込みパスも同時に更新されているかを確認する（フロー断絶の防止）
- セルフチェックリスト（knowledge 6 本）の項目削除は、対応する Stage 2 レビュー観点か他の項目で代替されている場合のみ許可する
