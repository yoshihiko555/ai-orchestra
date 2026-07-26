# review 評価セット（スキルフロー）

**対象スキル群**: `review`（正本: `facets/instructions/review.md`, `facets/output-contracts/tiered-review.md`）
**単位**: スキルフロー（`/review` は Phase 0-7（Phase 3 と Phase 4 の間に指摘検証の Phase 3.5 を挟む） — コンテキスト収集 → スマート選定 → 並列レビュー → 指摘検証 → 集約 → Pass/Fail 判定 → Auto-Fix → 再レビュー — の多段フローであり、複数エージェントが成果物（diff・指摘・修正）を介して連続する。スキル名は単一だが「フロー単位で作る」ポリシーに沿うフローとして扱う）
**作成日**: 2026-07-23
**最終レビュー日**: 2026-07-26（Phase 3.5 指摘検証・`finding-verifier` エージェント追加を反映）
**情報源**: `facets/instructions/review.md` / `facets/output-contracts/tiered-review.md` / `packages/agent-routing/agents/adversarial-reviewer.md` / `packages/agent-routing/agents/finding-verifier.md` / `facets/instructions/skill-review-policy.md` / `cli-tools.yaml` の `review` セクション

> **パッケージ評価セットとの違い**: スキルは Markdown 指示書であり pytest で強制できない。
> この評価セットは「振る舞い仕様書」として機能し、テストコードとの突合（`evaluation-set-policy`
> ルールの MUST 手順）の対象外。検証手段は下記「検証方法」に従う。

## 1. フロー責務定義

`/review` フローは、作業ツリーの diff に対して適切なレビュアー群をスマート選定し、事前収集した共有コンテキストのもとで並列レビューを実行し、Tiered Output（Critical/High/Medium/Low）に集約したうえで、Critical 指摘を自動修正して再レビューする品質ゲートループを提供する。レビュアー選定はベースライン 2 名（`code-reviewer` + `adversarial-reviewer`）+ 専門枠最大 2 名の最大 4 名構成を基本とし、コストと網羅性のバランスを保証する。

### Non-Goals

- テストコードの作成（`/tdd` の責務）
- スキル内レビューフェーズ（issue-fix 等）の選定ロジック（`skill-review-policy` が規定。本フローとは別スキーム）
- 指摘検証（Phase 3.5・`finding-verifier`）の `skill-review-policy`（スキル内レビュー）への展開（v1 は `/review` 限定。導入判断は別 Issue）
- マージ可否の最終判断（`/release-readiness` の責務）

## 2. 期待するフローと成果物

| ステップ | スキル / フェーズ | 入力 | 期待する成果物・振る舞い |
| -------- | ----------------- | ---- | ------------------------ |
| 1 | review Phase 0 | git diff | diff_stat / diff_full / file_contexts を 1 回だけ収集（500 行超は変更ハンク + 前後 30 行） |
| 2 | review Phase 1 | 変更ファイル・diff | ベースライン 2 名 + パス/コンテンツシグナルによる専門枠最大 2 名の選定結果 |
| 3 | review Phase 2 | diff サイズ・リスクシグナル | モデル選択（≤100 行かつ override なしで sonnet 明示指定） |
| 4 | review Phase 3 | 選定レビュアー + 事前収集コンテキスト | 並列 Task 起動（コンテキスト注入済みプロンプト） |
| 5 | review Phase 3.5 | 各レビュアーの Critical/High 指摘 | `finding-verifier` による反証検証（confirmed / refuted / uncertain）。`review.verify_findings: false` ならスキップ |
| 6 | review Phase 4 | 各レビュアーの Tiered 報告 + 検証結果 | 重複統合済みの Review Summary（refuted は除外し「Refuted Findings」に理由付き表示、severity 過大は格下げ） |
| 7 | review Phase 5 | 集約結果 + `review.*` config | Pass/Fail 判定（`critical_zero`。uncertain Critical も Fail 扱い） |
| 8 | review Phase 6 | Critical 指摘（confirmed のみ） | 拡張子マッピングに基づく修正エージェントによる自動修正 |
| 9 | review Phase 7 | 修正後の新 diff | 再レビューループ（`max_loops` 上限）。新規/変更 Critical/High は再検証し、flip-flop は NEEDS_REVIEW で停止して Final Report |

## 3. 評価観点

