# Packages

AI Orchestra のパッケージ一覧と詳細。`packages/*/agents` と `packages/*/config` は `.claude/` へ同期される配布元です。

## パッケージ概要

| パッケージ                                          | 概要                                                                           | カテゴリ     |
| --------------------------------------------------- | ------------------------------------------------------------------------------ | ------------ |
| [core](#core)                                       | 全パッケージ共通の基盤ライブラリ                                               | 基盤         |
| [agent-routing](#agent-routing)                     | cli-tools.yaml 駆動のエージェントルーティング提案                              | 基盤         |
| [quality-gates](#quality-gates)                     | 実装後レビュー・テスト分析・自動 lint の品質ゲート                             | 品質         |
| [loop-harness](#loop-harness)                       | Issue 起点の Maker / Checker 反復と PR レビュー対応を安全に駆動                | ハーネス     |
| [docker-runtime](#docker-runtime)                   | ハーネス共通の Docker / broker ライフサイクル基盤                              | 基盤         |
| [meta-harness](#meta-harness)                       | 候補ハーネス・スキル・ルーティング設定の評価・進化基盤（Docker 隔離実行 + propose/promote/loop） | ハーネス     |
| [codd](#codd)                                       | ドキュメント依存グラフの scan / validate（整合性レイヤー）                     | 整合性       |
| [audit](#audit)                                     | 統一イベントログによるオーケストレーション監査基盤                             | 監査         |
| [codex-suggestions](#codex-suggestions)             | ファイル編集・プラン完了時の Codex 相談提案                                    | 提案         |
| [codex-harness](#codex-harness)                     | Codex CLI 向け repo-local ハーネス（hooks/rules/schemas + 非対話 run・review） | ハーネス     |
| [antigravity-suggestions](#antigravity-suggestions) | Web 検索・fetch 時の Antigravity リサーチ提案                                  | 提案         |
| [git-workflow](#git-workflow)                       | Git/GitHub ワークフロー（Issue・PR・開発フロー）                               | ワークフロー |
| [cocoindex](#cocoindex)                             | cocoindex MCP サーバーの自動プロビジョニング                                   | MCP          |
| [tmux-monitor](#tmux-monitor)                       | tmux でサブエージェント出力をリアルタイム監視（opt-in、`setup all` 対象外）    | 監視         |

---

## 各パッケージ詳細

### core

全パッケージ共通の基盤ライブラリ。タスク状態管理・プランゲート制御など、オーケストレーション基盤を担う。

- **バージョン**: 0.4.0
- **依存**: なし

**提供するもの:**

- hooks: `load-task-state.py`, `clear-plan-gate.py`, `check-plan-gate.py`, `set-plan-gate.py`, `inject-shared-context.py`, `capture-task-result.py`, `update-working-context.py`, `cleanup-session-context.py`
- ユーティリティ: `hook_common.py`（全 hook 共通ライブラリ）, `log_common.py`, `context_store.py`
- skills (facet build): `preflight`, `startproject`, `task-state`, `design`
- rules (facet build): `config-loading`, `coding-principles`, `task-memory-usage`, `context-sharing`
- config: `task-memory.yaml`

---

### agent-routing

`cli-tools.yaml` に基づいてエージェントをルーティング提案する。28 エージェントの定義と使い方ルールを管理し、`.claude/agents/` に同期される配布元でもある。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks: `agent-router.py`（UserPromptSubmit で自動提案）
- agents: 28 エージェント定義（planner, architect, code-reviewer, general-purpose 等）
- rules (facet build): `orchestra-usage`, `agent-routing-policy`
- config: `cli-tools.yaml`（モデル名・サンドボックス・フラグの一元管理）

---

### quality-gates

実装後の品質チェックを自動化する。コード編集時にファイル種別ごとの lint / format・レビュー提案・テスト分析を実行する。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks:
  - `post-implementation-review.py` — Edit/Write 後にレビュー提案
  - `post-test-analysis.py` — Bash 実行後にテスト結果分析
  - `lint-on-save.py` — Edit/Write 後にファイル種別ごとの自動 lint / format
  - `test-tampering-detector.py` — PostToolUse で skip/disable 追加やテスト削除を警告
  - `test-gate-checker.py` — テスト品質チェック
- skills (facet build): `review`, `tdd`, `design-tracker`, `release-readiness`
- rules (facet build): `skill-review-policy`

---

### loop-harness

Issue 起点の反復ループを、永続 state / journal、lease fencing、two-phase の `propose` / `complete` 契約で安全に駆動する。LP-1 では `/loop-issue` が Maker と Checker を分離し、機械検証・LLM レビュー・外部 PR レビュー対応から成功／失敗／安全停止までをオーケストレーションする。

- **バージョン**: 0.1.0
- **依存**: audit, quality-gates, git-workflow

**提供するもの:**

- lib: `loop_common.py`（状態機械・lease・ガード・artifact）、`loop_definition.py`（設定解決）、`worktree_manager.py`（Issue worktree）、`pr_review_wait.py`（外部レビュー待機・指摘取り込み）
- scripts: `loop_step.py`（LP-1 の JSON CLI。start / attach / resume、propose / complete、reconcile / heartbeat、Checker 実行）
- skills (facet build): `loop-issue`（Issue 消化ループの LP-1 オーケストレーター）
- config: `loop-harness.yaml`, `loops/issue-loop.yaml`

---

### docker-runtime

meta-harness と loop-harness が共有する Docker CLI、hardened security profile、dual-homed broker、
所有 container/network の cleanup を提供する。worktree・git・成果物・設定スキーマは各 harness に残す。

- **バージョン**: 0.1.0
- **依存**: core

---

### meta-harness

候補ハーネス（`claude-harness` 自身 / `skill:<name>` / `routing-config`）の評価・進化基盤。store I/O・ledger 畳み込み・Pareto 判定・schema 検証（Phase 1a）、Docker コンテナ隔離下での evaluate 実行（Phase 1b）、population ベースの propose/promote（Phase 2）、自動探索 loop（Phase 3）を提供する。

- **バージョン**: 0.1.0
- **依存**: core, docker-runtime

**提供するもの:**

- scripts: `meta_harness.py` — CLI（全 9 サブコマンド）
  - Phase 1a: `init` / `register` / `frontier` / `status` / `purge`
  - Phase 1b: `evaluate`（Docker コンテナ隔離実行、docker-runtime 依存）
  - Phase 2: `propose` / `promote`
  - Phase 3: `loop`（自動探索）
- config: `meta-harness.yaml`（store・シナリオ・`config_patch.allowlist` 等）
- target 種別: `claude-harness`（既定、own）／ `skill:<name>`（skill-evolution 連携）／ `routing-config`（`cli-tools.yaml` の `agents.*.tool` / `codex.model` / `antigravity.model` へのパッチ候補、Phase A）

---

### codd

ドキュメント間の依存関係をフロントマター（`codd:` ブロック）で宣言し、`scan` で依存グラフを構築、`validate` で整合性（リンク切れ・重複・循環・孤立・ドリフト・欠落）を検証する整合性レイヤー。essential プリセットに含まれ常時有効。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- lib: `codd_common.py`（フロントマター parser + グラフモデル + config ローダー）
- scripts:
  - `codd.py` — `scan`（依存グラフ構築 → `.claude/codd/graph.jsonl`）/ `validate`（整合性検証）/ `graph`（テキスト可視化）
- skills (facet build): `codd-scan`, `codd-validate`
- rules (facet build): `codd-frontmatter-policy`
- config: `codd.yaml`（scope glob・kind/relation 語彙・検査レベル・グラフ保存先）

---

### audit

統一イベントログによるオーケストレーション監査基盤。ルーティング監査・CLI 呼び出し記録・サブエージェント追跡をセッション単位の統一ログに集約する。旧 `route-audit` + `cli-logging` を統合・再設計したパッケージ。

- **バージョン**: 1.0.0
- **依存**: core, agent-routing

**提供するもの:**

- hooks:
  - `audit-bootstrap.py` — SessionStart 時にセッションログ初期化 + session_start イベント
  - `audit-session-end.py` — SessionEnd 時にセッションサマリー記録
  - `audit-prompt.py` — UserPromptSubmit で期待ルート予測 + prompt イベント
  - `audit-route.py` — PostToolUse でルーティング監査 + quality_gate 検出
  - `audit-cli.py` — PostToolUse:Bash で Codex/Antigravity CLI 呼び出しを記録
  - `audit-subagent-start.py` / `audit-subagent-end.py` — サブエージェントのライフサイクル記録
- ライブラリ: `event_logger.py`（統一スキーマ v1 + セッションローテーション）
- scripts:
  - `dashboard.py` — 運用ダッシュボード
  - `log-viewer.py` — イベントログ閲覧
  - `kpi-report.py` — KPI レポート生成
  - `analyze-cli-usage.py` — CLI 使用状況分析
- config: `delegation-policy.json`, `audit-flags.json`

---

### codex-suggestions

ファイル編集前に Codex 相談を提案し、設計品質を高める。プラン完了後も Codex レビューを促す。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks:
  - `check-codex-before-write.py` — Edit/Write 前に `[Codex Suggestion]` を出力
  - `check-codex-after-plan.py` — Task 完了後に Codex レビューを提案
- skills (facet build): `codex-system`
- rules (facet build): `codex-delegation`, `codex-suggestion-compliance`
- context files (所有):
  - `AGENTS.md` — init/sync 時に配布（Codex CLI 用指示書）
  - `.codex/config.toml` — init 時に配布
  - `.codex/skills/context-loader/` — init 時に配布

---

### codex-harness

Codex CLI を主たる利用面とする repo-local ハーネス。hooks（secret scan / pre-tool-use policy / stop 時の JSON 検証）と非対話実行スクリプト（run / read-only review）を `.codex/` 配下に hash 保護付きで配布する。詳細設計は [docs/design/codex-cli-harness.md](../docs/design/codex-cli-harness.md) を参照。

- **バージョン**: 0.1.0
- **依存**: codex-suggestions

**提供するもの:**

- scripts:
  - `codex_run.py` — 非対話タスクモードで `codex exec --json` を実行し、run artifact 一式を保存
  - `codex_review.py` — read-only レビューモードで base ブランチとの diff を渡し、構造化 findings を保存
- context files（`codex_files`。hash 保護付き配布、`.codex/` 配下）:
  - `.codex/hooks.json`, `.codex/hooks/user_prompt_secret_scan.py`, `.codex/hooks/pre_tool_use_policy.py`, `.codex/hooks/stop_validate.py`
  - `.codex/schemas/task_result.schema.json`, `.codex/schemas/review_result.schema.json`
  - `.codex/rules/codex-harness.rules`, `.codex/validation.json`

---

### antigravity-suggestions

WebSearch/WebFetch の前に Antigravity CLI でのリサーチを提案し、最新情報へのアクセスを最適化する。

- **バージョン**: 0.1.0
- **依存**: core

**提供するもの:**

- hooks: `suggest-antigravity-research.py`（WebSearch/WebFetch 前に `[Antigravity Suggestion]` を出力）
- skills (facet build): `antigravity-system`
- rules (facet build): `antigravity-delegation`, `antigravity-suggestion-compliance`
- context files: なし（Antigravity 向け指示は codex-suggestions の `AGENTS.md` に `antigravity.md` セクションとして合成。旧 `.gemini/GEMINI.md` の生成物は context sync 時に自動削除される）

---

### git-workflow

GitHub Issue の登録・開発フロー・PR 作成を含む Git/GitHub ワークフローをスキルとして提供する。

- **バージョン**: 0.1.0
- **依存**: なし

**提供するもの:**

- skills (facet build):
  - `issue-create` — GitHub Issue の作成と計画策定
  - `issue-fix` — 計画→実装→テスト→レビューの開発フロー実行
  - `pr-create` — Pull Request の作成
- config: `sandbox-requirements.json`

---

### cocoindex

cocoindex-code MCP サーバーを Claude Code / Codex CLI / Antigravity CLI に自動プロビジョニングする。v1（stdio）と v2（proxy）の2モードに対応。

- **バージョン**: 0.2.0
- **依存**: core

**提供するもの:**

- hooks:
  - `provision-mcp-servers.py` — SessionStart 時に各 CLI の MCP 設定を生成
  - `stop-mcp-proxy.py` — SessionEnd 時に proxy を停止（v2 モード時）
  - `proxy_manager.py` — proxy 管理ユーティリティ
- rules (facet build): `cocoindex-usage`
- config: `cocoindex.yaml`

---

### tmux-monitor

tmux ペインでサブエージェントの起動・停止をリアルタイム表示する。マルチエージェント並列実行の可視化に使用する。

> **opt-in パッケージ**: `orchex setup all` には含まれないため、`orchex install tmux-monitor` で明示的にインストールする。

- **バージョン**: 0.2.0
- **依存**: core

**提供するもの:**

- hooks:
  - `tmux-session-start.py` / `tmux-session-end.py` — セッション開始・終了時の tmux セットアップ
  - `tmux-pre-task.py` — Task 実行前の準備
  - `tmux-subagent-start.py` / `tmux-subagent-stop.py` — サブエージェント起動・停止の表示
  - `tmux-format-output.py` — 出力フォーマット
  - `tmux_common.py` — 共通ユーティリティ
