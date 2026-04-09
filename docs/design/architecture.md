# AI Orchestra アーキテクチャドキュメント

**作成日**: 2026-03-20
**更新日**: 2026-04-09
**対象**: `main` ブランチ時点の実装

---

![アーキテクチャ図](../assets/architecture.png)

---

## 1. プロジェクト概要

AI Orchestra は Claude Code + Codex CLI + Gemini CLI の協調実行を管理する Python 製オーケストレーション基盤。
`orchex` CLI でパッケージ・テンプレート・スクリプトを配布・同期し、複数プロジェクトへの横展開を可能にする。

- **PyPI パッケージ名**: `orchex`
- **Python**: 3.12+
- **依存**: `pyyaml>=6.0`
- **ビルド**: Hatchling + hatch-vcs（git tag からバージョン自動生成）

---

## 2. ディレクトリ構成

```
ai-orchestra/
├── ai_orchestra/              # Python パッケージ（CLI エントリポイント）
│   ├── __init__.py            # __version__ エクスポート
│   ├── _version.py            # setuptools-scm 自動生成
│   └── cli.py                 # orchex CLI → orchestra-manager.py 動的ロード
│
├── scripts/
│   ├── orchestra-manager.py   # メイン CLI エントリポイント
│   ├── sync-orchestra.py      # SessionStart 自動同期エントリポイント
│   └── lib/                   # 共有ライブラリ
│       ├── hook_utils.py      # Hook 操作共通関数
│       ├── settings_io.py     # settings/orchestra.json I/O
│       ├── sync_engine.py     # パッケージ同期コアロジック
│       ├── scaffold.py        # scaffold / .claudeignore 管理
│       ├── agent_model_patch.py # エージェント model パッチ
│       ├── facet_builder.py   # Facet 合成ビルダー
│       ├── gitignore_sync.py  # .gitignore ブロック管理
│       ├── orchestra_models.py # データモデル（Package, HookEntry）
│       ├── orchestra_hooks.py  # Hook 管理 Mixin
│       └── orchestra_context.py # Context テンプレート Mixin
│
├── packages/                  # 配布パッケージ群（10パッケージ）
│   ├── core/                  # 共通基盤
│   ├── agent-routing/         # エージェント定義・ルーティング
│   ├── quality-gates/         # 自動lint・テストゲート
│   ├── route-audit/           # ルーティング監査・KPI
│   ├── codex-suggestions/     # Codex 相談提案
│   ├── gemini-suggestions/    # Gemini リサーチ提案
│   ├── cli-logging/           # CLI 呼び出しログ
│   ├── cocoindex/             # MCP サーバー自動プロビジョニング
│   ├── tmux-monitor/          # tmux サブエージェント監視
│   └── issue-workflow/        # GitHub Issue 開発フロー
│
├── facets/                    # Facet 合成システム
│   ├── compositions/          # 合成定義 YAML（26ファイル）
│   ├── policies/              # 共有ポリシー（4ファイル）
│   ├── output-contracts/      # 出力フォーマット（3ファイル）
│   ├── instructions/          # スキル固有指示（25ファイル）
│   ├── knowledge/             # 参考資料（14ファイル）
│   └── scripts/               # 同梱スクリプト（2ファイル）
│
├── templates/                 # 配布テンプレート
│   ├── context/               # 正本（手編集するソース）
│   ├── project/               # 生成物 → CLAUDE.md
│   ├── codex/                 # 生成物 → AGENTS.md
│   └── gemini/                # 生成物 → GEMINI.md
│
├── tests/                     # ユニットテスト（27ファイル）
├── .claude/                   # 同期後の実行コンテキスト
└── pyproject.toml             # パッケージメタデータ
```

---

## 3. コアモジュール

### 3.1 ai_orchestra/cli.py

`orchex` CLI のエントリポイント。`get_orchestra_dir()` で AI Orchestra ルートを解決し、`orchestra-manager.py` を動的にインポートして実行する。

**ルート解決の優先順位**:
1. インストール済みパッケージのリソースパス
2. 開発リポジトリ（`cli.py` の親ディレクトリ）
3. `AI_ORCHESTRA_DIR` 環境変数

### 3.2 scripts/orchestra-manager.py

メイン管理 CLI。`OrchestraManager` クラスがエントリポイントとなり、`scripts/lib/` の共有モジュール群を利用する。

