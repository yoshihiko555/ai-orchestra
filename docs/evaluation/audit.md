# audit 評価セット

**パッケージ**: `packages/audit`
**類型**: 主: hook 型、副: CLI ツール型（ダッシュボード/KPI scripts）
**作成日**: 2026-07-03
**最終レビュー日**: —（未レビュー）
**情報源**: packages/audit/README.md, docs/reference/packages.md（audit セクション）, .claude/rules/config-loading.md（補助: packages/audit/manifest.json, hooks/scripts のファイル名と docstring 冒頭）

## 1. 責務定義

audit パッケージは、Claude Code セッション中のルーティング・CLI 呼び出し・サブエージェント実行・指示書読み込みを、セッション単位の JSONL イベントログ（`.claude/logs/audit/sessions/{session_id}.jsonl`）へ設定不要で自動記録する統合監査基盤である。記録は `audit-flags.json` の機能フラグ単位で有効/無効を切り替えられ、ログにはプロンプトやコマンド文字列に含まれ得る機密情報をマスキングした上で書き込む。蓄積したログは `dashboard` / `dashboard-html` / `log-viewer` / `kpi-report` / `analyze-cli-usage` スクリプトで可視化・分析できる。

### Non-Goals

- 品質ゲートの合否判定・ブロックそのものは行わない（`quality_gate` イベントの記録元は `quality-gates` パッケージの `post-test-analysis.py` であり、audit は関連フラグを保管・共有するのみ）— 根拠: packages/audit/README.md
- エージェントのルーティング判断そのものは行わない（ルーティング決定は `agent-routing`、audit は予測ルートと実ルートの照合・記録のみを担う）— 根拠: docs/reference/packages.md（audit セクション）
- リアルタイム通知・アラートは提供しない（スクリプトはオンデマンド実行のレポート生成のみ）— 根拠: packages/audit/README.md（スクリプトはすべて手動実行のコマンドとして記載）

## 2. 期待する入出力・副作用

| 構成要素                                                       | 入力                             | 期待する出力                                                 | 副作用                                                   |
| -------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------- |
| `audit-bootstrap.py`（SessionStart）                           | hook 入力 JSON（session_id 等）  | `session_start` イベント記録                                 | セッションログディレクトリ初期化 + JSONL 新規作成        |
| `audit-session-end.py`（SessionEnd）                           | hook 入力 JSON                   | イベント数・エラー数等のサマリーを含む `session_end` 記録    | JSONL 追記                                               |
| `audit-prompt.py`（UserPromptSubmit）                          | hook 入力 JSON（プロンプト本文） | 期待ルート予測 + `prompt` イベント記録（マスク済み抜粋）     | JSONL 追記、トレース state 保存                          |
| `audit-route.py`（PostToolUse）                                | hook 入力 JSON（ツール呼び出し） | 実ルート検出 + `route_decision` イベント記録（予測との照合） | JSONL 追記                                               |
| `audit-cli.py`（PostToolUse:Bash）                             | hook 入力 JSON（Bash コマンド）  | `cli_call` イベント記録（マスク済みコマンド）                | JSONL 追記                                               |
| `audit-subagent-start.py`（SubagentStart）                     | hook 入力 JSON                   | `subagent_start` イベント記録                                | JSONL 追記、サブエージェントトレース保存                 |
| `audit-subagent-end.py`（SubagentStop）                        | hook 入力 JSON                   | `subagent_end` イベント記録                                  | JSONL 追記、サブエージェントトレースのクリーンアップ     |
| `audit-instructions-loaded.py`（InstructionsLoaded）           | hook 入力 JSON（読込指示書情報） | 観測専用（stdout への JSON 応答なし）                        | JSONL 追記のみ                                           |
| `event_logger.py`（共有モジュール）                            | 各 hook からの呼び出し           | `emit_event` 等のログ書き込み API                            | JSONL / トレース state ファイル I/O                      |
| `secret_masking.py`（共有モジュール）                          | ログ対象文字列                   | マスク済み文字列                                             | なし（純粋関数）                                         |
| `dashboard.py`（`orchex run audit dashboard`）                 | JSONL ログ + オプション          | テキスト集計をターミナル表示                                 | なし                                                     |
| `dashboard-html.py`（`orchex run audit dashboard-html`）       | JSONL ログ + オプション          | HTML（Chart.js）レポート                                     | `.claude/YYYYMMDD-dashboard.html` 書き込み（デフォルト） |
| `log-viewer.py`（`orchex run audit log-viewer`）               | JSONL ログ + フィルタ引数        | フィルタ済みイベント一覧（テキスト or raw JSONL）            | なし                                                     |
| `kpi-report.py`（`orchex run audit kpi-report`）               | JSONL ログ + オプション          | KPI スコアカード（markdown）                                 | `--output` 指定時ファイル書き込み                        |
| `analyze-cli-usage.py`（`orchex run audit analyze-cli-usage`） | JSONL ログ + オプション          | CLI 利用パターン分析（テキスト/JSON）                        | なし                                                     |

