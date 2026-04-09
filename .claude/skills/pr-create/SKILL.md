---
name: pr-create
description: '現在のブランチから Pull Request を作成する。

  PULL_REQUEST_TEMPLATE.md を読み込みセクションを自動生成する。

  ブランチ名からタイトルプレフィックスとラベルを自動決定する。

  「PR 作成して」「プルリク作って」「PR 出して」等のリクエストや、

  実装完了後の PR 作成フローで使用する。

  トリガー: /pr-create

  '
metadata:
  short-description: GitHub PR の作成
---

# CLI Language Policy

**外部 CLI（Codex CLI / Gemini CLI）と連携するスキルで守るべき共通ルール。**

## 言語プロトコル

| 対象 | 言語 |
|------|------|
| Codex / Gemini への質問 | **英語** |
| Codex / Gemini からの回答 | **英語** |
| ユーザーへの報告 | **日本語** |

## Config-Driven ルーティング

CLI ツールの利用可否と設定は `cli-tools.yaml` で一元管理する。

### 読み込み手順

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `{tool}.enabled` を確認する（`false` なら `claude-direct` にフォールバック）
4. `agents.{name}.tool` で実行先を決定する

### ルーティング規則

| `agents.{name}.tool` | 動作 |
|----------------------|------|
| `codex` | Codex CLI を使用 |
| `gemini` | Gemini CLI を使用 |
| `claude-direct` | 外部 CLI を呼ばず Claude で処理 |
| `auto` | タスク種別に応じて選択（深い推論 → Codex、調査 → Gemini、単純作業 → Claude） |

## サンドボックス実行

外部 CLI（Codex / Gemini）は sandbox 内で直接実行する。
エラー時は `claude-direct` にフォールバックする。

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

# PR Standards Policy

**Pull Request 作成時に守るべき共通ルール。`pr-create` および `issue-fix` から参照される。**

## PR テンプレート

PR 本文は以下のテンプレート構造に従う。プロジェクトに `.github/PULL_REQUEST_TEMPLATE.md` がある場合はそれを優先する。

### フォールバックテンプレート

```markdown
## Summary

-

## Testing

- [ ] テスト実施済み
- [ ] 未実施（理由を記載）

## Release Note

- ユーザー向け変更点:
- `CHANGELOG.md` 更新:

## Checklist

- [ ] PR タイトルが GitHub Release にそのまま載っても読める
- [ ] 適切なラベルを付けた (`feature` / `fix` / `docs` / `chore` / ...)
- [ ] ユーザー向け変更がある場合は `CHANGELOG.md` の `Unreleased` を更新した
```

### セクション埋め込みルール

| セクション | 入力ソース | 記述ルール |
|-----------|-----------|-----------|
| Summary | コミット履歴 + diff stat | 変更内容を箇条書きで要約 |
| Testing | テスト実行結果 | 実施済みなら結果を記載、未実施なら理由を記載 |
| Release Note | 変更内容の分析 | ユーザー向け変更がある場合のみ記載 |
| Checklist | 自動チェック | 可能な項目は事前にチェック済みにする |

## PR タイトル

- 形式: `{prefix}: {要約}`
- タイトルは **GitHub Release にそのまま載っても読める** 簡潔さにする
- 70 文字以内を目安にする

## ブランチプレフィックスとラベルの対応

| ブランチプレフィックス | PR タイトルプレフィックス | ラベル |
|---------------------|----------------------|--------|
| `fix/` | `fix:` | `fix` |
| `feat/` | `feat:` | `feature` |
| `docs/` | `docs:` | `docs` |
| `chore/` | `chore:` | `chore` |
| `refactor/` | `refactor:` | `refactor` |
| `test/` | `test:` | `test` |
| `task/` | `chore:` | `chore` |
| `release/` | `release:` | `release` |
| その他 | `chore:` | `chore` |

## Issue 連携

- Issue がある場合、PR 本文冒頭に `Closes #{番号}` を追加する
- Issue のラベルも参照してラベル決定を補完する

## Git 操作ルール

- `main` への直接 push は行わない
- マージ方式は GitHub 上の **Squash and merge** を前提とする
- 競合解決は PR ブランチ側で `origin/main` を取り込んで行う
- Push は `-u` フラグでトラッキングを設定する: `git push -u origin {ブランチ名}`

---

# PR Create — Pull Request の作成

**現在のブランチから Pull Request を作成する。**

## Usage

