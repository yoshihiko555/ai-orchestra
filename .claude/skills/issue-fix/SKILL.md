---
name: issue-fix
description: 'GitHub Issue を起点に計画→実装→テスト→レビューの開発フローを実行する。

  Issue の内容を読み取り、コード調査・実装計画の提示・ブランチ作成・コミットまでを行う。

  トリガー: /issue-fix

  '
metadata:
  short-description: Issue 起点の開発フロー
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
- [ ] 適切なラベルを付けた (`bug` / `enhancement` / `documentation` / `refactor` / `task` / ...)
- [ ] ユーザー向け変更がある場合は `CHANGELOG.md` の `Unreleased` を更新した
```

### セクション埋め込みルール

| セクション   | 入力ソース               | 記述ルール                                   |
| ------------ | ------------------------ | -------------------------------------------- |
| Summary      | コミット履歴 + diff stat | 変更内容を箇条書きで要約                     |
| Testing      | テスト実行結果           | 実施済みなら結果を記載、未実施なら理由を記載 |
| Release Note | 変更内容の分析           | ユーザー向け変更がある場合のみ記載           |
| Checklist    | 自動チェック             | 可能な項目は事前にチェック済みにする         |

## PR タイトル

- 形式: `{prefix}: {要約}`
- タイトルは **GitHub Release にそのまま載っても読める** 簡潔さにする
- 70 文字以内を目安にする

## ブランチプレフィックスとラベルの対応

ラベルは GitHub リポジトリで実際に定義されているものに合わせる。存在しないラベルを指定すると `gh pr create` がエラーを返すため、ポリシーと実リポジトリを同期させる。

| ブランチプレフィックス | PR タイトルプレフィックス | ラベル          |
| ---------------------- | ------------------------- | --------------- |
| `fix/`                 | `fix:`                    | `bug`           |
| `feat/`                | `feat:`                   | `enhancement`   |
| `docs/`                | `docs:`                   | `documentation` |
| `chore/`               | `chore:`                  | `task`          |
| `refactor/`            | `refactor:`               | `refactor`      |
| `test/`                | `test:`                   | `task`          |
| `task/`                | `chore:`                  | `task`          |
| `release/`             | `release:`                | `task`          |
| その他                 | `chore:`                  | `task`          |

> **Note**: `bug` / `enhancement` / `documentation` は GitHub のデフォルトラベルをそのまま採用している。`refactor` / `task` はプロジェクト固有ラベル。リポジトリが異なるラベル体系を使っている場合は、この表と実ラベルを個別に調整すること。

## Issue 連携

- Issue がある場合、PR 本文冒頭に `Closes #{番号}` を追加する
- Issue のラベルも参照してラベル決定を補完する

## Git 操作ルール

- `main` / 解決済み base branch への直接 push は行わない
- マージ方式は GitHub 上の **Squash and merge** を前提とする
- 競合解決は PR ブランチ側で `origin/{base}` を取り込んで行う（`{base}` は後述の resolver で解決）
- Push は `-u` フラグでトラッキングを設定する: `git push -u origin {ブランチ名}`

## Base Branch Resolution

**PR の base branch を固定せず、resolver スクリプトで解決する。** `pr-create` / `issue-fix` / その他 PR を作成するスキルは、このルールに従って `$BASE` を取得する。

### Resolver スクリプト

```bash
: "${AI_ORCHESTRA_DIR:?AI_ORCHESTRA_DIR is not set}"
BASE=$(python3 "$AI_ORCHESTRA_DIR/packages/git-workflow/scripts/resolve_base_branch.py" \
  ${BASE_OVERRIDE:+--base "$BASE_OVERRIDE"})
```

- 実体: `packages/git-workflow/scripts/resolve_base_branch.py`
- 出力: stdout に解決済み base branch 名を 1 行（`origin/` プレフィックスは除去される）
- `AI_ORCHESTRA_DIR` 未設定時はガードで即座に失敗させ、`$BASE` が空のまま `gh pr create --base ""` が実行される事故を防ぐ
- `BASE_OVERRIDE` が未定義の場合 `${BASE_OVERRIDE:+...}` は空に展開され、`--base` 引数なしで resolver を呼ぶ

