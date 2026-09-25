# Development Workflow — 発注書と実行エンジンの使い分け

**開発は「対話で決める → 発注書に書く → 実行エンジンが作る → 出す」の順で進める（ADR-20260926-056）。**

## 前半（決める）

1. grill-me 等の対話で要件と計画を詰める。正式な設計書（codd 付き `docs/`）が要るときだけ `/design` を使う
2. `/order` が対話の結論を発注書に固めて 1 回提示し、承認後にエンジンの発注書として 1 か所に書く
   （Plans.md か GitHub Issue）。AC は聞き直さない

## 実行エンジン

| 実行エンジン             | 発注書                                      | 状態                    | 再開                                                                                                                                                                                                            | 向いている場面                                                                                                               |
| ------------------------ | ------------------------------------------- | ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `/goal`                  | Plans.md                                    | Plans.md の `cc:` と AC | SessionStart hook の注入                                                                                                                                                                                        | セッション内で完結し AC が機械検証できる                                                                                     |
| Codex 直接（`/handoff`） | Plans.md から生成した引き継ぎファイル       | Plans.md                | `codex -C <project> --model <codex.model> --sandbox <codex.sandbox.implementation> "$(cat <引き継ぎファイルの絶対パス>)"`（新規セッションにプロンプトとして渡す。model / sandbox は `cli-tools.yaml` の実効値） | Codex に実装を任せる、レート制限時の退避                                                                                     |
| `/loop-issue`            | Issue 本文                                  | loop_step の state.json | `--attach` / `--resume`                                                                                                                                                                                         | 無人で Maker / Checker を反復させる                                                                                          |
| TAKT                     | Issue 本文（`takt add '#N'`）またはタスク文 | `.takt/tasks/<id>/`     | `takt list`（retry / requeue）                                                                                                                                                                                  | 別クローンで並列に回す、実装プロバイダを Codex にする。PR は TAKT が作る（`takt add` の `Auto-create PR?` で Yes。既定 Yes） |
| `/issue-fix`（補助）     | Issue 本文                                  | 会話と git のみ         | なし                                                                                                                                                                                                            | Issue 起点でセッション内に小さく直す                                                                                         |

### 判断軸

| 観点                  | Plans.md 系（/goal・Codex 直接）   | Issue 系（/loop-issue・TAKT）                 |
| --------------------- | ---------------------------------- | --------------------------------------------- |
| AC が機械検証できるか | verify が中心なら /goal            | verify が中心で無人にしたいなら /loop-issue   |
| 無人で回すか          | セッション内で見守る               | 無人（loop-issue）/ 別クローンで並列（TAKT）  |
| 実装プロバイダ        | Claude（/goal）/ Codex（/handoff） | loop-harness の設定 / TAKT の設定（Codex 等） |
| 規模                  | 小〜中                             | 中〜大、または複数を並列                      |

Issue 起点でセッション内に小さく直すだけなら `/issue-fix`（Plans.md は作らない）。

## 発注書と状態の分離

- 発注書は 1 エンジン 1 か所。Plans.md と Issue の両方を正本にしない
- 状態はエンジンが持つ（表の「状態」列）。Plans.md の Project を Issue に出したら、その Project は
  `/order --from-plans` で Plans.archive.md に「#N へ引き渡し」として移す
- Open Questions が残る発注書は自律エンジン（/loop-issue・TAKT）へ出さない。`/order` が Issue 化の前に解消を求める

## 出す

- PR をまだ作っていないエンジン（/goal・Codex 直接・/issue-fix）だけ `/pr-create` → `/review-respond`
- `/loop-issue` は PR 作成と外部レビュー対応を内包する。TAKT は `takt add '#N'` の `Auto-create PR?` で
  Yes を選び PR 作成を TAKT に任せる。これらの後に共通手順を重ねない
- `/goal` のループ完了後は、ユーザーが `/release-readiness` で AC 全チェック・テスト・レビューを確認する
