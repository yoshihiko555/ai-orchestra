# Order — 対話の結論を発注書にする

**対話（grill-me 等）で確定した計画を、実行エンジンの発注書として 1 か所に書き出す。**

対象と流れの正本は ADR-20260926-056（§決定 1・3）と `development-workflow` ルール。発注書の書式は
`task-memory-usage` ルール（plan ファイル `.claude/plans/<slug>.md`。ADR-20261027-059）。

## Usage

```
/order                          # 出力先を AskUserQuestion で選ぶ
/order --to plans               # 新しい plan ファイル .claude/plans/<slug>.md（/goal・Codex 直接 向け）
/order --to issue               # GitHub Issue（/loop-issue・TAKT 向け）
/order --from-plans {slug} --to issue   # plan ファイルを Issue に引き渡す
/order --from-issue {N} --to plans      # Issue を新しい plan ファイルに引き渡す
```

`--from-plans` / `--from-issue` は引き渡しモードで、Step 0〜2 を行わず Step 3 だけを実行する（Issue や plan ファイルを
二重に作らない）。それ以外は通常モード（Step 0〜2）。

## Workflow

### Step 0: 発注書案を固める

会話の結論を次の内容モデルに落とす。未確定の項目は空にせず Open Questions に残す。

| 節             | 内容                                                                                     |
| -------------- | ---------------------------------------------------------------------------------------- |
| Goal           | 目的と背景（1〜3 行）                                                                    |
| Context        | 参照する設計書（`docs/` の該当節のパス）・Issue・ADR、確定している事実                   |
| Out of Scope   | やらないこと（誰が決めたか）                                                             |
| Constraints    | 規約、触らないファイル、使ってはいけない手段、互換性条件                                 |
| Open Questions | 未決事項                                                                                 |
| Phase / AC     | Phase ごとの受け入れ条件。`— verify: \`{コマンド}\``か`— judge: {判定基準}` を必ず付ける |
| Tasks          | `{何をするか} — 対象: \`{ファイル}\` / 確認: {方法}`（確認は省略可）                     |

- `docs/requirements/` `docs/architecture/` `docs/screens/` `docs/api/` `docs/database/` に設計書があれば、
  該当する節のパスを Context に載せる。発注内容が設計書と矛盾するときはユーザーに指摘する。設計書が無い
  プロジェクトでは Context の設計書参照を省いて続行する
- 設計書が無く、アーキテクチャ・API・データモデルに触る発注なら `/design` の先行を提案する。迷えば設計側に倒す
- 5 節には `cc:` マーカーを書かない。タスクは Tasks にだけ書く
- 出力先が plans なら、ファイル名にする slug を Project 名から `[a-z0-9][a-z0-9-]*`（ASCII 小文字・数字・ハイフン）で
  提案し、発注書案に含める。`.claude/plans/<slug>.md` が既にあれば、使われていない slug を提案し直す

### Step 1: 1 回だけ提示する

発注書案をそのまま表示し、AskUserQuestion で「承認 / 修正 / 中止」を選ばせる。修正は発注書案への差分編集で
行い、対話に差し戻さない。出力先が plans なら slug も同じ提示で承認を得る。

### Step 2: 1 か所に書く

`--to` が無ければ AskUserQuestion で出力先を選ぶ。

- **plans**: 新しいファイル `.claude/plans/<slug>.md` に `task-memory-usage` ルールの書式で `## Project:` + 5 節 +
  Phase（AC と Tasks）+ `## Decisions` / `## Notes` を 1 回で書く（書式は `task-memory-usage` ルールの「plan ファイルのフォーマット」に従う）。
  1 つの発注書につき 1 ファイルで、既存の plan ファイルには追記しない。`.claude/plans/` が無ければ作る。書く直前に
  同じ名前のファイルがあれば上書きせず、使われていない slug を示して「別の slug にする / 上書きする」を
  AskUserQuestion で確認する（上書きは利用者が選んだときだけ）。旧形式の `.claude/Plans.md` には書かない。
  以後の状態更新は `/task-state` で行う
