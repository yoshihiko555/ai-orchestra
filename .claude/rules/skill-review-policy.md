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

# Skill Review Policy

**レビューフェーズを持つスキルは、変更内容に応じた適切なレビュアーで実質的なレビューを実施すること。**

## 原則

スキルのレビューフェーズでは以下を必ず実行する:

1. **変更ファイルの分析**: `git diff --stat` で変更ファイル一覧を取得
2. **レビュアー選定**: 下記マッピングに基づき 1-2 個のレビュアーを選定
3. **サブエージェントレビュー実行**: 選定されたレビュアーをサブエージェントとして起動
4. **結果の確認**: Critical/High 指摘があればレビューフェーズ内で対応

---

## レビュアー選定ガイド

### パスパターンによる追加選定

パスパターンは**ベースライン（`code-reviewer`）に追加**する形で専門レビュアーを選定する。
ソースコード変更がある限り `code-reviewer` は必ず含まれる。

変更ファイルのパスに以下のパターンが含まれる場合、対応するレビュアーを**追加**する:

| パスパターン | 追加レビュアー | 理由 |
|-------------|--------------|------|
| `packages/core/`, `packages/*/hooks/` | + `architecture-reviewer` | インフラ・共通基盤 |
| `auth`, `login`, `session`, `token`, `password`, `secret`, `permission` | + `security-reviewer` | 認証・認可関連 |
| `api/`, `routes/`, `endpoints/`, `graphql/`, `handler` | + `security-reviewer` | API 変更 |
| `db/`, `migration`, `schema`, `model`, `prisma`, `drizzle` | + `performance-reviewer` | データベース変更 |
| `components/`, `pages/`, `views/`, `ui/`, `styles/`, `css` | + `ux-reviewer` | フロントエンド変更 |
| `config/`, `settings`, `.env`, `docker`, `infra/`, `terraform` | + `security-reviewer` | インフラ・設定変更 |
| `test`, `spec`, `__tests__` | （追加なし） | テストコード変更 |
| `docs/`, `*.md`（ドキュメントのみの変更） | レビュー不要 | ドキュメントのみ |

### 選定ルール

1. **ベースライン**: ソースコード変更がある限り `code-reviewer` を必ず含める
2. **最大 2 レビュアー**: パスマッピングの結果を統合し、最大 2 個に絞る
3. **優先順位**: `security-reviewer` > `code-reviewer` > `performance-reviewer` > `ux-reviewer`
4. **ドキュメントのみの例外**: `.md` ファイルのみの変更はレビューをスキップ可

---

## レビュー実行パターン

```
# 1. 変更ファイル一覧を取得
git diff --stat

# 2. 上記ガイドに基づきレビュアーを選定（例: code-reviewer + security-reviewer）

# 3. サブエージェントでレビュー実行
Task(subagent_type="code-reviewer", prompt="""
以下の変更をレビューしてください:

変更ファイル:
{git diff --stat の結果}

変更内容:
{git diff の結果（対象ファイル）}

Tiered Output 形式（Critical/High/Medium/Low）で報告してください。
""")
```

複数レビュアーの場合は **並列実行** する（`run_in_background=true`）。

---

## Critical/High 指摘への対応

レビュアーが Critical または High の指摘を返した場合:

| 重要度 | 対応 |
|--------|------|
| **Critical** | 必ず修正してから次のフェーズに進む |
| **High** | ユーザーに AskUserQuestion で対応を確認 |
| **Medium/Low** | 報告のみ。次のフェーズに進んでよい |

---

## 対象スキル

このポリシーはレビューフェーズを持つ全スキルに適用される:

- `issue-fix`（Phase 4）
- `startproject`（Phase 7）
- その他、レビューフェーズを含むカスタムスキル

---

## `/review` スキルとの違い

| | このポリシー（スキル内レビュー） | `/review`（スマート選定） | `/review all` |
|-|-------------------------------|-------------------------|---------------|
| レビュアー数 | 1-2（パスパターンのみ） | 2-3（パス + diff コンテンツスキャン） | 全 6 レビュアー |
| 起動タイミング | スキル内レビューフェーズ | ユーザー明示的実行 | ユーザー明示的実行 |
| 目的 | 変更に応じた最低限の品質確認 | 効率的な包括レビュー | リリース前の網羅的レビュー |
| 選定ロジック | パスパターンのみ | パス + diff コンテンツスキャン | 選定なし（全員） |
| 指摘への対応 | Critical は必須修正 | ユーザー判断 | ユーザー判断 |

---

## 運用補足（非破壊）

- このポリシーは「スキル内レビューフェーズ」の最低基準を定義する。
- マージ前の最終確認は `/review` や `/release-readiness` で補完する。
