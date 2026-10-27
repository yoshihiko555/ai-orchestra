# order 評価セット（スキルフロー）

**対象スキル群**: `order`（正本: `facets/instructions/order.md`）と、その出力先の `issue-create`（AC 確定済み経路）・`task-memory-usage` ルール（plan ファイル `.claude/plans/<slug>.md`）・配布ルール `development-workflow`
**単位**: スキルフロー（対話の結論 → 発注書 → 実行エンジンへの引き渡し）
**作成日**: 2026-09-26
**最終レビュー日**: 2026-10-27（ADR-20261027-059 で Plans.md を Project ごとの plan ファイル `.claude/plans/<slug>.md` に分割したのに合わせ、表の行 3・4 と EV-07〜10 / 12 を plan ファイル前提に改め、slug の決め方（EV-14）を新設。2026-10-27 人間レビュー済み。同日、/review の指摘で EV-10 に削除前の slug・ファイル種別の確認を追記（2026-10-27 人間レビュー済み）。前回 2026-09-28: Step 4 の報告に `/grasp-view` → `/grasp-check` の案内（EV-13）を追加。2026-09-28 人間レビュー済み。前回 2026-09-28: ADR-20260928-058 で引き渡し時のアーカイブを廃止し、EV-10 と引き渡し表の行 4 を「Plans.md から取り除く」に改めた。2026-09-28 人間レビュー済み。前回 2026-09-26: 初版。ADR-20260926-056 §決定 1・3 に基づく先行作成。design-flow の旧 EV-14 / 15 / 17 / 18 / 19 / 21 の移管先）
**情報源**: docs/adr/ADR-20260926-056.md, docs/adr/ADR-20260928-058.md, docs/adr/ADR-20261027-059.md, facets/instructions/order.md, facets/instructions/development-workflow.md, facets/instructions/issue-create.md, .claude/rules/task-memory-usage.md, docs/evaluation/skills/design-flow.md

> **パッケージ評価セットとの違い**: スキルは Markdown 指示書であり pytest で強制できない。
> この評価セットは「振る舞い仕様書」として機能し、テストコードとの突合（`evaluation-set-policy`
> ルールの MUST 手順）の対象外。検証手段は下記「検証方法」に従う。

## 1. フロー責務定義

`/order` は、対話（grill-me 等）で確定した計画を発注書の内容モデル（Goal / Context / Out of Scope /
Constraints / Open Questions + Phase ごとの Acceptance Criteria と Tasks）に固めて 1 回だけ提示し、承認後に
実行エンジンの発注書として **1 か所** に書く。出力先は plan ファイル（`.claude/plans/<slug>.md`。`/goal` / Codex 直接）か GitHub Issue
（`/loop-issue` / TAKT）で、状態管理には関与しない（ADR-20260926-056 §決定 1・3）。

### Non-Goals

- 実行エンジンの起動や進捗管理（`/goal` / `/loop-issue` / TAKT / `/handoff` の責務）
- 設計書の作成（`/design` の責務。`/order` は参照するだけ）
- Issue 本文の書式やラベル付与の細部（`issue-create` の責務。`/order` は AC 確定済み経路で呼ぶだけ）

## 2. 期待するフローと成果物

| ステップ | スキル / フェーズ | 入力                                 | 期待する成果物・振る舞い                                                                                                           |
| -------- | ----------------- | ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| 1        | order Step 0      | 会話の結論、`docs/` の設計書         | 内容モデル 7 節が埋まった発注書案。設計書があれば Context に参照、矛盾があれば指摘                                                 |
| 2        | order Step 1      | 発注書案                             | AskUserQuestion で 1 回提示（承認 / 修正 / 中止）。修正は発注書への差分編集                                                        |
| 3        | order Step 2      | 承認済み発注書、出力先               | 新しい plan ファイル（Project + Phase + AC + Tasks）か Issue（AC 確定済み経路）に 1 回で書く                                       |
| 4        | order Step 3      | 既存の plan ファイル / 既存 Issue    | `--from-plans`: Issue 化し、その plan ファイルを削除する。`--from-issue`: 新しい plan ファイルにし、Issue に引き渡しコメントを付けて close |
| 5        | order Step 4      | 書き先                               | 次に使うエンジンのコマンドを 1 行で案内。条件に当てはまる発注書では `/grasp-view` → `/grasp-check` も 1 行で案内                   |

