# CODD Frontmatter Policy

**CODD 管理下のドキュメントが守るフロントマター記法ルール。**

`scan` / `validate`（`packages/codd`）はこの記法を正本（SSOT）として依存グラフを構築する。
依存宣言は外部ファイルではなく、各ドキュメント先頭の `codd:` ブロック1箇所に書く。

## 記法

各対象ドキュメントの**先頭**に YAML frontmatter を置き、`codd:` ブロックを埋め込む:

```yaml
---
codd:
  node_id: "design:codd-coherence-layer" # 一意ID。形式は <kind>:<file-slug>
  kind: design # requirement|design|adr|plan|rule|instruction
  status: draft # kind ごとに語彙が異なる（下表）
  depends_on:
    - id: "req:coherence-guardrail" # 参照先 node_id（実在必須）
      relation: derives_from # derives_from|refines|implements|references|supersedes
  owner: ai-orchestra # 任意。責任主体
---
```

- parser は**先頭ブロックのみ**を読む。本文中のコードブロック内 `---` や YAML 例は無視される。
- **1 ファイル = 1 ノード**。集約ファイル（FT 群を含む等）もファイル全体で 1 ノードとする。

## node_id 体系

`node_id` は `<kind>:<file-slug>`（file-slug = 拡張子を除いたファイル名 or 安定スラッグ）。

| kind        | node_id 例                  | 由来                     |
| ----------- | --------------------------- | ------------------------ |
| requirement | `req:feature-list`          | `docs/requirements/*.md` |
| design      | `design:architecture`       | `docs/architecture/*.md` |
| adr         | `adr:ADR-20260624-010`      | `docs/adr/ADR-*.md`      |
| plan        | `plan:codd-coherence-layer` | `.claude/Plans.md`       |
| rule        | `rule:config-loading`       | `.claude/rules/*.md`     |
| instruction | `instruction:claude-md`     | `templates/context/*.md` |

## status 語彙（kind 依存）

| kind                                             | status 語彙                                                        |
| ------------------------------------------------ | ------------------------------------------------------------------ |
| adr                                              | `proposed` / `accepted` / `rejected` / `superseded` / `deprecated` |
| requirement / design / plan / rule / instruction | `draft` / `active` / `deprecated`                                  |

## relation（関係種別）

| relation       | 意味                 | 典型的な向き         |
| -------------- | -------------------- | -------------------- |
| `derives_from` | 上流から派生         | design → requirement |
| `refines`      | 詳細化               | 詳細設計 → 基本設計  |
| `implements`   | 実装関係             | plan → design        |
| `references`   | 参照（弱い依存）     | 任意 → 任意          |
| `supersedes`   | 置換（旧版を無効化） | 新 ADR → 旧 ADR      |

## 運用ルール

- `depends_on.id` は実在する node_id を指すこと（リンク切れは `validate` で error）。
- 同一 node_id を複数ファイルで使わないこと（重複は error）。
- 依存は循環させないこと（循環は error）。
- `roots`（requirement / instruction）以外で参照ゼロのノードは孤立 warning になる。
- 上流ノードを更新したら、下流ノードも追従して更新する（drift warning の回避）。
- `off` を YAML に直接書くと boolean False と解釈されるため、検査レベルは引用符付き `"off"` で書く。
