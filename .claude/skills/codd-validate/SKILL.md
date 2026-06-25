---
name: codd-validate
description: 'Validate the document dependency graph for coherence issues:

  dangling links, duplicates, cycles, unknown vocab (error) and

  missing frontmatter, orphans, drift (warning).

  Trigger on: "codd validate", "整合性チェック", "リンク切れ検証", "/codd-validate".

  '
metadata:
  short-description: Validate CODD document coherence
---

# codd-validate — 整合性の検証

**依存グラフを検証し、リンク切れ・重複・循環・孤立・ドリフト・フロントマター欠落を報告する。**

## 使い方

```
/codd-validate
```

## ワークフロー

### Step 1: validate 実行

`codd` パッケージの CLI を `orchex run` 経由で実行する:

```bash
orchex run codd codd -- validate
```

scan を内部で実行してからグラフを検証するため、事前の `/codd-scan` は必須ではない。

### Step 2: 検査項目

| 検査                  | 内容                                       | 既定レベル |
| --------------------- | ------------------------------------------ | ---------- |
| `dangling`            | `depends_on.id` が存在しない               | error      |
| `duplicate`           | 同一 node_id が複数ファイルに存在          | error      |
| `cycle`               | depends_on が循環している                  | error      |
| `unknown`             | 未定義の kind / relation / status          | error      |
| `missing_frontmatter` | scope 内なのに `codd:` ブロックが無い      | warning    |
| `orphan`              | 参照ゼロ（roots kind は除外）              | warning    |
| `drift`               | 上流ノードが下流より新しい（追従漏れ疑い） | warning    |

検査レベルは `codd.yaml` の `checks` で `error` / `warning` / `off` に変更できる。

### Step 3: 終了コードと対応

- **error が 1 件以上**: CLI は終了コード 1。マージ前に必ず解消する。
- **warning のみ / 検出なし**: 終了コード 0。

### Step 4: 結果報告（日本語）

ユーザーへは Critical/High に相当する error を優先して報告する:

- error 件数・warning 件数のサマリ
- error 各件の対象ファイルと内容（リンク切れ先 node_id など）
- 修正方針（dangling なら参照先の node_id 修正 or 上流ノード追加）

## 補足

- drift の時刻ソースは `git log -1 --format=%ct`。未コミットはファイル mtime にフォールバック。
- フロントマターの記法・語彙は `codd-frontmatter-policy` ルールを参照する。
