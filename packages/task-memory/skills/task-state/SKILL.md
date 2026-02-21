---
name: task-state
description: |
  Plans.md (SSOT) のタスク状態を管理する。
  タスクの追加・状態更新・一覧表示・Plans.md の初期化が可能。
metadata:
  short-description: SSOT タスク管理（Plans.md の作成・更新）
---

# Task State — Plans.md によるタスク管理

**Plans.md を SSOT として、タスクの進捗・状態をファイルベースで管理します。**

## 使い方

```bash
# Plans.md の初期化（新規プロジェクト）
/task-state init

# タスク一覧の表示
/task-state

# タスクの状態更新
/task-state update "タスク名" --status wip
/task-state update "タスク名" --status done
/task-state update "タスク名" --status blocked --reason "API仕様が未確定"

# フェーズの追加
/task-state add-phase "Phase 2: 実装" --tasks "API実装" "テスト作成" "ドキュメント"

# 設計判断の記録
/task-state decision "REST API ではなく GraphQL を採用（理由: フロントエンドの柔軟性）"
```

## 処理内容

### init モード

1. `.claude/Plans.md` が存在しない場合、テンプレートから生成
2. プロジェクト名は `git` リポジトリ名またはディレクトリ名から自動取得
3. 既存ファイルがある場合はスキップ（上書きしない）

### 表示モード（引数なし）

1. `.claude/Plans.md` を読み込み
2. 状態マーカー別にタスクを集計
3. WIP → blocked → TODO の優先度でサマリー表示

### update モード

1. Plans.md 内で指定タスクを検索
2. 状態マーカーを更新
3. blocked の場合は理由を追記

### add-phase モード

1. Plans.md に新しいフェーズセクションを追加
2. タスクは `cc:TODO` で初期化

### decision モード

1. `## Decisions` セクションに日付付きで設計判断を追記

## Plans.md フォーマット

```markdown
# Plans

## Project: my-app

### Phase 1: 設計 `cc:done`

#### アーキテクチャ

- `cc:done` 技術スタック選定
- `cc:done` DB スキーマ設計

### Phase 2: 実装 `cc:WIP`

#### API

- `cc:done` ユーザー認証 API
- `cc:WIP` 商品一覧 API
- `cc:TODO` 注文 API
- `cc:blocked` 決済 API — 理由: 外部決済サービスの契約待ち

---

## Decisions

- 2026-02-21: PostgreSQL を選定（理由: JSON サポートと拡張性）

## Notes

- MVP スコープは Phase 2 まで
```

## 状態マーカー

| マーカー     | 意味         |
| ------------ | ------------ |
| `cc:TODO`    | 未着手       |
| `cc:WIP`     | 作業中       |
| `cc:done`    | 完了         |
| `cc:blocked` | ブロック中   |

## 注意事項

- Plans.md は `.claude/Plans.md` に配置する
- 手動編集も可能（フォーマットに従うこと）
- git にコミットしてチーム間で共有することを推奨
- 状態マーカーはバッククォートで囲む: `` `cc:WIP` ``
