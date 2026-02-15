# ログ仕様

このドキュメントは、`ai-orchestra` が出力するログ/集計ファイルの役割を整理したものです。  
対象時点: 2026-02-15

## 1. 主要ログ一覧

| ファイル | 主な出力元 | 役割 | 主な参照先 |
|---|---|---|---|
| `.claude/logs/cli-tools.jsonl` | `packages/cli-logging/hooks/log-cli-tools.py` | Codex/Gemini CLI 実行履歴（prompt/response/model/success） | `packages/cli-logging/scripts/analyze-cli-usage.py` |
| `.claude/logs/orchestration/events.jsonl` | `packages/core/hooks/log_common.py` 経由（`expected-route` / `route-audit` / `log-cli-tools`） | 統一イベントログ（時系列可視化用） | `packages/route-audit/scripts/log-viewer.py`, `packages/route-audit/scripts/dashboard.py` |
| `.claude/logs/orchestration/expected-routes.jsonl` | `packages/route-audit/hooks/orchestration-expected-route.py` | プロンプトごとの期待ルート判定履歴 | 監査時の詳細確認（手動） |
| `.claude/logs/orchestration/route-audit.jsonl` | `packages/route-audit/hooks/orchestration-route-audit.py` | 期待ルートと実際ルートの監査結果 | `packages/route-audit/scripts/orchestration-kpi-report.py` |
| `.claude/logs/orchestration/quality-gate.jsonl` | `packages/route-audit/hooks/orchestration-route-audit.py`（テストコマンド検知時） | テスト結果ベースの品質ゲート履歴 | `packages/route-audit/scripts/orchestration-kpi-report.py` |
| `.claude/logs/orchestration/agent-trace.jsonl` | `packages/route-audit/hooks/orchestration-expected-route.py`, `packages/route-audit/hooks/orchestration-route-audit.py` | ルーティング関連イベントの生トレース | 監査時の詳細確認（手動） |

## 2. 集計/出力ファイル

| ファイル | 生成元 | 役割 |
|---|---|---|
| `.claude/logs/orchestration/scorecard.json` | `packages/route-audit/scripts/orchestration-kpi-report.py --json-out` | KPI スコアカードの機械可読出力 |
| `docs/comparisons/[WIP]2026-02-14-orchestration-kpi-scorecard.md` | `packages/route-audit/scripts/orchestration-kpi-report.py --out` | KPI スコアカードの Markdown 出力（既定値） |
| `.claude/logs/cli-usage-*.csv` | `packages/cli-logging/scripts/analyze-cli-usage.py --export` | CLI 利用状況の CSV エクスポート |

## 3. 使い分け

- Codex/Gemini の利用状況を見たい: `.claude/logs/cli-tools.jsonl`
- ルーティング精度を評価したい: `route-audit.jsonl` + `quality-gate.jsonl`（`orchestration-kpi-report.py`）
- 全体時系列を確認したい: `events.jsonl`（`log-viewer.py` / `dashboard.py`）
- ルーティングの詳細調査をしたい: `agent-trace.jsonl` / `expected-routes.jsonl`

## 4. 既知の差異・注意点

- `dashboard.py` は `events.jsonl` の `session_start` / `session_end` / `quality_gate` も集計対象にしますが、現行フックではこれらイベント出力が限定的です。
- `scripts/analyze-cli-usage.py`（リポジトリ直下版）は `logs/cli-tools.jsonl` を参照します。  
  現在の標準ログパスは `.claude/logs/cli-tools.jsonl` のため、通常は `packages/cli-logging/scripts/analyze-cli-usage.py` の利用を推奨します。
- `packages/route-audit/hooks/orchestration-bootstrap.py` は `.claude/state/agent-trace.jsonl` を touch しますが、実際のトレース追記先は `.claude/logs/orchestration/agent-trace.jsonl` です。