**主要コマンド**:

| コマンド | 用途 |
|---------|------|
| `orchex setup <preset> --project <path>` | 初期 scaffold 作成 + プリセット一括セットアップ |
| `orchex install <pkg>` | パッケージインストール（依存解決 + hook 登録） |
| `orchex uninstall <pkg>` | パッケージ削除 |
| `orchex list` | 全パッケージ一覧 |
| `orchex status` | インストール状況 |
| `orchex enable/disable <pkg>` | hook 有効化/無効化 |
| `orchex context build` | テンプレート再生成 |
| `orchex context check` | テンプレート整合性検証 |
| `orchex context sync --project <path>` | テンプレートをプロジェクトに同期 |
| `orchex facet build` | Facet 合成 → SKILL.md/rule.md 生成 |
| `orchex facet extract` | 生成ファイルからソースへ逆抽出 |
| `orchex setup <preset>` | プリセット一括セットアップ |
| `orchex proxy stop/status` | MCP proxy 管理 |

**インストールフロー**:
1. manifest.json 読み込み → 依存パッケージをトポロジカルソート
2. `AI_ORCHESTRA_DIR` 環境変数を `~/.claude/settings.json` に設定
3. config ファイルを `.claude/config/{pkg}/` にコピー
4. hook を `.claude/settings.local.json` に登録
5. `sync-orchestra.py` の SessionStart hook を登録
6. `.claude/orchestra.json` にパッケージ記録
7. agents/config をコピー（skills/rules は facet build で生成）

### 3.3 scripts/sync-orchestra.py

SessionStart hook として毎セッション自動実行。エントリポイントのみを持ち、コアロジックは `scripts/lib/` に委譲する。mtime 比較による差分同期で高速化（変更なし時 ~70ms）。

**主な処理対象**（`lib/sync_engine.py` で実装）:
- agents, config（パッケージごと）※ skills/rules は facet build で生成するため同期しない
- facet build（`facets/` をソースに SKILL.md / ルールを再生成）
- .claudeignore（`lib/scaffold.py` で実装）
- agent .md ファイルの model フロントマター（`lib/agent_model_patch.py` で実装）

**特徴**:
- `.local.*` ファイルは同期対象外（上書きしない）
- 前回同期されたが現在は不要なファイルを自動削除（stale file removal）
- facet ソースが更新された場合のみ facet build を実行

### 3.4 scripts/lib/ — 共有ライブラリ

`orchestra-manager.py` と `sync-orchestra.py` の両方から利用される共有モジュール群。

| モジュール | 役割 |
|-----------|------|
| `hook_utils.py` | Hook コマンド生成・検索・追加・削除の共通関数 |
| `settings_io.py` | `settings.local.json` / `orchestra.json` の読み書き |
| `sync_engine.py` | パッケージ同期・hook 同期・facet ビルドのコアロジック |
| `scaffold.py` | プロジェクト scaffold と `.claudeignore` 管理 |
| `agent_model_patch.py` | エージェント `.md` の frontmatter model パッチ |
| `facet_builder.py` | Facet composition → SKILL.md / rule.md のビルダー |
| `gitignore_sync.py` | `.gitignore` の AI Orchestra ブロック管理 |
| `orchestra_models.py` | `Package` / `HookEntry` データクラス |
| `orchestra_hooks.py` | `HooksMixin`（OrchestraManager 用 hook 管理） |
| `orchestra_context.py` | `ContextMixin`（OrchestraManager 用 context テンプレート管理） |

---

## 4. パッケージシステム

### 4.1 パッケージ構造

```
packages/{name}/
  manifest.json         # メタデータ・依存・hook定義・ファイルリスト（skills/rulesは composition 名形式）
  hooks/                # Claude Code hook 実装（Python）
  agents/               # エージェント定義（.md）
  config/               # 設定ファイル（YAML/JSON）
  scripts/              # ユーティリティスクリプト
  tests/                # パッケージ単位テスト
```

> **Note**: `skills/` `rules/` ディレクトリはパッケージ内には存在しない。スキル（SKILL.md）とルール（.md）は `facet build` で `.claude/skills/` `.claude/rules/` に直接生成される。manifest.json の `skills` `rules` フィールドは composition 名（例: `"preflight"`, `"coding-principles"`）のリストで管理する。

### 4.2 パッケージ一覧と依存関係

