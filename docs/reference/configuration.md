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
  cli_query: english # Codex への質問言語
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
    tool: codex | claude-direct | auto | api
    api_profile: research # tool: api のときに使用するプロファイル
    sandbox: workspace-write # codex 使用時のサンドボックスモード（任意）
    model: null # エージェント固有のモデル上書き（任意）
```

| `tool` 値       | 動作                                                     |
| --------------- | -------------------------------------------------------- |
| `codex`         | Codex CLI を使用                                         |
| `claude-direct` | 外部 CLI を呼ばず Claude で処理                          |
| `auto`          | タスク種別に応じて自動選択                               |
| `api`           | api_profile の API を使用。利用不可なら claude-direct     |

#### デフォルトのルーティング

| tool 値         | エージェント                                                                                                                                                                                                                                                                                                                                        |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude-direct` | architect, api-designer, code-reviewer, finding-verifier, security-reviewer, performance-reviewer, adversarial-reviewer, ux-reviewer, spec-reviewer, architecture-reviewer, auth-designer, data-modeler, docs-writer, planner, prompt-engineer, requirements, specialized-mcp-builder, support-executive-summary-generator, testing-reality-checker, engineering-privacy-engineer, engineering-identity-access-engineer, engineering-payments-billing-engineer, engineering-database-reliability-engineer, engineering-minimal-change-engineer |
| `codex`         | ai-dev, backend-go-dev, backend-python-dev, debugger, frontend-dev, rag-engineer, spec-writer, tester                                                                                                                                                                                                                                               |
| `auto`          | ai-architect, general-purpose                                                                                                                                                                                                                                                                                                                       |
| `api`           | researcher（api_profile: research。探索は Claude 自身） |

### api セクション

API 機能は `orchex[llm]` で導入する。キーは環境変数または `orchex auth set <provider>` で登録し、YAML には保存しない。
`api.providers` は anthropic / openai / gemini ごとの設定、`api.profiles` は用途に依存しない呼び出し設定を持つ。
既定のプロファイルは research（gemini、Web 検索と添付を許可）だけ。各社の既定モデルは配布する `cli-tools.yaml` を参照する。

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
api:
  profiles:
    review:
      provider: anthropic
      # model を省略すると api.providers.anthropic.model を使う
      features:
        web_search: false
        attachments: true
      limits:
        timeout_sec: 60
        max_output_tokens: 4096
```

`orchex llm ask --profile review "質問"` で呼び出せる。プロファイルの追加にコード変更は不要。
local は既存の deep_merge 規則に従い、mapping はキーごとに上書きし、配列は置き換える。

| 設定 | 意味 |
|------|------|
| `api.providers.<provider>.enabled` | 真偽値。省略時は true |
| `api.providers.<provider>.model` | プロファイルで model を省略した場合のモデル |
| `api.providers.<provider>.model_allowlist` | モデル候補の文字列配列。範囲外は警告して実行する |
| `api.profiles.<name>.provider` | anthropic / openai / gemini のいずれか |
| `api.profiles.<name>.model` | プロバイダの model より優先する任意の上書き |
| `api.profiles.<name>.features.web_search` | 真偽値。省略時は false。許可時だけ各社の検索機能を付ける |
| `api.profiles.<name>.features.attachments` | 真偽値。省略時は false。`--attach` を許可する |
| `api.profiles.<name>.limits.timeout_sec` | 正の有限数の秒。省略時は120秒 |
| `api.profiles.<name>.limits.max_output_tokens` | 正の整数。省略時は4096 |

プロファイル名とモデルは英数字および `_ . : / @ + = -` の一行で指定する。
添付は PNG/JPEG/WebP/GIF/PDF を扱い、Gemini の GIF は拒否する。
`llm ask` は通常1回だけ呼び出す。一時的な `rate_limited` の場合だけ、2秒、4秒と待って
合計3回まで試行する。`quota_exhausted` と `auth_error` は再試行しない。
SDK 内部の自動再試行は無効にする。

キーの解決順は `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` → `~/.config/orchex/credentials`。
空の環境変数は未設定として扱う。`GOOGLE_API_KEY` は orchex のキー解決には使わない。
`auth status` は設定有無だけを表示する。
プロバイダ無効 → キー未設定 → SDK 未導入の順で最初の理由を選び、routing は claude-direct に戻して note に示す。
プロファイルが不正・未定義の場合も direct に戻す。`[Resolved Routing]` にはキーを含めない。

| llm ask の終了コード | 意味 |
|---------------------|------|
| 0 | 成功 |
| 2 | 引数エラー |
| 3 | キー未設定（auth set の案内付き） |
| 4 | プロバイダ無効 |
| 5 | SDK 未導入 |
| 6 | タイムアウト |
| 7 | API エラー |
| 8 | 設定・添付・ファイル入出力のエラー |
| 9 | 利用枠・残高不足（quota_exhausted） |
| 10 | 再試行後も続く一時的なレート制限（rate_limited） |
| 11 | API の認証・権限エラー（auth_error） |

エラー分類にはHTTPステータスと機械向けコードだけを使い、本文・ヘッダ・キーは出力しない。
OpenAI の429は `insufficient_quota` の場合に利用枠不足、それ以外は一時的な制限とする。
Anthropic は402、`billing_error`、支出上限コードを利用枠不足として扱う。
Gemini は `RESOURCE_EXHAUSTED` の `QuotaFailure.violations[].quotaId` に `PerDay` がある場合を
利用枠不足とし、分単位や詳細のない429は一時的な制限とする。401/403は認証エラー、
Gemini の400に含まれる `ErrorInfo.reason=API_KEY_INVALID` も認証エラーとする。

### 読み取り道具で探索するエージェント

`orchex llm agent --profile repo-reader [--project DIR] [--output DIR] "質問"` で、
API側のモデルがリポジトリを探索する。呼び出すサブエージェントの用途には依存しない。
既定のresearcher設定やroutingは変更せず、プロファイルは明示的に選ぶ。

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
api:
  profiles:
    repo-reader:
      provider: gemini
      tools:
        repo_read: true
      deny:
        - "docs/internal/*"
      limits:
        timeout_sec: 120
        max_output_tokens: 8192
        max_turns: 30
        max_total_tokens: 2000000
        max_duration_sec: 900
        rate_limit_attempts: 3
        rate_limit_initial_delay_sec: 2.0
```

