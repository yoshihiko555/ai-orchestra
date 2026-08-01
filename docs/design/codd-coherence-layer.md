---
codd:
  node_id: "design:codd-coherence-layer"
  kind: design
  status: draft
  depends_on:
    - id: "req:coherence-guardrail"
      relation: derives_from
  owner: ai-orchestra
---

# CODD 整合性レイヤー 設計ドキュメント

**作成日**: 2026-06-24
**ステータス**: draft（設計フェーズ。Codex レビュー反映済み）
**対象**: `feat/codd` ブランチ

---

## 1. 背景と目的

[CODD（Coherence-Driven Development）](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence) は、
設計書・コード・テストの**整合性（coherence）**を保ちながら開発を駆動する方法論。
依存関係をドキュメントのフロントマターに宣言し、`scan` で依存グラフを構築、変更時に
影響範囲を自動特定する（参考実装: [yohey-w/codd-dev](https://github.com/yohey-w/codd-dev)）。

AI Orchestra にこの思想を取り込む目的は2段階ある。

1. **AI Orchestra 自身**のドキュメント（指示書・ルール・設計・ADR）の整合性を保つ。
2. **最重要**: AI Orchestra を導入した**各プロジェクトが生成するドキュメント**を、すべて
   CODD の思想に則って管理されるようにする。`/design` などのスキルが生成する成果物が
   依存グラフを持ち、整合性をガードレールで検知できる状態を「配布物」として届ける。

> AI Orchestra は packages/facets/hooks を導入先へ配る基盤を既に持つ。本機能はその配布
> レーンに「整合性パッケージ」を載せることで、新しい配布機構を作らずに全導入先へ展開する。

---

## 2. CODD からの借用範囲（思想のみ）

codd-dev のコードは使わず、以下の**設計思想だけ**を AI Orchestra の流儀（package / facet /
skill / hook）で独自実装する。

| 借用する思想                              | AI Orchestra での実装                           |
| ----------------------------------------- | ----------------------------------------------- |
| フロントマターで依存を宣言（SSOT 1箇所）  | 各 `.md` 先頭に `codd:` ブロック                |
| `scan` で依存グラフを構築                 | `packages/codd` の Python スクリプト            |
| `validate` で不整合を検出                 | リンク切れ・孤立・ドリフト・循環・欠落の検出    |
| 信頼度3帯域（Green/Amber/Gray）の影響分析 | **Phase 2（実装済み）**: `codd impact`（4.5.1） |
| hook / CI への組み込み                    | **Phase 2 以降**（既存 manifest hooks レール）  |

---

## 3. スコープ

### 3.1 In Scope（Phase 1）

- フロントマター規約（CODD スキーマ）の定義（**1ファイル=1ノード**）
- `packages/codd/` パッケージ新設し、**essential プリセットに追加**（常時有効）
- 依存グラフ構築（`scan`）と整合性検証（`validate`）
- ドキュメント生成スキル（`design` / `design-tracker` / `task-state`）が
  CODD フロントマターを生成するよう改修
- AI Orchestra 自身のドキュメントでドッグフード

### 3.2 Out of Scope（別Issue 登録）

| 項目                                                                                           | 理由                                       | 移管先                          |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------ | ------------------------------- |
| ~~impact 分析（Green/Amber/Gray 信頼度スコア）~~ → **Phase 2 で実装済み（Issue #94 / 4.5.1）**             | —                                           | 完了                             |
| ~~hook 自動配線（PostToolUse scan / pre-commit validate）の導入先展開~~ → **Issue #95 で実装済み（4.8.1）** | —                                           | 完了                             |
| 実 git pre-commit hook（PreToolUse 代替ではない本物の git hook）の配布                                     | 既存 pre-commit 環境との衝突・uninstall 時の原状回復を避けるため、Issue #95 では PreToolUse (Bash) の `git commit` 検出方式で代替した | Issue: codd-real-git-hook-distribution |
| CI（PR に verdict 投稿）                                                                                   | impact 分析に依存                           | Issue: codd-ci-guardrail         |
| ~~コード ⇔ ドキュメントのトレーサビリティ~~ → **Phase 3 で opt-in 実装済み（Issue #98 / 4.3.1）** | —                                            | 完了                             |
| ノードのサブ粒度化（1ファイル内 FT-xxx 単位のノード）                                                      | parser/validate が複雑化                    | Issue: codd-subnode-granularity  |

---

## 4. アーキテクチャ

### 4.0 全体パイプライン（図解）

各ドキュメント先頭の `codd:` frontmatter を SSOT とし、`scan` で依存グラフ（`graph.jsonl`）を構築、
`validate` / `impact` / `graph` で利用する。

![CODD 整合性レイヤー](../assets/codd-coherence-ja.png)

<details>
<summary>Mermaid ソース（パイプライン）</summary>

```mermaid
flowchart LR
    subgraph DOCS["各ドキュメント（先頭に codd: frontmatter）"]
        RQ[requirement]
        DS[design]
        AD[adr]
        PL[plan]
        RL[rule]
        IN[instruction]
    end
    DOCS -- "codd scan" --> G[".claude/codd/graph.jsonl<br/>ノード + depends_on エッジ"]
    G -- "codd validate" --> V["リンク切れ / 循環 / 重複 / 未知語（error）<br/>孤立 / drift（warning）"]
    G -- "codd impact --diff" --> IM["下流影響を Green / Amber / Gray<br/>の信頼度バンドで分類"]
    G -- "codd graph" --> GR["依存グラフ可視化"]
```

</details>

依存は各 doc の `codd:` ブロック1箇所が SSOT（`derives_from` / `refines` / `implements` /
`references` / `supersedes`）。

### 4.1 パッケージ構成

```
packages/codd/
  manifest.json          # SSOT。depends=[core]、skills/config/scripts を宣言
  lib/
    codd_common.py       # フロントマター parser + グラフモデル（共通ライブラリ。hook ではない）
  scripts/
    codd.py              # CLI: scan / validate / graph（orchex run / skill から呼ぶ）
  config/
    codd.yaml            # スコープ glob・kind/relation 定義・検査レベル・graph 保存先
  tests/
    test_codd_graph.py
```

facet composition で配布されるスキル / ルール:

- `facets/compositions/skills/codd-scan.yaml` → `/codd-scan`
- `facets/compositions/skills/codd-validate.yaml` → `/codd-validate`
- `facets/compositions/rules/codd-frontmatter-policy.yaml` → フロントマター記法の運用ルール

> facet モデルは composition 名 = skill 名。2スキルは 2 composition に分ける（H-2）。
> skill / rule の正本は `facets/` ソースのみ。`.agents/skills/` は build 生成物なので直接編集しない（L-2）。

### 4.2 フロントマター規約（CODD スキーマ）

各対象ドキュメントの先頭に以下を埋め込む。**正本はこのフロントマター1箇所**（二重管理なし）。
**1ファイル = 1ノード**とし、複数 ID を含む集約ファイルもファイル全体で1ノードとする（4.3）。

```yaml
---
codd:
  node_id: "design:codd-coherence-layer" # 一意ID。形式は <kind>:<file-slug>
  kind: design # requirement|design|adr|plan|rule|instruction
  status: draft # kind ごとに語彙が異なる（下表）
  depends_on:
    - id: "req:coherence-guardrail" # 参照先 node_id（実在必須）
      relation: derives_from # 関係種別（4.4 参照）
  owner: ai-orchestra # 任意。責任主体
---
```

- parser は**ドキュメント先頭の YAML frontmatter ブロックのみ**を読む。本文中のコードブロック内 `---` や YAML 例は無視する（M-1）。
- `status` の語彙は kind 依存（M-2）:

| kind                                             | status 語彙                                                        |
| ------------------------------------------------ | ------------------------------------------------------------------ |
| adr                                              | `proposed` / `accepted` / `rejected` / `superseded` / `deprecated` |
| requirement / design / plan / rule / instruction | `draft` / `active` / `deprecated`                                  |

この5プロパティで scan/validate の全検査が成立する（4.5 で検証）。
将来 `doc_links.yaml` 方式が必要になっても、parser に読み取り口を1つ足すだけで後付けできる
（Phase 1 では作らない）。

### 4.3 node_id 体系（1ファイル=1ノード）

`node_id` は `<kind>:<file-slug>`（file-slug = 拡張子を除いたファイル名 or 安定スラッグ）。

| kind        | node_id 例                               | 由来（1ファイル1ノード）                                                   |
| ----------- | ---------------------------------------- | -------------------------------------------------------------------------- |
| requirement | `req:feature-list`, `req:non-functional` | `docs/requirements/*.md`（FT 群を含む集約ファイル全体で1ノード）           |
| design      | `design:architecture`, `design:API-001`  | `docs/architecture/*.md`, `docs/api/API-001.md`（個別設計は元々1ファイル） |
| adr         | `adr:ADR-20260624-010`                   | `docs/adr/ADR-*.md`                                                        |
| plan        | `plan:codd-coherence-layer`              | `.claude/Plans.md`（プロジェクト単位で1ノード）                            |
| rule        | `rule:config-loading`                    | `.claude/rules/*.md`                                                       |
| instruction | `instruction:claude-md`                  | `templates/context/*.md`                                                   |

> ファイル内の FT-001 等の細粒度 ID は Phase 1 ではノード化しない（別Issue: codd-subnode-granularity）。

### 4.3.1 code / test ノード（コード⇔ドキュメントのトレーサビリティ / Issue #98）

`code` / `test` kind をノード語彙へ追加し、ソースファイルからも `implements` / `references` 等の
depends_on を宣言できるようにする。doc frontmatter との違いは次の3点。

1. **opt-in スコープ**: `codd.yaml` の新設 `code_scope.include`（既定: 空リスト）に glob を
   追加したファイルのみが走査対象になる。未設定プロジェクトは挙動が変わらない。
   `code_scope.exclude` も `include` と同じ `Path.glob()` で解決するが、末尾を `/**`
   ではなく `/**/*` にする（`Path.glob("**/.venv/**")` は Python 3.12 ではディレクトリの
   みを返し、実装の `_glob_relpaths()` にある `is_file()` フィルタで除外候補として拾えず
   実質無効になるため。既定 exclude 3 件はいずれも `/**/*` 形式）。`include` / `exclude`
   は文字列またはリストで書ける（単一文字列は単要素リスト扱い。YAML でリスト記法
   `- ` を書き忘れて文字イテレートされる誤動作を防ぐ。`scope.include` / `scope.exclude`
   にも同じ正規化を適用し、doc scope とコード scope の扱いを一貫させる）。空文字列
   （`""`）は「対象なし」を表す既存設定との後方互換のため空リストとして扱うが、
   リスト**内**の空文字列要素（例: `["", "src/**/*.py"]`）も同様に除去する
   （除去しないと `[""]` のまま `Path.glob("")` に渡り `ValueError` になる）。`../*.py` の
   ような相対パスでプロジェクトルート外に解決される glob マッチは黙って除外する
   （`Path.glob` はルート外のパスもそのまま解決してしまうため）。root 内へ戻ってくる
   相対 glob（`../proj/src/**/*.py`、root == proj）は `os.path.normpath` によるドット
   記法のレキシカルな畳み込みだけで root 相対へ正規化し、通常パターンで見つかる同一
   ファイルとの重複登録を防ぐ。root 内部のシンボリックリンクにマッチした場合は、
   解決先パスではなくリンク自体の論理パスを登録する（`git diff` が返すパスと
   `path_to_id` を一致させるため。シンボリックリンクの解決先が root 外の場合のみ
   従来どおり除外する）。走査対象言語（Python /
   `//` 系）に対応しない拡張子のファイルは、読み込み前に除外する（画像等が混在ディレクトリ
   glob にマッチしても UTF-8 テキストとして復号しようとしない）。
2. **1行の軽量注釈**: doc の YAML frontmatter 全体を書く代わりに、`codd:<key> <value>` の
   1行注釈を並べる。`key` は予約語（`node_id` / `kind` / `status` / `owner`）または
   relation 名（`implements` 等）で、value は対象 node_id。`node_id` を省略すると
   `<kind>:<file-stem>` から自動導出し、`kind` を省略するとパス規約
   （`tests/` 配下や `test_*` / `*_test` ファイル名）から `test` / `code` を推定する。
   `codd:kind` を明示する場合、ソース注釈では `code` / `test` のみ有効（requirement /
   design 等のドキュメント語彙は使えない。誤った値は `malformed_annotation` として
   報告したうえで推定 kind へフォールバックする）。relation 名の注釈（例:
   `codd:implements`）に参照先 value が無い場合は依存として黙って除外せず、
   `malformed_annotation` 検査（既定 error、4.5 参照）として報告する（予約語のみは
   value 省略を許容する。owner を書かない等）。`codd:` で始まりながら
   `codd:<key>` / `codd:<key> <value>` の文法に一致しない行（`codd:node-id`
   のようなハイフン混じりの key、`codd:node_id=value` のような `=` 区切り等の
   タイプミス）も同様に `malformed_annotation` として報告する（`codd:` で始まらない
   通常のコメント行は従来通り無視する）。予約語（`node_id` / `kind` / `status` /
   `owner`）が同一ファイル内で複数回指定された場合、採用値は最初の 1 件のみだが、
   重複自体も `malformed_annotation` として報告する（例: 正しい `codd:kind code` の
   後に禁止された `codd:kind requirement` が続いても、最初の truthy 値だけを見る
   構築ロジックでは検証をすり抜けてしまうため）。抽出前に先頭 BOM（U+FEFF）を取り除く
   （BOM が残ると Python は `ast.parse` が構文エラーになり、`//` 系言語は先頭行の
   コメント判定に失敗し、注釈が無言でスキップされるため）。
   言語別の抽出領域:
   - Python: `ast.parse` + `ast.get_docstring` でモジュール docstring のみを対象にする
     （本文コード中の文字列リテラルを誤って注釈と解釈しない）。実装は `ast.parse` の
     代わりに `tokenize` で先頭トークンのみ読む軽量版だが、結果は
     `ast.get_docstring(clean=False)` と一致させる（`("""...""")` のような丸括弧付き
     docstring も認識し、`"""..."""  + "suffix"` のような文字列連結式は docstring
     として誤抽出しない）。先頭の文（docstring）の終端が判明した時点で `tokenize` を
     打ち切り、以降のトークンは読まない（全トークンを事前にリスト化すると、大規模
     ファイルほど不要な CPU/メモリを消費するうえ、docstring より後方にある未閉じ
     文字列等の構文エラーが `TokenError` として伝播し、有効な先頭注釈まで失って
     しまうため）。モジュール先頭からインデントされた不正な Python（`ast.parse`
     なら `IndentationError` になるケース）は、最初の有意トークンが `INDENT` に
     なることで判定し、docstring として誤って取り込まない。ファイル読み込みは
     PEP 263 の宣言済みエンコーディング（先頭2行の coding cookie / BOM）を
     `tokenize.detect_encoding` で尊重する（固定 UTF-8 だと Latin-1 等の有効な
     Python ファイルが `UnicodeDecodeError` になるため）。`impact` の削除上流検出で
     `git show <ref>:<path>` から旧内容を取得する際も、working tree と同じ PEP 263
     規約でコミット時点のバイト列を復号する（旧内容を固定 UTF-8 でしか読まないと、
     Latin-1 等を宣言していた Python ファイルの削除が誤検出/例外になるため）。
   - `//` 系言語（TS/JS/Go/Java/Rust/C 系。`.mjs` / `.cjs` / `.mts` / `.cts` を含む）:
     ファイル先頭から連続する行コメントのみを対象にする（shebang 行はスキップ）。
   - **復号失敗のスキップ**: `//` 系ファイルが UTF-16 保存等で UTF-8 として復号できない、
     または Python ファイルの coding cookie が不正で `tokenize.detect_encoding` が
     `SyntaxError` / `LookupError` を投げる場合、working tree 側の読み込み
     （`_read_source_text`）は削除上流検出の `_decode_ref_source` と同じ規約で `None` を
     返し、`scan_code_nodes` は当該ファイルを注釈が無いものとして黙ってスキップする
     （復号不能ファイル1件で `scan`/`validate`/`impact` 全体がトレースバックで落ちるのを
     防ぐ）。
3. **信頼度（confidence）**: doc frontmatter 由来の depends_on は既定 confidence 1.0（人手
   レビュー済みの確定宣言）。コード注釈由来の depends_on は `codd.yaml` の `inline_confidence`
   （既定 0.7）を使う。`impact` のエッジ重みは `relation 重み × confidence`（4.5.1 参照）になり、
   低信頼リンクは下流影響の判定に比例して弱く反映される。`inline_confidence` は有限な
   `[0, 1]` へ正規化され、範囲外の有限値（例: `-0.1`）は境界にクランプ、NaN/Inf のような
   非有限値は既定値（0.7）へフォールバックする（範囲外/非有限値がそのままエッジ重みに
   流れ込むと、負値やNaNで誤って Gray 判定になったり `graph.jsonl` の JSON 出力が壊れたり
   するため）。doc frontmatter 側の `depends_on[].confidence` も同じ正規化を受ける。
   `bool` は `int` のサブクラスで `float(False) == 0.0` / `float(True) == 1.0` が例外なく
   通ってしまうため、数値設定として明示的に拒否する（`inline_confidence: false` が
   全エッジ重みゼロの一斉 Gray 化に化けるのを防ぐ）。`inline_confidence` / doc の
   `confidence` はいずれも bool を不正値として既定値へフォールバックし、`impact.*`
   （decay・thresholds・weights 等）は bool 混入を config エラー（4.6 参照）として拒否する。
   `CoddConfig`（`codd_common.py`）はこの3フィールド（`code_include` / `code_exclude` /
   `inline_confidence`）に既定値（空リスト / `DEFAULT_INLINE_CONFIDENCE`）を持たせ、
   Issue #98 追加前の全フィールドだけでも直接コンストラクタ呼び出しで構築できる
   （`codd_common.py` を共有ライブラリとして直接使う既存連携の後方互換を壊さないため）。

注釈が無いコードファイルは doc scope の `missing_frontmatter`（warning）とは異なり黙って
スキップする。コードベース全体への注釈強制はせず、追跡したいファイルにだけ opt-in で
付与する運用を想定するため。code/test ノードは他の kind と同じグラフに統合され、
dangling / duplicate / cycle / unknown / orphan / drift の各検査を特別扱いなしで受ける。
`impact` の削除上流検出（4.5.1）も doc scope 同様に code_scope 内の注釈付きファイル削除を
対象にする（旧内容からコード注釈を再抽出し、現グラフに node_id が残っているかで判定）。

### 4.4 relation（関係種別）

| relation       | 意味                 | 典型的な向き         |
| -------------- | -------------------- | -------------------- |
| `derives_from` | 上流から派生         | design → requirement |
| `refines`      | 詳細化               | 詳細設計 → 基本設計  |
| `implements`   | 実装関係             | plan/code → design   |
| `references`   | 参照（弱い依存）     | 任意 → 任意          |
| `supersedes`   | 置換（旧版を無効化） | 新ADR → 旧ADR        |

### 4.5 整合性検査ルール（scan / validate）

`scan` がフロントマターを収集してグラフを構築し、`validate` が以下を検査する。

| 検査                       | 条件                                                 | 既定レベル |
| -------------------------- | ---------------------------------------------------- | ---------- |
| **dangling**（リンク切れ） | `depends_on.id` が既存 node_id に存在しない          | error      |
| **duplicate**              | 同一 node_id が複数ドキュメントに存在                | error      |
| **cycle**（循環依存）      | depends_on を辿ると循環する                          | error      |
| **unknown**                | 未定義の kind / relation / status を使用             | error      |
| **malformed_annotation**（Issue #98） | code_scope の注釈が不正（relation 注釈の参照先 value 欠落、`codd:kind` にソース非対応の値、`codd:<key>` 文法違反等） | error      |
| **missing_frontmatter**    | scope 内なのに `codd:` ブロックが無い（H-5）         | warning    |
| **orphan**（孤立）         | 被参照ゼロ かつ 参照ゼロ（`roots` 指定 kind は除外） | warning    |
| **drift**（ドリフト疑い）  | 上流ノードの最終コミット時刻が下流より新しい         | warning    |

- **drift の時刻ソース**（H-3）: `git log -1 --format=%ct -- <path>`（最終コミット時刻）を用いる。
  未コミット（ワーキングツリーのみ）の場合はファイルシステム mtime にフォールバックする。
  git はファイル mtime を履歴保持しないため、必ずコミット時刻 or 内容で判定する。
  ノードごとに `git status` / `git log` を個別起動すると 1,000 ノード規模で著しく
  遅いため、`batch_commit_times()` が `git status --porcelain -z`（dirty 判定）と
  `git log -z --name-only`（コミット時刻）をそれぞれ 1 回にまとめて実行する
  （判定規約は単発の `commit_time()` と同一。Issue #98 レビュー対応）。`git log` は
  `-z` で NUL 区切り取得することで `core.quotePath`（既定 true）による非 ASCII
  パスの引用（8 進エスケープ）を回避する（引用されたままだと `rel_paths` の
  キーと一致せず、クリーンな追跡ファイルでも常に mtime フォールバックへ落ちる）。
  `--root` が git リポジトリルート以外（サブディレクトリ）を指す場合は、
  `git rev-parse --show-prefix` で得た prefix を使って `git status` / `git log` が
  返すリポジトリルート相対パスを `--root` 相対へ正規化してから突き合わせる。
  `git status` はリポジトリ全体（`--root` の外を含む）の dirty パスを返すため、
  prefix 配下に無いパスは正規化せず破棄する（破棄せず素通りさせると、prefix 外の
  dirty ファイルが `--root` 内ノードと偶然同じ相対名を持った場合に、clean な
  ノードまで誤って dirty 扱いされてしまう）。部分履歴 clone（shallow clone 等）
  では、対象パスの無制限 `git log` が最新 timestamp を出力した後、古い履歴
  （欠けた tree）の走査中に nonzero で終了することがある。`_log_commit_times()`
  は stdout が空でなければ nonzero 終了でも取得できた分の timestamp を使う
  （0 件扱いで mtime へ全面フォールバックしない）。
- **missing_frontmatter** は Phase 1 では warning。将来 essential 運用が定着したら error 昇格を検討。
- drift は「上流を変えたのに下流が追従していないかもしれない」という**素朴な Amber 相当**。
  信頼度スコアによる本格的な impact 分析は Phase 2。

### 4.5.1 impact 分析（信頼度3帯域 / Issue #94）

`impact` は変更 diff から下流ドキュメントへの影響を **Green（自動更新可）/ Amber（要確認）/
Gray（参考）** に分類する。Phase 1 の素朴な drift（コミット時刻比較）を、宣言された依存関係を
証拠とした信頼度スコアへ発展させたもの。

```bash
codd impact --diff <ref> [--json]   # 既定 ref = HEAD
```

**手順:**

1. `git diff --name-status <ref>` で変更/削除ファイルを取得し、frontmatter の `node_id` にマップ。
2. `depends_on` の逆引き（`incoming`）を単純パスで辿り、変更ノードに依存する下流を列挙
   （サイクル安全・`max_hops` で打ち切り）。
3. 各経路を信頼度スコア化し、ノードごとに最良値を採って帯域へ分類。

**信頼度スコア:**

| 要素          | 反映                                                                          |
| ------------- | ----------------------------------------------------------------------------- |
| relation 強度 | `derives_from`/`refines`/`implements`=1.0、`supersedes`=0.6、`references`=0.3 |
| 距離減衰      | `decay^(hops-1)`（既定 decay=0.5）                                            |
| パススコア    | `min(経路上の重み) × decay^(hops-1)`（最弱リンクが信頼度を決める）            |
| ノードスコア  | 全経路・全起点の最大値（最良証拠が勝つ）                                      |
| 件数ボーナス  | amber 以上の経路を持つ複数起点が裏付ける場合のみ加点（水増し防止）            |

**帯域分類（補正込み）:**

- `score >= green_threshold`（0.8）→ Green / `>= amber_threshold`（0.4）→ Amber / それ未満 → Gray。
- **Corroboration rule**: Green は「直接の強依存（1 hop・強 relation）= 事実」か、
  「裏付け起点 ≥ `corroboration_min_origins`（2）」のみ許す。多段単一経路（推論的）は Amber 上限。
- **co_changed cap**: 下流ノード自身が同一 diff で変更済みなら Amber 上限にフラグ表示する
  （スコアは下げず、破壊的変更を Gray に隠さない）。
- 削除された上流ファイルは dangling 注意として別建てで報告する。削除済みパスは
  `Path.glob` が使えないため、scope glob を segment-aware な正規表現へ変換した
  `_scope_pattern_to_regex` で判定する。`*`/`**`/`?` に加えて文字クラス（`[seq]` /
  `[!seq]`）も `Path.glob` と同じ意味に解釈し（通常走査の `collect_files` と削除後
  判定とで glob 解釈が食い違わないようにする）、閉じ `]` が無い場合は fnmatch と
  同様リテラル `[` として扱う。不正な文字範囲（`lo > hi`。例: `[z-a]`、`[ab-a]`）は
  `_char_class_to_regex` が CPython `fnmatch.translate()` と同一のアルゴリズムで
  正規化し、範囲部分だけを除去して他の有効なリテラル文字は保持する（`[ab-a]` は
  リテラル `a` にマッチし、クラス全体が空になる `[z-a]` のみ「常に非マッチ」）。
  文字クラス以外の未知の要因による `re.error` はクラッシュせず「常に非マッチ」の
  防御的フォールバックへ倒す。`../proj/src/**/*.py`（root == proj）のように root 外へ
  出て同じ root 内へ戻るパターンも、通常走査側（`_glob_relpaths()`）と同じ
  レキシカルな正規化（`os.path.normpath`、ファイルシステムへはアクセスしない）を
  適用してから判定する。正規化しないと削除済みパスの判定で通常パターンと別名扱い
  になり、scan と impact 判定の解釈が食い違う。`src/**.py` のような不正な再帰
  glob（`**` がパスセグメント全体を占めていない）による `Path.glob()` の
  `ValueError` は `_glob_relpaths()` が捕捉し、パターンを含む分かりやすい
  `ValueError` へ変換して再送出する。`main()` は config 読み込みだけでなく
  scan/validate/impact のコマンド実行全体を同じ try/except で包むため、この
  ValueError も `[codd] ERROR: ...`（非ゼロ終了）として整形される。
- symlink はスコープ内の走査（scan、working tree）でも、ref 側の旧内容取得
  （`git show <ref>:<path>`）でも一貫して dereference する。scan は working tree の
  symlink をリンク先の内容ごと登録するため、リンク先だけを変更した場合も
  `git diff` はリンク先のパスを返す。node.path（symlink 自身）しか見ないと変更が
  検出されないため、リンク先の root 相対パスも `changed_paths` の突合対象に加える。
  `alias.py -> links/current.py -> v1.py` のような中継 symlink チェーンでは、
  `_symlink_target_relpath()` が 1 hop 先のみを `os.readlink` で解決し、
  `_symlink_chain_relpaths()` がそれを繰り返し呼んでチェーン上の全 hop
  （`links/current.py` も）を突合対象へ加える（`Path.resolve()` で最終ターゲット
  だけを一気に解決すると、中間リンクだけの retarget を見逃す）。
  一方 `git show <ref>:<path>` は symlink blob の中身（リンク先パス文字列）を
  そのまま返してしまうため、`git ls-tree` でモード（`120000` = symlink）を判定
  しながらリンク先を辿ってから内容を取得する（辿らないと、削除された symlink
  ノードの旧 node_id を frontmatter として復元できず、dangling 化を見逃す）。ref
  側のリンク先文字列は `strip()` せずそのまま解決する（先頭/末尾に有意な空白を
  含むファイル名を指す symlink でも working tree と同じパスに解決するため）。
  code_scope 内の symlink で、alias 自体は変更されずリンク先だけが削除されて
  壊れた symlink になった場合（`git diff` の changed/deleted どちらにも alias
  自身は現れない）は `_broken_code_symlink_relpaths()` が検出し、ref 側の旧内容
  から node_id を回収して deleted_upstream の検出対象に含める。
- code_scope 内のコードファイルは、削除だけでなく **ファイルが残ったまま**
  `codd:` 注釈の削除や node_id 変更で旧コードノードが消失するケースも dangling
  注意として同じ集合に含める。`changed_paths`（削除されていない変更ファイル）の
  code_scope 該当分についても ref 時点の内容から旧注釈を再抽出し、現グラフから
  消えていれば報告する（`compute_impact_result`）。
- drift 検査（`batch_commit_times` / `_log_commit_times`）の一括 `git log` は
  各ノードパスを `:(literal)` を前置した pathspec として渡す。前置しないと、
  `:(bad.md` のような git pathspec magic 構文と衝突する正当なファイル名が1つ
  あるだけで `fatal: Invalid pathspec magic` により一括呼び出し全体が失敗し、
  同じバッチ内の他の clean node まで commit time ではなく working-tree mtime で
  比較されてしまう（drift 判定が不安定になる）。

**設計判断（codd-dev 比較 / ADR-026 D3）:** CODD は依存宣言を frontmatter に限定するため、証拠源は
relation 種別とグラフ距離のみ。codd-dev の Noisy-OR・エビデンス種別分類（static/inferred/human 等）は
コード静的解析由来の多様な証拠を確率合成する設計であり、本レイヤーには証拠源が無く適用しない。
Corroboration rule と testimony cap（co_changed）の思想のみ借用した。`must_review` エッジは将来フェーズ。

### 4.6 config（codd.yaml）

```yaml
# .claude/config/codd/codd.yaml
scope:
  include:
    - "docs/**/*.md"
    - ".claude/rules/*.md"
    - ".claude/Plans.md"
    - "templates/context/*.md"
  exclude:
    - "docs/adr/_template.md"
    - "docs/adr/DECISIONS.md"
# code_scope は opt-in（既定 include: []）。追加すると code/test ノードを抽出する（4.3.1）。
code_scope:
  include: []
  exclude:
    - "**/__pycache__/**/*" # 末尾は /**/* にする（Python 3.12 の Path.glob 対策、4.3.1）
    - "**/node_modules/**/*"
    - "**/.venv/**/*"
inline_confidence: 0.7 # code_scope 由来 depends_on の既定信頼度（4.3.1）
kinds: [requirement, design, adr, plan, rule, instruction, code, test] # scope と整合（H-4）
relations: [derives_from, refines, implements, references, supersedes]
roots: [requirement, instruction] # 被参照ゼロを許容する最上流 kind
graph_store:
  format: jsonl # 小〜中規模。規模拡大時に SQLite を検討（L-3 決定）
  path: ".claude/codd/graph.jsonl"
checks:
  dangling: error
  duplicate: error
  cycle: error
  unknown: error
  malformed_annotation: error # code_scope の値無し依存注釈（4.3.1 / 4.5）
  missing_frontmatter: warning
  orphan: warning
  drift: warning
```

導入先固有の上書きは `codd.local.yaml`（`config-loading` ルール準拠、同期対象外）。

`scope.include` / `code_scope.include` の型不正（数値・非文字列要素混入）や `impact.*` への
bool 混入は config ロード時に `ValueError` / `TypeError` になる。`scope` / `code_scope` /
`graph_store` / `impact` / `checks` / `impact.relation_weights` に mapping 以外（文字列・
リスト等）を書いた場合も同様に `ValueError` になる（`.get()` / `.items()` の `AttributeError`
を素通りさせない）。`impact.max_hops` / `impact.corroboration_min_origins` に `.inf` /
`.nan` のような非有限値を書いた場合も `int()` の `OverflowError`（`ValueError` のサブクラス
ではない）を素通りさせず `ValueError` にする。`scope.include` 等に空文字列（`""`）を書いた
場合は「対象なし」を表す既存設定との後方互換のため空リストとして扱う（`[""]` にはしない。
`Path.glob("")` の `ValueError` を招くため）。CLI エントリポイント（`main()`）はこれらを
トレースバックとして漏らさず捕捉し、`[codd] ERROR: ...` を stderr へ出力して非ゼロ終了する
（scan/graph/validate/impact 全コマンド共通）。

### 4.7 既存スキルとの統合

ドキュメント生成スキルが CODD フロントマターを**自動で**出力するよう SKILL.md を改修する。
codd は essential（常時有効）のため、**条件分岐は不要**で常にフロントマターを生成してよい（C-1 解決）。

| スキル           | 改修内容                                                                                     |
| ---------------- | -------------------------------------------------------------------------------------------- |
| `design`         | Phase 1-3 の各成果物（ファイル）先頭に `codd:` ブロックを生成。`derives_from` で上流へリンク |
| `design-tracker` | ADR に `codd:` ブロックを付与。`関連:` を `depends_on`（references/supersedes）へ移行        |
| `task-state`     | Plans.md に `plan:` node_id を1つ付け、実装対象の `design:` ノードへ `implements` リンク     |

> 改修対象は facet composition のソース（`facets/compositions/skills/*.yaml` 等）。
> 生成物 `.agents/skills/*/SKILL.md` は直接編集しない（L-2）。

### 4.8 配布モデル（導入先への展開）

`packages/codd/` は **essential プリセット**に含め、`setup essential` で全導入先へ自動導入する
（C-1 / H-1 解決）。install 後、既存レールで以下が導入先 `.claude/` に展開される。

1. `config/codd.yaml` → `.claude/config/codd/codd.yaml`（SessionStart sync）
2. skill/rule → facet build で `.claude/skills/codd-*/`・`.agents/skills/`・`.claude/rules/`
3. scripts → `orchex run codd codd -- scan|validate` で実行可能
4. manifest hooks → `.claude/settings.local.json` に PostToolUse/PreToolUse 自動登録（**実装済み・
   Issue #95**。詳細は 4.8.1）

→ **導入先プロジェクトは essential セットアップだけで、`/design` 等が生成する
ドキュメントが CODD 管理下に入り、`/codd-validate` で整合性を検証できる。**

### 4.8.1 hook 自動配線（Issue #95）

`packages/codd/manifest.json` に以下の hooks を宣言し、既存の同期レール（sync_hooks）経由で
導入先 `.claude/settings.local.json` へ自動登録する（essential プリセットのため全導入先が対象）。

| hook スクリプト                | イベント    | matcher   | 役割                                       |
| ------------------------------ | ----------- | --------- | ------------------------------------------ |
| `codd-scan-postedit.py`        | PostToolUse | `Edit\|Write` | scope 内ファイル編集時に `scan` を実行し、graph を再構築する（常に非ブロック） |
| `codd-validate-precommit.py`   | PreToolUse  | `Bash`    | `git commit` を検出したら `validate` を実行し、警告またはブロックする |

両 hook とも manifest 上で `"timeout": 90`（秒）を宣言し、同期レール（`sync_hooks`）が
導入先 `.claude/settings.local.json` へ登録する際にこの値を反映する。`codd-validate-precommit.py`
は write-tree / rev-parse / checkout-index / 一時 index 構築 / `codd validate` の全 subprocess で
`HOOK_TIMEOUT_BUDGET_SECONDS`（75秒）の単一 deadline を共有する（Issue #338 反復3。詳細は
本節後半「共有 timeout budget」参照）。manifest 値より小さく取り、hook 自身の import 等の
オーバーヘッドとランナー側の余裕を確保する。

**pre-commit の代替方式**: 実 git hook（`.git/hooks/pre-commit`）を配布するのではなく、
PreToolUse (Bash) で `git commit` コマンドを検出するアプローチを採る。理由は次の3点。

1. 導入先が既に pre-commit framework 等を運用している場合との衝突を避ける
2. codd の uninstall 時に `.git/hooks/` を書き換えたままにしない（原状回復が容易）
3. 「導入先の commit フローを侵さない」という opt-in 方針と整合する

**既知の制限**: この方式がガードするのは **Claude Code セッション経由の `git commit`** のみ。
手動シェル操作・GUI Git クライアント・CI からの commit は対象外になる。これは上記「導入先の
commit フローを侵さない」という要件の裏返しであり、意図した制限として明記する。実 git hook の
配布機構が必要な場合は Out of Scope（2章）に切り出した別Issue（`codd-real-git-hook-distribution`）
で扱う。

**validate hook の判定基準**: `codd validate` の終了コードが **1** の場合のみ整合性エラー
として `warn`/`block` の分岐に流す。それ以外の非ゼロ終了（例: 設定エラーの exit 2）は
実行失敗とみなし、非ブロックで通過させる（hook が誤って commit を止めないようにするため）。

**対象 root の限定**: Bash コマンド中の `git -C <path> commit` が、現在の検証 root
（プロジェクトルート）以外を指す場合はガード対象外とする（skip）。`cd <path> && git commit`
のような複合コマンドは、root を実行時の cwd で近似判定する（既知の制限。`-C` のような
明示的な path 引数を持たないため厳密な root 解決はできない）。

**index スナップショット検証（Issue #338）**: `git commit` が実際にコミットするのは
working tree ではなく **git index** の内容である。hook は `git write-tree` で index の
妥当性を確認したうえで `git --work-tree=<tmp> checkout-index -a -f` により index の内容を
一時ディレクトリへ展開し、その一時ディレクトリに対して `codd validate` を実行する（実体の
working tree・index は一切変更しない）。これにより「壊れた依存を `git add` した後、同じ
ファイルを未ステージで修正する」ケースでも、実際にコミットされる内容（index）を正しく
検証できる。index スナップショットを構築できない場合（対象が git working tree でない、
index に unmerged エントリがある、subprocess の timeout/OSError 等）は、validate 実行自体の
失敗と同様に fail-safe で commit をブロックしない。

**スナップショットへの git コンテキスト伝播（Issue #338 反復2、レビュー High 対応）**:
一時ディレクトリは `.git` を持たないため、素朴に checkout-index するだけでは codd の
drift 検査（`_check_drift` / `batch_commit_times`）内の `git status` / `git log` が全て
失敗し、本来の commit 履歴ではなく checkout-index 実行時の mtime（ほぼ同時・パス順で
書き込まれるため実際の履歴と無関係）へ黙ってフォールバックしてしまう。これにより
「上流が下流より新しい」drift を見逃す（false negative）副作用があった。既定の drift
level は warning のため通常は commit をブロックしないが、`checks.drift: error` に昇格した
導入先では判定が不安定になりうる。

対応方針として、次の2案を比較検討した:

1. **（採用）実リポジトリの git コンテキストをスナップショットへ伝播する**: `git rev-parse
   --path-format=absolute --git-dir` で解決した絶対 git-dir を `GIT_DIR`、一時ディレクトリを
   `GIT_WORK_TREE` として `codd validate` サブプロセスの環境変数に渡す。checkout-index の
   内容は index そのもののコピーなので、この状態で `git status` を実行すると
   worktree（スナップショット）と index の差分は常にクリーンになり、index と HEAD の差分
   （= まだ commit されていない staged 変更）だけが dirty として残る。結果として、
   drift 検査は「clean な（= 既に commit 済みの）ノードは実際の commit 履歴」「staged
   変更のあるノードは checkout 時刻（≒ now、これから commit される内容の意味論として妥当）」
   という working tree 直接検証時と同等の判定基準を維持できる。git-dir を解決できない場合は
   スナップショット構築自体の失敗として扱い、fail-safe で commit をブロックしない。
2. **（不採用）スナップショット検証時は drift 検査を無効化する**: 実装は単純だが、
   `checks.drift` を `error` に昇格した導入先で drift 検査が index スナップショット化
   （Issue #338）の副作用として黙って無効化されるのは、ユーザーの明示的な設定意図に反する。
   drift 以外の検査（dangling / cycle 等）は index の内容だけで完結するため、drift だけを
   特別扱いで無効化する非対称性も複雑さを増す。

案1を採用した理由は、実装コストが小さい（`codd_common.py` / `codd.py` 側の変更は不要。
環境変数の伝播だけで完結する）ことに加え、drift 検査の精度を working tree 直接検証と
同等に保てるため。

**ambient GIT\_\* 環境変数のサニタイズ**: hook が起動する git / `codd validate`
サブプロセスは `hook_common.sanitized_git_env()` で ambient な `GIT_DIR`/`GIT_WORK_TREE`
等を除去した環境変数を使う。外側の実行環境（例: loop-harness の ephemeral git isolation）が
既に `GIT_DIR`/`GIT_WORK_TREE` を設定しているケースでこれを継承すると、`write-tree` /
`checkout-index` が検証対象のプロジェクトとは無関係なリポジトリを誤って参照してしまう
（cwd より環境変数が優先されるため）。この関数は Issue #95 由来の共通ユーティリティで、
`fail-logs` パッケージの `capture-failures.py` でも同じ目的に使われている（既存の確立された
パターンへの追従）。

**複合コマンドの既知の制限（Issue #338）**: PreToolUse hook は Bash コマンドが実行される
**前**に動作するため、`generate-docs && git add docs && git commit` のような複合コマンドで
は、hook 実行時点の index に同一コマンド内の先行ステップ（`git add` 等）の結果はまだ
反映されていない。これは index スナップショット化によっても解消できない、PreToolUse
アーキテクチャそのものに起因する制限である。hook は `git ... commit` 呼び出しの直前に
shell 連結演算子（`&&` / `;` / `||` / `|`）を検出した場合、warn/block メッセージに
この制限を注記する（ブロックはしない。検証対象が「hook 実行時点の index」であることの
明示に留める）。この制限を本質的に解消するには、実 git hook（`.git/hooks/pre-commit`）の
配布機構が必要であり、それは 4.8.1 冒頭で述べた通り Out of Scope
（`codd-real-git-hook-distribution`）とする。

**実効設定の materialize（Issue #338 反復3、bot レビュー P1 対応）**: `codd.local.yaml` は
`config-loading` ルールにより同期対象外の未追跡ファイルとして置かれる運用が通常であり、
`git checkout-index` は未追跡ファイルを展開しない。そのため index スナップショット上で
起動される `codd validate` は base 設定（`codd.yaml`）だけを再ロードしてしまい、外側の
`main()` が実 working tree の local override から読んだ block/off モードと、実際の検査に
使われる `scope`/`checks.*` が食い違う（正当な commit の誤ブロック、または必要な error の
見逃し）。これを避けるため、`_run_validate` は実 root の `.claude/config/codd/codd.yaml` と
（存在すれば）`codd.local.yaml` を snapshot 側の対応するパスへ明示的にコピーしてから
`codd validate` を実行する。

**モノレポ（サブディレクトリ project root）対応（Issue #338 反復3、bot レビュー P1 対応）**:
`checkout-index -a` は index 全体（= リポジトリ全体）を snapshot_dir へ書き出すため、
project root がリポジトリ直下でない構成（例: `/repo/apps/foo`）では `snapshot_dir` 直下では
なく `snapshot_dir/<prefix>` に project が存在する。`git rev-parse --show-prefix` で
prefix を解決し、`codd validate` の cwd をそこに合わせる（`GIT_WORK_TREE` は snapshot_dir の
ままでよい。checkout 先のパスは常に repo root 基準のため）。prefix を解決できない場合は
スナップショット構築自体の失敗として扱い、fail-safe で commit をブロックしない。

**`git commit -a/--all` の候補ツリー再現（Issue #338 反復3、bot レビュー P1 対応）**:
`-a`/`--all` は hook 実行後に working tree の追跡ファイル変更を index へ取り込んでから
commit するため、現在の index をそのまま検証するだけでは実際の commit tree と一致しない
（`git commit -am` で壊れた文書がすり抜ける）。`-a`/`--all` を検出した場合は、実 index を
コピーした一時 index に対して `git add -u`（追跡済みファイルの変更・削除を全てステージ。
`git commit -a` と同じ意味論）を適用し、その候補 index を検証する（実 index・実 working
tree は一切変更しない）。`--include`/`--only`/`-p`/`--patch`/`-i`/`--interactive`/pathspec
指定は正確な再現が困難なため候補ツリー再現を行わず、既存の複合コマンド注記と同じ枠組みで
「この形式では hook 実行時点の index を検証しており、実際の commit tree と異なる可能性が
あります」旨を warn/block メッセージに注記する（ブロック判定自体は変えない）。

**共有 timeout budget（Issue #338 反復3、bot レビュー P2 対応）**: write-tree / rev-parse /
checkout-index / 一時 index 構築 / `codd validate` の全 subprocess をそれぞれ独立した
timeout（旧: 30秒 / 60秒）で管理していたため、合計上限（150秒）が manifest.json の
PreToolUse timeout（90秒）を上回っていた。外側の runner が hook 全体を先に打ち切ると、
意図した `TimeoutExpired` の fail-safe や `finally` の一時ディレクトリ削除に到達しない
おそれがある。これを解消するため、全 subprocess で単一の `_Deadline`
（`HOOK_TIMEOUT_BUDGET_SECONDS = 75`秒）を共有し、manifest timeout 内に収める。

**`GIT_OPTIONAL_LOCKS=0`（Issue #338 反復3、bot レビュー P2 対応）**: 実 `GIT_DIR` と一時
`GIT_WORK_TREE` の組み合わせで `codd validate` サブプロセス内の drift 検査（`git status` /
`git log`）が走ると、Git が実 index の stat cache を refresh して書き戻すことがある（実
working tree・index は一切変更しない設計方針に反し、`index.lock` 競合も起こしうる）。これを
防ぐため `codd validate` サブプロセスの env に `GIT_OPTIONAL_LOCKS=0` を渡す。

**既知の制限（Issue #338 反復3、非採用）**: 以下は本反復では実装しない（別 Issue で扱う）。

- root 内の絶対パス symlink を snapshot 側で再配置すること（checkout-index は symlink の
  ターゲットパスをそのまま書き出すため、絶対パスの symlink は snapshot 内で正しく解決
  できない場合がある）
- `git commit` が明示的に `GIT_INDEX_FILE` で alternate index を指定するケースへの対応
- scope 外ファイルの checkout filter 失敗による検証全体の無効化への対応（`checkout-index`
  が一部ファイルの書き出しに失敗しても、hook は現状それを検知しない）
- index の gitlink（submodule）は `checkout-index -a` で参照先 commit の内容が展開されず
  空ディレクトリになるため、submodule 配下の CoDD ノードは検証対象から消える。
  superproject から submodule ノードへの依存は false dangling になり、submodule 内だけの
  不整合は見逃す（Issue #342 で追跡）

**反復4（PR #339 2巡目 bot レビュー対応）**: 以下を修正した。

- **config materialize の symlink 非追従化**: index 側の config が
  `checkout-index` 展開後に snapshot 外への symlink（working tree 側は通常ファイルへ
  戻っているケース等）である場合、`shutil.copy2` はこの symlink を辿ってリンク先の
  任意の書き込み可能ファイルを上書きしてしまう。`_materialize_config` はコピー先を
  必ず一度削除してから新規ファイルとして作成し（`O_CREAT | O_EXCL | O_NOFOLLOW`）、
  symlink 追従を物理的に不可能にする。あわせて書き込み先が snapshot 境界内に留まる
  ことも検証する。
- **候補 index の permission と不変性**: 一時 index は `shutil.copyfile`
  （メタデータを複製しない）＋明示 `chmod(0o600)` で作成し、実 index の 0644
  permission を引き継がない。また `git write-tree` は実 index に直接実行すると
  cache-tree extension が実 index へ書き戻されうるため、実 index・`-a/--all` 候補
  index いずれの場合も専用のコピー（候補 index）に対して `write-tree` /
  `checkout-index` を実行する。`git add -u` / `write-tree` は内部で index を
  tmp ファイル作成 + rename により書き直すため、その都度 chmod を再適用して
  0600 を維持する。
- **候補 index の validate への伝播**: 上記の候補 index は `codd validate`
  サブプロセスにも `GIT_INDEX_FILE` として渡し、validate 完了後に削除する。
  渡さないと drift 検査の `git status` が候補 snapshot を stale な実 index と
  比較してしまい、`git commit -a` で「upstream を stage 後に working tree だけ
  HEAD 内容へ戻す」ような、実質的に変更なしの commit を誤って drift block しうる。
- **checkout 順に依存しない drift 判定**: `checkout-index` はパス辞書順に書き出す
  ため、同一変更で複数ノードを同時に stage すると、drift 検査の mtime フォール
  バックが書き込み順を「新旧」として誤解釈しうる。checkout 直後に snapshot 内の
  全ファイルへ共通の prospective timestamp を与え、この artifact を解消する。
- **repo prefix の空白保持**: `_resolve_repo_prefix` は `git rev-parse
  --show-prefix` の出力から末尾改行のみを除去する（`.strip()` は project root
  ディレクトリ名の有効な先頭空白まで削ってしまう）。
- **snapshot cleanup の確実化**: `_materialize_config` 呼び出しから `codd
  validate` 実行までを単一の `finally` で包み、途中で例外（ENOSPC / permission /
  I/O error 等）が発生しても snapshot・候補 index が `/tmp` に残留しないようにする。
- **skip-worktree エントリの展開**: `checkout-index` に
  `--ignore-skip-worktree-bits` を付け、sparse checkout で skip-worktree bit が
  付いたエントリも実際の commit tree 通りに snapshot へ展開する。
- **commit 引数分類の精度向上**: `_classify_commit_invocation` は、値を取る
  短縮オプション（`-m`/`-F`/`-c`/`-C`/`-t` は次トークンも値として消費しうる、
  `-u` は attached value のみ）に到達した時点で結合形の走査を打ち切り、以降を
  attached value として扱う（`-amfix` の value 部分 `"fix"` に含まれる `i` を
  `-i`(interactive) と誤認しない、`-ma`（`-m` の attached value `"a"`）を
  `--all` と誤認しない）。`--pathspec-from-file` は候補ツリー再現が困難なモード
  として分類する。

**反復5（Issue #338、PR #339 3巡目 bot レビュー対応）**: 以下を修正した。

- **config 親ディレクトリ作成の境界検証**: 反復4の境界検証は config ファイルの
  書き込み時には機能していたが、その前の `Path.mkdir(parents=True)` は祖先 symlink を
  辿り、snapshot 外へ `config/codd` を作成しえた。`_safe_mkdir_within` は snapshot root
  から各 component を `Path.is_symlink()` で検査し、一段ずつ作成してから
  `_safe_copy_config` によるファイル書き込みへ進む。
- **snapshot 一時ディレクトリ作成失敗時の cleanup**: `_build_index_snapshot` は
  `tempfile.mkdtemp` の `OSError` を fail-safe の失敗結果へ収束させ、先に作成済みの
  候補 index を削除する。
- **mtime 正規化の deadline 適用**: `_normalize_snapshot_mtimes` も subprocess 群と同じ
  `_Deadline` を共有し、予算切れ時は stderr に警告して残りの正規化を打ち切る。
  これにより外側の hook timeout 前に snapshot・候補 index の cleanup へ進める。

**反復6（Issue #338、PR #339 3巡目 bot レビュー追加指摘対応）**: 以下を修正した。

- **`--trailer` の値を pathspec と誤認しない**: `_classify_commit_invocation` の値を取る
  long option テーブルに `--trailer` を追加した。未対応のままだと `git commit -a --trailer
  "Acked-by: dev" -m x` の値がパススペック指定と誤認され、`has_unsupported=True` となって
  `-a` 候補ツリー再現（`simulate_commit_all`）が無効化され、実際には `-a` で取り込まれる
  未ステージの追跡済み文書が古い index だけで検査されてしまっていた（block モードでも
  不整合を含む commit が通りうる）。
- **後置 `--no-all` で all 判定を正しく解除**: `git commit -h` の `-a, --[no-]all` の
  仕様どおり、後に現れたオプションが有効になるよう `--no-all` トークンで `has_all` を
  `False` へ戻すようにした。未対応のままだと `git commit -a --no-all` のように working
  tree の変更を commit 対象から除外する呼び出しでも `-a` 候補ツリーが構築され、実際には
  commit されない未ステージ文書まで検証対象に含めて正当な commit を誤って block しうる。
- **`_build_commit_all_index_file` の copy 失敗時の cleanup**: `mkstemp` 成功後の
  `shutil.copyfile` / `chmod` を try/except で囲み、ENOSPC・quota・権限エラー等での失敗時も
  `_prepare_candidate_index` と同じ fail-safe 方針で診断を返しつつ一時ファイルを削除する
  ようにした（直前の反復5で塞いだのは `tempfile.mkdtemp` 経路のみで、この copy 経路は
  未対応のまま `/tmp/codd-commit-a-index-*` が残留しえた）。

**反復7（Issue #338、PR #339 4巡目 bot レビュー対応）**: 以下を修正した。

- **`-S<keyid>` の attached value を `-a`/`--all` と誤認しない**: `_classify_commit_invocation`
  の値を取る短縮オプション文字（`_COMMIT_VALUE_SHORT_CHARS`）に `-S`（`--gpg-sign`）を
  追加した。未対応のままだと `git commit -Sabc1234 -m msg` の GPG keyid（16 進表記が一般的で
  `a` を含みやすい）中の `a` を独立した `-a` フラグと誤認し、`simulate_commit_all=True` として
  未ステージの追跡ファイル変更まで候補ツリーへ誤って含め、実際には commit されない変更で
  block してしまっていた（`-amfix`/`-ma` で修正済みの欠陥と同じクラス）。`-S` は
  `-u` と同様 attached optional value のみを取るため、`_COMMIT_NEXT_TOKEN_VALUE_SHORT_CHARS`
  には追加しない（`git commit -S abc` の `abc` は keyid ではなく pathspec 扱いになるのが
  git の挙動のため、次トークンを keyid として消費してはならない）。
- **config コピー書き込み失敗時の空ファイル残留防止**: `_copy_no_follow` は
  `os.open(O_CREAT | O_EXCL | O_NOFOLLOW)` 成功後の書き込み（`src.read_bytes()` または
  `dest` への write）が失敗した場合、作成済みの 0 バイト `dest` を削除してから `False` を
  返すようにした。未対応のままだと、呼び出し元の `_safe_copy_config` は警告のみで継続する
  ため、snapshot 上に空の `codd.yaml`（または `codd.local.yaml`）が残り、`codd validate`
  がその空設定を実 root の設定とは異なる「設定あり」として読み込み、判定がずれる可能性が
  あった。削除後は snapshot 側にファイルが存在しない状態になり「設定不在」として扱われる。

**既知の制限（Issue #338、追跡中）**: hook プロセス自身の起動コマンド
（`scripts/lib/hook_utils.py` が生成する `python3 "$AI_ORCHESTRA_DIR/..."`）は
`PATH` 上の `python3` に依存している。4.8.1 前半で述べた「`codd` サブプロセスは
`sys.executable` で起動する」対応（Issue #338、EV-71）は hook プロセスが起動
できた**後**にのみ効果があり、`PATH` 上の `python3` が壊れている環境では hook
本体自体が起動できず、この対応の効果に到達しない。hook 起動コマンド自体を
`PATH` 非依存にする対応は本設計の対象外とし、**Issue #343** で別途追跡する。

**二段構えの opt-in**: hook の「登録」は essential プリセットで全導入先に自動展開されるが、
「実動作」は `codd.yaml` の `hooks:` セクションで制御する（config キーは 4.6 の `checks` 等と同じ
`.claude/config/codd/codd.yaml` 配下）。

| キー                       | 型                            | 既定値  | 意味                                                                 |
| -------------------------- | ------------------------------ | ------- | ---------------------------------------------------------------------- |
| `hooks.scan_on_edit`       | bool                            | `false` | scope 内ファイル編集時に `scan` で graph を再構築するか。常に非ブロック |
| `hooks.validate_on_commit` | `"off"` / `warn` / `block`      | `warn`  | `warn` は additionalContext で警告表示のみ。`block` は validate error 検出時に commit を止める（exit 2） |

`hooks.validate_on_commit` の bare `off` は YAML 1.1 仕様上 boolean `False` としてパースされるが、
`normalize_check_level`（`codd-frontmatter-policy.md` と同じ正規化関数）により大文字小文字違いも
含めて `"off"` へ正規化される（`checks.*` の検査レベルと同じ扱い）。

**fail-safe**: codd 未初期化（`codd.yaml` が存在しない）または `enabled: false` のプロジェクトでは、
両 hook とも即座に no-op として終了する。実行時例外が発生した場合も exit 0 でフェイルセーフに倒し、
hook 導入がホスト側の commit/edit フローをブロックしないことを優先する。

### 4.9 ドッグフーディング

AI Orchestra 自身の `.claude/orchestra.json` に `codd` を追加し、最初の導入先とする。
本設計ドキュメント自体が `design:codd-coherence-layer` ノードとしてグラフに載る（フロントマター付与済み）。

---

## 5. フェーズ計画（概要）

詳細タスクは `.claude/Plans.md` を SSOT とする。

- **Phase 1**: フロントマター規約 + `packages/codd`（scan/validate）+ essential 化 + skill 改修 + ドッグフード
- **Phase 2**: impact 分析（Green/Amber/Gray）**実装済み（Issue #94）** + hook 自動配線（PostToolUse scan /
  pre-commit validate 代替）**実装済み（Issue #95 / 4.8.1）**
- **Phase 3**: コード⇔ドキュメントトレース（opt-in・**実装済み（Issue #98 / 4.3.1）**）+ CI verdict（別Issue）

---

## 6. リスクと対策

| リスク                                           | 対策                                                                        |
| ------------------------------------------------ | --------------------------------------------------------------------------- |
| フロントマターのプロパティ不足                   | 5プロパティで全検査が成立することを 4.5 で確認済み。不足時は後方互換で追加  |
| 既存ドキュメントへのフロントマター一括付与コスト | Phase 1 は対象を docs/・.claude/rules/・Plans.md・templates/context/ に限定 |
| drift 検査の誤検知                               | warning 止まり。コミット時刻ベースと明示し、本格判定は Phase 2              |
| essential 化による全導入先への影響               | hook の**登録**は essential で自動展開されるが、**実動作**は `codd.yaml` の `hooks.scan_on_edit`（既定 false）/ `hooks.validate_on_commit`（既定 warn）による二段構え opt-in で制御（Issue #95 / 4.8.1） |
| frontmatter parser の誤検出                      | 先頭ブロックのみ読む実装に限定（M-1）                                       |

---

## 7. 決定事項

- **D1**: codd-dev のコードは使わず、思想のみ借りて独自実装する。
- **D2**: 整合性レイヤーは独立パッケージ `packages/codd/` として実装する。
- **D3**: 依存宣言の正本はドキュメント内フロントマター（`codd:` ブロック）。外部 doc_links.yaml は作らない。
- **D4**: Phase 1 は scan/validate まで。impact 分析・hook 配線・CI・コード⇔ドキュメントは別Issue。
- **D5**: 整合性管理の最重要対象は「導入先プロジェクトが生成するドキュメント」。配布前提で設計する。
- **D6**: codd を essential プリセットに追加し常時有効とする（C-1 解決。facet 条件付きオーバーレイは作らない）。
- **D7**: 1ファイル=1ノード。集約ファイル内の FT-xxx 等の細粒度ノード化は別Issue（C-2 解決）。
- **D8**: graph 保存形式は JSONL（`.claude/codd/graph.jsonl`）。規模拡大時に SQLite を検討（L-3）。
- **D9**（Issue #98）: コード⇔ドキュメントのトレーサビリティは、doc frontmatter と同じ 1行注釈
  ではなく `codd:<key> <value>` の軽量記法を採用し、`code_scope.include`（既定空）による opt-in
  スコープとする。全コードへのフロントマター強制は行わず、注釈の有無で confidence
  （既定 1.0 vs 0.7）を差別化することで、doc の確定宣言とコードの軽量宣言を区別する。

---

## 8. 未解決事項

- ADR の `関連:` 行から `depends_on` への移行を一括変換するか段階移行か（実装時に確定）
- essential 化した場合の既存導入先への frontmatter 一括バックフィル手順（Phase 1 後半で詰める）
