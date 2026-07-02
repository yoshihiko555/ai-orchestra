---
name: design-tracker
description: PROACTIVELY track and document project design decisions without being
  asked. Activate automatically when detecting architecture discussions, implementation
  decisions, pattern choices, library selections, or any technical decisions. Also
  use when user explicitly says "記録して", "設計どうなってる", "record this". Do NOT wait for
  user to ask - record important decisions immediately.
---

# Design Tracker Skill

## Purpose

プロジェクトの設計判断を ADR（Architecture Decision Records）として `docs/adr/` に記録する。

追跡対象:

- アーキテクチャ判断
- 実装方針の決定
- ライブラリ選定とその理由
- トレードオフの検討結果

## When to Activate

- ユーザーがアーキテクチャや設計パターンについて議論したとき
- ユーザーが実装判断を下したとき（例: 「ReAct パターンを使おう」）
- ユーザーが「記録して」「設計どうなってる」「record this」と言ったとき
- 会話中に重要な技術的判断が行われたとき

## Workflow

### Recording Decisions

1. `docs/adr/DECISIONS.md` を読み、既存の ADR 一覧と最新の ADR 番号を確認する
2. 会話から設計判断を抽出する
3. `docs/adr/_template.md` に基づいて新しい ADR ファイルを作成する
4. `docs/adr/DECISIONS.md` の一覧に新しい ADR を追加する

### ADR ファイル名規則

```
docs/adr/ADR-{YYYYMMDD}-{連番}.md
```

例: `docs/adr/ADR-20260308-009.md`

### ADR テンプレート

ファイル先頭に CODD フロントマターを付与する（codd は essential のため常に付与。記法は
`codd-frontmatter-policy` ルール参照）。

```markdown
---
codd:
  node_id: "adr:ADR-{YYYYMMDD}-{連番}"
  kind: adr
  status: accepted # proposed/accepted/rejected/superseded/deprecated（ステータス行と一致させる）
  depends_on: [] # 「関連」がある場合のみ記載（下記マッピング参照）
  owner: # 任意
---

# ADR-{YYYYMMDD}-{連番}: {タイトル}

- **ステータス**: accepted
- **日付**: {YYYY-MM-DD}
- **決定者**: （名前 or チーム）

## コンテキスト

何が問題か、なぜ決定が必要か。

## 検討した選択肢

### 選択肢 A: （名前）

- メリット:
- デメリット:

### 選択肢 B: （名前）

- メリット:
- デメリット:

## 決定

どの選択肢を採用し、なぜそう判断したか。

## 影響

この決定によって変わること、今後の制約。
```

### CODD フロントマターの付与

ADR 作成・更新時に `codd:` ブロックを必ず維持する。

- **node_id**: `adr:ADR-{YYYYMMDD}-{連番}`（ファイル名と一致）。
- **status**: 本文の `**ステータス**` 行と常に一致させる（accepted / superseded など）。
  ステータスを変更したらフロントマターの `status` も同時に更新する。
- **depends_on（`関連:` からの移行）**:

| 関連の種類                            | relation     |
| ------------------------------------- | ------------ |
| 旧 ADR を置き換える（supersede）      | `supersedes` |
| 関連する ADR / 設計を参照する（弱い） | `references` |

例: ADR-20260624-011 が ADR-20260101-003 を置き換える場合:

```yaml
depends_on:
  - id: "adr:ADR-20260101-003"
    relation: supersedes
```

置き換えられた旧 ADR 側は `status: superseded` に更新する。

### Viewing Current Decisions

ユーザーが「設計どうなってる」「what have we decided?」と聞いた場合:

1. `docs/adr/DECISIONS.md` を読み込む
2. accepted ステータスの ADR 一覧を要約して報告する
3. 必要に応じて個別 ADR の詳細を読み込む

## Setup

プロジェクトに `docs/adr/` が存在しない場合:

1. `docs/adr/` ディレクトリを作成する
2. `docs/adr/_template.md` を配置する
3. `docs/adr/DECISIONS.md` を初期化する

## Output Format

When recording, confirm in Japanese:

- 何を記録したか
- ADR ファイル名
- 判断の要約

## Language Rules

- **Thinking/Reasoning**: English
- **Code examples**: English
- **Document content**: English (technical terms) + Japanese (descriptions OK)
- **User communication**: Japanese
