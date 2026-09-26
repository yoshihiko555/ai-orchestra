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
  - 以降のフェーズ（4-3 のコミット、4-4 の `/pr-create` による push）は、ここで採用した現在ブランチ（`$CURRENT_BRANCH`）上で行う
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

レビューは `/review` スキルに委ねる。レビュアーの選定、設計書との突合、指摘の検証は `/review` の手順に従う。

**委譲先スキルの呼び出し方**（4-1 の `/review`、4-4 の `/pr-create`）: Claude Code では Skill ツールで呼ぶ。Skill ツールの無い CLI（Codex CLI 等）では、このスキルと同じ skills ディレクトリにある委譲先の `SKILL.md`（例: `.agents/skills/review/SKILL.md`）を読み、その手順どおりに実行する。

#### 4-1. `/review` の実行

`/review`（引数なし。スマート選定）を実行する。`.md` のみの変更の扱い（原則スキップ、仕様書・API ドキュメントは `spec-reviewer`）も `/review` に従う。

`/review` が使えない環境（quality-gates パッケージ未導入）では、`code-reviewer` をサブエージェントで起動し、`git diff --stat` と `git diff` を渡して Tiered Output 形式（Critical / High / Medium / Low）で報告させる（`skill-review-policy` ルールがあれば、そのパスパターンで専門レビュアーを 1 名まで足す）。

#### 4-2. 指摘対応

`/review` が最後に出力した Review Summary（Phase 4 の Tiered Output。`review.auto_fix` が有効ならループ最終回のもの）で判断する。auto_fix 有効時の Final Report は件数の推移しか持たないため、High の内容は Review Summary から拾う:

- **Critical**: Phase 2 に戻り修正する（必須）。auto_fix 有効時は、自動修正の後も残った Critical（Final Report が FAILED）が対象。指摘検証で `uncertain` のまま残った Critical と `NEEDS_REVIEW` の指摘は、AskUserQuestion で修正するか受け入れるかを確認してから進む
- **High**: ユーザーに AskUserQuestion で対応を確認（Final Report が PASSED でも High が残っていれば確認する）
- **指摘なし / Medium 以下のみ**: 次のステップに進む（`.md` のみの変更で `/review` がレビューをスキップした場合も同じ）

`/review` の Auto-Fix がコードを変更した場合（Final Report に修正の記録がある場合）は、4-3 に進む前に Phase 3 に戻り、テストと受け入れ条件の verify をやり直す。

#### 4-3. コミット

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

#### 4-4. 次アクション選択

AskUserQuestion で次のアクションを選択:

- **PR 作成**: `/pr-create` で Pull Request を作成
- **追加修正**: Phase 2 に戻る
- **完了**: 現在の状態で終了

##### PR 作成時

`/pr-create --issue {番号}` を実行する。base branch の解決、PR テンプレート、タイトルとラベル、`Closes #{番号}` の付与、push は `/pr-create` の手順に従う（`--issue` 付きの呼び出しでは作成前のプレビュー確認を省略する。同じブランチに既存 PR がある場合の確認は `/pr-create` 側で行う）。4-2 のレビュー結果を本文に残す場合は `--reviewers "{レビュアー}: {結果の要約}"` を付ける。

## 注意事項

- `gh` コマンドは認証済みであることを前提とする
- Phase 1 で必ずユーザーの承認を取ってから実装に進む
- コミットメッセージは日本語で記述する
- 既存の仕様や振る舞いを壊さないことを最優先する
- 大きな変更が必要な場合は、複数の小さなコミットに分割する
- 説明・出力は日本語で行う