| agent用設定 | 意味・省略時の値 |
|------------|----------------|
| `tools.repo_read` | 真偽値。省略時false。read_file / list_dir / glob / grepを許可する |
| `deny` | 既定の拒否リストに追加する文字列配列。省略時は空。配列のlocal上書きは置換 |
| `limits.max_turns` | 正の整数。探索の上限、既定30往復 |
| `limits.max_total_tokens` | 正の整数。成功した往復の入力＋出力の累計、既定2,000,000 |
| `limits.max_duration_sec` | 正の有限数。探索の実行時間上限、既定900秒 |
| `limits.rate_limit_attempts` | 正の整数。429の初回を含む試行数、既定3 |
| `limits.rate_limit_initial_delay_sec` | 0以上の有限数。429の初回待ち時間、既定2秒。その後は2倍 |

道具の不許可・拒否は結果としてモデルへ返す。根拠はシステム指示で主張ごとに要求し、
最後の回答を `{"answer": "本文", "citations": [...]}` として取り出す。
回答全体を包む1つのjsonコードフェンスも受理する。構造化できない本文はそのまま返し、根拠は空にする。
根拠のverifiedは、ファイルの実在・行範囲・その実行での読取記録の3条件で決める。
不正な根拠要素も未確認として残す。verifiedは主張の意味的な正しさを保証するものではない。

探索上限では未実行の道具にエラー結果を渡し、道具なしのまとめを1回だけ要求する。
rate_limitedだけを設定どおり再試行し、quota_exhausted / auth_errorは即時停止する。
再試行待ちも実行時間に含める。実行時間の判定はAPI要求の間で行い、
進行中の要求はtimeout_secに従う。上限後のまとめは探索の予算外で行う。
まとめがAPIエラーで失敗した場合は、そのエラーを停止理由にして直前の回答を保持する。

結果JSONは次の形式で、成功・部分結果とも同じフィールドを持つ。

```json
{
  "status": "complete",
  "stop_reason": null,
  "turns": 2,
  "tokens": {"input": 100, "output": 30, "total": 130},
  "answer": "回答本文",
  "citations": [
    {"path": "src/a.py", "start_line": 10, "end_line": 20, "claim": "主張", "verified": true}
  ],
  "files_read": ["src/a.py"],
  "unverified_count": 0
}
```

turnsとtokensは成功した往復だけを数え、まとめも含む。失敗した試行の使用量は含めない。
途中停止時にも最後の回答・読取一覧・使用量を保持する。SDKの生データは公開せず、キーのエコーは伏せる。

`--output` がなければ結果JSONをstdoutへ返す。指定時は、空の出力先を実行開始前に確認・占有し、
途中のturnsとtokensを `progress.json`、結果を `result.json`、最後に終了コードと改行を `exit` へ書く。
ファイルは一時ファイルから原子的に置き換える。APIエラーでも部分結果とexitを残す。
設定・利用可否のエラーでは結果がない場合もexitを残す。使用済み出力先には書き込まない。
入出力自体が失敗した場合は、ファイルの完備を保証できない。

| llm agentの終了コード | status / stop_reason または意味 |
|----------------------|--------------------------------|
| 0 | complete / null |
| 12 | partial / max_turns・token_limit・timeout（探索上限） |
| 9 | partial / quota_exhausted |
| 10 | partial / rate_limited（再試行後も回復しない） |
| 11 | partial / auth_error |
| 6 | partial / api_timeout（1要求のタイムアウト） |
| 7 | partial / api_error |
| 2 | 引数エラー |
| 3 / 4 / 5 | キー未設定 / プロバイダ無効 / SDK未導入 |
| 8 | 設定・ファイル入出力のエラー |
| 13 | 空でない出力先。実行を開始しない |

### 道具付きの1往復を呼ぶ部品

