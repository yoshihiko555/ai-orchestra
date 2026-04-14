# audit パッケージ

AI Orchestra の統合イベントログ・監査基盤。Claude Code セッション中のルーティング、CLI 呼び出し、サブエージェント実行を JSONL 形式で記録し、ダッシュボードや KPI レポートで可視化します。

## 概要

- セッション単位でイベントを `.claude/logs/audit/` に蓄積
- フックが自動でイベントを記録（設定不要）
- テキスト・HTML のダッシュボードでセッションを横断分析
- KPI スコアカードで品質傾向を把握

## クイックスタート

```bash
# 利用可能なスクリプトの確認
orchex scripts --package audit

# テキストダッシュボード（ターミナル表示）
orchex run audit dashboard

# HTML ダッシュボード（.claude/YYYYMMDD-dashboard.html に自動保存）
orchex run audit dashboard-html

# ログビューア（最新イベントを表示）
orchex run audit log-viewer

# KPI レポート（直近 7 日間）
orchex run audit kpi-report
```

## スクリプト詳細

### dashboard — テキストダッシュボード

ターミナル上でセッション統計をテキスト形式で表示します。

```bash
orchex run audit dashboard
```

### dashboard-html — HTML ダッシュボード

Chart.js グラフ付きの HTML レポートを生成します。デフォルトでは `.claude/YYYYMMDD-dashboard.html` に自動保存されます。

```bash
# 自動保存（.claude/YYYYMMDD-dashboard.html）
orchex run audit dashboard-html

# 特定セッションのみ表示
orchex run audit dashboard-html -- --session <SESSION_ID>

# 保存先を指定
orchex run audit dashboard-html -- -o custom-path.html

# 標準出力に出力（パイプ利用時）
orchex run audit dashboard-html -- -o -
```

| オプション       | 説明                                |
| ---------------- | ----------------------------------- |
| `--session <ID>` | 表示対象のセッション ID を絞り込む  |
| `-o <PATH>`      | 出力先ファイルパス（`-` で stdout） |

### log-viewer — 監査ログビューア

イベントの検索・フィルタリング・トレース追跡を行います。

```bash
# 最新ログを表示
orchex run audit log-viewer

# セッション絞り込み
orchex run audit log-viewer -- --session <SESSION_ID>

# イベント種別で絞り込み
orchex run audit log-viewer -- --type <EVENT_TYPE>

# トレース ID で追跡
orchex run audit log-viewer -- --trace <TRACE_ID>

# 表示件数を指定（デフォルト: 20）
orchex run audit log-viewer -- --limit 50

# JSONL 生ログ形式で出力
orchex run audit log-viewer -- --raw
```

| オプション       | 説明                                              |
| ---------------- | ------------------------------------------------- |
| `--session <ID>` | セッション ID でフィルタ                          |
| `--type <TYPE>`  | イベント種別でフィルタ（例: `route`, `cli_call`） |
| `--trace <ID>`   | トレース ID でイベント連鎖を追跡                  |
| `--limit <N>`    | 表示件数の上限（デフォルト: 20）                  |
| `--raw`          | JSONL 形式のまま出力                              |

### kpi-report — KPI スコアカードレポート

ルーティング精度・品質ゲート通過率などの KPI を集計します。

```bash
# 直近 7 日間（デフォルト）
orchex run audit kpi-report

# 集計期間を指定（日数）
orchex run audit kpi-report -- --days 7

# ファイルに保存
orchex run audit kpi-report -- --output report.txt
```

| オプション        | 説明                            |
| ----------------- | ------------------------------- |
| `--days <N>`      | 集計対象の日数（デフォルト: 7） |
| `--output <PATH>` | レポートの保存先ファイルパス    |

### analyze-cli-usage — CLI 利用パターン分析

Codex CLI・Gemini CLI の呼び出し頻度・パターンを分析します。

```bash
# 直近 30 日間（デフォルト）
orchex run audit analyze-cli-usage

# 集計期間を指定
orchex run audit analyze-cli-usage -- --days 30
```

| オプション   | 説明                             |
| ------------ | -------------------------------- |
| `--days <N>` | 集計対象の日数（デフォルト: 30） |

## フック一覧

パッケージインストール後、以下のフックが自動で有効になります。

| フック                         | タイミング          | 記録内容                                            |
| ------------------------------ | ------------------- | --------------------------------------------------- |
| `audit-bootstrap.py`           | SessionStart        | セッション開始・メタ情報（セッション ID、開始時刻） |
| `audit-session-end.py`         | SessionEnd          | セッション終了・サマリー統計                        |
| `audit-prompt.py`              | UserPromptSubmit    | ユーザープロンプトの受信イベント                    |
| `audit-route.py`               | PostToolUse         | エージェントルーティング結果                        |
| `audit-cli.py`                 | PostToolUse（Bash） | CLI ツール（Codex/Gemini）の呼び出し記録            |
| `audit-subagent-start.py`      | SubagentStart       | サブエージェントの起動                              |
| `audit-subagent-end.py`        | SubagentStop        | サブエージェントの終了・結果                        |
| `audit-instructions-loaded.py` | InstructionsLoaded  | 指示書の読み込み完了                                |

ログは `.claude/logs/audit/` 配下に JSONL 形式で保存されます。

## 設定

設定ファイルは `.claude/config/audit/` に配置されます。

### audit-flags.json — 機能フラグ

| フラグ                                     | デフォルト | 説明                                  |
| ------------------------------------------ | ---------- | ------------------------------------- |
| `route_audit.enabled`                      | `true`     | ルーティング記録の有効/無効           |
| `route_audit.max_excerpt_chars`            | `160`      | プロンプト抜粋の最大文字数            |
| `quality_gate.enabled`                     | `true`     | 品質ゲートチェックの有効/無効         |
| `quality_gate.block_on_failed_test`        | `false`    | テスト失敗時にブロックするか          |
| `kpi_scorecard.enabled`                    | `true`     | KPI スコアカード集計の有効/無効       |
| `kpi_scorecard.default_period_days`        | `7`        | KPI 集計のデフォルト期間（日）        |
| `context_optimization.enabled`             | `true`     | コンテキスト最適化チェックの有効/無効 |
| `context_optimization.read_line_threshold` | `200`      | 警告を出すファイル読み込み行数の閾値  |

### delegation-policy.json — ルーティングポリシー

キーワードベースのエージェントルーティングルールを定義します。`default_route` でフォールバック先を指定し、`rules` でキーワード→エージェントのマッピングを追加できます。

プロジェクト固有の上書きは `.claude/config/audit/audit-flags.local.json` で行います（`config-loading` ルール準拠）。

```json
// .claude/config/audit/audit-flags.local.json
{
  "features": {
    "quality_gate": {
      "block_on_failed_test": true
    }
  }
}
```
