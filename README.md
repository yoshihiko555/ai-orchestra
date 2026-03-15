# AI Orchestra

Claude Code用のマルチエージェントオーケストレーションシステム

## 構成

```
ai-orchestra/
├── facets/           # ファセットプロンプティング基盤（スキル・ルールの部品化）
│   ├── policies/          # 共有 Policy（dialog-rules, cli-language, code-quality, factual-writing）
│   ├── output-contracts/  # 共有 Output Contract（tiered-review, compare-report, deep-dive-report）
│   ├── instructions/      # スキル・ルール固有の instruction
│   └── compositions/      # 組み立て定義 YAML（facet build で SKILL.md / ルール .md を生成）
├── packages/         # パッケージ（hooks・scripts・agents・skills・rules・config）— 詳細は packages/README.md
│   ├── core/              # 共通基盤ライブラリ + coding-principles / config-loading ルール
│   ├── agent-routing/     # 25 エージェント定義 + ルーティング hooks + orchestra-usage ルール
│   ├── cli-logging/       # Codex/Gemini CLI ログ記録 + checkpointing スキル
│   ├── codex-suggestions/ # Codex 相談提案 + codex-delegation ルール + codex-system スキル
│   ├── gemini-suggestions/# Gemini リサーチ提案 + gemini-delegation ルール + gemini-system スキル
│   ├── quality-gates/     # 品質ゲート + review/tdd/simplify/release-readiness (+ design-tracker)
│   ├── route-audit/       # ルーティング監査・KPIレポート
│   ├── issue-workflow/    # GitHub Issue 起票 + 計画→実装→テスト→レビューの開発フロー
│   ├── cocoindex/         # cocoindex MCP サーバーの自動プロビジョニング
│   └── tmux-monitor/      # tmux サブエージェント監視
├── scripts/          # 管理CLI
├── templates/        # テンプレート（エージェント・スキル・プロジェクト）
├── tests/            # Python 単体テスト
├── docs/             # ドキュメント（設計・マイグレーション等）
├── taskfiles/        # Task CLI 用タスク定義
└── Taskfile.yml      # メインタスクファイル
```

## エージェント一覧

### コア
- `planner` - タスク分解・マイルストーン策定
- `researcher` - リサーチ・ドキュメント解析

### 要件・仕様
- `requirements` - 要件抽出・NFR整理
- `spec-writer` - 仕様書生成

### 設計
- `architect` - アーキテクチャ設計・技術選定
- `api-designer` - API/IF設計
- `data-modeler` - データモデリング・スキーマ設計
- `auth-designer` - 認証認可設計

### 実装
- `frontend-dev` - React/Next.js/TypeScript
- `backend-python-dev` - Python API
- `backend-go-dev` - Go API

### AI/ML
- `ai-architect` - AIアーキテクチャ・モデル選定
- `ai-dev` - AI機能実装
- `prompt-engineer` - プロンプト設計
- `rag-engineer` - RAG実装

### テスト・デバッグ
- `debugger` - バグ原因分析
- `tester` - テスト戦略・実装

### レビュー（実装）
- `code-reviewer` - コード品質
- `security-reviewer` - セキュリティ
- `performance-reviewer` - パフォーマンス

### レビュー（設計・仕様）
- `spec-reviewer` - 仕様整合性
- `architecture-reviewer` - アーキテクチャ妥当性
- `ux-reviewer` - UX・アクセシビリティ

### ドキュメント
- `docs-writer` - 技術文書・手順書

### ユーティリティ
- `general-purpose` - 汎用タスク・Codex/Gemini委譲

---

## セットアップ

### 1. インストール

```bash
# uv（推奨）
uv tool install orchex

# pip
pip install orchex

# pipx
pipx install orchex
```

### 2. プロジェクトへのセットアップ

```bash
# チームメンバー向け: 最低限のパッケージを一括インストール
orchex setup essential --project /path/to/project

# 管理者・開発者向け: 全パッケージを一括インストール
orchex setup all --project /path/to/project

# 事前確認（dry-run）
orchex setup essential --project /path/to/project --dry-run
```