## 3. 評価観点

### 発注書の内容

- [ ] EV-01（正常 / must）: 発注書は Goal / Context / Out of Scope / Constraints / Open Questions と、Phase ごとの Acceptance Criteria（`verify` / `judge`）・Tasks（対象 / 確認）の 7 節をそろえる。未確定の項目は空にせず Open Questions に残す — 根拠: ADR-056 §決定 2・3, task-memory-usage.md「発注書の節」 / 検証: 実行観察
- [ ] EV-02（正常 / must）: `docs/` 配下に設計書（requirements / architecture / screens / api / database）があれば、該当する節のパスを Context に載せ、発注内容が設計書と矛盾する場合はユーザーに指摘する（旧 design-flow EV-15 / EV-18 / EV-19 / EV-21 の移管） — 根拠: ADR-056 §決定 3 / 検証: 実行観察
- [ ] EV-03（境界 / must）: 設計書が存在しないプロジェクトでもエラーにせず、Context の設計書参照を省いて続行する — 根拠: ADR-056 §決定 3 / 検証: 実行観察
- [ ] EV-04（境界 / should）: 設計書が無く、アーキテクチャ・API・データモデルに触る発注では `/design` の先行を提案する。判定に迷えば設計側に倒す（旧 EV-14 / EV-17 の移管） — 根拠: ADR-056 §決定 1「正式な設計書が要るときだけ `/design`」 / 検証: 実行観察
- [ ] EV-05（境界 / must）: 5 節には `cc:` マーカーを書かない。タスクは Phase 配下の Tasks にだけ書く — 根拠: task-memory-usage.md「発注書の節」 / 検証: config-analyze

### 提示と書き出し