```
core (v0.4.0)                   ← 依存なし（共通基盤）
├── agent-routing (v0.1.0)      ← core
│   └── route-audit (v0.2.0)    ← core, agent-routing
├── quality-gates (v0.1.0)      ← core
├── codex-suggestions (v0.1.0)  ← core
├── gemini-suggestions (v0.1.0) ← core
├── cli-logging (v0.1.0)        ← core
├── cocoindex (v0.2.0)          ← core
└── tmux-monitor (v0.2.0)       ← core

issue-workflow (v0.1.0)         ← 依存なし（独立）
```

### 4.3 core パッケージ — 共通ライブラリ

| モジュール | 役割 |
|-----------|------|
| `hook_common.py` | 設定読み込み（deep_merge, load_package_config）、hook I/O、JSON/JSONL 操作、sys.path 管理、エラーハンドリング |
| `context_store.py` | セッション/共有コンテキストの CRUD（fcntl ファイルロック付き） |
| `log_common.py` | 統一イベントログ（events.jsonl）への書き出し |

**load_package_config の解決順序**:
```
1. {project}/.claude/config/{package}/{name}.yaml  (ベース)
2. {project}/.claude/config/{package}/{name}.local.yaml  (上書き)
→ deep_merge(base, local)
```

### 4.4 agent-routing パッケージ — ルーティング

**28 エージェント定義**（日英バイリンガルトリガー付き）:

| カテゴリ | エージェント |
|---------|------------|
| Planning | planner, researcher, requirements |
| Design | architect, api-designer, data-modeler, auth-designer, spec-writer |
| Implementation | frontend-dev, backend-python-dev, backend-go-dev |
| AI/ML | ai-architect, ai-dev, prompt-engineer, rag-engineer |
| Test/Debug | debugger, tester |
| Review | code-reviewer, security-reviewer, performance-reviewer, spec-reviewer, architecture-reviewer, ux-reviewer |
| Docs | docs-writer |
| Utility | general-purpose, specialized-mcp-builder, support-executive-summary-generator, testing-reality-checker |

**ルーティング解決**:
```
agents.{name}.tool の値:
  codex         → Codex CLI 使用
  gemini        → Gemini CLI 使用
  claude-direct → Claude Code で直接処理
  auto          → ヒューリスティクス（設計→Codex、調査→Gemini、単純→Claude）
```

---

## 5. Hook システム

### 5.1 Hook イベントごとの代表処理

```
SessionStart:
  - sync-orchestra.py (外部)             パッケージ差分同期 + facet build
  - load-task-state.py (core)            Plans.md 読み込み・自動アーカイブ・タスクサマリー表示
  - orchestration-bootstrap.py (audit)   state/logs ディレクトリ初期化
  - provision-mcp-servers.py (cocoindex) MCP 設定書き出し
  - tmux-session-start.py (tmux)         tmux セットアップ

UserPromptSubmit:
  - clear-plan-gate.py (core)            プランゲートクリア
  - orchestration-expected-route.py      期待ルート予測
  - agent-router.py (routing)            プロンプト解析 → [Agent Routing] 提案

PreToolUse(Edit|Write):
  - check-codex-before-write.py (codex)  設計判断を伴う変更で [Codex Suggestion] 出力

PreToolUse(Agent|Task):
  - check-plan-gate.py (core)            実装エージェントのブロック判定
  - inject-shared-context.py (core)      前回サブエージェント結果 + 作業コンテキスト注入
  - tmux-pre-task.py (tmux)              タスク実行前の準備

PreToolUse(WebSearch|WebFetch):
  - suggest-gemini-research.py (gemini)  リサーチ向きクエリで [Gemini Suggestion] 出力

PostToolUse(Edit|Write):
  - post-implementation-review.py        一定量の変更後にレビューを提案
  - lint-on-save.py (quality)            .py ファイルに ruff format/check 自動実行
  - test-gate-checker.py (quality)       テスト品質ゲートチェック
  - update-working-context.py (core)     変更ファイルを working-context.json に記録

PostToolUse(Agent|Task):
  - set-plan-gate.py (core)              プランゲートを設定
  - capture-task-result.py (core)        サブエージェント結果を session/entries/ に保存
  - check-codex-after-plan.py (codex)    プラン完了後に Codex レビューを提案

SessionEnd:
  - cleanup-session-context.py (core)    session/ ディレクトリ削除
  - stop-mcp-proxy.py (cocoindex)        proxy 停止（v2 有効時）
  - tmux-session-end.py (tmux)           tmux クリーンアップ
```