プリセットは `presets.json` で定義されています:
- **essential** — core, route-audit, quality-gates
- **all** — 全パッケージ

### 2b. 個別インストール

```bash
# 個別にパッケージをインストールする場合
orchex install core --project /path/to/project
orchex install tmux-monitor --project /path/to/project
```

orchex が内部で以下を実行:
1. `~/.claude/settings.json` に `env.AI_ORCHESTRA_DIR` を設定
2. `.claude/orchestra.json` にパッケージ情報を記録
3. `.claude/settings.local.json` に hooks を登録（`$AI_ORCHESTRA_DIR/packages/...` 参照）
4. `sync-orchestra.py` の SessionStart hook を登録（初回のみ）
5. skills/agents/rules の初回同期を実行

### セットアップ完了条件

以下をすべて満たしたらセットアップ完了です:

- `~/.claude/settings.json` に `env.AI_ORCHESTRA_DIR` が設定されている
- `.claude/settings.local.json` に AI Orchestra の hooks が登録されている
- `.claude/orchestra.json` が存在し、インストール済みパッケージが記録されている
- Claude Code 次回起動時に SessionStart hook が走り `.claude/` 配下へ差分同期される

### 3. 管理コマンド

```bash
# バージョン確認
orchex --version
```

全ログの役割・参照先は `docs/specs/logging.md` を参照。

### 4. パッケージ管理コマンド

```bash
# プリセットで一括セットアップ
orchex setup essential --project .
orchex setup all --project .

# パッケージ一覧
orchex list

# プロジェクトでの導入状況
orchex status --project .

# インストール / アンインストール
orchex install <package> --project .
orchex uninstall <package> --project .

# 一時的な有効化 / 無効化（hooks の登録/解除のみ）
orchex enable <package> --project .
orchex disable <package> --project .

# パッケージ内スクリプトの一覧表示
orchex scripts
orchex scripts --package route-audit

# CLAUDE.md / AGENTS.md / GEMINI.md テンプレート管理
orchex context build
orchex context check
orchex context sync --project /path/to/project
orchex context sync --project /path/to/project --force

# パッケージ内スクリプトの実行（-- 以降はスクリプトにパススルー）
orchex run route-audit dashboard
orchex run route-audit log-viewer --project /path/to/project -- --last 10

# dry-run（変更内容を表示のみ）
orchex setup essential --project . --dry-run
orchex install <package> --project . --dry-run
```

### 5. ファセット管理コマンド

スキル・ルールをファセット（Policy / Output Contract / Instruction）から自動生成・管理する。

```bash
# 全 composition をビルド（SKILL.md / ルール .md を生成）
orchex facet build --project .

# 単一 composition をビルド
orchex facet build --name review --project .

# Codex CLI 向けに生成（.codex/skills/ に出力）
orchex facet build --target codex --project .

# 生成済みファイルから instruction をソースに書き戻す（チューニング反映）
orchex facet extract --name review --project .

# 全件書き戻し
orchex facet extract --project .
```

**運用フロー:**

```
facets/policies/*.md        ← 共有ルール（1箇所修正 → 全スキル・ルールに反映）
facets/output-contracts/*.md ← 共有出力形式
facets/instructions/*.md     ← スキル・ルール固有の手順
facets/compositions/*.yaml   ← 組み立て定義

    ↓ facet build

.claude/skills/{name}/SKILL.md  ← 生成物（Claude Code 用）
.claude/rules/{name}.md         ← 生成物（Claude Code 用）
.codex/skills/{name}/SKILL.md   ← 生成物（Codex CLI 用）
```

**チューニング後の反映:**

```
/config-tune 等で SKILL.md を直接編集
    ↓
orchex facet extract --name {name}   ← instruction をソースに書き戻し
    ↓
次回 facet build で変更が保持される
```

SessionStart 時に `facet build` が自動実行されるため、通常は手動ビルド不要。

### 開発者向け: ソースからのインストール

```bash
git clone https://github.com/yoshihiko555/ai-orchestra.git
cd ai-orchestra
uv tool install -e .
```

---

## 更新フロー

