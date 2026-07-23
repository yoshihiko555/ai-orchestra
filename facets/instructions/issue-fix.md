# Issue Fix — Issue 起点の開発フロー

**GitHub Issue の内容を読み取り、計画→実装→テスト→レビューの 4 フェーズで開発を進めます。**

## Usage

```
/issue-fix #42
/issue-fix 42
/issue-fix           # AskUserQuestion で Issue 番号をヒアリング
```

## Context 収集

スキル実行時に以下の情報を収集する:

```bash
# ブランチ・ステータス・最近のコミット
git branch --show-current
git status --short
git log --oneline -5
```

## Workflow

### Phase 1: 計画

#### 1-1. Issue 内容の取得

`$ARGUMENTS` から Issue 番号を取得する。引数がなければ AskUserQuestion で確認する。

```bash
gh issue view {番号} --json number,title,body,labels,assignees
```

#### 1-2. 関連コードの調査

Issue の内容から関連するコードを Grep/Glob で調査する:

- エラーメッセージやキーワードで検索
- 関連ファイルの特定
- 影響範囲の把握

#### 1-3. 実装計画の提示

以下の形式で計画を提示する:

```markdown
## Issue #{番号}: {タイトル}

### 要約

{Issue の内容を 1-2 文で要約}

### 変更予定ファイル

- `path/to/file1.ts` — {変更内容}
- `path/to/file2.ts` — {変更内容}

### 実装手順

1. {ステップ 1}
2. {ステップ 2}
3. {ステップ 3}

### リスク・注意点

- {潜在的な問題と対策}

### 受け入れ条件

- [ ] {機械検証可能な条件} — verify: `{コマンド}`
- [ ] {主観的な条件} — judge: {判定基準}
```

Issue 本文に受け入れ条件（Acceptance Criteria）が記載されている場合はそのまま転記する。記載されていない場合は、Issue の内容から補完案を提示し、1-4 のユーザー承認と合わせて合意を得てから確定する。

#### 1-4. ユーザー承認

AskUserQuestion で計画の承認を求める:

- 「計画通り進める」
- 「計画を修正する」
- 「中止する」

承認されなければ修正または中止する。

---

### Phase 2: 実装

#### 2-1. ブランチの準備状況を確認

**issue ごとに先に worktree を作成し、その worktree 上で作業を進める。**
そのため Phase 2-1 ではまず「作業用ブランチが既に準備済みか」を判定し、**準備済みならブランチ作成をスキップ**して現在ブランチでそのまま作業を開始する。worktree 作成の責務とブランチ作成の責務を二重化させない。

##### 準備状況の判定

以下のいずれかを満たせば「準備済み」とみなす:

- **worktree 内で実行している**: `git rev-parse --git-dir` と `git rev-parse --git-common-dir` が異なる（最も確実なシグナル）
- **base 以外のブランチにいる**: 現在ブランチが解決済み base branch（`$BASE`）と異なる

```bash
GIT_DIR=$(git rev-parse --git-dir)
GIT_COMMON_DIR=$(git rev-parse --git-common-dir)
CURRENT_BRANCH=$(git branch --show-current)

# base branch 解決（PR Standards Policy の resolver を利用）
: "${AI_ORCHESTRA_DIR:?AI_ORCHESTRA_DIR is not set}"
BASE=$(python3 "$AI_ORCHESTRA_DIR/packages/git-workflow/scripts/resolve_base_branch.py" 2>/dev/null || echo "")

# 判定に使う比較（下記いずれかが真なら「準備済み」）:
#   worktree 内     : [ "$GIT_DIR" != "$GIT_COMMON_DIR" ]
#   base 以外にいる : [ -n "$BASE" ] && [ "$CURRENT_BRANCH" != "$BASE" ]
```

- **準備済み（`$GIT_DIR` ≠ `$GIT_COMMON_DIR`、または `$BASE` が非空かつ `$CURRENT_BRANCH` ≠ `$BASE`）の場合**:
  - 追加のブランチ作成は **行わない**
  - 現在のブランチをそのまま採用し、「作業ブランチ: `{現在ブランチ}`」と明示報告する
  - 以降のフェーズ（4-5 コミット / 4-6 PR push）で参照する `{ブランチ名}` は、ここで採用した現在ブランチ（`$CURRENT_BRANCH`）を指す
  - そのまま 2-2 へ進む
- **未準備（base 上 かつ 非 worktree）の場合のみ**: 下記フォールバックでブランチを作成する

> **安全側の判断**: `$BASE` の解決に失敗した（空になった）場合、現在ブランチが `main` / `master` / `develop` / `stage` / `staging`（resolver の候補と同じ統合ブランチ）なら未準備として扱いブランチを作成する。それ以外（既に feature ブランチ等）は準備済みとみなしスキップする。統合ブランチ上で直接作業しないことを優先する。

##### フォールバック: ブランチ作成（base 上・非 worktree のときのみ）

Issue のラベルからブランチプレフィックスを決定する:

| ラベル  | プレフィックス | 例                         |
| ------- | -------------- | -------------------------- |
| bug     | `fix/`         | `fix/issue-42-login-error` |
| feature | `feat/`        | `feat/issue-42-dark-mode`  |
| task    | `chore/`       | `chore/issue-42-ci-setup`  |
| その他  | `fix/`         | `fix/issue-42-slug`        |

```bash
git checkout -b {prefix}issue-{番号}-{slug}
```