### 5.2 Hook 設計原則

- **Fail-open**: `safe_hook_execution` デコレータで例外を catch し `exit(0)`（ツール実行をブロックしない）
- **例外**: `check-plan-gate.py` のみ `exit(2)` でツール実行をブロック可能
- **出力**: `hookSpecificOutput.additionalContext` で Claude のコンテキストに情報注入

---

## 6. Facet 合成システム

### 6.1 概要

再利用可能なプロンプト部品（facet）を YAML 定義で合成し、SKILL.md や rule.md を自動生成する。

### 6.2 6 層構造

| 層 | ディレクトリ | 内容 |
|----|------------|------|
| **Compositions** | `facets/compositions/` | 合成定義 YAML（26ファイル）。どの policy + instruction を結合するか指定 |
| **Policies** | `facets/policies/` | 共有ポリシー（4ファイル）: cli-language, dialog-rules, code-quality, factual-writing |
| **Output Contracts** | `facets/output-contracts/` | 出力フォーマット（3ファイル）: tiered-review, compare-report, deep-dive-report |
| **Instructions** | `facets/instructions/` | スキル/ルール固有の指示（25ファイル） |
| **Knowledge** | `facets/knowledge/` | スキルに同梱する参考資料（14ファイル） |
| **Scripts** | `facets/scripts/` | スキルに同梱するユーティリティ（2ファイル） |

### 6.3 合成フロー

```
facets/compositions/review.yaml
  ├─ policies: [cli-language, dialog-rules]
  │   └─ facets/policies/cli-language.md
  │   └─ facets/policies/dialog-rules.md
  ├─ output_contracts: [tiered-review]
  │   └─ facets/output-contracts/tiered-review.md
  └─ instruction: review
      └─ facets/instructions/review.md
          ↓ 合成
  .claude/skills/review/SKILL.md  (frontmatter + policies + contracts + instruction)
```

### 6.4 Composition YAML 例

```yaml
name: codex-system
description: Claude Code ↔ Codex CLI collaboration (config-driven)
# package フィールドは不要（所有者は manifest.json の skills リストから解決）

frontmatter:
  name: codex-system
  description: |
    Use Codex CLI with config-driven routing...

policies:
  - cli-language

instruction: codex-system           # facets/instructions/codex-system.md を参照
```

---

## 7. テンプレートシステム

### 7.1 正本と生成物の関係

```
templates/context/claude.md  (正本・手編集)
templates/context/shared.md  (共通フラグメント)
        ↓  orchex context build
templates/project/CLAUDE.md  (生成物・直接編集禁止)
        ↓  orchex context sync --project <path>
<project>/CLAUDE.md          (配布先)
```

| 正本 | 生成先テンプレート | プロジェクト配置先 |
|------|------------------|------------------|
| `context/claude.md` | `templates/project/CLAUDE.md` | `CLAUDE.md` |
| `context/codex.md` | `templates/codex/AGENTS.md` | `AGENTS.md` |
| `context/gemini.md` | `templates/gemini/GEMINI.md` | `.gemini/GEMINI.md` |

---

## 8. 設定の階層構造

### 8.1 Config-Loading ルール

```
ベース:   .claude/config/{package}/{name}.yaml     ← 自動同期
ローカル: .claude/config/{package}/{name}.local.yaml ← ユーザー管理（同期対象外）
→ deep_merge(base, local) で最終値を決定
```

### 8.2 主要設定ファイル

| ファイル | パッケージ | 内容 |
|---------|-----------|------|
| `cli-tools.yaml` | agent-routing | Codex/Gemini モデル名、sandbox 設定、28 エージェントの tool 割り当て |
| `cocoindex.yaml` | cocoindex | MCP サーバー設定、proxy 設定 |
| `task-memory.yaml` | core | Plans.md パス、タスクマーカー定義 |
| `orchestration-flags.json` | route-audit | 機能フラグ（route_audit, quality_gate, kpi_scorecard, tmux_monitoring） |
| `delegation-policy.json` | route-audit | ルーティングポリシー（将来用） |

### 8.3 cli-tools.yaml の構造