## 3. 評価観点

- [ ] EV-01（正常 / must）: SessionStart 時にセッションログ（`.claude/logs/audit/sessions/{session_id}.jsonl`）を初期化し `session_start` イベントを記録する — 根拠: packages/audit/README.md（フック一覧）
- [ ] EV-02（正常 / must）: SessionEnd 時にイベント数・エラー数等のサマリーを含む `session_end` イベントを記録する — 根拠: packages/audit/README.md（フック一覧）
- [ ] EV-03（正常 / must）: UserPromptSubmit 時に期待ルートを予測し `prompt` イベントとして記録する — 根拠: docs/reference/packages.md（audit セクション）
- [ ] EV-04（正常 / must）: PostToolUse 時に実際のルートを検出し、予測ルートとの照合結果を `route_decision` イベントとして記録する — 根拠: docs/reference/packages.md（audit セクション）
- [ ] EV-05（正常 / must）: Bash 実行時に CLI 呼び出しを検出し `cli_call` イベントとして記録する — 根拠: packages/audit/README.md（フック一覧）
- [ ] EV-06（正常 / should）: SubagentStart / SubagentStop でそれぞれ `subagent_start` / `subagent_end` を記録し、`log-viewer --trace` でイベント連鎖を追跡できる — 根拠: packages/audit/README.md（log-viewer の `--trace` オプション説明）
- [ ] EV-07（正常 / should）: InstructionsLoaded は読み込まれた指示書をログへ記録するが、stdout へ JSON 応答を出力しない観測専用フックである — 根拠: 実装挙動（audit-instructions-loaded.py モジュール docstring「stdout への JSON 出力は行わない（観測専用）」）
- [ ] EV-08（正常 / must）: `route_audit.enabled=false` の場合、`prompt` / `route_decision` の記録が抑制される — 根拠: packages/audit/README.md（audit-flags.json フラグ表）
- [ ] EV-09（正常 / must）: `quality_gate` イベント自体は audit 側の hook からは記録されず、`quality-gates` パッケージの `post-test-analysis.py` から記録される（audit は関連フラグの保管のみ） — 根拠: packages/audit/README.md（フック一覧の注記）
- [ ] EV-10（境界 / should）: `route_audit.max_excerpt_chars`（デフォルト 160）を超えるプロンプト抜粋は切り詰めて記録される — 根拠: packages/audit/README.md（audit-flags.json フラグ表）
- [ ] EV-11（境界 / should）: `--days` 未指定時、`kpi-report` は `kpi_scorecard.default_period_days`（デフォルト 7 日）を集計期間として使用する — 根拠: packages/audit/README.md（audit-flags.json フラグ表 + kpi-report 節）
- [ ] EV-12（異常 / must）: `.claude/config/audit/audit-flags.local.json` が存在する場合、ベース設定（`audit-flags.json`）より優先して適用される — 根拠: packages/audit/README.md（設定節）+ .claude/rules/config-loading.md
- [ ] EV-13（異常 / must）: hook 内部で例外が発生してもセッション進行を止めない（fail-open） — 根拠: 実装挙動（各 hook の `@safe_hook_execution` デコレータ）
- [ ] EV-14（異常 / must）: ログ（`prompt` / `cli_call` イベント等）に書き込まれるテキストから API キー・トークン・パスワード・クラウド認証情報・秘密鍵等の機密情報がマスキングされ、生の値が JSONL に残らない — 根拠: 実装挙動（secret_masking.py モジュール docstring「audit-prompt.py / audit-cli.py で共用」+ 両 hook からの import）
- [ ] EV-15（正常 / should）: `dashboard-html -- -o -` 指定時は標準出力に HTML を出力し、ファイル書き込みを行わない — 根拠: packages/audit/README.md（dashboard-html 節）

