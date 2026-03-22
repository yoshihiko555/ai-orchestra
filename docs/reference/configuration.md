# 設定リファレンス

AI Orchestra の全設定ファイルとオプションの詳細。

---

## レイヤード構成

AI Orchestra の設定はベースファイルとローカル上書きファイルの2層で管理される。

```
.claude/config/{package_name}/
  {name}.yaml          ← ベース設定（ai-orchestra から自動同期）
  {name}.local.yaml    ← プロジェクト固有の上書き（手動作成、同期対象外）
```

**動作原理:**

1. ベースファイル（`{name}.yaml`）を読み込む
2. ローカルファイル（`{name}.local.yaml`）が存在すれば、定義されたキーでベースを上書き（deep merge）
3. ローカルに未定義のキーはベースの値を継続使用

ローカルファイルは `sync-orchestra.py` の同期対象外のため、プロジェクト固有のカスタマイズが上書きされることはない。

---

## cli-tools.yaml

**パス:** `.claude/config/agent-routing/cli-tools.yaml`
**パッケージ:** agent-routing

エージェントルーティング・CLI ツール・モデル設定の中核ファイル。

### codex セクション

Codex CLI の全体設定。

```yaml
codex:
  # Codex CLI の有効/無効。false にすると全 codex エージェントが claude-direct にフォールバック
  enabled: true

  # codex exec --model <この値> で使用されるモデル名
  model: gpt-5.3-codex

  # サンドボックスモード
  sandbox:
    # 分析・レビュー用（ファイル変更不可）
    analysis: read-only
    # 実装・修正用（ワークスペース内のファイル変更可）
    implementation: workspace-write

  # codex exec に常に付与するフラグ
  flags: --full-auto

  # sandbox 内で実行可能か（enableWeakerNetworkIsolation: true が前提）
  requires_sandbox_disable: false
```

| キー | 型 | デフォルト | 説明 |
|------|-----|-----------|------|
| `enabled` | bool | `true` | `false` で Codex 呼び出しを全停止 |
| `model` | string | `gpt-5.3-codex` | Codex CLI で使用するモデル |
| `sandbox.analysis` | string | `read-only` | 分析用サンドボックスモード |
| `sandbox.implementation` | string | `workspace-write` | 実装用サンドボックスモード |
| `flags` | string | `--full-auto` | Codex CLI に常時付与するフラグ |
| `requires_sandbox_disable` | bool | `false` | sandbox 外での実行が必要か |

### gemini セクション

Gemini CLI の全体設定。

```yaml
gemini:
  # Gemini CLI の有効/無効
  enabled: true

  # gemini -m <この値> で使用されるモデル名。空文字で CLI デフォルトを使用
  model: gemini-3.1-pro-preview

  # gemini コマンドに常に付与するフラグ
  flags: ""

  # sandbox 内で実行可能か
  requires_sandbox_disable: false
```

| キー | 型 | デフォルト | 説明 |
|------|-----|-----------|------|
| `enabled` | bool | `true` | `false` で Gemini 呼び出しを全停止 |
| `model` | string | `gemini-3.1-pro-preview` | Gemini CLI で使用するモデル |
| `flags` | string | `""` | Gemini CLI に常時付与するフラグ |
| `requires_sandbox_disable` | bool | `false` | sandbox 外での実行が必要か |

### subagent セクション

Claude Code サブエージェントの設定。

```yaml
subagent:
  # 全エージェント .md のフロントマター model に適用されるデフォルトモデル
  # sync-orchestra.py が SessionStart 時にこの値で .md を自動パッチする
  # 選択肢: sonnet, opus, haiku
  default_model: sonnet
```

| キー | 型 | デフォルト | 説明 |
|------|-----|-----------|------|
| `default_model` | string | `sonnet` | 全エージェントのデフォルトモデル（`sonnet` / `opus` / `haiku`） |

### language セクション

言語プロトコル設定。