```yaml
codex:
  enabled: true
  model: gpt-5.3-codex
  sandbox:
    analysis: read-only
    implementation: workspace-write
  flags: --full-auto

gemini:
  enabled: true
  model: gemini-3.1-pro-preview

subagent:
  default_model: sonnet

agents:
  architect:        { tool: claude-direct }
  frontend-dev:     { tool: codex, sandbox: workspace-write }
  researcher:       { tool: gemini }
  general-purpose:  { tool: auto }
  # ... 21 more agents
```

---

## 9. コンテキスト共有基盤

### 9.1 ストレージ構造

```
.claude/context/
  session/                          # セッションスコープ（SessionEnd で削除）
    meta.json                       # session_id, started_at
    entries/
      {agent_id}_{timestamp}.json   # サブエージェント結果サマリー
  shared/
    working-context.json            # 変更ファイルリスト、フェーズ
```

### 9.2 データフロー

1. **SessionStart**: `init_context_dir()` でディレクトリ初期化
2. **PreToolUse(Agent/Task)**: 直近 5 件のエントリ + working-context をプロンプトに注入
3. **PostToolUse(Agent/Task)**: サブエージェント結果を entry として保存（先頭 2000 文字）
4. **PostToolUse(Edit|Write)**: 変更ファイルパスを working-context に追記
5. **SessionEnd**: session/ を削除

---

## 10. プロジェクト出力構造

初回 `orchex setup` または `orchex install` 後のプロジェクト構造:

```
<project>/
├── CLAUDE.md                       # テンプレートから同期
├── AGENTS.md                       # テンプレートから同期
├── .gemini/GEMINI.md               # テンプレートから同期
├── .mcp.json                       # MCP サーバー設定（cocoindex 利用時）
├── .claude/
│   ├── orchestra.json              # インストール済みパッケージ・同期状態
│   ├── settings.local.json         # hook 登録（自動管理）
│   ├── Plans.md                    # タスク管理（SSOT）
│   ├── Plans.archive.md            # 完了プロジェクトアーカイブ
│   ├── agents/*.md                 # エージェント定義（28ファイル）
│   ├── skills/*/SKILL.md           # スキル定義（14ディレクトリ）
│   ├── rules/*.md                  # ルール定義（12ファイル）
│   ├── config/{package}/*.yaml     # ベース設定 + ローカル上書き
│   ├── context/                    # セッションコンテキスト（ephemeral）
│   ├── state/                      # 永続状態
│   ├── logs/orchestration/         # イベントログ
│   ├── docs/                       # ドキュメント
│   ├── checkpoints/                # セッションチェックポイント
│   └── facets/                     # プロジェクトローカル facet 上書き
└── .gitignore                      # AI Orchestra ブロック追加済み
```

---

## 11. テストカバレッジ

**計 46 テストファイル**（tests/ 27 + packages/*/tests/ 19）

| 領域 | テスト内容 |
|------|----------|
| 設定管理 | base + local override の読み込み優先順位、YAML/JSON パース |
| 一貫性 | manifest ↔ 実ファイル、config ↔ 生成物の整合性検証 |
| Hook | コンテキスト注入・キャプチャ・クリーンアップ |
| CLI 検出 | `codex exec` / `gemini -p` の正規表現マッチング |
| Facet | composition 解決、policy 注入、エラーハンドリング |
| 同期 | agents/config の差分同期、.claudeignore 生成、agent model パッチ |
| タスク状態 | Plans.md パース、マーカー更新、アーカイブ |

**テストパターン**: `module_loader.py` による動的モジュールロード、`tmp_path` フィクスチャ、`monkeypatch`、`@pytest.mark.parametrize`

---

## 12. 設計上の特徴

| 特徴 | 説明 |
|------|------|
| **Config-Driven** | CLI 選択・モデル・エージェントの振る舞いを全て YAML で制御 |
| **Layered Override** | ベース設定 + `.local.*` で上書き。同期で上書きを破壊しない |
| **Faceted Prompting** | ポリシー・指示・出力契約を分離合成。DRY なプロンプト管理 |
| **Fail-Open Hooks** | 全 hook が `safe_hook_execution` で例外を吸収。CI/CD を止めない |
| **mtime-Based Sync** | 変更なし時は ~70ms。変更ファイルのみコピー |
| **Package System** | manifest.json + トポロジカルソートで依存解決 |
| **Context Isolation** | セッションデータは ephemeral。セッション間記憶は claude-mem に委任 |
