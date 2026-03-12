# Packages

AI Orchestra のパッケージ一覧と詳細。

## パッケージ概要

| パッケージ | 概要 | カテゴリ |
|-----------|------|----------|
| [core](#core) | 全パッケージ共通の基盤ライブラリ | 基盤 |
| [agent-routing](#agent-routing) | cli-tools.yaml 駆動のエージェントルーティング提案 | 基盤 |
| [quality-gates](#quality-gates) | 実装後レビュー・テスト分析・自動 lint の品質ゲート | 品質 |
| [route-audit](#route-audit) | 期待ルート予測とルーティング監査（管理者向け） | 監査 |
| [cli-logging](#cli-logging) | Codex/Gemini CLI 呼び出しのログ記録と分析 | ログ |
| [codex-suggestions](#codex-suggestions) | ファイル編集・プラン完了時の Codex 相談提案 | 提案 |
| [gemini-suggestions](#gemini-suggestions) | Web 検索・fetch 時の Gemini リサーチ提案 | 提案 |
| [issue-workflow](#issue-workflow) | GitHub Issue の登録と計画→実装→テスト→レビューの開発フロー | ワークフロー |
| [cocoindex](#cocoindex) | cocoindex MCP サーバーの自動プロビジョニング | MCP |
| [tmux-monitor](#tmux-monitor) | tmux でサブエージェント出力をリアルタイム監視 | 監視 |

---

## 各パッケージ詳細

### core

全パッケージ共通の基盤ライブラリ。タスク状態管理・プランゲート制御など、オーケストレーション基盤を担う。

- **バージョン**: 0.3.0
- **依存**: なし

**提供するもの:**

- hooks: `load-task-state.py`, `clear-plan-gate.py`, `check-plan-gate.py`, `set-plan-gate.py`
- ユーティリティ: `hook_common.py`（全 hook 共通ライブラリ）, `log_common.py`
- skills: `/preflight`, `/startproject`, `/checkpointing`, `/task-state`
- rules: `config-loading.md`, `coding-principles.md`, `skill-review-policy.md`, `task-memory-usage.md`
- config: `task-memory.yaml`

---

### agent-routing

`cli-tools.yaml` に基づいてエージェントをルーティング提案する。25 エージェントの定義と使い方ルールを管理する。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks: `agent-router.py`（UserPromptSubmit で自動提案）
- agents: 25 エージェント定義（planner, architect, code-reviewer, general-purpose 等）
- rules: `orchestra-usage.md`, `agent-routing-policy.md`
- config: `cli-tools.yaml`（モデル名・サンドボックス・フラグの一元管理）

---

### quality-gates

実装後の品質チェックを自動化する。コード編集時に lint・レビュー提案・テスト分析を実行する。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks:
  - `post-implementation-review.py` — Edit/Write 後にレビュー提案
  - `post-test-analysis.py` — Bash 実行後にテスト結果分析
  - `lint-on-save.py` — Edit/Write 後に自動 lint
  - `test-gate-checker.py` — テスト品質チェック
- skills: `/review`, `/tdd`, `/simplify`, `/design-tracker`, `/release-readiness`

---

### route-audit

エージェントルーティングの期待値予測・実績照合・KPI 集計を行う管理者向けパッケージ。

- **バージョン**: 0.2.0
- **依存**: core, agent-routing

**提供するもの:**

- hooks:
  - `orchestration-bootstrap.py` — SessionStart 時に初期化
  - `orchestration-expected-route.py` — UserPromptSubmit で期待ルートを予測
  - `orchestration-route-audit.py` — PostToolUse で実績ルートを記録
- scripts:
  - `log-viewer.py` — ルーティングログ閲覧
  - `dashboard.py` — ルーティング状況ダッシュボード
  - `orchestration-kpi-report.py` — KPI レポート生成
- config: `delegation-policy.json`, `orchestration-flags.json`

---

### cli-logging

Codex/Gemini CLI の呼び出し履歴を記録し、後から分析できるようにする。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks: `log-cli-tools.py`（Bash PostToolUse で CLI 呼び出しを検出・記録）
- scripts: `analyze-cli-usage.py`（使用状況の集計・分析）

---

### codex-suggestions

ファイル編集前に Codex 相談を提案し、設計品質を高める。プラン完了後も Codex レビューを促す。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks:
  - `check-codex-before-write.py` — Edit/Write 前に `[Codex Suggestion]` を出力
  - `check-codex-after-plan.py` — Task 完了後に Codex レビューを提案
- skills: `/codex-system`
- rules: `codex-delegation.md`, `codex-suggestion-compliance.md`

---

### gemini-suggestions

WebSearch/WebFetch の前に Gemini CLI でのリサーチを提案し、最新情報へのアクセスを最適化する。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks: `suggest-gemini-research.py`（WebSearch/WebFetch 前に `[Gemini Suggestion]` を出力）
- skills: `/gemini-system`
- rules: `gemini-delegation.md`, `gemini-suggestion-compliance.md`

---

### issue-workflow

GitHub Issue の起票から実装・テスト・レビューまでの一連の開発フローをスキルとして提供する。

- **バージョン**: 0.1.0
- **依存**: なし

**提供するもの:**

- skills:
  - `/issue-create` — GitHub Issue の作成と計画策定
  - `/issue-fix` — 計画→実装→テスト→レビューの開発フロー実行
- config: `sandbox-requirements.json`

---

### cocoindex

cocoindex-code MCP サーバーを Claude Code / Codex CLI / Gemini CLI に自動プロビジョニングする。v1（stdio）と v2（proxy）の2モードに対応。

- **バージョン**: 0.2.0
- **依存**: core

**提供するもの:**

- hooks:
  - `provision-mcp-servers.py` — SessionStart 時に各 CLI の MCP 設定を生成
  - `stop-mcp-proxy.py` — SessionEnd 時に proxy を停止（v2 モード時）
  - `proxy_manager.py` — proxy 管理ユーティリティ
- rules: `cocoindex-usage.md`
- config: `cocoindex.yaml`

---

### tmux-monitor

tmux ペインでサブエージェントの起動・停止をリアルタイム表示する。マルチエージェント並列実行の可視化に使用する。

- **バージョン**: 0.2.0
- **依存**: core

**提供するもの:**

- hooks:
  - `tmux-session-start.py` / `tmux-session-end.py` — セッション開始・終了時の tmux セットアップ
  - `tmux-pre-task.py` — Task 実行前の準備
  - `tmux-subagent-start.py` / `tmux-subagent-stop.py` — サブエージェント起動・停止の表示
  - `tmux-format-output.py` — 出力フォーマット
  - `tmux_common.py` — 共通ユーティリティ
