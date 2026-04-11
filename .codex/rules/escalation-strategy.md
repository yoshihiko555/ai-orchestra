# Context Escalation Policy

**コンテキスト消費を最小化するツール選択のガイドライン。情報取得は「絞り込みファースト」で段階的に行う。**

## 基本原則

Claude Code のコンテキストウィンドウは有限。ツール選択を最適化することで、用途によっては大幅なコンテキスト削減が可能（タスク特性により 50% 以上の削減事例あり）。

- 必要な情報だけを取得し、最小の出力形式から開始する
- 対象が絞れてから詳細を読む
- 不明瞭な段階で全文読み込みをしない

## エスカレーション戦略

対象ファイルが特定されていない状態から段階的に絞り込む。必要な情報が得られた段階で停止する。

### Step 1: Glob — ファイル名パターンで候補を集める

ファイル名パターンのみで探せる場合は Glob を使う。パスしか返さないためコンテキストは最小。

```
Glob: pattern="src/**/*.ts"
```

### Step 1.5: Grep（`output_mode: "count"`）— マッチ総数を事前把握

検索語のマッチ件数を先に確認し、後続ステップの `head_limit` 設定根拠にする。大量マッチ時のブローアップ防止。

```
Grep: pattern="foo", output_mode="count"
```

### Step 2: Grep（`output_mode: "files_with_matches"`）— 該当ファイルだけ取得

マッチしたファイルパスのみ返却。どのファイルを読むべきかを特定する段階。

```
Grep: pattern="foo", output_mode="files_with_matches"
```

### Step 3: Grep（`output_mode: "content"`, `head_limit: N`）— 該当行を制限付きで取得

マッチ行周辺の内容を最小限で確認。`head_limit` は Step 1.5 で把握したマッチ数を根拠に決める。

```
Grep: pattern="foo", output_mode="content", head_limit=20, -C=3
```

### Step 4: Read（`offset` / `limit` 指定）— 必要な範囲のみ部分読み込み

Grep で特定した行番号を `offset` にそのまま渡し、必要な前後範囲だけを読む。全文読み込みは避ける。

- `offset`: **読み始める行番号（1-indexed）**。Grep の `-n=true` で得た行番号をそのまま指定する。
- `limit`: 読み取る行数。

```
# Grep で line=121 にマッチが見つかった場合、少し前から読む
Read: file_path="...", offset=115, limit=80
```

## 判断基準ガイドライン

対象ファイル数・サイズに応じて適切な手法を選ぶ。

| 条件                             | 推奨手法                                       |
| -------------------------------- | ---------------------------------------------- |
| 対象ファイル数が不明（探索段階） | エスカレーション戦略（Step 1 から順番に）      |
| 対象ファイル数 5 個以上          | エスカレーション戦略を徹底する                 |
| 対象ファイル数 3 個以下          | 直接 Read（部分読み込みを優先）                |
| 対象ファイル数 10 個以上         | サブエージェント委譲でメインコンテキストを保護 |
| ファイルサイズ 200 行超          | `offset` / `limit` で部分読み込み              |
| ファイル全体の構造理解が必要     | 直接 Read（目的が明確な場合に限る）            |
| 大量の検索結果が予想される       | サブエージェント委譲 + 要約返却                |

## アンチパターン

以下の操作はコンテキストを無駄に消費するため避ける。

### 全文 Read の乱用

```
# Bad: いきなり全文 Read
Read: file_path="src/large_module.ts"  # 1000 行超のファイル

# Good: Grep で対象行を特定 → その行番号を offset にそのまま渡して部分 Read
Grep: pattern="handleClick", output_mode="content", -n=true  # 例: line 121 にヒット
Read: file_path="src/large_module.ts", offset=115, limit=60  # 少し前から 60 行読む
```

### `output_mode: "content"` の乱用

マッチ件数を把握せずに content モードを使うと、数百行の出力がコンテキストに流れ込む。

```
# Bad: 件数不明のまま content モード
Grep: pattern="log", output_mode="content"  # 数百マッチの可能性

# Good: まず count で把握し、head_limit を設定
Grep: pattern="log", output_mode="count"
Grep: pattern="log", output_mode="content", head_limit=30
```

### `head_limit` 未指定

`output_mode: "content"` で `head_limit` を指定しないのは危険。デフォルト 250 行まで返却される。

```
# Bad
Grep: pattern="import", output_mode="content"

# Good
Grep: pattern="import", output_mode="content", head_limit=50
```

### 全ファイル Read による一括確認

複数ファイルを順に全文 Read するのは最悪手。サブエージェント委譲で要約を得る。

```
# Bad: 10 ファイルを順に全文 Read
for f in files:
    Read: file_path=f

# Good: サブエージェント委譲
Task(subagent_type="general-purpose", prompt="次の 10 ファイルから X を抽出し要約を返せ: ...")
```

### Bash での `cat` / `head` / `tail` / `find` / `grep`

専用ツール（Read / Glob / Grep）を使うべき操作を Bash に流すと、出力制御ができずコンテキストを食う。

```
# Bad: Bash 経由で全文出力
Bash: command="cat src/large_module.ts"
Bash: command="find . -name '*.ts'"
Bash: command="grep -rn 'foo' src/"

# Good: 専用ツールを使う
Read: file_path="src/large_module.ts", offset=1, limit=100
Glob: pattern="**/*.ts"
Grep: pattern="foo", path="src", output_mode="files_with_matches"
```

## サブエージェント委譲の判断

次のいずれかに該当する場合はサブエージェント経由で実行する:

- 対象ファイル 10 個以上
- 検索結果が数百行以上になる見込み
- 外部 CLI（Codex / Gemini）の大きな出力を伴う
- 複数の独立した調査を並列で進めたい

サブエージェントには **要約形式での返却** を指示し、メインコンテキストには要点のみを残す。

## 運用補足

- このポリシーは全スキル・全エージェントが共有する最低基準。
- 個別スキル内の手順書で上書きが必要な場合は、明示的に理由を記載する。
- 既存ルール（`context-sharing`, `orchestra-usage`）と整合する。
