---
name: issue-create
description: |
  GitHub Issue を作成する。種類（bug/feature/task）に応じたテンプレートで
  本文を構成し、ラベルを自動付与する。
  トリガー: /issue-create
metadata:
  short-description: GitHub Issue の作成
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
