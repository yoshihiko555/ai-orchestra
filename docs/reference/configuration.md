---
codd:
  node_id: "design:configuration"
  kind: design
  status: active
  depends_on:
    - id: "design:architecture"
      relation: references
  owner: ai-orchestra
---

# 設定リファレンス

**更新日**: 2026-04-09
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

  # codex exec に常に付与するフラグ（sandbox を無効化・上書きするフラグ
  # --full-auto / --sandbox / --dangerously-bypass-approvals-and-sandbox 等を書くと Codex は使われない）
  flags: ""

  # sandbox 外での実行が必要か（codex 0.145 系は sandbox 内で app-server 初期化に失敗する。Issue #85）
  requires_sandbox_disable: true
```

| キー                       | 型     | デフォルト        | 説明                                                                                    |
| -------------------------- | ------ | ----------------- | --------------------------------------------------------------------------------------- |
| `enabled`                  | bool   | `true`            | `false` で Codex 呼び出しを全停止                                                       |
| `model`                    | string | `gpt-5.3-codex`   | Codex CLI で使用するモデル                                                              |
| `sandbox.analysis`         | string | `read-only`       | 分析用サンドボックスモード                                                              |
| `sandbox.implementation`   | string | `workspace-write` | 実装用サンドボックスモード                                                              |
| `flags`                    | string | `""`              | Codex CLI に常時付与するフラグ（sandbox を上書きするフラグを含むと Codex は使われない） |
| `requires_sandbox_disable` | bool   | `true`            | sandbox 外での実行が必要か                                                              |

### antigravity セクション

Antigravity CLI（agy）の全体設定。旧 `gemini:` キー（`.local.yaml` 残存分）は
読み込み時に正規化される（`enabled: false` のみ反映。`model` / `flags` は引き継がない）。

```yaml
antigravity:
  # Antigravity CLI の有効/無効
  enabled: true

  # agy -p "..." --model <この値> で使用されるモデル slug。空文字で CLI デフォルトを使用
  # 注意: agy は無効な slug でも exit 0 でデフォルトモデルに黙ってフォールバックする
  model: gemini-3.1-pro-high

  # model の妥当性チェック用 allowlist（未掲載モデルはコマンド提案時に [WARN] 付与）
  model_allowlist:
    - gemini-3.1-pro-high
    # ...（cli-tools.yaml 参照）

  # agy コマンドに常に付与するフラグ
  flags: ""

  # sandbox 内で実行可能か（sandbox.excludedCommands に agy 追加が前提）
  requires_sandbox_disable: false
```

| キー                       | 型     | デフォルト            | 説明                                            |
| -------------------------- | ------ | --------------------- | ----------------------------------------------- |
| `enabled`                  | bool   | `true`                | `false` で Antigravity 呼び出しを全停止         |
| `model`                    | string | `gemini-3.1-pro-high` | agy で使用するモデル slug                       |
| `model_allowlist`          | list   | `agy models` の ID    | slug 妥当性チェック用（黙示フォールバック対策） |
| `flags`                    | string | `""`                  | agy に常時付与するフラグ                        |
| `requires_sandbox_disable` | bool   | `false`               | sandbox 外での実行が必要か                      |

### subagent セクション

Claude Code サブエージェントの設定。

```yaml
subagent:
  # 全エージェント .md のフロントマター model に適用されるデフォルトモデル
  # sync-orchestra.py が SessionStart 時にこの値で .md を自動パッチする
  # 選択肢: sonnet, opus, haiku
  default_model: sonnet
```

| キー            | 型     | デフォルト | 説明                                                            |
| --------------- | ------ | ---------- | --------------------------------------------------------------- |
| `default_model` | string | `sonnet`   | 全エージェントのデフォルトモデル（`sonnet` / `opus` / `haiku`） |

### language セクション

言語プロトコル設定。

```yaml
language:
  cli_query: english # Codex/Antigravity への質問言語
  user_output: japanese # ユーザーへの出力言語
```

### review セクション

`/review` スキルの自動修正ループ設定。

```yaml
review:
  # 最大ループ回数（レビュー → 修正 → 再レビューのサイクル上限）
  max_loops: 3

  # 通過基準: critical_zero = Critical が 0 件で通過
  pass_threshold: critical_zero

  # true の場合、Critical 指摘を自動修正して再レビューする
  auto_fix: true

  # true の場合、Phase 3.5 で finding-verifier が Critical/High 指摘を反証検証する
  verify_findings: true
```

| キー              | 型     | デフォルト      | 説明                                                                                   |
| ----------------- | ------ | --------------- | -------------------------------------------------------------------------------------- |
| `max_loops`       | int    | `3`             | 自動修正ループの上限回数                                                               |
| `pass_threshold`  | string | `critical_zero` | `/review` の通過基準                                                                   |
| `auto_fix`        | bool   | `true`          | Critical 指摘の自動修正ループを有効化                                                  |
| `verify_findings` | bool   | `true`          | `finding-verifier` による指摘検証（Phase 3.5）を有効化。`false` で従来動作（検証なし） |

### agents セクション

各エージェントのルーティング設定。

```yaml
agents:
  { agent_name }:
    tool: codex | antigravity | claude-direct | auto
    sandbox: workspace-write # codex 使用時のサンドボックスモード（任意）
    model: null # エージェント固有のモデル上書き（任意）