```yaml
language:
  cli_query: english      # Codex/Gemini への質問言語
  user_output: japanese   # ユーザーへの出力言語
```

### agents セクション

各エージェントのルーティング設定。

```yaml
agents:
  {agent_name}:
    tool: codex | gemini | claude-direct | auto
    sandbox: workspace-write    # codex 使用時のサンドボックスモード（任意）
    model: null                 # エージェント固有のモデル上書き（任意）
```

| `tool` 値 | 動作 |
|-----------|------|
| `codex` | Codex CLI を使用 |
| `gemini` | Gemini CLI を使用 |
| `claude-direct` | 外部 CLI を呼ばず Claude で処理 |
| `auto` | タスク種別に応じて自動選択 |

#### デフォルトのルーティング

| tool 値 | エージェント |
|---------|------------|
| `claude-direct` | architect, api-designer, code-reviewer, security-reviewer, performance-reviewer, ux-reviewer, spec-reviewer, architecture-reviewer, auth-designer, data-modeler, docs-writer, planner, prompt-engineer, requirements |
| `codex` | ai-dev, backend-go-dev, backend-python-dev, debugger, frontend-dev, rag-engineer, spec-writer, tester |
| `gemini` | researcher |
| `auto` | ai-architect, general-purpose |

---

## orchestra.json

**パス:** `.claude/orchestra.json`

プロジェクトの AI Orchestra 状態を管理するファイル。`orchex install` 時に自動生成・更新される。

```json
{
  "orchestra_dir": "/path/to/ai-orchestra",
  "installed_packages": [
    "core",
    "agent-routing",
    "quality-gates"
  ],
  "synced_files": [
    "agents/planner.md",
    "rules/config-loading.md",
    "config/agent-routing/cli-tools.yaml"
  ],
  "last_sync": "2026-03-21T11:37:04.904409+00:00"
}
```

| キー | 説明 |
|------|------|
| `orchestra_dir` | ai-orchestra のインストールディレクトリ |
| `installed_packages` | インストール済みパッケージ一覧 |
| `synced_files` | 最後の同期で `.claude/` に配置されたファイル一覧 |
| `last_sync` | 最終同期日時（ISO 8601） |

---

## delegation-policy.json

**パス:** `.claude/config/route-audit/delegation-policy.json`
**パッケージ:** route-audit

ルーティングルールとエイリアスの定義。

```json
{
  "version": 3,
  "default_route": "claude-direct",
  "helper_routes": ["task:Explore", "task:Plan"],
  "rules": [],
  "aliases": {
    "claude-direct": [
      "skill:commit", "skill:memory-tidy",
      "skill:issue-create", "skill:issue-fix"
    ]
  }
}
```

| キー | 説明 |
|------|------|
| `version` | 設定バージョン |
| `default_route` | デフォルトのルーティング先 |
| `helper_routes` | ヘルパーとして許可されるルート |
| `rules` | カスタムルーティングルール（空の場合は cli-tools.yaml に委譲） |
| `aliases` | ルートのエイリアス（スキルをルートに紐付け） |

---

## orchestration-flags.json

**パス:** `.claude/config/route-audit/orchestration-flags.json`
**パッケージ:** route-audit

機能フラグの管理。

```json
{
  "version": 1,
  "features": {
    "route_audit": {
      "enabled": true,
      "record_input_excerpt": true,
      "max_excerpt_chars": 160
    },
    "route_guard": {
      "enabled": false,
      "mode": "warn"
    },
    "quality_gate": {
      "enabled": false,
      "block_on_failed_test": false,
      "test_file_threshold": 3,
      "test_line_threshold": 100
    },
    "kpi_scorecard": {
      "enabled": true,
      "default_period_days": 7
    },
    "tmux_monitoring": {
      "enabled": false
    }
  },
  "paths": {
    "state_dir": ".claude/state",
    "logs_dir": ".claude/logs/orchestration",
    "delegation_policy": ".claude/config/route-audit/delegation-policy.json"
  }
}
```