後続の実装からは `lib.llm.providers.call_tool_turn(provider, request, api_key)` を利用できる。
`lib.llm.tool_types` の `ToolSpec`、`UserMessage`、`ToolTurnRequest` で要求を作り、返された
`ToolTurnResponse.message` の `ToolCall`（ID・名前・JSON引数）を受け取る。
次の要求には、元のメッセージ列、返された `AssistantMessage`、
呼び出しIDに対応する `ToolResult` をまとめた `ToolResultsMessage` を渡す。
`AssistantMessage.provider_state` は署名などを保持する不透明な状態なので、同じプロバイダへそのまま戻す。
`ToolResult.is_error` は、失敗結果をJSONの `error` フィールドとしてモデルへ返す指定である。
`SystemMessage` はシステムの指示を表す。`ToolTurnResponse.usage` は
`TokenUsage(input_tokens, output_tokens)` で、使用量がない場合は0を返す。

この部品は `any-llm-sdk[gemini]==1.55.0` で3社の形式を変換する。SDKとライブラリは呼び出し時に
読み込むため、基本インストールでは認証・既存CLIの起動に影響しない。
道具の実行とエージェントループは `lib.llm.agent_loop.run_agent_loop` が担当し、この入口自身は再試行しない。
再試行が必要な利用側は `lib.llm.retry.call_with_rate_limit_retry` にoperation、
`max_attempts`（初回を含む回数）、`initial_delay_sec`、必要なら `backoff`（既定2）を渡す。
詳細な選定根拠は [ADR-068](../adr/ADR-20261127-068.md) を参照。

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

機能フラグの管理。Issue #153 で `quality_gate` / `context_optimization` / `evaluation_set_check` の
機能フラグと `paths.state_dir` を `quality-gates.json`（次節）へ分離し、v3 では audit 固有のフラグ
（`route_audit` / `kpi_scorecard`）だけが残る。

```json
{
  "version": 3,
  "features": {
    "route_audit": {
      "enabled": true,
      "max_excerpt_chars": 160
    },
    "kpi_scorecard": {
      "enabled": true,
      "default_period_days": 7
    }
  },
  "paths": {
    "logs_dir": ".claude/logs/audit"
  }
}
```

| 機能            | 説明                   | デフォルト |
| --------------- | ---------------------- | ---------- |
| `route_audit`   | ルーティング実績の記録 | 有効       |
| `kpi_scorecard` | KPI スコアカード生成   | 有効       |

**移行**: 既存プロジェクトの `audit-flags.local.json` に旧 `quality_gate` / `context_optimization` /
`evaluation_set_check` / `paths.state_dir` が残っていても、`quality-gates` 側のローダーはこれを読まない。
SessionStart 時に `audit-bootstrap.py` が該当を検出すると移行案内を 1 行出力するので、
`.claude/config/quality-gates/quality-gates.local.json` へ移す。

---

## quality-gates.json

**パス:** `.claude/config/quality-gates/quality-gates.json`
**パッケージ:** quality-gates

quality-gates の機能フラグと状態ディレクトリの管理。Issue #153 で `audit-flags.json` から分離した。

```json
{
  "version": 1,
  "features": {
    "quality_gate": {
      "enabled": true,
      "block_on_failed_test": true,
      "test_file_threshold": 3,
      "test_line_threshold": 100
    },
    "context_optimization": {
      "enabled": true,
      "read_line_threshold": 200,
      "max_file_size_bytes": 5242880
    },
    "evaluation_set_check": {
      "enabled": true
    }
  },
  "paths": {
    "state_dir": ".claude/state"
  }
}
```

| 機能                   | 説明                                                     | デフォルト |
| ---------------------- | -------------------------------------------------------- | ---------- |
| `quality_gate`         | 品質ゲート（レビュー提案・テスト分析・ブロック等）の設定 | 有効       |
| `context_optimization` | 大きすぎる読み込みや `cat` 利用の抑制                    | 有効       |
| `evaluation_set_check` | 評価セット突合案内の有効/無効                            | 有効       |

`.claude/config/quality-gates/quality-gates.local.json`（opt-out 例）:

```json
{
  "features": {
    "quality_gate": {
      "block_on_failed_test": false
    }
  }
}
```

**旧 `audit-flags.local.json` からの移行**: 実効値の優先順位（後勝ち）は次のとおり。

1. `quality-gates.json`（base）
2. `quality-gates.local.json`（最優先）

`audit-flags.local.json` に残る旧キーは読まれない。詳細は `packages/quality-gates/README.md`
「設定キー」節を参照。

---

## task-memory.yaml

**パス:** `.claude/config/core/task-memory.yaml`
**パッケージ:** core

plan ファイル（`.claude/plans/<slug>.md`）によるタスク管理の設定。plan ファイルの置き場所は固定で、設定では変えられない
（旧キー `plans_file` は ADR-20261027-059 で廃止。残っていても読まれない）。

```yaml
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
| quality-gates.json   | 次回の hook 発火時（即時）                       |
| task-memory.yaml     | 次回セッション開始時（SessionStart hook）        |
| ベースファイル全般   | SessionStart 時に `sync-orchestra.py` で自動同期 |
