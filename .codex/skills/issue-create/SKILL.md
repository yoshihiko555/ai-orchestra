---
name: issue-create
description: 'GitHub Issue を作成する。種類（bug/feature/task）に応じたテンプレートで

  本文を構成し、ラベルを自動付与する。

  トリガー: /issue-create

  '
metadata:
  short-description: GitHub Issue の作成
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

# Issue Create — GitHub Issue の作成

**引数から Issue の種類とタイトルを判定し、テンプレートに沿って GitHub Issue を作成します。**

## Usage

```
/issue-create bug ログインエラー
/issue-create feature ダークモード対応
/issue-create task CI パイプライン整備
/issue-create                          # 対話的に種類・タイトルを決定
```

## Context 収集

スキル実行時に以下の情報を収集する:

```bash
# 現在のブランチとリモート URL
git branch --show-current
git remote get-url origin

# リポジトリの既存ラベル一覧
gh label list --json name,description --limit 100
```

## Workflow

### Step 1: 引数の解析

`$ARGUMENTS` から種類とタイトルを判定する。

| パターン | 種類 | 例 |
|---------|------|-----|
| `bug ...` | bug | `/issue-create bug ログインエラー` |
| `feature ...` | feature | `/issue-create feature ダークモード` |
| `task ...` | task | `/issue-create task CI整備` |
| 引数なし | — | AskUserQuestion で種類・タイトルをヒアリング |

種類またはタイトルが不足している場合は AskUserQuestion で確認する。

### Step 2: テンプレートに基づく本文作成

種類に応じたテンプレートで本文を構成する。

#### bug テンプレート

```markdown
## バグの概要

{タイトルから推定、またはユーザーに確認}

## 再現手順

1. {ユーザーに確認}

## 期待される動作

{ユーザーに確認}

## 実際の動作

{ユーザーに確認}

## 環境

- OS: {自動検出}
- ブランチ: {自動検出}
```

#### feature テンプレート

```markdown
## 概要

{タイトルから推定}

## モチベーション

{ユーザーに確認}

## 提案する実装

{ユーザーに確認、または「未定」}

## 受け入れ条件

- [ ] {ユーザーに確認}
```

#### task テンプレート

```markdown
## タスク内容

{タイトルから推定}

## 完了条件

- [ ] {ユーザーに確認}

## 備考

{任意}
```

### Step 3: プレビューと確認

1. 作成する Issue のプレビューを表示する:
   - タイトル
   - ラベル
   - 本文
2. AskUserQuestion で確認:
   - 「このまま作成」
   - 「修正してから作成」
   - 「キャンセル」

### Step 4: Issue 作成

```bash
# ラベルが存在しない場合は作成
gh label create "bug" --description "バグ報告" --color "d73a4a" 2>/dev/null || true
gh label create "feature" --description "新機能" --color "a2eeef" 2>/dev/null || true
gh label create "task" --description "タスク" --color "0075ca" 2>/dev/null || true

# Issue 作成
gh issue create --title "{タイトル}" --body "{本文}" --label "{種類}"
```

### Step 5: 結果報告

作成された Issue の番号と URL を報告する。

```
Issue #42 を作成しました: https://github.com/owner/repo/issues/42
```

## 注意事項

- `gh` コマンドは認証済みであることを前提とする
- ラベル作成に失敗しても Issue 作成は続行する（権限不足の場合）
- 本文が長すぎる場合はファイル経由で渡す（`--body-file`）
- 説明・出力は日本語で行う
