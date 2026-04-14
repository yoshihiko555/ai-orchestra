# ログ仕様

このドキュメントは、`ai-orchestra` の現行ログ構成を整理したものです。
対象時点: 2026-04-14

## 1. 主要ログ / 状態ファイル

| パス | 主な出力元 | 役割 | 主な参照先 |
|---|---|---|---|
| `.claude/logs/audit/sessions/<session_id>.jsonl` | `packages/audit/hooks/event_logger.py` 経由（`audit-*` hook 全般） | セッション単位の統一監査イベントログ | `packages/audit/scripts/log-viewer.py`, `dashboard.py`, `dashboard-html.py`, `kpi-report.py`, `analyze-cli-usage.py` |
| `.claude/state/audit-trace.json` | `audit-bootstrap.py`, `audit-prompt.py` | 現在のトレース ID / expected route の受け渡し | `audit-route.py`, `audit-cli.py`, `audit-instructions-loaded.py` |
| `.claude/state/audit-subagent-<agent_id>.json` | `audit-subagent-start.py` | サブエージェント固有のトレース状態 | `audit-subagent-end.py` |

worktree 環境では、監査ログは root worktree 側の `.claude/logs/audit/` に集約されます。

## 2. 主なイベント種別

| type | 主な出力元 | 用途 |
|---|---|---|
| `session_start` / `session_end` | `audit-bootstrap.py`, `audit-session-end.py` | セッション開始 / 終了の集計 |
| `prompt` | `audit-prompt.py` | expected route と入力抜粋の記録 |
| `route_decision` | `audit-route.py` | expected / actual route の照合 |
| `quality_gate` | `audit-route.py` | テストコマンド実行結果の記録 |
| `cli_call` | `audit-cli.py` | Codex / Gemini CLI 呼び出しの記録 |
| `subagent_start` / `subagent_end` | `audit-subagent-start.py`, `audit-subagent-end.py` | サブエージェントのライフサイクル |
| `instructions_loaded` | `audit-instructions-loaded.py` | 読み込まれた指示書の監査 |

## 3. 集計と確認

- 全体を時系列で確認する: `orchex run audit log-viewer`
- ルーティング精度を見る: `prompt` と `route_decision` を `log-viewer` / `kpi-report` で確認
- CLI 利用状況を見る: `orchex run audit analyze-cli-usage`
- セッション全体を俯瞰する: `orchex run audit dashboard`
- HTML で共有する: `orchex run audit dashboard-html -- -o dashboard.html`

`kpi-report.py` や `dashboard-html.py` の出力ファイル名は固定ではなく、呼び出し側が `--output` / `-o` で指定します。

## 4. 補足

- 旧 `route-audit` / `cli-logging` 系の個別 JSONL は現行実装では使っていません。
- 監査ログの正本は `audit` パッケージのセッション単位 JSONL です。