```

| `tool` 値       | 動作                                                     |
| --------------- | -------------------------------------------------------- |
| `codex`         | Codex CLI を使用                                         |
| `antigravity`   | Antigravity CLI（agy）を使用（旧値 `gemini` は読み替え） |
| `claude-direct` | 外部 CLI を呼ばず Claude で処理                          |
| `auto`          | タスク種別に応じて自動選択                               |

#### デフォルトのルーティング

| tool 値         | エージェント                                                                                                                                                                                                                                                                                                                                        |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude-direct` | architect, api-designer, code-reviewer, finding-verifier, security-reviewer, performance-reviewer, adversarial-reviewer, ux-reviewer, spec-reviewer, architecture-reviewer, auth-designer, data-modeler, docs-writer, planner, prompt-engineer, requirements, specialized-mcp-builder, support-executive-summary-generator, testing-reality-checker |
| `codex`         | ai-dev, backend-go-dev, backend-python-dev, debugger, frontend-dev, rag-engineer, spec-writer, tester                                                                                                                                                                                                                                               |
| `antigravity`   | researcher                                                                                                                                                                                                                                                                                                                                          |
| `auto`          | ai-architect, general-purpose                                                                                                                                                                                                                                                                                                                       |

---

## orchestra.json

**パス:** `.claude/orchestra.json`

プロジェクトの AI Orchestra 状態を管理するファイル。`orchex install` 時に自動生成・更新される。

```json
{
  "orchestra_dir": "/path/to/ai-orchestra",
  "installed_packages": ["core", "agent-routing", "quality-gates"],
  "synced_files": [
    "agents/planner.md",
    "agents/code-reviewer.md",
    "config/agent-routing/cli-tools.yaml"
  ],
  "last_sync": "2026-03-21T11:37:04.904409+00:00"
}
```

| キー                 | 説明                                                                         |
| -------------------- | ---------------------------------------------------------------------------- |
| `orchestra_dir`      | ai-orchestra のインストールディレクトリ                                      |
| `installed_packages` | インストール済みパッケージ一覧                                               |
| `synced_files`       | 最後の同期で `.claude/agents/` と `.claude/config/` に配置されたファイル一覧 |
| `last_sync`          | 最終同期日時（ISO 8601）                                                     |

---

## delegation-policy.json

**パス:** `.claude/config/audit/delegation-policy.json`
**パッケージ:** audit

ルーティングルールとエイリアスの定義。

```json
{
  "version": 3,
  "default_route": "claude-direct",
  "helper_routes": ["task:Explore", "task:Plan"],
  "rules": [],
  "aliases": {
    "claude-direct": [
      "skill:commit",
      "skill:memory-tidy",
      "skill:issue-create",
      "skill:issue-fix"
    ]
  }
}
```

| キー            | 説明                                                           |
| --------------- | -------------------------------------------------------------- |
| `version`       | 設定バージョン                                                 |
| `default_route` | デフォルトのルーティング先                                     |
| `helper_routes` | ヘルパーとして許可されるルート                                 |
| `rules`         | カスタムルーティングルール（空の場合は cli-tools.yaml に委譲） |
| `aliases`       | ルートのエイリアス（スキルをルートに紐付け）                   |

---

## audit-flags.json

**パス:** `.claude/config/audit/audit-flags.json`
**パッケージ:** audit

機能フラグの管理。

```json
{
  "version": 2,
  "features": {
    "route_audit": {
      "enabled": true,
      "max_excerpt_chars": 160
    },
    "quality_gate": {
      "enabled": true,
      "block_on_failed_test": true,
      "test_file_threshold": 3,
      "test_line_threshold": 100
    },
    "kpi_scorecard": {
      "enabled": true,
      "default_period_days": 7
    },
    "context_optimization": {
      "enabled": true,
      "read_line_threshold": 200,
      "max_file_size_bytes": 5242880
    }
  },
  "paths": {
    "state_dir": ".claude/state",
    "logs_dir": ".claude/logs/audit"
  }
}
```

| 機能                   | 説明                                           | デフォルト |
| ---------------------- | ---------------------------------------------- | ---------- |
| `route_audit`          | ルーティング実績の記録                         | 有効       |
| `quality_gate`         | `audit` / `quality-gates` 共有の品質ゲート設定 | 有効       |
| `kpi_scorecard`        | KPI スコアカード生成                           | 有効       |
| `context_optimization` | 大きすぎる読み込みや `cat` 利用の抑制          | 有効       |

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

## sandbox-requirements.json

**パス:** `.claude/config/git-workflow/sandbox-requirements.json`
**パッケージ:** git-workflow

```json
{
  "description": "git-workflow パッケージが必要とする sandbox 設定",
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
antigravity:
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

---

## 設定の反映タイミング

| 変更対象             | 反映タイミング                                   |
| -------------------- | ------------------------------------------------ |
| cli-tools.yaml       | 次回のエージェント呼び出し時（即時）             |
| cli-tools.local.yaml | 次回のエージェント呼び出し時（即時）             |
| audit-flags.json     | 次回の hook 発火時（即時）                       |
| task-memory.yaml     | 次回セッション開始時（SessionStart hook）        |
| ベースファイル全般   | SessionStart 時に `sync-orchestra.py` で自動同期 |
