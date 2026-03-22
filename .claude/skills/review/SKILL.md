---
name: review
description: |
  Run code reviews using specialized reviewer agents.
  Supports individual or batch review modes with smart reviewer selection.
metadata:
  short-description: Multi-agent code review (smart selection)
---

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

### Phase 4: 集約・報告（Tiered Output）

全レビュアーの結果を重要度別に集約して報告する。

**重複指摘の統合**: 複数レビュアーが同一ファイル・同一箇所を指摘した場合:
- severity が最も高いものを採用し、他のレビュアー名を `[{reviewer1}, {reviewer2}]` で併記
- 異なる観点の指摘（例: security と performance）は別エントリとして残す

```markdown
## Review Summary

**レビュアー**: {選定されたレビュアー一覧}
**変更ファイル**: {ファイル数} files, {追加行数} insertions(+), {削除行数} deletions(-)

### Critical ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**
  {問題の説明 + 影響 + 修正案}
  ```{lang}
  {コードスニペット}
  ```

### High ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**
  {問題の説明 + 修正案}

### Medium ({count})
- [{reviewer}] `{file}:{line}` - {1行サマリ}

### Low ({count})
- [{reviewer}] `{file}:{line}` - {1行サマリ}
```

**重要度の定義**（`/review` スキル固有。スキル内レビューフェーズの共通定義は `skill-review-policy.md` を参照）:

| 重要度 | 基準 | 対応 |
|--------|------|------|
| **Critical** | セキュリティ脆弱性、データ損失リスク、本番障害の可能性 | 自動修正（Phase 6） |
| **High** | バグの可能性、設計上の問題、パフォーマンス劣化 | 報告のみ |
| **Medium** | コード品質、可読性、軽微な改善 | 報告のみ |
| **Low** | スタイル、命名、コメント改善 | 報告のみ |

**詳細度**:
- **Critical / High**: 詳細な説明 + 影響範囲 + 修正案（コードスニペット付き）
- **Medium / Low**: 1行サマリのみ

### Phase 5: Pass/Fail 判定

Phase 4 の集約結果から通過判定を行う。

1. `cli-tools.yaml` の `review.auto_fix` を確認する（config-loading ルールに従い `.local.yaml` 上書きを適用。詳細は `config-loading.md` 参照）
2. `auto_fix: false` の場合 → Phase 4 の Review Summary を出力してスキル終了（従来動作）
3. `auto_fix: true` の場合 → 通過基準を `review.pass_threshold` から取得
4. 通過基準の評価:
   - `critical_zero`: Critical 指摘が 0 件で通過
5. 通過 → Final Report（PASSED）を出力してスキル終了
6. 不通過 → Phase 6 へ進む（ループ制御は Phase 7 の `while` で管理）

### Phase 6: Auto-Fix（自動修正）

Critical 指摘をサブエージェントで自動修正する。

1. Critical 指摘をファイルごとにグループ化
2. 各ファイルの拡張子から修正エージェントを決定（マッピングテーブル参照）
3. 修正エージェントをサブエージェントとして起動（並列可）

**修正エージェントマッピングテーブル**:

| 拡張子 | 修正エージェント |
|--------|----------------|
| `.py` | `backend-python-dev` |
| `.go` | `backend-go-dev` |
| `.tsx`, `.jsx` | `frontend-dev` |
| `.ts`, `.js` | `frontend-dev` |
| `.vue`, `.svelte` | `frontend-dev` |
| その他 | `general-purpose` |

修正エージェント起動パターン:

```
Task(subagent_type="{fix_agent}", prompt="""
以下の Critical 指摘を修正してください。

## 対象ファイル
{file_path}

## Critical 指摘一覧
{critical_issues のリスト（レビュアー名、行番号、問題の説明、修正案を含む）}

## コンテキスト
{Phase 0 で収集したファイルコンテキスト}

指摘の修正案に従い、コードを修正してください。
修正内容を簡潔に報告してください。
""")
```

**注意**:
- 修正エージェントの `agents.{name}.tool` は cli-tools.yaml の設定に従う
- High/Medium/Low 指摘は修正対象外（報告のみ）

### Phase 7: Re-Review Loop

修正完了後、再レビューを実行する。

1. ループカウンターをインクリメント
2. Phase 0 に戻り、新しい diff でコンテキストを再収集
3. Phase 1-4 を再実行（レビュアー選定も再実行）
4. Phase 5 で再び Pass/Fail 判定

ループ制御:

```
loop_count = 0
max_loops = cli-tools.yaml の review.max_loops（デフォルト: 3）

while loop_count < max_loops:
    Phase 0-4: レビュー実行
    Phase 5: 判定
    if passed:
        Final Report を出力して終了
    Phase 6: Auto-Fix
    loop_count += 1

# ループ上限到達
Final Report（残存 Critical 付き）を出力
```

#### Final Report フォーマット

ループ終了後（通過・上限到達どちらでも）に出力:

```markdown
## Review Loop Summary

**結果**: {PASSED | FAILED (max loops reached)}
**ループ回数**: {loop_count} / {max_loops}
**通過基準**: {pass_threshold}

### Loop History
| Loop | Critical | High | Medium | Low | Status |
|------|----------|------|--------|-----|--------|
| 1    | {n}      | {n}  | {n}    | {n} | {Fixed → Re-review / Passed} |
| 2    | {n}      | {n}  | {n}    | {n} | {Fixed → Re-review / Passed} |

### Remaining Issues (if FAILED)
{残存 Critical/High 指摘のリスト}

### Auto-Fix Summary
{各ループで行った修正の概要}
```

### 全モードへの適用

Phase 5-7 のループは以下の全モードに共通して適用される:

- Smart Review (`/review`)
- Full Review (`/review all`)
- Individual Review (`/review {type}`)
- Group Review (`/review impl`, `/review design`)

各モードの Phase 0-4 の後に Phase 5-7 が実行される。

---

## Execution: Full Review (`/review all`)

全 6 レビュアーを並列起動する（旧 `/review` のデフォルト動作）。

1. Phase 0 のコンテキスト事前収集を実行
2. モデル選択を実行（Phase 2 と同じ）
3. 全 6 レビュアーを起動（スマート選定をスキップ）
4. Phase 4 の Tiered Output で集約
5. Phase 5-7（Pass/Fail 判定 → Auto-Fix → Re-Review）を実行

## Execution: Individual Review (`/review {type}`)

指定されたレビュアーのみ起動する。

1. Phase 0 のコンテキスト事前収集を実行
2. モデル選択を実行（Phase 2 と同じ）
3. 指定レビュアーを起動
4. 結果を報告
5. Phase 5-7（Pass/Fail 判定 → Auto-Fix → Re-Review）を実行

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
5. Phase 5-7（Pass/Fail 判定 → Auto-Fix → Re-Review）を実行

---

## Tips

- デフォルト `/review` はスマート選定で 2-3 レビュアーに絞り、効率的にレビュー
- 全レビュアーが必要な場合は `/review all` を使用
- `/review impl` でクイック実装レビュー
- `/review design` は大規模リファクタリング前に推奨
- リリース前は `/review all` を推奨

## Quality Gate Rule（v3: Auto-Loop）

- `/review` はデフォルトで自動修正ループが有効
- `Critical` は自動修正 → 再レビューで解消を目指す
- `High` は報告のみ（自動修正対象外）
- ループ上限（`review.max_loops`）で無限ループを防止
- `review.auto_fix: false` で従来動作（報告のみ）に切替可能
- テストコード作成は `/tdd` の責務であり、`/review` の責務ではない
