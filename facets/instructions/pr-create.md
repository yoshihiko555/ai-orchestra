# PR Create — Pull Request の作成

**現在のブランチから Pull Request を作成する。**

## Usage

```
/pr-create
/pr-create --issue 42
/pr-create --base stage
/pr-create --issue 42 --reviewers "code-reviewer: LGTM"
```

- `--base <branch>` は base branch を明示指定する（省略時は resolver で解決）

## Context 収集

スキル実行時に以下の情報を収集する:

```bash
# base branch の解決（PR Standards Policy の "Base Branch Resolution" 参照）
: "${AI_ORCHESTRA_DIR:?AI_ORCHESTRA_DIR is not set}"
BASE=$(python3 "$AI_ORCHESTRA_DIR/packages/git-workflow/scripts/resolve_base_branch.py" \
  ${BASE_OVERRIDE:+--base "$BASE_OVERRIDE"})

# ブランチ・ステータス・ベースブランチとの差分
git branch --show-current
git status --short
git log --oneline "$BASE..HEAD"
git diff --stat "$BASE..HEAD"
```

- `--base` 引数があれば `BASE_OVERRIDE` に代入してから resolver を呼ぶ
- 以降の手順で PR タイトル生成・差分収集・`gh pr create` のすべてに `$BASE` を使う

## Workflow

### Step 1: 情報収集

#### 1-1. ブランチ確認

```bash
BRANCH=$(git branch --show-current)
```

解決済み `$BASE` と `$BRANCH` が一致する場合（= base branch 上で実行された場合）はエラーで終了する（PR 作成対象のブランチに移動するよう案内）。

#### 1-2. コミット履歴の取得

```bash
git log --oneline "$BASE..HEAD"
git diff --stat "$BASE..HEAD"
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
ベースブランチ: {$BASE}

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
  --base "$BASE" \
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
- ベースブランチ: {$BASE}
```

## 注意事項

- `gh` コマンドは認証済みであることを前提とする
- 解決済み base branch への直接 push は行わない
- マージ方式は GitHub 上の Squash and merge を前提とする
- ユーザー向け変更がある場合は `CHANGELOG.md` の `Unreleased` 更新を Checklist で確認する
- PR タイトルは GitHub Release にそのまま載ることを想定し、簡潔かつ明確にする
- 説明・出力は日本語で行う