```bash
# PyPI からの更新
uv tool upgrade orchex

# 開発版の更新（ソースインストール時）
cd ai-orchestra && git pull
```

| 変更内容 | 操作 |
|---------|------|
| 全般 | `uv tool upgrade orchex`（PyPI 経由） |
| Hook スクリプト修正 | アップグレード後、即反映 |
| Skills/Agents/Rules 修正 | アップグレード後、次回 Claude Code 起動時に自動同期 |
| 新フックイベント追加 | アップグレード + `orchex install` 再実行 |

---

## .claudeignore の管理

- `.claudeignore` は AI Orchestra が自動生成するため直接編集しないでください
- プロジェクト固有の除外パターンは `.claudeignore.local` に記載
- SessionStart 時に ベース + `.claudeignore.local` がマージ生成されます

## .gitignore の管理

- `orchex init` 実行時に `.gitignore` へ AI Orchestra 用 block を追加/更新します
- 対象: `.claude/docs/`, `.claude/logs/`, `.claude/state/`, `.claude/checkpoints/`, `.claude/Plans.md`

---

## 使い方

### エージェントの呼び出し

```
Task(subagent_type="planner", prompt="このタスクを分解して")
Task(subagent_type="code-reviewer", prompt="このコードをレビューして")
```

### スキル一覧

| スキル | 用途 |
|--------|------|
| `/review` | コード・セキュリティ・設計レビュー（並列実行対応） |
| `/startproject` | マルチエージェント協調で新規開発を開始 |
| `/codex-system` | `cli-tools.yaml` に基づく Codex 利用ガイド（config-driven） |
| `/gemini-system` | Gemini CLI でのリサーチ・マルチモーダル処理 |
| `/checkpointing` | セッションコンテキストの保存・復元 |
| `/design-tracker` | 設計記録スキル（現運用は `CLAUDE.md`/`Plans.md`/ADR/docs を優先） |
| `/preflight` | 実装計画の策定 |
| `/simplify` | コードの簡素化 |
| `/tdd` | テスト駆動開発ワークフロー |

### レビュースキル

```
/review              # 全レビュアー並列実行
/review code         # コードレビューのみ
/review security     # セキュリティレビューのみ
/review impl         # 実装系レビュー
/review design       # 設計系レビュー
```

---

## アーキテクチャ

```
Claude Code (Orchestrator)
    │
    ├── Codex CLI    # `cli-tools.yaml` の設定に応じて利用（役割は config-driven）
    ├── Gemini CLI   # `cli-tools.yaml` の設定に応じて利用（役割は config-driven）
    │
    ├── $AI_ORCHESTRA_DIR/packages/
    │   ├── core/               # 共通ユーティリティ
    │   ├── cli-logging/        # CLI 呼び出しログ
    │   ├── codex-suggestions/  # Codex 相談提案
    │   ├── gemini-suggestions/ # Gemini リサーチ提案
    │   ├── quality-gates/      # 品質ゲート
    │   ├── route-audit/        # ルーティング監査
    │   ├── issue-workflow/     # GitHub Issue 開発フロー
    │   ├── cocoindex/          # MCP サーバー自動プロビジョニング
    │   └── tmux-monitor/       # tmux リアルタイム監視
    │
    └── 25 Specialized Agents
        ├── Planning: planner, researcher, requirements
        ├── Design: architect, api-designer, data-modeler, auth-designer, spec-writer
        ├── Implementation: frontend-dev, backend-*-dev, ai-*, debugger, tester
        └── Review: code-reviewer, security-reviewer, performance-reviewer, ...
```

### 仕組み

- **Hooks**: `$AI_ORCHESTRA_DIR` 環境変数で直接参照（シンボリックリンク不要）
- **Skills/Agents/Rules**: SessionStart hook (`sync-orchestra.py`) で `$AI_ORCHESTRA_DIR` から `.claude/` に差分コピー
- **Facets**: `facets/` のポリシー・出力形式を同期し、`facet build` で SKILL.md / ルール .md を自動生成
- **CLI Scripts**: `$AI_ORCHESTRA_DIR/packages/{pkg}/scripts/` を直接実行