| 機能 | 説明 | デフォルト |
|------|------|-----------|
| `route_audit` | ルーティング実績の記録 | 有効 |
| `route_guard` | ルーティング逸脱の警告/ブロック | 無効 |
| `quality_gate` | テスト品質ゲート | 無効 |
| `kpi_scorecard` | KPI スコアカード生成 | 有効 |
| `tmux_monitoring` | tmux サブエージェント監視 | 無効 |

---

## task-memory.yaml

**パス:** `.claude/config/core/task-memory.yaml`
**パッケージ:** core

Plans.md によるタスク管理の設定。

```yaml
# Plans.md ファイルパス（プロジェクトルートからの相対パス）
plans_file: ".claude/Plans.md"

# SessionStart 時にタスク状態サマリーを出力するか
show_summary_on_start: true

# サマリーで表示するタスク合計の最大数（0 = 無制限）
max_display_tasks: 20

# 状態マーカー定義（値は重複不可）
markers:
  todo: "cc:TODO"
  wip: "cc:WIP"
  done: "cc:done"
  blocked: "cc:blocked"
```

---

## cocoindex.yaml

**パス:** `.claude/config/cocoindex/cocoindex.yaml`
**パッケージ:** cocoindex

cocoindex MCP サーバーのプロビジョニング設定。

```yaml
# MCP サーバーの有効/無効
enabled: true
server_name: "cocoindex-code"
command: "uvx"
args:
  - "--prerelease=explicit"
  - "--with"
  - "cocoindex>=1.0.0a16"
  - "cocoindex-code@latest"

# CLI ごとの有効/無効
targets:
  claude:
    enabled: true
    type: "stdio"
    force_stdio: false
  codex:
    enabled: true
    force_stdio: false
  gemini:
    enabled: true
    force_stdio: false

# mcp-proxy モード（v2）
proxy:
  enabled: false          # .local.yaml で true にしてオプトイン
  port: 8792
  port_range: 100         # project_dir ハッシュで自動割り当て
  host: "127.0.0.1"
  pid_file: ".claude/.mcp-proxy.pid"
  startup_timeout: 10
```

---

## sandbox-requirements.json

**パス:** `.claude/config/issue-workflow/sandbox-requirements.json`
**パッケージ:** issue-workflow

```json
{
  "description": "issue-workflow パッケージが必要とする sandbox 設定",
  "sandbox": {
    "excludedCommands": ["gh"]
  },
  "note": "gh は macOS キーリングにアクセスするためサンドボックス外で実行する必要がある"
}
```

---

## ローカル上書きの例

### CLI 未インストール環境

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
codex:
  enabled: false
gemini:
  enabled: false
```

### モデル変更のみ

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
codex:
  model: o3-pro
subagent:
  default_model: opus
```

### 特定エージェントのルーティング変更

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
agents:
  debugger:
    tool: claude-direct
  researcher:
    tool: claude-direct
```

### cocoindex の特定 CLI を無効化

```yaml
# .claude/config/cocoindex/cocoindex.local.yaml
targets:
  codex:
    enabled: false
```

### cocoindex バージョン固定

```yaml
# .claude/config/cocoindex/cocoindex.local.yaml
args:
  - "--prerelease=explicit"
  - "--with"
  - "cocoindex==1.0.0a16"
  - "cocoindex-code==0.2.0"
```

---

## 設定の反映タイミング

| 変更対象 | 反映タイミング |
|---------|--------------|
| cli-tools.yaml | 次回のエージェント呼び出し時（即時） |
| cli-tools.local.yaml | 次回のエージェント呼び出し時（即時） |
| orchestration-flags.json | 次回の hook 発火時（即時） |
| cocoindex.yaml | 次回セッション開始時（SessionStart hook） |
| task-memory.yaml | 次回セッション開始時（SessionStart hook） |
| ベースファイル全般 | SessionStart 時に `sync-orchestra.py` で自動同期 |
