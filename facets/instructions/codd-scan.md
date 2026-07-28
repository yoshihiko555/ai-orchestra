# codd-scan — 依存グラフの構築

**ドキュメントのフロントマター（`codd:` ブロック）を走査し、依存グラフを `.claude/codd/graph.jsonl` に構築する。**

## 使い方

```
/codd-scan
```

## ワークフロー

### Step 1: scan 実行

`codd` パッケージの CLI を `orchex run` 経由で実行する:

```bash
orchex run codd codd -- scan
```

- scope（`.claude/config/codd/codd.yaml` の `scope.include` − `scope.exclude`）を走査する
- 各ドキュメント先頭の `codd:` フロントマターからノードを構築する
- `code_scope.include`（既定は空 = 何も走査しない opt-in 設定）が設定されていれば、対象
  ソースファイルも走査する。コード注釈は 1 行形式（`codd:<key> <value>`）で、Python は
  module docstring、`//` 系言語（TS/JS/Go/Java/Rust/C 系）は先頭の行コメントブロックに書く
  （`kind: code` / `test`）。コード注釈由来のリンクは confidence 既定 0.7（doc frontmatter
  は 1.0）で、`/codd-impact` の帯域分類に影響する
- 依存グラフを `.claude/codd/graph.jsonl`（1 ノード 1 行）に出力する

### Step 2: グラフの確認（任意）

テキスト形式で依存関係を確認したい場合:

```bash
orchex run codd codd -- graph
```

`[missing]` 付きの依存先はリンク切れ（`/codd-validate` で error 報告される）。

### Step 3: 結果報告（日本語）

ユーザーへは以下を報告する:

- 構築したノード数 / フロントマター欠落ファイル数
- グラフ出力先（`.claude/codd/graph.jsonl`）
- リンク切れ等の懸念があれば `/codd-validate` の実行を促す

## 補足

- 設定の参照順は `config-loading` ルールに従う（`codd.yaml` → `codd.local.yaml`）。
- scope や kind/relation 語彙を変えたい場合は `codd.local.yaml` で上書きする。
- フロントマターの記法は `codd-frontmatter-policy` ルールを参照する。
