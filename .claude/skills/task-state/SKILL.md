---
name: task-state
description:
  "plan ファイル（.claude/plans/<slug>.md、Project ごとの SSOT）のタスク状態を管理する。

  plan ファイルの一覧・新規作成・タスクの状態更新・フェーズ追加・設計判断の記録・

  完了したファイルの片付け（close）が可能。

  "
argument-hint: "[new|update|add-phase|decision|close] [args...] [--plan <slug>]"
metadata:
  short-description: SSOT タスク管理（plan ファイルの作成・更新・片付け）
---

# Task State — plan ファイルによるタスク管理

**Project ごとの plan ファイル `.claude/plans/<slug>.md` を SSOT として、タスクの進捗・状態をファイルベースで管理します。**

## 使い方

```bash
# plan ファイルの一覧（未完了・完了）
/task-state

# 新しい plan ファイルを作る（通常は /order --to plans が作る）
/task-state new my-feature

# タスクの状態更新（--plan を省略したときの対象は下の「対象ファイルの決め方」）
/task-state update "タスク名" --status wip
/task-state update "タスク名" --status done --plan my-feature
/task-state update "タスク名" --status blocked --reason "API仕様が未確定"

# フェーズの追加（合意済みの AC があれば --ac で渡す。聞き直さない）
/task-state add-phase "Phase 2: 実装" --tasks "API実装" "テスト作成" "ドキュメント"
/task-state add-phase "Phase 2: 実装" --tasks "API実装" --ac "API が仕様通り — verify: \`pytest tests/test_api.py\`"

# 設計判断の記録
/task-state decision "REST API ではなく GraphQL を採用（理由: フロントエンドの柔軟性）"

# 完了した plan ファイルの片付け
/task-state close my-feature
```

## 対象ファイルの決め方

`update` / `add-phase` / `decision` は `--plan <slug>` で対象を指定する。省略したときは未完了の plan ファイルで決める（完了の定義は下の「完了判定」）。

| 未完了の plan ファイル | 対象                                                                |
| ---------------------- | ------------------------------------------------------------------- |
| 1 つ                   | そのファイル                                                        |
| 2 つ以上               | AskUserQuestion で選ばせる（選択肢は slug と Goal の先頭 1 行）     |
| 0                      | 対象が無いことを伝え、`/order --to plans` か `new` を案内して終える |

`close` の対象は、slug を省略したときは完了したファイルから同じ規則で決める。

slug は `[a-z0-9][a-z0-9-]*`（ASCII 小文字・数字・ハイフン）に完全一致する値だけを受け付ける。規則に合わない値やパス区切りを含む値は使わず、利用者に確認する。

## 処理内容

### 一覧モード（引数なし）

1. `.claude/plans/` 直下の `<slug>.md` を列挙する（symlink と命名規則に合わないファイルは読まない）
2. ファイルごとに状態マーカー別にタスクを集計し、完了かどうかを判定する
3. 未完了のファイルは WIP → TODO → blocked の優先度でサマリーを表示し、完了したファイルは slug の一覧と `close` の案内を表示する
4. 旧形式の `.claude/Plans.md` があれば、読み込まれないことと `.claude/plans/<slug>.md` への移し方を伝える

### new モード

1. slug を確かめる（規則に合わない場合は Project 名から提案し直して承認を得る）
2. `.claude/plans/<slug>.md` が既にあれば上書きせず、使われていない slug を提案して確認する
3. `.claude/plans/` が無ければ作り、`task-memory-usage` ルールの「plan ファイルのフォーマット」（発注書の 5 節 Goal / Context / Out of Scope / Constraints / Open Questions 付き。下の例も参照）に従って作る
4. 5 節は対話で確定した内容で埋める。未確定の点は Open Questions に残す（節ごと空にしない）

対話で確定した計画を書き出すときは `/order --to plans` を使う（AC と Tasks までそろえて 1 回で書く）。

### update モード

1. 対象の plan ファイル内で指定タスクを検索する（見つからない、または複数に一致する場合は候補を示して確認する）
2. 状態マーカーを更新する
3. blocked の場合は理由を追記する

### add-phase モード

1. 対象の plan ファイルに新しいフェーズセクションを追加
2. タスクは `cc:TODO` で初期化
3. Acceptance Criteria が決まっている場合は、タスクグループより前に `#### Acceptance Criteria` セクションを追記する
   - 機械検証可能な条件は `— verify: \`コマンド\``、主観的条件は `— judge: 判定基準`を併記する（記法は`task-memory-usage.md` 参照）
   - 登録時は全条件を未チェック `- [ ]` で記載する
   - `--ac` で渡された AC、または呼び出し元のコンテキストで既に合意済みの AC（`/order` 等）はそのまま反映し、聞き直さない。どちらも無い場合のみ AC の有無をユーザーに確認する