- [ ] EV-06（正常 / must）: 発注書案は AskUserQuestion で 1 回提示し、承認 / 修正 / 中止を選ばせる。修正は発注書案への差分編集で行い、対話に差し戻さない — 根拠: ADR-056 §決定 1 の 2, dialog-rules / 検証: 実行観察
- [ ] EV-07（正常 / must）: AC は対話で確定した内容を転記し、plan ファイル・Issue のどちらに書く場合も聞き直さない（`issue-create` は AC 確定済み経路で呼ぶ） — 根拠: ADR-056 §決定 3 / 検証: 実行観察
- [ ] EV-08（正常 / must）: 出力先は plan ファイルか Issue のどちらか 1 か所。plan ファイルは 1 つの発注書につき新しいファイル `.claude/plans/<slug>.md` を 1 つ作り、`task-memory-usage` の書式で Project + Phase（と、そのファイル自身の Decisions / Notes）を 1 回で書く。既存の plan ファイルに Project を追記しない。`.claude/plans/` が無ければ作る。旧 `.claude/Plans.md` には書かない — 根拠: ADR-056 §決定 1「発注書は 1 エンジン 1 か所」、ADR-20261027-059 / 検証: 実行観察
- [ ] EV-09（異常 / must）: Issue 化（`--to issue` / `--from-plans`）の前に Open Questions が残っていれば解消をユーザーに求め、解消できない項目が残る場合は Issue 化せず plan ファイルに留める — 根拠: ADR-056 §決定 3 / 検証: 実行観察
- [ ] EV-10（正常 / must）: `--from-plans <slug>` は対象の plan ファイルを Issue 本文に変換し、Issue の作成に成功したことを確かめてから、その plan ファイルを削除する。Issue の作成に失敗した場合はファイルを残す。削除の前に、slug が `[a-z0-9][a-z0-9-]*` に完全一致し、`.claude/plans/<slug>.md` が symlink でない通常ファイルであることを確かめ、満たさなければ削除しない。対象のファイルは slug で指定し、省略時は未完了のファイルが 1 つならそれ、複数なら AskUserQuestion で選ばせ、0 なら案内して終える（`task-memory-usage` の対象ファイルの決め方） — 根拠: ADR-056 §決定 1「エンジンをまたぐときは片方だけ残す」、ADR-20260928-058、ADR-20261027-059 / 検証: 実行観察
- [ ] EV-12（正常 / must）: `--from-issue N --to plans` は Issue 本文を新しい plan ファイルの Project に変換し、ファイルの書き込みに成功してから、Issue に引き渡しコメント（plan ファイルのパスを含む）を付けて close する（Issue と plan ファイルの両方を正本にしない）。書き込みやコメント・close の途中で失敗した場合は、どこまで済んだかを報告し、Issue を close しないまま残す。slug は EV-14 と同じ規則で提案し、本文は聞き直さず slug だけ承認を得る。`--from-plans` / `--from-issue` は Step 0〜2 を行わず引き渡しだけを実行する — 根拠: ADR-056 §決定 1「逆方向も同様に変換して片方だけ残す」 / 検証: 実行観察
- [ ] EV-11（正常 / should）: 完了報告で、書き先と次に使うエンジンのコマンド（`/goal` はそのまま、`/loop-issue N`、`takt add '#N'`、`/handoff`）をエンジンごとに 1 行ずつ案内する — 根拠: development-workflow.md / 検証: 実行観察
- [ ] EV-13（正常 / must）: 完了報告で、発注書が Issue に出したもの、またはアーキテクチャ・API・データモデル・認証・設定キーに触るものであれば、実装前に `/grasp-view` → `/grasp-check` を実行できることを 1 行で案内する。案内は警告だけで、Step 1 の提示や書き出しを変えない（詳細は `docs/evaluation/skills/grasp-check.md` EV-25 / EV-26） — 根拠: 2026-09-28 の設計相談での合意事項 / 検証: 実行観察
- [ ] EV-14（境界 / must）: 新しい plan ファイルの slug は Project 名から `[a-z0-9][a-z0-9-]*`（ASCII 小文字・数字・ハイフン）で提案し、Step 1 の提示に含めて承認を得る。同じ slug のファイルが既にあれば、そのまま上書きせず、使われていない slug を提案したうえで「別の slug にする / 上書きする」を AskUserQuestion で確認する（上書きは利用者が選んだときだけ） — 根拠: ADR-20261027-059 / 検証: 実行観察

## 4. 検証方法

1. **スキル改修 PR のレビュー時**: `facets/instructions/order.md`・`development-workflow.md`・`issue-create.md` の変更が本評価セットと矛盾しないか突合する。仕様変更は本評価セットを先に更新して人間レビューを経る
2. **`/config-analyze`**: 指示書のルーブリック評価・トリガーテストで観点の記述漏れを検出する
3. **実行観察**: 実際の `/order` 実行（または skill-evolution のテレメトリ）で観点どおりに振る舞ったかを確認する

## 5. レビュー判断基準（フロー固有）

- 出力先を増やす変更（例: TAKT 専用ファイル）は「発注書は 1 エンジン 1 か所」を崩さないか確認する
- 1 つの plan ファイルに複数の発注書を追記する変更は、Project ごとにファイルを分けた理由（肥大化の防止・完了判定と片付けの単位）を崩さないか確認する
- AC の聞き直しを復活させる変更は EV-07 に反する。`issue-create` 側の Step 3 省略経路を壊していないか確認する
- Open Questions のゲート（EV-09）を弱める変更はユーザー承認を要する仕様変更として扱う