- `{slug}` は Issue タイトルから英語 kebab-case で生成（最大 30 文字）
- 既にブランチが存在する場合は AskUserQuestion で確認

#### 2-2. コード変更

Phase 1 の計画に基づいてコードを変更する。

**変更が 3 箇所以上の場合**: 適切な implementation agent に委譲する。

```
Task(subagent_type="{agent}", prompt="""
タスク: {計画に基づく変更内容}
対象ファイル: {files}

IMPORTANT: cli-tools.yaml の設定に従い実装すること。
""")
```

**変更が 1-2 箇所の軽微な修正**: オーケストレーターが直接 Edit で実行してよい。

- 既存のコードスタイルに合わせる
- 小さく安全なステップで修正する
- 変更後は差分の要点を報告する

---

### Phase 3: テスト

#### 3-1. テスト実行

プロジェクトにテストコマンドがある場合は実行する:

```bash
# package.json の scripts.test があれば
npm test

# pytest が使えれば
pytest

# テストコマンドが不明な場合はスキップし、理由を明示
```

#### 3-2. 完了条件チェック

以下をチェックする:

- [ ] Issue に記載された条件を満たしているか
- [ ] テストが通るか（テストがある場合）
- [ ] 既存の機能を壊していないか

Issue 本文の受け入れ条件は次の手順で検証する:

- `— verify: \`コマンド\`` 付きの条件は、**そのコマンドを実際に実行して pass を確認する**（未実行のままチェック済み扱いにしない）
- `— judge:` 付きの条件は、判定基準と照合して確認する
- 検証対象は Phase 1（1-3「受け入れ条件」）でユーザーと合意した受け入れ条件とする

NG の場合は Phase 2 に戻って修正する。

---

### Phase 4: レビュー

`skill-review-policy.md` に基づき、変更内容に応じた実質的なレビューを実施する。

#### 4-1. 変更サマリー作成

```bash
git diff --stat
```

変更内容のサマリーを作成する。

#### 4-2. レビュアー選定

`git diff --stat` の出力からファイルパス一覧を取得し、`skill-review-policy.md` のパスパターンマッピングに基づいてレビュアーを選定する（最大 2 個）。

**選定手順:**

1. 変更ファイルのパスをパスパターンマッピングに照合
2. 優先順位（security > code > performance > ux）に基づき最大 2 レビュアーに絞る
3. コード変更がある限り最低 `code-reviewer` は選定する
4. ドキュメント（`.md`）のみの変更の場合はレビューをスキップ

#### 4-3. サブエージェントレビュー実行

選定されたレビュアーをサブエージェントとして起動する:

```
Task(subagent_type="{selected-reviewer}", prompt="""
以下の変更をレビューしてください:

Issue: #{番号} - {タイトル}

変更ファイル:
{git diff --stat の結果}

変更内容:
{git diff の結果}

重要な指摘のみ報告してください（Critical / High）。
Minor は省略可。
""")
```

複数レビュアーの場合は並列実行する（`run_in_background=true`）。

#### 4-4. 指摘対応

- **Critical**: Phase 2 に戻り修正する（必須）
- **High**: ユーザーに AskUserQuestion で対応を確認
- **指摘なし / Medium 以下のみ**: 次のステップに進む

#### 4-5. コミット

コミットメッセージは日本語で、Issue 参照を含める:

```bash
git add {変更ファイル}
git commit -m "{prefix}: {変更内容の要約}

Closes #{番号}"
```

プレフィックスは Issue のラベルに応じて決定する:

- bug → `fix:`
- feature → `feat:`
- task → `chore:`

#### 4-6. 次アクション選択

AskUserQuestion で次のアクションを選択:

- **PR 作成**: PR Standards Policy に従い Pull Request を作成
- **追加修正**: Phase 2 に戻る
- **完了**: 現在の状態で終了

##### PR 作成時

PR Standards Policy に従い、以下を実行する:

1. PR Standards Policy の "Base Branch Resolution" に従い `$BASE` を解決する（issue-fix では `--base` 引数は持たず、環境変数 `AI_ORCHESTRA_BASE_BRANCH` → 自動推定 → fallback の順で解決される）:
   ```bash
   : "${AI_ORCHESTRA_DIR:?AI_ORCHESTRA_DIR is not set}"
   BASE=$(python3 "$AI_ORCHESTRA_DIR/packages/git-workflow/scripts/resolve_base_branch.py")
   ```
2. PR テンプレートを取得する（`.github/PULL_REQUEST_TEMPLATE.md` → フォールバック）
3. ブランチプレフィックスからタイトルプレフィックスとラベルを決定する
4. テンプレートの各セクションを埋める（レビュー結果がある場合は Summary に追記）
5. `Closes #{番号}` を本文冒頭に追加する
6. Push して PR を作成する:

```bash
git push -u origin {ブランチ名}
gh pr create --title "{prefix}: {要約}" --label "{ラベル}" --base "$BASE" --body "{生成された本文}"
```

## 注意事項

- `gh` コマンドは認証済みであることを前提とする
- Phase 1 で必ずユーザーの承認を取ってから実装に進む
- コミットメッセージは日本語で記述する（AI_POLICY.md 準拠）
- 既存の仕様や振る舞いを壊さないことを最優先する
- 大きな変更が必要な場合は、複数の小さなコミットに分割する
- 説明・出力は日本語で行う
