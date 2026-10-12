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

| 検査                   | 内容                                                                                                                                          | 既定レベル |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `dangling`             | `depends_on.id` が存在しない                                                                                                                  | error      |
| `duplicate`            | 同一 node_id が複数ファイルに存在                                                                                                             | error      |
| `cycle`                | depends_on が循環している                                                                                                                     | error      |
| `unknown`              | 未定義の kind / relation / status                                                                                                             | error      |
| `malformed_annotation` | code_scope のコード注釈が不正（値の無いキー、`node_id`/`kind`/`status`/`owner` の重複指定など。Issue #98）                                    | error      |
| `missing_frontmatter`  | scope 内なのに `codd:` ブロックが無い                                                                                                         | warning    |
| `orphan`               | 参照ゼロ（roots kind は除外）                                                                                                                 | warning    |
| `drift`                | 追従すべき上流に、基準時刻より新しい実質変更がある（追従漏れ疑い。コミット済みの原因は短 SHA と日付、未コミットは `未コミットの変更` と表示） | warning    |

検査レベルは `codd.yaml` の `checks` で `error` / `warning` / `off` に変更できる。

### Step 3: 終了コードと対応

- **error が 1 件以上**: CLI は終了コード 1。マージ前に必ず解消する。
- **warning のみ / 検出なし**: 終了コード 0。

### Step 3.5: drift の対処

- 下流を追従させる必要がある → 下流ドキュメントを更新する
- 中身を見て追従不要と判断した → `orchex run codd codd -- ack <node_id>` で drift 中のエッジに `synced`（確認済みの上流コミット）を付ける（`--all` で drift 対象の全エッジ）。`synced` は下流ファイルの frontmatter に書かれるので commit する。コミットの無い上流・コード注釈ノード・flow style の項目は SKIP として出力され、終了コードは 0 のまま
- 一覧 → 個票のように上流の一部だけに依存している → 下流の `depends_on[].anchor` に依存する行の文字列（例: `API-010`）を書く

### Step 4: 結果報告（日本語）

ユーザーへは Critical/High に相当する error を優先して報告する:

- error 件数・warning 件数のサマリ
- error 各件の対象ファイルと内容（リンク切れ先 node_id など）
- 修正方針（dangling なら参照先の node_id 修正 or 上流ノード追加）

## 補足

- drift は relation が `derives_from` / `refines` / `implements` で下流が ADR 以外のエッジだけを見る（`codd.yaml` の `drift:` で変更可）。空白・空行・表の区切り行だけの上流コミットは数えない。時刻ソースは `git log` のコミット時刻で、未コミットはファイル mtime にフォールバック。
- フロントマターの記法・語彙は `codd-frontmatter-policy` ルールを参照する。
