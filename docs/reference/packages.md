# パッケージリファレンス

AI Orchestra の全パッケージ一覧と詳細。

---

## 概要

| パッケージ | 概要 | カテゴリ |
|-----------|------|----------|
| [core](#core) | 全パッケージ共通の基盤ライブラリ | 基盤 |
| [agent-routing](#agent-routing) | cli-tools.yaml 駆動のエージェントルーティング提案 | 基盤 |
| [quality-gates](#quality-gates) | 実装後レビュー・テスト分析・自動 lint | 品質 |
| [route-audit](#route-audit) | ルーティング監査・KPI レポート | 監査 |
| [cli-logging](#cli-logging) | Codex/Gemini CLI ログ記録と分析 | ログ |
| [codex-suggestions](#codex-suggestions) | ファイル編集時の Codex 相談提案 | 提案 |
| [gemini-suggestions](#gemini-suggestions) | Web 検索時の Gemini リサーチ提案 | 提案 |
| [issue-workflow](#issue-workflow) | GitHub Issue 起票と開発フロー | ワークフロー |
| [cocoindex](#cocoindex) | cocoindex MCP サーバーの自動プロビジョニング | MCP |
| [tmux-monitor](#tmux-monitor) | tmux でサブエージェント出力をリアルタイム監視 | 監視 |

### プリセット

| プリセット | 含まれるパッケージ |
|-----------|------------------|
| `essential` | core, route-audit, quality-gates |
| `all` | 全パッケージ |

---

## core

全パッケージ共通の基盤ライブラリ。タスク状態管理・プランゲート制御・コンテキスト共有など、オーケストレーションの土台を担う。

- **バージョン**: 0.4.0
- **依存**: なし（他の全パッケージがこれに依存）

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `load-task-state.py` | SessionStart: Plans.md からタスク状態を読み込みサマリーを出力 |
| hook | `set-plan-gate.py` | ExitPlanMode: プランゲートを設定 |
| hook | `check-plan-gate.py` | PreToolUse: プランゲートの確認 |
| hook | `clear-plan-gate.py` | PostToolUse: プランゲートのクリア |
| hook | `inject-shared-context.py` | PreToolUse(Agent): サブエージェントに共有コンテキストを注入 |
| hook | `capture-task-result.py` | PostToolUse(Agent): サブエージェント結果を記録 |
| hook | `update-working-context.py` | PostToolUse(Edit/Write): 変更ファイルを working-context に追記 |
| hook | `cleanup-session-context.py` | SessionEnd: セッションコンテキストをクリーンアップ |
| util | `hook_common.py` | 全 hook 共通ユーティリティ（config 読み込み、JSON 操作等） |
| util | `log_common.py` | ログ関連ユーティリティ |
| util | `context_store.py` | コンテキスト共有ストア |
| skill | `/preflight` | 実装計画の策定 |
| skill | `/startproject` | マルチエージェント協調で新規開発を開始 |
| skill | `/checkpointing` | セッションコンテキストの保存・復元 |
| skill | `/task-state` | Plans.md の作成・更新 |
| rule | `config-loading.md` | 設定ファイルのレイヤード構成ルール |
| rule | `coding-principles.md` | コード品質の共通ルール |
| rule | `skill-review-policy.md` | レビュー系スキルのポリシー |
| rule | `task-memory-usage.md` | Plans.md によるタスク管理ルール |
| rule | `context-sharing.md` | CLI 間コンテキスト共有ルール |
| config | `task-memory.yaml` | Plans.md のパス・マーカー定義 |

---

## agent-routing

`cli-tools.yaml` に基づいてエージェントをルーティング提案する。28 エージェントの定義と使い方ルールを管理する。

- **バージョン**: 0.1.0
- **依存**: core

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `agent-router.py` | UserPromptSubmit: プロンプトからエージェントを検出し提案 |
| util | `route_config.py` | ルーティング設定読み込み・エージェント検出ロジック |
| agents | 28 定義 | planner, architect, code-reviewer, general-purpose 等 |
| rule | `orchestra-usage.md` | AI Orchestra 使用ガイド |
| rule | `agent-routing-policy.md` | エージェントルーティングポリシー |
| config | `cli-tools.yaml` | モデル名・サンドボックス・フラグの一元管理 |

### エージェント一覧

| カテゴリ | エージェント |
|---------|------------|
| Planning | planner, researcher, requirements |
| Design | architect, api-designer, data-modeler, auth-designer, spec-writer |
| Implementation | frontend-dev, backend-python-dev, backend-go-dev, ai-dev, rag-engineer |
| AI/ML | ai-architect, prompt-engineer |
| Test/Debug | debugger, tester |
| Review | code-reviewer, security-reviewer, performance-reviewer, spec-reviewer, architecture-reviewer, ux-reviewer |
| Docs | docs-writer |
| Utility | general-purpose, specialized-mcp-builder, support-executive-summary-generator, testing-reality-checker |

---

## quality-gates

実装後の品質チェックを自動化する。コード編集時に lint・レビュー提案・テスト分析を実行する。

- **バージョン**: 0.1.0
- **依存**: core

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `post-implementation-review.py` | PostToolUse(Edit/Write): 一定量の変更後にレビューを提案 |
| hook | `post-test-analysis.py` | PostToolUse(Bash): テスト実行後に結果を分析 |
| hook | `lint-on-save.py` | PostToolUse(Edit/Write): 自動 lint（ruff）実行 |
| hook | `test-gate-checker.py` | PreToolUse: テスト品質ゲートチェック |
| skill | `/review` | マルチエージェントコードレビュー（スマート選定） |
| skill | `/tdd` | テスト駆動開発ワークフロー |
| skill | `/design-tracker` | 設計記録 |
| skill | `/release-readiness` | リリース前最終チェック |

---

## route-audit

エージェントルーティングの期待値予測・実績照合・KPI 集計を行う管理者向けパッケージ。

- **バージョン**: 0.2.0
- **依存**: core, agent-routing

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `orchestration-bootstrap.py` | SessionStart: 監査ログの初期化 |
| hook | `orchestration-expected-route.py` | UserPromptSubmit: 期待ルートの予測・記録 |
| hook | `orchestration-route-audit.py` | PostToolUse: 実績ルートの記録・照合 |
| script | `log-viewer.py` | ルーティングログの閲覧 |
| script | `dashboard.py` | ルーティング状況ダッシュボード |
| script | `orchestration-kpi-report.py` | KPI レポート生成 |
| config | `delegation-policy.json` | ルーティングルール定義 |
| config | `orchestration-flags.json` | 機能フラグ（route_audit, quality_gate 等） |

### スクリプト実行

```bash
orchex run route-audit dashboard
orchex run route-audit log-viewer --project . -- --last 10
orchex run route-audit orchestration-kpi-report
```

---

## cli-logging

Codex/Gemini CLI の呼び出し履歴を記録し、後から分析できるようにする。

- **バージョン**: 0.1.0
- **依存**: core

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `log-cli-tools.py` | PostToolUse(Bash): CLI 呼び出しを検出しログ記録 |
| script | `analyze-cli-usage.py` | CLI 使用状況の集計・分析 |

---

## codex-suggestions

ファイル編集前に Codex 相談を提案し、設計品質を高める。

- **バージョン**: 0.1.0
- **依存**: core

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `check-codex-before-write.py` | PreToolUse(Edit/Write): `[Codex Suggestion]` を出力 |
| hook | `check-codex-after-plan.py` | PostToolUse(Task): プラン完了後に Codex レビューを提案 |
| skill | `/codex-system` | Codex CLI 利用ガイド |
| rule | `codex-delegation.md` | Codex CLI 委譲ルール |
| rule | `codex-suggestion-compliance.md` | Codex 提案への遵守ルール |

### 発火条件

`check-codex-before-write.py` は以下の条件で `[Codex Suggestion]` を出力する:

- `core/` を含むファイルパスへの変更
- `config` や `class` 等のキーワードを含む変更内容
- 大きなコンテンツを含む新規ファイル作成

---

## gemini-suggestions

WebSearch/WebFetch の前に Gemini CLI でのリサーチを提案する。

- **バージョン**: 0.1.0
- **依存**: core

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `suggest-gemini-research.py` | PreToolUse(WebSearch/WebFetch): `[Gemini Suggestion]` を出力 |
| skill | `/gemini-system` | Gemini CLI 利用ガイド |
| rule | `gemini-delegation.md` | Gemini CLI 委譲ルール |
| rule | `gemini-suggestion-compliance.md` | Gemini 提案への遵守ルール |

---

## issue-workflow

GitHub Issue の起票から実装・テスト・レビューまでの一連の開発フローを提供する。

- **バージョン**: 0.1.0
- **依存**: なし

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| skill | `/issue-create` | GitHub Issue の作成と計画策定 |
| skill | `/issue-fix` | 計画→実装→テスト→レビューの開発フロー |
| config | `sandbox-requirements.json` | sandbox 設定（`gh` コマンドの除外） |

---

## cocoindex

cocoindex-code MCP サーバーを Claude Code / Codex CLI / Gemini CLI に自動プロビジョニングする。

- **バージョン**: 0.2.0
- **依存**: core

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `provision-mcp-servers.py` | SessionStart: 各 CLI の MCP 設定を生成 |
| hook | `stop-mcp-proxy.py` | SessionEnd: proxy を停止（v2 モード時） |
| util | `proxy_manager.py` | proxy 管理ユーティリティ |
| rule | `cocoindex-usage.md` | cocoindex MCP サーバーの利用ルール |
| config | `cocoindex.yaml` | MCP サーバー設定（stdio/proxy モード） |

### 動作モード

| モード | 説明 |
|--------|------|
| v1（stdio） | 各 CLI が個別に MCP サーバーを起動（デフォルト） |
| v2（proxy） | mcp-proxy で単一プロセス化（`.local.yaml` で `proxy.enabled: true`） |

---

## tmux-monitor

tmux ペインでサブエージェントの起動・停止をリアルタイム表示する。

- **バージョン**: 0.2.0
- **依存**: core

### コンポーネント

| 種別 | 名前 | 説明 |
|------|------|------|
| hook | `tmux-session-start.py` | SessionStart: tmux セットアップ |
| hook | `tmux-session-end.py` | SessionEnd: tmux クリーンアップ |
| hook | `tmux-pre-task.py` | PreToolUse(Agent): タスク実行前の準備 |
| hook | `tmux-subagent-start.py` | SubagentStart: サブエージェント起動表示 |
| hook | `tmux-subagent-stop.py` | SubagentStop: サブエージェント停止表示 |
| hook | `tmux-format-output.py` | PostToolUse: 出力フォーマット |
| util | `tmux_common.py` | tmux 操作の共通ユーティリティ |

### 有効化

`orchestration-flags.json` で有効化する:

```json
{
  "features": {
    "tmux_monitoring": {
      "enabled": true
    }
  }
}
```

---

## パッケージ管理コマンド

```bash
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
```
