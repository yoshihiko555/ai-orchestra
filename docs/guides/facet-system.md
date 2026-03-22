# Facet システム解説

ファセットプロンプティングの仕組みと運用フロー。

---

![Facet 合成システム](../assets/facet.png)

---

## コンセプト

Facet システムは、スキル（SKILL.md）やルール（.md）を **再利用可能な部品（ファセット）** から自動生成する仕組み。

従来の問題:
- 同じルール（言語ポリシー、コード品質等）を複数のスキル・ルールに手動コピー
- 1箇所を修正すると全ファイルを手動更新する必要がある

Facet の解決策:
- 共通ルールを **Policy** として1箇所で管理
- 出力形式を **Output Contract** として共有
- スキル固有の手順を **Instruction** として分離
- **Composition YAML** で組み立てを定義し、`facet build` で自動生成

---

## 構成要素

```
facets/
├── policies/           ← 共有 Policy（複数スキル・ルールで再利用）
├── output-contracts/   ← 共有 Output Contract（出力形式の標準化）
├── instructions/       ← スキル・ルール固有の Instruction
├── knowledge/          ← 参考資料（スキルの references/ に配布）
├── scripts/            ← ユーティリティスクリプト（スキルの scripts/ に配布）
└── compositions/       ← 組み立て定義 YAML
```

### Policy（ポリシー）

複数のスキル・ルールで共有される横断的なルール。1箇所を修正すれば、参照する全スキル・ルールに反映される。

| ファイル | 内容 |
|---------|------|
| `cli-language.md` | 外部 CLI との言語プロトコル（英語で質問、日本語で報告） |
| `code-quality.md` | コード品質の共通ルール（シンプルさ、型ヒント、命名等） |
| `dialog-rules.md` | 対話ルール |
| `factual-writing.md` | 事実に基づいた記述ルール |

### Output Contract（出力契約）

レビューやレポート等、出力形式を標準化するテンプレート。

| ファイル | 内容 |
|---------|------|
| `tiered-review.md` | Critical/High/Medium/Low の4段階レビュー形式 |
| `compare-report.md` | 比較レポート形式 |
| `deep-dive-report.md` | 詳細分析レポート形式 |

### Instruction（インストラクション）

各スキル・ルール固有の手順や仕様。Composition から1対1で参照される。

| カテゴリ | Instruction |
|---------|-------------|
| ルーティング | `agent-routing-policy`, `orchestra-usage`, `config-loading` |
| Codex/Gemini | `codex-delegation`, `codex-suggestion-compliance`, `codex-system`, `gemini-delegation`, `gemini-suggestion-compliance`, `gemini-system` |
| 品質 | `review`, `skill-review-policy`, `tdd`, `release-readiness` |
| 開発フロー | `startproject`, `issue-create`, `issue-fix`, `preflight` |
| 状態管理 | `task-memory-usage`, `task-state`, `checkpointing`, `context-sharing` |
| その他 | `coding-principles` (rule), `cocoindex-usage`, `design`, `design-tracker` |

### Knowledge（ナレッジ）

スキルに同梱する参考資料。composition の `knowledge` フィールドで宣言すると、`facet build` 時に `skills/{name}/references/` に自動配布される。

### Scripts（スクリプト）

スキルに同梱するユーティリティスクリプト。composition の `scripts` フィールドで宣言すると、`facet build` 時に `skills/{name}/scripts/` に自動配布される。

---

## Composition YAML

各スキル・ルールの組み立てを定義するファイル。

### ルール用 Composition

```yaml
# codex-delegation.yaml
name: codex-delegation
description: Codex CLI 委譲ルール
# package フィールドは不要（所有パッケージは manifest.json の rules リストから解決）
type: rule                    # "rule" を指定するとルール .md を生成

policies:
  - cli-language              # facets/policies/ から参照

instruction: codex-delegation # facets/instructions/ から参照
```

### スキル用 Composition

```yaml
# review.yaml
name: review
description: マルチエージェントコードレビュー（スマート選定）
# package フィールドは不要（所有パッケージは manifest.json の skills リストから解決）
# type を省略するとスキル（SKILL.md）を生成

# フロントマター（生成される SKILL.md に付与）
frontmatter:
  name: review
  description: |
    Run code reviews using specialized reviewer agents.
    Supports individual or batch review modes with smart reviewer selection.
  metadata:
    short-description: Multi-agent code review (smart selection)

# 参照する Output Contract
output_contracts:
  - tiered-review             # facets/output-contracts/ から参照

# 参照するポリシー
policies: []                  # なし（スキル固有ルールのみ）

# スキル固有の instruction
instruction: review           # facets/instructions/ から参照

# スキルに同梱するリソース（任意）
knowledge:                    # facets/knowledge/ から参照 → references/ に配布
  - review-guidelines
scripts:                      # facets/scripts/ から参照 → scripts/ に配布
  - analyze.py
```

### 全フィールド

| フィールド | 必須 | 説明 |
|-----------|------|------|
| `name` | 必須 | 生成物の名前 |
| `description` | 必須 | 説明 |
| `type` | 任意 | `rule` でルール生成。省略でスキル生成 |
| `frontmatter` | 任意 | SKILL.md のフロントマター（スキルのみ） |
| `policies` | 任意 | 参照する Policy 名のリスト |
| `output_contracts` | 任意 | 参照する Output Contract 名のリスト |
| `instruction` | 必須 | 参照する Instruction 名 |
| `knowledge` | 任意 | 同梱する Knowledge 名のリスト（スキルのみ） |
| `scripts` | 任意 | 同梱する Script ファイル名のリスト（スキルのみ） |