<!-- ID はファイル内一意の連番（欠番は再利用しない）。1 観点 = 1 振る舞い。 -->

### 選定（Phase 0-1）

- [ ] EV-01（正常 / must）: コンテキスト収集は Phase 0 の 1 回のみで、レビュアーサブエージェント内で git diff / Read の再実行を要求しない — 根拠: `facets/instructions/review.md` Phase 0/3 / 検証: PR レビュー
- [ ] EV-02（正常 / must）: ソースコード変更がある限り、ベースライン 2 名（`code-reviewer` と `adversarial-reviewer`）が必ず選定される — 根拠: `facets/instructions/review.md` Phase 1 Step 1 / 検証: 実行観察
- [ ] EV-03（正常 / must）: 選定結果は最大 4 名（ベースライン 2 + 専門枠最大 2）に収まり、専門枠は `security > spec > performance > architecture > ux` の優先順位で割り当てられる — 根拠: 同 Step 4 / 検証: 実行観察
- [ ] EV-04（異常 / must）: `.md` のみの変更は原則レビュースキップし、仕様書・API ドキュメントの変更のみ `spec-reviewer` にフォールバックする — 根拠: 同 Phase 0 / 検証: 実行観察
- [ ] EV-05（境界 / should）: パス/コンテンツシグナルに一切マッチしない変更でも、ベースライン 2 名がレビューを実施する — 根拠: 同 Step 4 注記 / 検証: 実行観察

### 敵対的検証（adversarial-reviewer）

- [ ] EV-06（正常 / must）: `adversarial-reviewer` の守備範囲は堅牢性（境界値・異常入力・エラー経路・競合/並行・リソース枯渇・前提条件の破れ・API 誤用）に限定され、セキュリティ意図の攻撃観点は `security-reviewer` に委ねる — 根拠: `packages/agent-routing/agents/adversarial-reviewer.md` / 検証: PR レビュー
- [ ] EV-07（正常 / must）: `adversarial-reviewer` の Critical/High 指摘には具体的失敗シナリオ（入力/状態 → 誤動作の具体経路）が明記される — 根拠: 同エージェント定義の規律 / 検証: 実行観察
- [ ] EV-08（異常 / must）: 具体的失敗シナリオを示せない敵対的指摘は Medium 以下に格下げされ、auto-fix（Critical 駆動）を誘発しない — 根拠: 同エージェント定義の規律 / 検証: 実行観察

### 指摘検証（Phase 3.5・finding-verifier）

- [ ] EV-18（境界 / must）: `review.verify_findings: false` のとき Phase 3.5 はスキップされ、Phase 3 の指摘がそのまま Phase 4 集約に渡る（従来動作） — 根拠: `facets/instructions/review.md` Phase 3.5 / `cli-tools.yaml` の `review` セクション / 検証: 実行観察
- [ ] EV-19（正常 / must）: `finding-verifier` は Critical/High 指摘のみを検証対象とし、各指摘に confirmed / refuted / uncertain の3値 verdict を付与する — 根拠: 同 Phase 3.5 / `packages/agent-routing/agents/finding-verifier.md` / 検証: 実行観察
- [ ] EV-20（正常 / must）: refuted 判定の指摘は Tiered Output の集約（Critical/High/Medium/Low）から除外され、理由付きで「Refuted Findings」に表示される — 根拠: 同 Phase 3.5-4 / 検証: 実行観察
- [ ] EV-21（正常 / should）: severity が過大と判定された confirmed 指摘は、除外せず severity を格下げしたうえで集約する — 根拠: 同 Phase 3.5 / 検証: 実行観察
- [ ] EV-22（異常 / must）: auto-fix（Phase 6）は confirmed Critical のみを対象とし、uncertain Critical は Fail 扱いのまま自動修正せず人手確認に回す — 根拠: 同 Phase 5-6 / 検証: 実行観察
- [ ] EV-23（異常 / must）: Phase 7 再レビューは新規/変更の Critical/High 指摘を再検証し、直前ループと逆の verdict（flip-flop）が生じた場合は自動修正を継続せず NEEDS_REVIEW で停止する — 根拠: 同 Phase 7 / 検証: 実行観察

### 集約・品質ゲート（Phase 4-7）