- **issue**: `issue-create` の task テンプレートで Issue を作る（本文は `/order` が組み立て、`issue-create` は
  AC 確定済み経路で Step 4 / 5 の検査だけ行う）。写し方: `## タスク内容` に Goal（冒頭）・`### 前提`（Context /
  Constraints）・`### 作業項目`（Tasks）、`## 完了条件` に AC、`## 備考` に `### 対象外`（Out of Scope）と
  `### 未決事項`（Open Questions）。
  **ゲート**: Issue 化の前に Open Questions が残っていれば解消をユーザーに求め、解消できない項目が残る場合は
  Issue 化せず plan ファイルに留める（自律エンジンに未確定の前提を推測させない）

AC は対話で確定した内容を転記し、どちらの出力先でも聞き直さない。

### Step 3: エンジンをまたぐ引き渡し（`--from-plans` / `--from-issue`）

**plan ファイル → Issue（`--from-plans`）**

1. 対象の plan ファイルを決める。slug を省略したときは、未完了の plan ファイルが 1 つならそれ、複数なら
   AskUserQuestion で選ばせ、0 なら案内して終える（`task-memory-usage` ルールの「対象ファイルの決め方」）
2. そのファイルを読み、Step 2 の issue と同じ写し方で Issue を作る（Open Questions のゲートも同じ）
3. Issue の作成に成功したこと（Issue 番号が返ったこと）を確かめてから、その plan ファイルを削除する。削除の前に、slug が `[a-z0-9][a-z0-9-]*` に完全一致し、`.claude/plans/<slug>.md` が symlink でない通常ファイルであることを確かめる（例: `test -f` かつ `! test -L`）。満たさなければ削除せずに止めて報告する。発注書の
   内容は Issue 本文に残るので、ほかの場所には写さない。状態を二重に持たない。Issue の作成に失敗した場合は
   ファイルを残して報告する

**Issue → plan ファイル（`--from-issue`）**

1. `gh issue view {N}` で本文を読み、逆の写し方で新しい plan ファイルの Project にする（`## タスク内容` の冒頭 → Goal、
   `### 前提` → Context / Constraints、`### 作業項目` → Tasks、`## 完了条件` → AC、`### 対象外` → Out of Scope、
   `### 未決事項` → Open Questions。見出しが無い Issue は本文全体を Goal と Tasks に振り分け、AC が無ければ
   Step 0 と同じくユーザーに確認する）。slug は Step 0 と同じ規則で提案し、本文は聞き直さず slug だけ承認を得る
2. `.claude/plans/<slug>.md` の書き込みに成功してから、Issue に「plan ファイルへ引き渡し（{worktree の相対パス}/.claude/plans/<slug>.md）」
   とコメントして close する（発注書を片方だけ残す）。書き込み・コメント・close のどこかで失敗した場合は、どこまで
   済んだかを報告し、Issue は close しないまま残す

### Step 4: 報告

書き先（`.claude/plans/<slug>.md` か Issue #N）と、次に使うエンジンのコマンドをエンジンごとに 1 行ずつ案内する。

| エンジン   | 次のコマンド                        |
| ---------- | ----------------------------------- |
| /goal      | `/goal` で plan ファイルを回す      |
| Codex 直接 | `/handoff`                          |
| loop-issue | `/loop-issue {N}`                   |
| TAKT       | `takt add '#{N}'`                   |

書き出した発注書が次のいずれかに当てはまる場合は、実装前に仕様を把握できているかを確かめられることを 1 行で案内する（条件は初期値。運用しながら見直す）。当てはまらない発注書では案内しない。

- Issue に出した（`/loop-issue`・TAKT が無人で実装するため、着手前に把握しておく価値が高い）
- アーキテクチャ・API・データモデル・認証・設定キーに触る（Step 0 で `/design` の先行を提案する判定と同じ観点）

```
実装前に仕様を把握できているか確かめるなら `/grasp-view {対象}` → `/grasp-check {対象}` を使えます（任意）。
```

`{対象}` は plan ファイルなら `plan {slug}`（`/grasp-view` にはファイルパス `.claude/plans/{slug}.md`）、Issue なら `#{N}`。案内は警告だけで、Step 1 の提示・選択肢・書き出しを変えない。

## 注意事項

- plan ファイルは worktree ごとのローカルファイル（gitignore）。Issue 化は `gh` の認証を前提とする
- 状態管理（`cc:` マーカーの更新、AC のチェック）は行わない。実行エンジンの責務
- 説明・出力は日本語で行う
