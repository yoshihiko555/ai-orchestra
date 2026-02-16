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

### パスパターンによる自動選定

変更ファイルのパスに以下のパターンが含まれる場合、対応するレビュアーを選定する:

| パスパターン | レビュアー | 理由 |
|-------------|-----------|------|
| `packages/core/`, `packages/*/hooks/` | `code-reviewer` + `architecture-reviewer` | インフラ・共通基盤 |
| `src/`, `lib/`, `app/`, `packages/` | `code-reviewer` | ソースコード変更 |
| `auth`, `login`, `session`, `token`, `password`, `secret`, `permission` | `security-reviewer` | 認証・認可関連 |
| `api/`, `routes/`, `endpoints/`, `graphql/`, `handler` | `code-reviewer` + `security-reviewer` | API 変更 |
| `db/`, `migration`, `schema`, `model`, `prisma`, `drizzle` | `code-reviewer` + `performance-reviewer` | データベース変更 |
| `components/`, `pages/`, `views/`, `ui/`, `styles/`, `css` | `code-reviewer` + `ux-reviewer` | フロントエンド変更 |
| `config/`, `settings`, `.env`, `docker`, `infra/`, `terraform` | `security-reviewer` | インフラ・設定変更 |
| `test`, `spec`, `__tests__` | `code-reviewer` | テストコード変更 |
| `docs/`, `*.md`（ドキュメントのみの変更） | レビュー不要 | ドキュメントのみ |

### 選定ルール

1. **最大 2 レビュアー**: パスマッピングの結果を統合し、最大 2 個に絞る
2. **優先順位**: `security-reviewer` > `code-reviewer` > `performance-reviewer` > `ux-reviewer`
3. **最低 1 レビュアー**: コード変更がある限り、最低 `code-reviewer` は実行する
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

重要な指摘のみ報告してください（Critical / High）。
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
- `startproject`（Phase 6）
- その他、レビューフェーズを含むカスタムスキル

---

## `/review` スキルとの違い

| | このポリシー | `/review` スキル |
|-|-------------|-----------------|
| レビュアー数 | 1-2（自動選定） | 最大 6（全レビュアー） |
| 起動タイミング | スキル内レビューフェーズ | ユーザー明示的実行 |
| 目的 | 変更に応じた最低限の品質確認 | リリース前の包括的レビュー |
| 指摘への対応 | Critical は必須修正 | ユーザー判断 |