```
/pr-create
/pr-create --issue 42
/pr-create --issue 42 --reviewers "code-reviewer: LGTM"
```

## Context 収集

スキル実行時に以下の情報を収集する:

```bash
# ブランチ・ステータス・ベースブランチとの差分
git branch --show-current
git status --short
git log --oneline main..HEAD
git diff --stat main..HEAD
```

## Workflow

### Step 1: 情報収集

#### 1-1. ブランチ確認

```bash
BRANCH=$(git branch --show-current)
```

`main` ブランチ上にいる場合はエラーで終了する（PR 作成対象のブランチに移動するよう案内）。

#### 1-2. コミット履歴の取得

```bash
git log --oneline main..HEAD
git diff --stat main..HEAD
```

コミットが 0 件の場合はエラーで終了する。

#### 1-3. 既存 PR の確認

同一ブランチで既に PR が存在するか確認する:

```bash
gh pr list --head {ブランチ名} --state open --json number,title,url
```

既存 PR がある場合は AskUserQuestion で対応を選択する:
- **既存 PR を開く** — URL を報告して終了
- **新規 PR を作成** — 新しい PR を作成する

#### 1-4. PR テンプレートの取得

以下の優先順で PR テンプレートを探す:

1. `.github/PULL_REQUEST_TEMPLATE.md`（プロジェクトローカル）
2. `gh api repos/{owner}/{repo}/community/profile --jq '.files.pull_request_template'` でテンプレート URL を取得

テンプレートが見つからない場合は PR Standards Policy のフォールバックテンプレートを使用する。

#### 1-5. Issue 情報の取得（引数がある場合）

`--issue` 引数がある場合、Issue 情報を取得する:

```bash
gh issue view {番号} --json number,title,labels
```

---

### Step 2: PR 内容の生成

#### 2-1. PR タイトルの決定

以下の優先順でタイトルを決定する:

1. Issue がある場合: Issue タイトルをベースに `{prefix}: {タイトル}` 形式で生成
2. Issue がない場合: コミット履歴から要約を生成

プレフィックスとラベルは PR Standards Policy の「ブランチプレフィックスとラベルの対応」表に従う。

#### 2-2. PR 本文の生成

テンプレートの各セクションを埋める:

- **Summary**: コミット履歴 + diff stat から変更内容を箇条書きで要約
- **Testing**: テスト実行結果があればその内容、なければ「未実施」にチェック
- **Release Note**: ユーザー向け変更がある場合は記載、`CHANGELOG.md` 更新状況を記載
- **Checklist**: 自動チェック可能な項目は事前チェック

Issue がある場合、本文冒頭に `Closes #{番号}` を追加する。

#### 2-3. ラベルの決定

PR Standards Policy の「ブランチプレフィックスとラベルの対応」表に従い、ブランチプレフィックスからラベルを決定する。

`--reviewers` 引数がある場合、レビュー結果を PR 本文に追記する。

---

### Step 3: 確認（standalone 呼び出し時のみ）

`--issue` 引数なしで呼ばれた場合、AskUserQuestion でプレビューと確認を行う:

```
PR タイトル: {タイトル}
ラベル: {ラベル}
ベースブランチ: main

--- PR 本文プレビュー ---
{生成された本文}
---

この内容で PR を作成しますか？
```

選択肢:
- **作成する** — そのまま PR を作成
- **タイトルを修正** — タイトルのみ変更
- **本文を修正** — 本文を変更
- **中止** — PR 作成をキャンセル

`issue-fix` 等から引数付きで呼ばれた場合は確認をスキップし、そのまま作成する。

---

### Step 4: PR 作成

#### 4-1. リモートへの Push

```bash
git push -u origin {ブランチ名}
```

#### 4-2. PR の作成

```bash
gh pr create \
  --title "{タイトル}" \
  --label "{ラベル}" \
  --body "$(cat <<'EOF'
{生成された本文}
EOF
)"
```

#### 4-3. 結果報告

```
PR を作成しました:
- URL: {PR URL}
- タイトル: {タイトル}
- ラベル: {ラベル}
- ベースブランチ: main
```

## 注意事項

- `gh` コマンドは認証済みであることを前提とする
- `main` への直接 push は行わない
- マージ方式は GitHub 上の Squash and merge を前提とする
- ユーザー向け変更がある場合は `CHANGELOG.md` の `Unreleased` 更新を Checklist で確認する
- PR タイトルは GitHub Release にそのまま載ることを想定し、簡潔かつ明確にする
- 説明・出力は日本語で行う