> **Note**: `package` フィールドは廃止。composition の所有パッケージは各パッケージの `manifest.json` の `skills` / `rules` リストに composition 名を記載することで管理する（manifest が SSOT）。

---

## ビルドプロセス

### `orchex facet build` の動作

```
1. compositions/*.yaml を読み込む
2. 各 composition に対して:
   a. policies/ から参照ポリシーを結合
   b. output-contracts/ から参照契約を結合
   c. instructions/ からインストラクションを読み込む
   d. [スキルの場合] frontmatter を YAML フロントマター形式で付与
   e. ポリシー + 出力契約 + インストラクション を結合して出力
   f. [スキルの場合] knowledge/ → references/ にコピー
   g. [スキルの場合] scripts/ → scripts/ にコピー
3. 出力先:
   - スキル → .claude/skills/{name}/SKILL.md + .codex/skills/{name}/SKILL.md
   - ルール → .claude/rules/{name}.md + .codex/rules/{name}.md
   - リソース → .claude/skills/{name}/references/*.md, scripts/*
```

### ビルドフロー図

```
facets/policies/cli-language.md ──────┐
facets/policies/code-quality.md ──────┤
facets/output-contracts/tiered-review.md ─┤
facets/instructions/{name}.md ────────┤
facets/knowledge/{name}.md ───────────┤  ← facet build ──→ .claude/skills/{name}/SKILL.md
facets/scripts/{name}.py ─────────────┘                ──→ .claude/skills/{name}/references/*.md
                                                       ──→ .claude/skills/{name}/scripts/*
facets/compositions/{name}.yaml                        ──→ .claude/rules/{name}.md
  ↑ 組み立て定義（どれを結合するか）                      ──→ .codex/skills/{name}/SKILL.md
```

### コマンド

```bash
# 全 composition をビルド
orchex facet build --project .

# 単一 composition をビルド
orchex facet build --name review --project .

# Codex CLI 向けのみ生成
orchex facet build --target codex --project .
```

SessionStart 時に `facet build` は自動実行されるため、通常は手動ビルド不要。

---

## Extract（書き戻し）

`/config-tune` 等で生成済みの SKILL.md を直接チューニングした後、変更を Instruction ソースに書き戻す仕組み。

### フロー

```
1. /config-tune で .claude/skills/{name}/SKILL.md を直接編集
2. orchex facet extract --name {name} --project .
   → SKILL.md から instruction 部分を抽出
   → facets/instructions/{name}.md を更新
3. 次回 facet build で変更が保持される
```

### コマンド

```bash
# 単一スキルの書き戻し
orchex facet extract --name review --project .

# 全件書き戻し
orchex facet extract --project .
```

---

## 運用例

### 新しいスキルを追加する

1. **Instruction を作成**

```bash
# facets/instructions/my-skill.md
# スキル固有の手順を記述
```

2. **Composition YAML を作成**

```yaml
# facets/compositions/my-skill.yaml
name: my-skill
description: 新しいスキルの説明
# package フィールドは不要（所有パッケージは manifest.json の skills リストに追加する）

frontmatter:
  name: my-skill
  description: |
    What this skill does.

policies:
  - code-quality        # 必要に応じてポリシーを参照

output_contracts: []

instruction: my-skill
```

3. **ビルド**

```bash
orchex facet build --name my-skill --project .
```

4. **確認**: `.claude/skills/my-skill/SKILL.md` が生成される

### 共通ポリシーを全スキルに反映する

1. `facets/policies/cli-language.md` を編集
2. `orchex facet build --project .` を実行
3. `cli-language` を参照する全スキル・ルールが更新される

### チューニング後の保存

1. Claude Code で `/config-tune` を実行し SKILL.md を直接編集
2. `orchex facet extract --name {name} --project .` で書き戻し
3. 次回ビルドで変更が保持される

---

## 既存 Composition 一覧

所有パッケージは各パッケージの `manifest.json` で管理される。

| Composition | 種別 | 所有パッケージ（manifest 参照） | ポリシー | 出力契約 |
|------------|------|-------------------------------|---------|---------|
| `agent-routing-policy` | rule | agent-routing | — | — |
| `checkpointing` | skill | cli-logging | — | — |
| `cocoindex-usage` | rule | cocoindex | — | — |
| `codex-delegation` | rule | codex-suggestions | cli-language | — |
| `codex-suggestion-compliance` | rule | codex-suggestions | — | — |
| `codex-system` | skill | codex-suggestions | cli-language | — |
| `coding-principles` | rule | core | — | — |
| `config-loading` | rule | core | — | — |
| `context-sharing` | rule | core | — | — |
| `design` | skill | core | — | — |
| `design-tracker` | skill | quality-gates | — | — |
| `gemini-delegation` | rule | gemini-suggestions | cli-language | — |
| `gemini-suggestion-compliance` | rule | gemini-suggestions | — | — |
| `gemini-system` | skill | gemini-suggestions | cli-language | — |
| `issue-create` | skill | issue-workflow | — | — |
| `issue-fix` | skill | issue-workflow | — | — |
| `orchestra-usage` | rule | agent-routing | cli-language | — |
| `preflight` | skill | core | — | — |
| `release-readiness` | skill | quality-gates | — | tiered-review |
| `review` | skill | quality-gates | — | tiered-review |
| `skill-review-policy` | rule | quality-gates | — | tiered-review |
| `startproject` | skill | core | — | — |
| `task-memory-usage` | rule | core | — | — |
| `task-state` | skill | core | — | — |
| `tdd` | skill | quality-gates | — | — |
