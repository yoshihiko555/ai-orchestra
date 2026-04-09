# Facets

`facets/` は、AI Orchestra の skill / rule を部品化して管理するソースディレクトリです。ここで編集した内容は `orchex facet build` によって `.claude/` と `.codex/` の生成物へ反映されます。

詳細な仕組みは [Facet システム解説](../docs/guides/facet-system.md) を参照してください。この README は「どこを触ればよいか」を最短で判断するための入口です。

---

## 役割

| ディレクトリ | 役割 | 触る場面 |
|-------------|------|----------|
| `policies/` | 複数の skill / rule で共有する横断ルール | 複数箇所へ同じルールを反映したいとき |
| `output-contracts/` | 出力形式の共通テンプレート | レビュー形式やレポート形式を統一したいとき |
| `instructions/` | skill / rule 固有の手順本文 | 個別ワークフローや振る舞いを調整したいとき |
| `knowledge/` | skill に同梱する参考資料 | `references/` に配りたい補助資料を追加したいとき |
| `scripts/` | skill に同梱する補助スクリプト | skill から使うユーティリティを配布したいとき |
| `compositions/` | どの部品をどう結合するかの定義 | 新しい skill / rule を追加するとき、参照部品を変えるとき |

---

## 変更の入り口

### 既存 skill / rule の文面だけ直したい

- `facets/instructions/{name}.md` を編集
- 必要なら `orchex facet build --name {name} --project .` で生成物を確認

### 複数 skill / rule に共通ルールを追加したい

- `facets/policies/*.md` または `facets/output-contracts/*.md` を編集
- 参照先が不足していれば `facets/compositions/{name}.yaml` に追加

### 新しい skill / rule を追加したい

1. `facets/instructions/{name}.md` を作成
2. 必要なら `facets/knowledge/` / `facets/scripts/` に補助リソースを追加
3. `facets/compositions/{name}.yaml` を作成
4. 対応する `packages/*/manifest.json` の `skills` または `rules` に `{name}` を登録
5. `orchex facet build --name {name} --project .` で生成確認

### 生成物を直接チューニングした変更をソースへ戻したい

- `.claude/skills/{name}/SKILL.md` を調整後に `orchex facet extract --name {name} --project .`
- 書き戻し先は `facets/instructions/{name}.md`

---

## 迷ったときの判断基準

- 横断的なルールなら `policies/`
- 出力フォーマットなら `output-contracts/`
- 個別の手順なら `instructions/`
- skill に配る補助資料なら `knowledge/`
- skill に配る実行物なら `scripts/`
- 組み合わせや配布対象の宣言なら `compositions/` と `packages/*/manifest.json`

---

## 最低限の確認コマンド

```bash
# 全体再生成
orchex facet build --project .

# 単体確認
orchex facet build --name review --project .

# 生成物から instruction へ書き戻し
orchex facet extract --name review --project .
```

SessionStart でも自動ビルドされますが、facet ソースを編集した直後は手動実行のほうが差分を確認しやすいです。