### 解決優先順位

1. **`--base <branch>` 明示指定** — ユーザーが `/pr-create --base stage` のように指定した値
2. **環境変数 `AI_ORCHESTRA_BASE_BRANCH`** — プロジェクト固有のデフォルト（shell 設定や `.envrc` 等で設定）
3. **自動推定** — 候補 `main` / `master` / `staging` / `stage` / `develop` の中で実在するものを対象に、各候補について `merge-base <candidate> HEAD` → `rev-list --count <merge-base>..<candidate>` を計算し、距離が最小のもの（≒ 最も近い親ブランチ）を選ぶ。remote を優先し、remote になければローカルブランチを見る
4. **Fallback: `main`** — 候補が 1 つも存在しない場合

### スキル側の使い方

- Usage に `--base <branch>` 引数を追加する（明示指定を受け付ける）
- Context 収集の冒頭で resolver を呼び `$BASE` に格納する
- 差分収集 (`git log`, `git diff`) / プレビュー / `gh pr create` のすべてで `$BASE` を使う
- 「ベースブランチ: main」のような固定表記はしない（`ベースブランチ: $BASE` と表現する）

### 検証手順

| 運用パターン                                          | 期待動作                                |
| ----------------------------------------------------- | --------------------------------------- |
| `main` only のリポジトリ                              | `$BASE = main`                          |
| `main` + `stage` で `stage` から切った feature branch | `$BASE = stage`                         |
| `main` + `stage` で `main` から切った feature branch  | `$BASE = main`                          |
| `--base release` を明示指定                           | `$BASE = release`（他条件を無視）       |
| `AI_ORCHESTRA_BASE_BRANCH=develop`                    | `$BASE = develop`（明示指定がなければ） |

自動テストは `tests/unit/test_resolve_base_branch.py` が担保する。

---

# Tiered Review Output Contract

**レビュー系スキルの段階別出力形式。**

## フォーマット

```markdown
## Review Summary

**レビュアー**: {選定されたレビュアー一覧}
**変更ファイル**: {ファイル数} files, {追加行数} insertions(+), {削除行数} deletions(-)

### Critical ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**
  {問題の説明 + 影響 + 修正案}
  ```{lang}
  {コードスニペット}
  ```

### High ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**
  {問題の説明 + 修正案}

### Medium ({count})
- [{reviewer}] `{file}:{line}` - {1行サマリ}

### Low ({count})
- [{reviewer}] `{file}:{line}` - {1行サマリ}
```

## 重要度の定義

| 重要度 | 基準 | 対応 |
|--------|------|------|
| **Critical** | セキュリティ脆弱性、データ損失リスク、本番障害の可能性 | 必ず修正してから次に進む |
| **High** | バグの可能性、設計上の問題、パフォーマンス劣化 | ユーザーに確認（AskUserQuestion） |
| **Medium** | コード品質、可読性、軽微な改善 | 報告のみ。修正は任意 |
| **Low** | スタイル、命名、コメント改善 | 報告のみ。修正は任意 |

## 集約ルール

### 重複指摘の統合

複数レビュアーが同一ファイル・同一箇所を指摘した場合:

- severity が最も高いものを採用する
- 他のレビュアー名を `[{reviewer1}, {reviewer2}]` で併記する
- 異なる観点の指摘（例: security と performance）は別エントリとして残す

### 詳細度

- **Critical / High**: 詳細な説明 + 影響範囲 + 修正案（コードスニペット付き）
- **Medium / Low**: 1行サマリのみ

---

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
```

#### 1-4. ユーザー承認

AskUserQuestion で計画の承認を求める:

- 「計画通り進める」
- 「計画を修正する」
- 「中止する」

承認されなければ修正または中止する。

---

### Phase 2: 実装

#### 2-1. ブランチ作成

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
