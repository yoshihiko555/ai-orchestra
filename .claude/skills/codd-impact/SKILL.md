---
name: codd-impact
description: 'Classify downstream documentation impact of a change into Green/Amber/Gray

  confidence bands by traversing the `depends_on` graph. Runs `codd impact --diff
  <ref>`.

  Trigger on: "codd impact", "影響分析", "信頼度", "/codd-impact".

  '
metadata:
  short-description: Classify change impact into Green/Amber/Gray bands
---

# codd-impact — 変更影響の信頼度分類

**変更 diff から下流ドキュメントへの影響を信頼度3帯域（Green=自動更新可 / Amber=要確認 / Gray=参考）で分類する。**

`depends_on` グラフを逆方向に辿り、変更ノードに依存している下流ノードを列挙して、
relation 強度とグラフ距離から信頼度スコアを算出する。

## 使い方

```
/codd-impact                 # HEAD（直近コミット）との差分で分析
/codd-impact origin/main     # origin/main との差分で分析
```

## ワークフロー

### Step 1: impact 実行

`codd` パッケージの CLI を `orchex run` 経由で実行する:

```bash
# 既定は HEAD との差分
orchex run codd codd -- impact --diff HEAD

# PR のベースとの差分（例）
orchex run codd codd -- impact --diff origin/main
```

- `git diff --name-status <ref>` で変更ファイルを取得し、frontmatter の `node_id` にマップする
- `depends_on` の逆引きで下流ノードを辿り、各ノードを信頼度帯域へ分類する
- 削除された上流ファイルは「dangling 注意」として別建てで報告する

### Step 2: 信頼度帯域の読み方

| 帯域      | 意味       | 推奨アクション                               |
| --------- | ---------- | -------------------------------------------- |
| **Green** | 自動更新可 | 直接の強依存。追従更新の候補として高信頼     |
| **Amber** | 要確認     | 多段・中強度依存。人間が追従要否を確認       |
| **Gray**  | 参考       | 弱依存（references）や遠い距離。情報提供のみ |

- `score` は `min(経路上の relation 重み) × decay^(hops-1)`。
- `co_changed` フラグは「下流ノード自身も同じ diff で変更済み」を示す（Amber 上限）。
- `via` は影響の起点になった変更ノード（裏付け起点）。

### Step 3: JSON 出力（CI 連携・機械処理）

```bash
orchex run codd codd -- impact --diff origin/main --json
```

`impacted[].band` / `score` / `origins` / `co_changed` を含む構造化出力を返す。

### Step 4: 結果報告（日本語）

ユーザーへは以下を報告する:

- 変更ノード数 / 影響先ノード数（green / amber / gray の内訳）
- Green / Amber の影響先（追従更新の要否）
- 削除された上流があれば `/codd-validate` での dangling 確認を促す

## 補足

- 設定の参照順は `config-loading` ルールに従う（`codd.yaml` → `codd.local.yaml`）。
- 重み・閾値・減衰は `codd.yaml` の `impact:` ブロックで上書きできる。
- フロントマターの記法は `codd-frontmatter-policy` ルールを参照する。
- `--root` は git ルートと一致させる（変更パスの突合は git ルート相対で行う）。
