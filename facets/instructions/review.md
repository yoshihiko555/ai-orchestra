# Review Skill

**変更内容に応じてレビュアーをスマート選定し、事前収集コンテキストで効率的にレビューを実行する。**

## Usage

```
/review              # ベースライン(code) + スマート選定（デフォルト）
/review all          # 全 6 レビュアー並列実行（旧デフォルト）
/review code         # コードレビューのみ
/review security     # セキュリティレビューのみ
/review performance  # パフォーマンスレビューのみ
/review spec         # 仕様整合性レビューのみ
/review architecture # アーキテクチャレビューのみ
/review ux           # UX/アクセシビリティレビューのみ
/review design       # 設計系レビュー（spec + architecture）
/review impl         # 実装系レビュー（code + security + performance）
```

## Reviewers

| Reviewer | Focus |
|----------|-------|
| `code-reviewer` | 可読性、保守性、バグ検出 |
| `security-reviewer` | 脆弱性、権限、情報漏洩 |
| `performance-reviewer` | 計算量、I/O、最適化 |
| `spec-reviewer` | 設計書との整合性 |
| `architecture-reviewer` | アーキテクチャ妥当性 |
| `ux-reviewer` | UX、アクセシビリティ |

---

## Execution: Smart Review (`/review` without arguments)

### Phase 0: コンテキスト事前収集

オーケストレーターが 1 回だけコンテキストを収集する。サブエージェント内での重複読み込みを排除する。

1. `git diff --stat` で変更ファイル一覧を取得
2. `git diff` で全差分を取得
3. 変更ファイルのソースコードを収集:
   - **500 行以下**: ファイル全文を Read
   - **500 行超**: 変更ハンク + 前後 30 行のみ（diff から特定）
4. 収集結果を変数に保持（後続フェーズで注入）

```
# 実行例
diff_stat = git diff --stat の結果
diff_full = git diff の結果
file_contexts = 各変更ファイルのソースコード（上記ルールで収集）
```

**ドキュメントのみ判定**: `.md` ファイルのみの変更 → 原則レビュースキップ（ユーザーに報告して終了）
- ただし仕様書・API ドキュメント（`spec/`, `api/`, `openapi` 等を含むパス）の `.md` 変更は `spec-reviewer` にフォールバック

### Phase 1: スマートレビュアー選定

3 段階のロジックでレビュアーを選定する。

#### Step 1: ベースライン

- ソースコード変更がある限り `code-reviewer` を必ず含める

#### Step 2: パスパターンマッチ

変更ファイルのパスからの専門レビュアー追加:

| パターン | 追加レビュアー |
|---------|--------------|
| `packages/core/`, `packages/*/hooks/`, フレームワーク基盤 | + architecture-reviewer |
| `auth`, `login`, `session`, `token`, `password`, `secret`, `permission` | + security-reviewer |
| `api/`, `routes/`, `endpoints/`, `graphql/`, `handler` | + security-reviewer |
| `db/`, `migration`, `schema`, `model`, `prisma`, `drizzle` | + performance-reviewer |
| `components/`, `pages/`, `views/`, `ui/`, `styles/`, `css`, `.tsx`, `.jsx` | + ux-reviewer |
| `config/`, `settings`, `.env`, `docker`, `infra/`, `terraform` | + security-reviewer |
| 仕様書・要件ドキュメントへのソースコード関連変更 | + spec-reviewer |
| 新モジュール/パッケージ作成（新ディレクトリ） | + architecture-reviewer |

#### Step 3: diff コンテンツスキャン

Phase 0 で収集した diff の**追加行（`+` プレフィックス）のみ**を対象にスキャンし、以下のシグナルが含まれる場合に対応するレビュアーを追加（コメント・文字列内の誤検知を軽減するため、削除行は無視する）:

| ドメイン | diff 内のシグナル例 | 追加レビュアー |
|---------|-------------------|--------------|
| セキュリティ | SQL (`SELECT`, `INSERT`, `.query(`, `.raw(`), 入力処理 (`request.body`, `req.params`, `JSON.parse`), 危険操作 (`eval(`, `exec(`, `subprocess`), 認証 (`password`, `token`, `jwt`, `hash`), ネットワーク (`http`, `fetch(`, `cors`, `cookie`) | + security-reviewer |
| パフォーマンス | DB クエリ (`SELECT`, `JOIN`, `findMany`), ループ (`for`, `while`, `forEach` + 大量データ), 非同期 (`Promise.all`, `concurrent`), キャッシュ (`cache`, `memo`, `redis`) | + performance-reviewer |
| UI/UX | コンポーネント (`className`, `style`, `css`), アクセシビリティ (`aria-`, `role=`, `tabIndex`, `alt=`), 状態 (`loading`, `error`, `empty`, `skeleton`) | + ux-reviewer |
| アーキテクチャ | 新規ファイル/ディレクトリ作成, 新しい依存関係の追加, クラス/インターフェース定義 (`class`, `interface`, `abstract`) | + architecture-reviewer |
| 仕様整合 | API コントラクト変更, スキーマ変更, OpenAPI/Swagger 定義 | + spec-reviewer |