- [ ] EV-09（正常 / must）: 全レビュアーの結果は Tiered Output に集約され、同一ファイル・同一箇所の重複指摘は最高 severity に統合しレビュアー名を併記する（`review.verify_findings: true` の場合は Phase 3.5 で refuted と判定された指摘を除く） — 根拠: `facets/instructions/review.md` Phase 4 / 検証: 実行観察
- [ ] EV-10（正常 / must）: `review.auto_fix: true` かつ Critical > 0 のとき、拡張子マッピングに従う修正エージェントで自動修正し、Phase 0 から再レビューする（`review.verify_findings: true` の場合は confirmed Critical のみが自動修正対象、EV-22 参照） — 根拠: 同 Phase 5-7 / 検証: 実行観察
- [ ] EV-11（正常 / must）: `pass_threshold: critical_zero` のもと Critical 0 件で PASSED の Final Report を出力して終了する（`review.verify_findings: true` の場合 uncertain Critical は 0 件に含めず Fail 扱い、EV-22 参照） — 根拠: 同 Phase 5 / 検証: 実行観察
- [ ] EV-12（異常 / must）: `review.auto_fix: false` のときは Review Summary の報告のみで終了する（従来動作） — 根拠: 同 Phase 5 / 検証: 実行観察
- [ ] EV-13（異常 / must）: `max_loops` 到達時は残存 Critical/High を含む Final Report（FAILED）を出力して終了する — 根拠: 同 Phase 7 / 検証: 実行観察
- [ ] EV-14（境界 / should）: `review.*`（auto_fix / pass_threshold / max_loops / verify_findings）は `.local.yaml` 上書きを含む config-loading 手順で解決される — 根拠: `config-loading` ルール / 検証: PR レビュー

### モード編成

- [ ] EV-15（正常 / must）: `/review all` は全 7 レビュアー、`/review impl` は code + security + performance + adversarial の 4 名、`/review design` は spec + architecture の 2 名を起動する — 根拠: `facets/instructions/review.md` Execution 節 / 検証: PR レビュー
- [ ] EV-16（正常 / must）: `/review adversarial` を含む Individual Review は指定レビュアーのみを起動し、Phase 5-7 のループを適用する — 根拠: 同 Individual Review 節 / 検証: 実行観察
- [ ] EV-17（境界 / should）: diff サイズ ≤100 行かつリスク override（security / spec 選定）なしの場合のみ sonnet ダウングレードを適用する — 根拠: 同 Phase 2 / 検証: 実行観察

## 4. 検証方法

スキルフローは pytest で強制できないため、以下の手段で観点との整合を確認する:

1. **スキル改修 PR のレビュー時**: 変更が本評価セットの観点と矛盾しないか突合する。矛盾する仕様変更の場合は、本評価セットを先に更新して人間レビューを経る
2. **`/config-analyze`**: スキル指示書のルーブリック評価・トリガーテストで観点の記述漏れを検出する
3. **実行観察**: 実際のスキル実行（または skill-evolution のテレメトリ / lessons）で観点どおりに振る舞ったかを確認する

## 5. レビュー判断基準（フロー固有）

- レビュアー構成（ベースライン・専門枠上限・グループ編成）を変更する PR は、`facets/instructions/orchestra-usage.md`・`facets/instructions/skill-review-policy.md` の記述と同期していること
- ベースライン枠の増減はデフォルト `/review` の恒常コストに直結するため、ユーザー合意（Issue / PR 上の明示的な承認）を伴うこと
- `adversarial-reviewer` の失敗シナリオ規律（EV-07/EV-08）を弱める変更は、auto-fix 誤発動リスクの増大として扱い、代替ガードレールの提示を必須とする
- `finding-verifier` の verdict 定義（confirmed/refuted/uncertain）・refuted 除外規律・confirmed Critical 限定の auto-fix 規律（EV-18〜EV-23）を弱める変更は、偽陽性フィルタの実効性低下として扱い、代替ガードレールの提示を必須とする
- `review.verify_findings` の既定値変更は、デフォルト `/review` の恒常コスト・所要時間に直結するため、ユーザー合意（Issue / PR 上の明示的な承認）を伴うこと
- エージェント追加・削除時は `tests/unit/test_agent_routing_consistency.py` が強制する 5 点セット（agents/*.md・cli-tools.yaml・route_config.py・manifest.json・docs/reference/packages.md）の同時更新を確認する
