---
name: order
description:
  "対話（grill-me 等）で確定した計画を、発注書の内容モデル（Goal / Context /

  Out of Scope / Constraints / Open Questions + Phase の受け入れ条件と Tasks）に

  固めて 1 回提示し、承認後に Plans.md か GitHub Issue のどちらか 1 か所に書く。

  Plans.md の Project を Issue に引き渡すこともできる。

  「発注書にして」「Plans.md に起こして」「Issue に出して」「計画を固めて」といった

  依頼や /order で使用する。

  "
metadata:
  short-description: 対話の結論を発注書（Plans.md / Issue）に書き出す
---

# Dialog Rules Policy

**対話系スキルで守るべき共通ルール。**

## 対話進行の原則

### 1質問1ターンの原則

- AskUserQuestion で質問し、回答を受け取ってから次の質問に進む
- 1回の質問で聞く項目は **2〜3個まで**（多すぎると回答の質が下がる）
- 回答のエコーバック（要約して確認）→ 次の質問、の流れを維持する

### 推測禁止

- ユーザーの回答を勝手に推測して先に進めない
- AskUserQuestion の選択肢にAI側の推測を混ぜない
- 不明な点は「わかりません」と認め、質問で解消する

### スキップ時の扱い

- ユーザーが質問をスキップした場合は、合理的なデフォルト値を採用してよい
- ただしスキップされた旨と採用したデフォルト値を明示する
- 重要な判断（アーキテクチャ選定等）のスキップは確認を求める

## AskUserQuestion の使い方

- 対話は **必ず AskUserQuestion ツール** を使用する（テキスト出力での質問は不可）
- 選択肢は具体的で、ユーザーが判断しやすい形にする
- 「その他」は自動で追加されるため、選択肢に含めない

## 段階的確認

- 大きなフェーズ（要件定義 → 設計 → 実装等）の境界で、ここまでの内容を要約して確認を取る
- フェーズ遷移の条件を満たしていない場合は、不足項目を明示して追加質問する

---

# Order — 対話の結論を発注書にする

**対話（grill-me 等）で確定した計画を、実行エンジンの発注書として 1 か所に書き出す。**

対象と流れの正本は ADR-20260926-056（§決定 1・3）と `development-workflow` ルール。発注書の書式は
`task-memory-usage` ルール（Plans.md v2）。

## Usage

```
/order                          # 出力先を AskUserQuestion で選ぶ
/order --to plans               # Plans.md（/goal・Codex 直接 向け）
/order --to issue               # GitHub Issue（/loop-issue・TAKT 向け）
/order --from-plans "{Project 名}" --to issue   # Plans.md の Project を Issue に引き渡す
```

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

### Step 1: 1 回だけ提示する

発注書案をそのまま表示し、AskUserQuestion で「承認 / 修正 / 中止」を選ばせる。修正は発注書案への差分編集で
行い、対話に差し戻さない。

### Step 2: 1 か所に書く

`--to` が無ければ AskUserQuestion で出力先を選ぶ。

- **plans**: `.claude/Plans.md` に `task-memory-usage` ルールの v2 書式で `## Project:` + 5 節 + Phase（AC と
  Tasks）を 1 回で書く。Plans.md が無ければ新規作成する。以後の状態更新は `/task-state` で行う
- **issue**: `issue-create` の task テンプレートで Issue を作る（本文は `/order` が組み立て、`issue-create` は
  AC 確定済み経路で Step 4 / 5 の検査だけ行う）。写し方: `## タスク内容` に Goal（冒頭）・`### 前提`（Context /
  Constraints）・`### 作業項目`（Tasks）、`## 完了条件` に AC、`## 備考` に `### 対象外`（Out of Scope）と
  `### 未決事項`（Open Questions）。
  **ゲート**: Issue 化の前に Open Questions が残っていれば解消をユーザーに求め、解消できない項目が残る場合は
  Issue 化せず Plans.md に留める（自律エンジンに未確定の前提を推測させない）

AC は対話で確定した内容を転記し、どちらの出力先でも聞き直さない。

### Step 3: Plans.md から Issue へ引き渡す（`--from-plans`）

1. Plans.md の指定 Project を読み、Step 2 の issue と同じ写し方で Issue を作る（Open Questions のゲートも同じ）
2. その Project セクション（+ 区切り線 `---`）を Plans.md から取り除き、`.claude/Plans.archive.md` に追記する。
   形は SessionStart の自動アーカイブに合わせる（ファイル新規作成時は先頭に `# Archived Plans`、見出しは
   `## Archived: {YYYY-MM-DD}` に `（#{N} へ引き渡し）` を付記、本文の後に `---`）。状態を二重に持たない

### Step 4: 報告

書き先（Plans.md か Issue #N）と、次に使うエンジンのコマンドをエンジンごとに 1 行ずつ案内する。

| エンジン   | 次のコマンド                        |
| ---------- | ----------------------------------- |
| /goal      | そのまま `/goal` で Plans.md を回す |
| Codex 直接 | `/handoff`                          |
| loop-issue | `/loop-issue {N}`                   |
| TAKT       | `takt add '#{N}'`                   |

## 注意事項

- Plans.md は worktree ごとのローカルファイル（gitignore）。Issue 化は `gh` の認証を前提とする
- 状態管理（`cc:` マーカーの更新、AC のチェック）は行わない。実行エンジンの責務
- 説明・出力は日本語で行う
