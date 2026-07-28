# fail-logs 評価セット

**パッケージ**: `packages/fail-logs`
**類型**: hook 型
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-28（価値フロー仕様確定・EV-17〜21 新設。裁定は docs/design/fail-logs.md / ADR-20260728-046 を正本とする）
**情報源**: docs/design/fail-logs.md（主、価値フロー設計 §1〜§7）, ADR-20260728-046（蓄積型ログの root worktree 解決と配置規約）, packages/fail-logs/README.md（主）。packages/fail-logs/manifest.json・hooks のファイル名/docstring 冒頭は構成要素の列挙にのみ使用し、期待値の導出には用いていない（README に記述がない振る舞いは「根拠: 実装挙動」と明示）。

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

- ~~EV-09~~（**欠番**, 2026-07-03 レビュー）: capture-failures の stdout 契約は「記録 → 集計 → 再発可視化」というパッケージの価値フローを捉えられておらず設計見直しが必要と判定（下記注記・Issue #131）。2026-07-28 裁定で後継 EV-17 を新設(欠番は再利用しない方針を維持)
- ~~EV-10~~（**欠番**, 2026-07-03 レビュー）: inject-failure-summary の注入・集計フローは「あるべき仕様」が未定義と判定。正しい集計軸・注入内容・活用フローの再定義が必要（下記注記・Issue #131）。2026-07-28 裁定で後継 EV-18/EV-19 を新設(欠番は再利用しない方針を維持)
- [ ] EV-11（境界 / should）: `capture-failures.py` は失敗を検知・記録した場合でも常に exit 0 を返し、PostToolUse をブロックしない（記録は非ブロッキングなメタデータ収集でありゲートではない） — 根拠: 実装挙動
- [ ] EV-12（異常 / must）: `capture-failures.py` 内部で例外が発生しても exit 0 で終了し、記録対象であるツール実行フロー（PostToolUse）を阻害しない（fail-safe） — 根拠: 実装挙動
- [ ] EV-13（異常 / must）: `inject-failure-summary.py` は `failures.jsonl` 内の壊れた行（不正 JSON）や内部例外があってもクラッシュせず、当該行をスキップして SessionStart をブロックしない（fail-safe）。**加えて（2026-07-03 裁定）**、注入機能そのものが壊れていないこと（正常な失敗ログから期待どおりサマリーが生成されること）を、破損スキップだけでなく機能回帰テストで担保する — 根拠: 実装挙動 + 2026-07-03 人間レビュー裁定（機能回帰の担保。あるべき集計・注入フローは 2026-07-28 裁定で EV-18/EV-19 として確定済み。本機能回帰テストは実装状況に依存せず追加可能）
- [ ] EV-14（境界 / should）: `inject-failure-summary.py` のログ走査は上限を設けた範囲に限定され、ログが肥大してもファイル全体を毎回読み込まない（性能） — 根拠: 実装挙動
- ~~EV-15~~（**欠番**, 2026-07-03 レビュー）: 注入サマリーの無害化（間接プロンプトインジェクション対策）は、具体的な担保内容が未確定と判定。脅威モデルと保証範囲を明文化した設計が必要（現状の `<fail-logs-summary>` 境界フレーム＋`[log]` プレフィックス＋山括弧中和を出発点として Issue #131 で再設計）。2026-07-28 裁定で後継 EV-20 を新設(欠番は再利用しない方針を維持)
- 秘匿情報: EV-06 で担保（機密パターンのマスキング）
- config 駆動: EV-07（enabled 全体無効化）・EV-08（targets 個別無効化）で担保
- [ ] EV-16（境界 / should）: 冪等性 — 同一失敗イベントが再入力されても重複排除は行わず、重複記録を許容する（`_append_secure_jsonl` は `O_APPEND` の純追記）。再発した失敗はそれ自体が「解決すべき重要な失敗」のシグナルであり、蓄積を許容して inject-failure-summary の再発集計で重要度として扱う — 根拠: 2026-07-03 人間レビュー裁定（現実装が重複排除しないことを確認済み。実装ギャップなし）
- [ ] EV-17（正常 / must）: capture の記録契約 — stdout 無出力・検知有無に関わらず常に exit 0、audit v1 互換スキーマ + data.branch フィールド（ADR-20260728-046）を含む記録が追記される — 根拠: docs/design/fail-logs.md §3
- [ ] EV-18（正常 / must）: 集計・注入の機能回帰 — 正常な失敗ログ（再発 min_occurrences 以上を含む）から、再発シグネチャ中心の期待どおりのサマリーが生成・注入される（シグネチャは command 先頭トークン + command_kind、非 Bash は failure_type + tool フォールバック） — 根拠: docs/design/fail-logs.md §4
- [ ] EV-19（境界 / must）: 注入の有界性 — ログが max_records を大きく超えて肥大しても、読み出しは末尾 max_records 件・注入は top_signatures 件・コマンド 120 字/抜粋 100 字上限で頭打ちになる — 根拠: docs/design/fail-logs.md §4
- [ ] EV-20（異常 / must）: 注入テキストの無害化 — ログ由来テキスト内の山括弧（</fail-logs-summary> 等の境界フレーム偽造を含む）が ‹ › へ中和され、境界フレームと [log] プレフィックスが維持される — 根拠: docs/design/fail-logs.md §5
- [ ] EV-21（正常 / must）: root worktree 解決 — worktree セッションからの書き込み・読み出しが root worktree の .claude/logs/fail-logs/ に集約され、git 解決不能時は project_dir へフォールバックする — 根拠: docs/adr/ADR-20260728-046.md / docs/design/fail-logs.md §6

> **fail-logs の価値フロー再設計（EV-09・EV-10・EV-15 欠番, 2026-07-03）**: 本パッケージの「記録 → 集計 → 再発サマリー注入」という価値フローと注入テキストの無害化要件は、現状「根拠: 実装挙動」に留まり「あるべき仕様」が未確定。(1) capture の記録契約、(2) inject の集計軸・注入内容・活用フロー、(3) 注入テキストの無害化（脅威モデルと保証範囲）を設計として明文化し、確定後に新 ID で観点を追加する（Issue #131）。再設計が決まるまで、現状実装を「正」とする回帰テストは追加しない。
>
> **2026-07-28 追記**: 上記の再設計は EV-17〜EV-21 の新設により解消済み。回帰テスト追加の抑制は解除された（テスト追加は evaluation-set-policy の手順に従う）。

## 5. テストレビュー判断基準（パッケージ固有）

- EV-09・EV-10・EV-15 は 2026-07-03 レビューで欠番化された（記録・集計・注入フロー、無害化要件が仕様未確定）。現状実装を「正」とするテストは追加せず、設計確定（Issue #131）を待つ。EV-14（性能・上限読み）は実装のみが根拠のため、テストが切り詰め値の丸写しになっていないか確認する。
- EV-06（機密情報マスキング）のテストは、実際のシークレットパターン（`sk-...`, `ghp_...`, `AKIA...` 等）を用いたケースを正常系のついでではなく独立した異常系として検証しているか確認する。
- EV-12・EV-13（fail-safe）のテストは、例外を強制的に発生させた上で exit code と「記録対象の本体処理（ツール実行・セッション開始）が継続すること」の両方を検証しているか確認する。