**選定結果**: Step 2 と Step 3 の union（どちらかでマッチすれば追加、重複は 1 回のみ）

#### Step 4: 上限キャップ

union の結果が多すぎる場合、以下のルールで **最大 3 レビュアー**（code-reviewer 含む）に絞る:

1. `code-reviewer` は常に確定（ベースライン）
2. 残り枠（最大 2）を以下の優先順位で割り当て:
   - `security-reviewer` > `spec-reviewer` > `performance-reviewer` > `architecture-reviewer` > `ux-reviewer`
3. 同優先度の場合、パスパターン + コンテンツスキャン両方でマッチしたレビュアーを優先

**重要**: パスパターンにもコンテンツスキャンにもマッチしないファイルは、`code-reviewer`（ベースライン）が必ずレビューする。

### Phase 2: モデル選択

diff サイズとリスクシグナルに応じてモデルを選択:

| 条件 | モデル | Task パラメータ |
|------|--------|----------------|
| ≤ 100 行 かつ リスク override なし | sonnet | `model="sonnet"` を明示指定 |
| ≤ 100 行 かつ リスク override あり | config のモデル | `model` パラメータ省略 |
| > 100 行 | config のモデル | `model` パラメータ省略（フロントマター値を使用） |

diff サイズは `git diff` の実質変更行（`+`/`-` 行の合計、空行・コメントのみの行を除く）で判定する。

**リスク override**: 以下の条件に該当する場合、diff サイズが ≤ 100 行でも sonnet ダウングレードを適用しない:
- Phase 1 で `security-reviewer` が選定された（認証・脆弱性関連の変更）
- Phase 1 で `spec-reviewer` が選定された（API コントラクト・スキーマ変更）

**注意**: 現在の全レビュアーのフロントマターは `model: sonnet` のため、`model` 省略時も sonnet が使われる。
より高性能なモデルを大規模 diff に使用したい場合は、各エージェントのフロントマター `model` を変更すること。

### Phase 3: レビュアー起動

選定されたレビュアーを並列起動する。**事前収集コンテキストをプロンプトに注入**する。

```
Task(subagent_type="{reviewer}", model="{model_or_omit}", run_in_background=true, prompt="""
以下の変更をレビューしてください。

## 変更ファイル一覧
{diff_stat}

## 差分
{diff_full}

## ファイルコンテキスト
{file_contexts}

Tiered Output 形式（Critical/High/Medium/Low）で報告してください。
""")
```

**注意**: プロンプトには事前収集コンテキストが含まれるため、サブエージェント内で git diff や Read を再実行する必要はない。

### Phase 4: 集約・報告

全レビュアーの結果を重要度別に集約して報告する。

**重複指摘の統合**: 複数レビュアーが同一ファイル・同一箇所を指摘した場合:
- severity が最も高いものを採用し、他のレビュアー名を `[{reviewer1}, {reviewer2}]` で併記
- 異なる観点の指摘（例: security と performance）は別エントリとして残す

上記の Tiered Review Output Contract に定義された形式で出力する。

---

## Execution: Full Review (`/review all`)

全 6 レビュアーを並列起動する（旧 `/review` のデフォルト動作）。

1. Phase 0 のコンテキスト事前収集を実行
2. モデル選択を実行（Phase 2 と同じ）
3. 全 6 レビュアーを起動（スマート選定をスキップ）
4. Phase 4 の Tiered Output で集約

## Execution: Individual Review (`/review {type}`)

指定されたレビュアーのみ起動する。

1. Phase 0 のコンテキスト事前収集を実行
2. モデル選択を実行（Phase 2 と同じ）
3. 指定レビュアーを起動
4. 結果を報告

## Execution: Group Review (`/review impl`, `/review design`)

グループに含まれるレビュアーを起動する。

| グループ | レビュアー |
|---------|-----------|
| `impl` | code-reviewer + security-reviewer + performance-reviewer |
| `design` | spec-reviewer + architecture-reviewer |

1. Phase 0 のコンテキスト事前収集を実行
2. モデル選択を実行（Phase 2 と同じ）
3. グループ内レビュアーを並列起動
4. Phase 4 の Tiered Output で集約

---

## Tips

- デフォルト `/review` はスマート選定で 2-3 レビュアーに絞り、効率的にレビュー
- 全レビュアーが必要な場合は `/review all` を使用
- `/review impl` でクイック実装レビュー
- `/review design` は大規模リファクタリング前に推奨
- リリース前は `/review all` を推奨

## Quality Gate Rule（v2）

- 最終品質ゲートとして使う場合（推奨: 別セッション実行）
- `Critical` は必ず修正して再レビューする
- `High` は運用方針に応じて必須修正またはユーザー確認とする
- テストコード作成は `/tdd` の責務であり、`/review` の責務ではない
