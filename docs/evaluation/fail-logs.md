# fail-logs 評価セット

**パッケージ**: `packages/fail-logs`
**類型**: hook 型
**作成日**: 2026-07-03
**最終レビュー日**: —（未レビュー）
**情報源**: packages/fail-logs/README.md（主）。packages/fail-logs/manifest.json・hooks のファイル名/docstring 冒頭は構成要素の列挙にのみ使用し、期待値の導出には用いていない（README に記述がない振る舞いは「根拠: 実装挙動」と明示）。

## 1. 責務定義

fail-logs は AI エージェント（Claude Code 等）のツール実行失敗を検知する PostToolUse hook（`capture-failures.py`）と、記録済みの失敗をセッション開始時にサマリーとしてコンテキストへ注入する SessionStart hook（`inject-failure-summary.py`）からなる、失敗知識蓄積の基盤である。失敗のみを記録し（成功は対象外、ノイズ抑制）、`core`（`hook_common`, `failure_detector`）にのみ依存する。記録処理自体の失敗が、記録対象であるツール実行やセッション開始そのものを妨げないことを保証する。

### Non-Goals

- 成功を含む実行統計・合格率の算出（`audit` の `quality_gate` が担う）
- 失敗の自動修正・自動リトライ（記録のみを行い、対応判断は下流に委ねる）
- `audit` への直接連携（`core` のみに依存し、`audit` には依存しない）

## 2. 期待する入出力・副作用

| 構成要素                                         | 入力                                                                                             | 期待する出力                                                                                                          | 副作用                                                                                  |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `capture-failures.py`（PostToolUse hook）        | PostToolUse hook 入力 JSON（`tool_name`, `tool_input`, `tool_response`, `cwd`, `session_id` 等） | 失敗検知時のみ `.claude/logs/fail-logs/failures.jsonl` に schema v1 の 1 行を追記。非検知時・無効化時は無出力・無追記 | ログディレクトリ/ファイルの作成（所有者限定パーミッション）、記録前の機密情報マスキング |
| `inject-failure-summary.py`（SessionStart hook） | SessionStart hook 入力 JSON（`cwd`, `session_id` 等）                                            | 再発している失敗のサマリーテキストを stdout に出力し、オーケストレーターの追加コンテキストとして注入される            | なし（読み取り専用。`failures.jsonl` への書き込みは行わない）                           |

## 3. 評価観点

- [ ] EV-01（正常 / must）: Bash の非ゼロ終了・他ツールの明示的エラーを `tool_error` として検知し `failures.jsonl` に追記する — 根拠: packages/fail-logs/README.md
- [ ] EV-02（正常 / must）: `exit_code` が 0 または欠落していても、test/lint 系コマンドの出力に失敗マーカーが含まれる場合は `test_failure`/`lint_failure` として検知する（2 段判定により `pytest ... | tail` のようなパイプでの終了コードマスクを回避する） — 根拠: packages/fail-logs/README.md
- [ ] EV-03（正常 / must）: 失敗イベントのみを記録し、成功したツール実行は記録しない（成功を含む実行統計は `audit` の `quality_gate` が別途担う） — 根拠: packages/fail-logs/README.md
- [ ] EV-04（正常 / must）: 記録するレコードは audit v1 互換スキーマ（`v`, `ts`, `sid`, `eid`, `type=failure`, `data{failure_type, error_type, detected_by, command_kind, tool, command, error_excerpt, exit_code, cwd}`）に従う — 根拠: packages/fail-logs/README.md
- [ ] EV-05（正常 / should）: codex / agy 等の外部 CLI 呼び出し失敗を `cli_failure` として検知する — 根拠: packages/fail-logs/README.md
- [ ] EV-06（異常 / must）: エラー抜粋・コマンド文字列に含まれる機密情報（API キー・トークン等の既知パターン）は記録前に `[REDACTED]` へマスクする — 根拠: packages/fail-logs/README.md
- [ ] EV-07（境界 / must）: `config/fail-logs.yaml`（または `fail-logs.local.yaml`）の `enabled: false` で記録機能全体を無効化できる — 根拠: packages/fail-logs/README.md
- [ ] EV-08（境界 / should）: `targets.<failure_type>: false` で失敗種別ごとに記録可否を個別に無効化できる — 根拠: packages/fail-logs/README.md

## 4. 類型別観点

<!-- docs/evaluation/README.md の hook 型チェックリストを本パッケージの実情で具体化する -->

- [ ] EV-09（正常 / must）: `capture-failures.py` は失敗検知の有無・無効化設定に関わらず stdout には出力せず、exit code のみで完結する（stdin/stdout 契約） — 根拠: 実装挙動（README に stdout 契約の明記なし）
- [ ] EV-10（正常 / must）: `inject-failure-summary.py` は SessionStart 入力を読み取り、記録済みの失敗を集計した再発サマリーのテキストを stdout に出力し、オーケストレーターの追加コンテキストとして注入する（stdin/stdout 契約） — 根拠: 実装挙動（README には SessionStart hook・活用フェーズの記述がなく、manifest.json の hook 登録とコードで存在のみ確認）
- [ ] EV-11（境界 / should）: `capture-failures.py` は失敗を検知・記録した場合でも常に exit 0 を返し、PostToolUse をブロックしない（記録は非ブロッキングなメタデータ収集でありゲートではない） — 根拠: 実装挙動
- [ ] EV-12（異常 / must）: `capture-failures.py` 内部で例外が発生しても exit 0 で終了し、記録対象であるツール実行フロー（PostToolUse）を阻害しない（fail-safe） — 根拠: 実装挙動
- [ ] EV-13（異常 / must）: `inject-failure-summary.py` は `failures.jsonl` 内の壊れた行（不正 JSON）や内部例外があってもクラッシュせず、当該行をスキップして SessionStart をブロックしない（fail-safe） — 根拠: 実装挙動
- [ ] EV-14（境界 / should）: `inject-failure-summary.py` のログ走査は上限を設けた範囲に限定され、ログが肥大してもファイル全体を毎回読み込まない（性能） — 根拠: 実装挙動
- [ ] EV-15（境界 / should）: SessionStart へ注入するサマリーテキストは、ログ由来の内容がそのまま特殊記号として解釈されないよう無害化される（記録された失敗コマンド文字列を経由した間接的なコンテキスト汚染を防ぐ） — 根拠: 実装挙動
- 秘匿情報: EV-06 で担保（機密パターンのマスキング）
- config 駆動: EV-07（enabled 全体無効化）・EV-08（targets 個別無効化）で担保
- 冪等性: N/A（要確認）— `capture-failures.py` に明示的な重複排除ロジックは確認できず、同一イベントが再入力された場合に重複記録が起きるかどうかを README から判断できない（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。EV 化は見送る

## 5. テストレビュー判断基準（パッケージ固有）

- EV-10・EV-14・EV-15（`inject-failure-summary.py` の集計・性能・無害化ロジック）は README に文書化がなく実装のみが根拠のため、テストの期待値が「現状のシグネチャ算出・切り詰めロジックをそのままコピーしただけ」になっていないか重点的に確認する。
- EV-06（機密情報マスキング）のテストは、実際のシークレットパターン（`sk-...`, `ghp_...`, `AKIA...` 等）を用いたケースを正常系のついでではなく独立した異常系として検証しているか確認する。
- EV-12・EV-13（fail-safe）のテストは、例外を強制的に発生させた上で exit code と「記録対象の本体処理（ツール実行・セッション開始）が継続すること」の両方を検証しているか確認する。