4. タスク行は `{何をするか} — 対象: \`{ファイル}\` / 確認: {方法}` の形にする（確認は省略可）

### decision モード

1. 対象の plan ファイルの `## Decisions` セクションに日付付きで設計判断を追記する

### close モード

worktree で作業している場合は worktree ごと消えるので不要。worktree を切らずに作業しているときに使う。

1. 対象を決める（slug を省略したときは完了したファイルから「対象ファイルの決め方」の規則で決める）
2. 完了しているかを確かめる。未完了なら、残っているタスクと未チェックの AC を示し、AskUserQuestion で「やめる / 未完了のまま片付ける」を選ばせる。「やめる」なら何もせず終える
3. `## Decisions` に記録があれば一覧で示し、後から参照するものを残す先を AskUserQuestion で選ばせる（ADR に残す / PR 本文に残す / 残さない）
   - ADR に残す: `decision-log` スキル（または `docs/adr/` の既存形式）で書く
   - PR 本文に残す: 対象の PR を確かめ、追記する文面を見せて承認を得てから `gh pr edit` で書く
   - 書き込みに失敗した場合は、ファイルを削除せずに止めて報告する
4. 利用者が途中で取り消した場合は削除しない
5. 削除の前に、slug が `[a-z0-9][a-z0-9-]*` に完全一致し、`.claude/plans/<slug>.md` が symlink でない通常ファイルであることを確かめる（例: `test -f` かつ `! test -L`）。満たさなければ削除せずに止めて報告する
6. ファイルを削除する（archive には移さない）。削除したパスを報告する

## plan ファイルのフォーマット

> フォーマットの正本（SSOT）は `task-memory-usage.md` ルール（配布先 `.claude/rules/task-memory-usage.md`）。
> 置き場所・命名規則・状態マーカー・Acceptance Criteria（AC）の記法（verify/judge）・完了判定の詳細は同ルールを参照すること。
> 以下は `.claude/plans/my-app.md` の具体例（AC を含む）。

```markdown
## Project: my-app

#### Goal

- 商品を注文できる MVP を出す（Phase 2 まで）

#### Context

- 設計書: docs/architecture/system-architecture.md、docs/api/API-001.md
- 確定事実: 認証 API は実装済み

#### Out of Scope

- 決済の本番接続（契約待ち、ユーザー確定）

#### Constraints

- 既存の `users` テーブルは変更しない

#### Open Questions

- 注文キャンセルの期限を設けるか

### Phase 1: 設計 `cc:done`

#### アーキテクチャ

- `cc:done` 技術スタック選定
- `cc:done` DB スキーマ設計

### Phase 2: 実装 `cc:WIP`

#### Acceptance Criteria

- [ ] API が仕様通りレスポンスを返す — verify: `pytest tests/test_api.py`
- [ ] エラーハンドリングが妥当 — judge: 想定される異常系が適切にハンドリングされている

#### API

- `cc:done` ユーザー認証 API
- `cc:WIP` 商品一覧 API
- `cc:TODO` 注文 API
- `cc:blocked` 決済 API — 理由: 外部決済サービスの契約待ち

---

## Decisions

- 2026-02-21: PostgreSQL を選定（理由: JSON サポートと拡張性）

## Notes

- MVP スコープは Phase 2 まで
```

## 状態マーカー

| マーカー     | 意味       |
| ------------ | ---------- |
| `cc:TODO`    | 未着手     |
| `cc:WIP`     | 作業中     |
| `cc:done`    | 完了       |
| `cc:blocked` | ブロック中 |

## Acceptance Criteria（受け入れ条件）と完了判定

- フェーズ単位の完了条件（任意）。詳細は `task-memory-usage.md` ルールを参照
- フェーズ完了 = タスク全て `cc:done` **かつ** AC 全て `[x]`
- plan ファイルの完了 = タスクが 1 件以上あり、すべて `cc:done` で、ファイル内の AC がすべて `[x]`（タスク 0 件のファイルは未完了）
- `verify` 条件はコマンド実行で pass を確認した実行者のみがチェックできる。検証なしでのチェックは禁止

## 注意事項

- plan ファイルは `.claude/plans/<slug>.md` に置く。1 ファイルに `## Project:` は 1 つ
- 手動編集も可能（フォーマットに従うこと）
- plan ファイルはローカル管理（`.gitignore`）。共有が必要な作業は Issue を発注書にする（`task-memory-usage.md` ルール参照）
- 旧形式の `.claude/Plans.md` には書かない（読み込まれない）
- 発注書の 5 節（Goal / Context / Out of Scope / Constraints / Open Questions）には `cc:` マーカーを書かない。タスクは Phase 配下の `#### Tasks` にだけ書く
- 状態マーカーはバッククォートで囲む: `` `cc:WIP` ``