## 4. 類型別観点

- [ ] EV-16（境界 / must）: hook 型 - stdin/stdout 契約: 各 hook は Claude Code の hook 入力 JSON（session_id / tool_name / prompt 等）を受け取り、`audit-instructions-loaded.py` を除きイベント記録のみを行い、additionalContext によるブロック等の応答を返さない — 根拠: docs/reference/packages.md（audit セクション）+ 実装挙動
- N/A: hook 型 - exit code 規約（block/warn 分岐） — audit の全 hook は記録専用でブロック判断を行わないため、Claude Code のブロック用 exit code は使用しない — 根拠: packages/audit/README.md（全フックが「記録」とのみ説明されている）
- [ ] EV-17（異常 / must）: hook 型 - fail-safe 方針: hook 内部エラー時にセッションを壊さない fail-open 方針を取る（EV-13 と同一観点を型チェックリスト項目として明示） — 根拠: 実装挙動（`safe_hook_execution` デコレータ）
- 冪等性: 明文化された仕様なし（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。EV 化は見送る
- config 駆動: EV-08 / EV-12 参照（`*.enabled` フラグと `*.local.json` 上書きで担保）
- 秘匿情報: EV-14 参照（機密情報マスキングで担保）
- [ ] EV-18（境界 / should）: hook 型 - 性能: 各 hook は JSONL への追記処理が主処理であり、同期実行でもセッション開始・ツール実行を著しく遅延させない軽量な処理に留まる — 根拠: 実装挙動（各 hook の docstring・処理内容がいずれも「記録」に限定される）
- [ ] EV-19（正常 / must）: CLI ツール型 - コマンド契約: `orchex run audit dashboard / dashboard-html / log-viewer / kpi-report / analyze-cli-usage` の 5 コマンドが提供され、記載されたオプション（`--session` / `--type` / `--trace` / `--limit` / `--raw` / `--days` / `--output` / `-o` / `--format`）が文書通りに機能する — 根拠: packages/audit/README.md（スクリプト詳細節）
- 入力バリデーション: 存在しない session-id・破損した JSONL 行に対する期待挙動が未文書化（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。EV 化は見送る
- N/A: CLI ツール型 - 破壊的操作の安全策 — audit のスクリプトはログ読み取り専用の分析・レポート生成ツールであり、JSONL ソースデータを変更・削除する操作を持たない（`-o` 指定時の出力ファイル上書きのみで、ログ自体への影響はない） — 根拠: packages/audit/README.md（各スクリプトの説明はすべて「表示」「生成」で、削除・変更系コマンドの記載がない）
- [ ] EV-20（境界 / should）: CLI ツール型 - 出力の安定性: JSONL の `type` フィールドは `session_start` / `session_end` / `prompt` / `route_decision` / `cli_call` / `subagent_start` / `subagent_end` のいずれかで安定しており、`log-viewer --raw` や機械可読出力はこの語彙に依存する — 根拠: packages/audit/README.md（フック一覧の「記録内容」列）
- 設定レイヤリング: EV-11 / EV-12 参照（スクリプトのデフォルト値も `audit-flags.json` + `*.local.json` の階層に従う）

## 5. テストレビュー判断基準（パッケージ固有）

- EV-14（秘密情報マスキング）を検証するテストは、`sk-` 形式トークン・`Bearer` ヘッダー・AWS/Google 系キー・PEM 秘密鍵ブロックなど、パターンごとに独立したケースを持つこと。1 パターンの通過を全パターン担保の代わりにしない。
- EV-09（quality_gate イベントの記録元が audit ではない）を検証する際、audit 側のテストは「audit hook が quality_gate イベントを発生させないこと」の確認に留め、quality-gates 側の記録内容の正しさまでは audit の評価セットの範囲外とする。
- EV-06（トレース連鎖）のテストは、`subagent_start` の `ptid` が親イベントのトレース ID と一致することを、単一イベントの存在確認ではなく親子関係の突合で検証すること。
- それ以外は README.md「テストレビュー判断基準（共通）」のみを適用する。
