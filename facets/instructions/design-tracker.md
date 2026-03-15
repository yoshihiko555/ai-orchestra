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

```markdown
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
