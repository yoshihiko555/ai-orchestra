# codex-suggestions 評価セット

**パッケージ**: `packages/codex-suggestions`
**類型**: hook 型
**作成日**: 2026-07-03
**最終レビュー日**: —（未レビュー）
**情報源**: docs/reference/packages.md（codex-suggestions セクション）, .claude/rules/codex-suggestion-compliance.md, .claude/rules/codex-delegation.md, .claude/rules/config-loading.md（補助: packages/codex-suggestions/manifest.json, hooks/check-codex-before-write.py, hooks/check-codex-after-plan.py, packages/core/hooks/hook_common.py の実装挙動）

## 1. 責務定義

本パッケージは、Edit/Write によるファイル変更前と、Plan 系サブエージェントタスク完了後の 2 箇所で、Codex CLI への相談を促す非拘束的な提案（advisory suggestion）を `additionalContext` として注入する。提案は `cli-tools.yaml`（+ `.local.yaml`）の `codex.enabled` に従って有効/無効を切り替えられ、いかなる場合もツール実行やエージェント実行そのものをブロックしない。

### Non-Goals

- Codex CLI を実際に実行すること（`codex exec` の呼び出し自体はオーケストレーター/ユーザーの判断に委ねる）
- 提案への遵守を強制すること（hook は advisory のみで、遵守判断は `codex-suggestion-compliance` ルールに従うオーケストレーター側の責務）
- Antigravity CLI に関する提案（`antigravity-suggestions` パッケージの責務）
- typo 修正等の軽微な変更を hook 自身が検出して非発火にすること（後述 EV-11・「5. テストレビュー判断基準」参照）

## 2. 期待する入出力・副作用

| 構成要素                      | 入力                                                                                        | 期待する出力                                                                                                              | 副作用                                                           |
| ----------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `check-codex-before-write.py` | PreToolUse(Edit\|Write) stdin JSON（`tool_input.file_path`, `content`/`new_string`, `cwd`） | 条件成立時: `hookSpecificOutput.additionalContext` に `[Codex Suggestion] ...` を含む JSON を stdout へ／非該当時: 無出力 | なし（ファイル書き込み・ブロックを行わない。exit code は常に 0） |
| `check-codex-after-plan.py`   | PostToolUse(Agent\|Task) stdin JSON（`tool_name`, `tool_input`, `tool_response`, `cwd`）    | 条件成立時: `[Codex Review Suggestion] ...` を含む JSON を stdout へ／非該当時: 無出力                                    | なし（同上）                                                     |

## 3. 評価観点

- [ ] EV-01（正常 / must）: before-write: ファイルパスに `core/` や `config` / `class ` 等の設計系キーワード（DESIGN_INDICATORS）を含む Edit/Write で `[Codex Suggestion]` を出力する — 根拠: docs/reference/packages.md（発火条件: `core/` を含むファイルパス、`config`/`class` 等のキーワード）
- [ ] EV-02（正常 / must）: before-write: 大きなコンテンツ（実装閾値 500 文字超）を含む新規ファイル作成で `[Codex Suggestion]` を出力する — 根拠: docs/reference/packages.md（発火条件: 大きなコンテンツを含む新規ファイル作成）
- [ ] EV-03（異常 / must）: before-write: `codex.enabled: false`（`cli-tools.yaml` または `.local.yaml` 上書き）のとき、他条件に関わらず `[Codex Suggestion]` を出力しない — 根拠: .claude/rules/codex-suggestion-compliance.md（例外: `codex.enabled: false` の場合は hook 自体が提案を抑制する）
- [ ] EV-04（境界 / should）: before-write: `SIMPLE_EDIT_PATTERNS`（`.gitignore` / `README.md` / `CHANGELOG.md` / `requirements.txt` / `package.json` / `pyproject.toml` / `.env.example`）に該当するファイルパスでは、他条件を満たしても提案を出力しない — 根拠: 実装挙動
- [ ] EV-05（境界 / should）: before-write: 設計系キーワードに非該当かつコンテンツも小さい通常の Edit/Write では `additionalContext` を出力しない — 根拠: 実装挙動
- [ ] EV-06（異常 / must）: before-write: `file_path` が空・4096 文字超、`content` が 1,000,000 文字超、または `file_path` に `..` を含む場合は提案を出力せず exit code 0 で終了する（入力バリデーション） — 根拠: 実装挙動
- [ ] EV-07（正常 / must）: after-plan: `tool_name` が `Agent`/`Task` かつ `subagent_type` が `plan`/`planner`、またはプロンプトに計画関連キーワード（「計画」「プラン」「plan」等）を含むタスクが成功で完了した場合、`[Codex Review Suggestion]` を出力する — 根拠: 実装挙動（docs/reference/packages.md は「プラン完了後も Codex レビューを促す」という概要のみで、判定条件の詳細は記載なし）
- [ ] EV-08（異常 / must）: after-plan: `tool_response` が構造化フィールド（`is_error` / `error`）でエラーを示す場合は提案を抑制する（`str(tool_response)` の部分一致では判定しない設計） — 根拠: 実装挙動
- [ ] EV-09（異常 / must）: after-plan: `codex.enabled: false` のとき提案を出力しない — 根拠: .claude/rules/codex-suggestion-compliance.md + 実装挙動
- [ ] EV-10（境界 / should）: after-plan: `tool_name` が `Agent`/`Task` 以外、または Plan 系タスクと判定されない場合は提案を出力しない — 根拠: 実装挙動
- [ ] EV-11（境界 / should）: オーケストレーターは `[Codex Suggestion]` 発火後であっても、typo 修正（1-2文字）やコメント文言修正等の軽微な変更に限り Codex 相談をスキップしてよい — 根拠: .claude/rules/codex-suggestion-compliance.md（例外セクション）。**スコープ注記**: これは hook スクリプト自体の非発火条件ではなく、提案が出た後のオーケストレーター側の遵守判断である（hook にはコンテンツの変更量から typo か否かを判定するロジックはない）。hook 単体の pytest ではなく、エージェント挙動の統合テスト/レビューで担保すべき観点

## 4. 類型別観点

- [ ] EV-12（境界 / must）: stdin/stdout 契約 — 両 hook とも PreToolUse/PostToolUse の stdin JSON を読み込み、提案時のみ `hookSpecificOutput.additionalContext` を含む JSON を stdout に出力する。非該当時は何も print しない — 根拠: 実装挙動
- [ ] EV-13（正常 / must）: exit code 規約 — 両 hook とも常に exit code 0 で終了し、ブロック（非ゼロ exit）は行わない — 根拠: 実装挙動 + .claude/rules/codex-suggestion-compliance.md（提案は advisory のみでツール実行を妨げない）
- [ ] EV-14（異常 / must）: fail-safe 方針 — hook 内部で例外が発生した場合、stderr にエラーメッセージを出力しつつ exit code 0 で終了し、Claude Code のツール実行・エージェント実行を妨げない（fail-open） — 根拠: 実装挙動
- N/A: 冪等性 — 両 hook とも状態を持たず、`additionalContext` の注入以外の副作用（ファイル書き込み等）がないため、同一入力の再実行による二重書き込み・二重注入の懸念が実質的に存在しない
- [ ] EV-15（境界 / must）: config 駆動 — `codex.enabled` は `.claude/config/agent-routing/cli-tools.yaml` をベースに `cli-tools.local.yaml` があれば上書きを適用し、`codex` セクション自体が未定義の場合はデフォルト有効（true）として扱う — 根拠: .claude/rules/config-loading.md + 実装挙動（`hook_common.is_cli_enabled` のデフォルト true、`load_package_config` のローカル上書き優先順）
- N/A: 秘匿情報 — 両 hook は `additionalContext` にファイルパスやコマンド文字列（モデル名・サンドボックス設定）のみを含め、ユーザー入力コンテンツの本文や外部送信は行わない。マスキング処理自体は実装されていないが、扱うデータの性質上、秘匿情報の露出リスクが低いため該当性が低いと判断
- [ ] EV-16（境界 / should）: 性能 — 両 hook は正規表現ではなく単純な文字列包含判定（`in` 演算子）のみで判定し、ネットワーク I/O や外部プロセス起動を行わないため、Edit/Write・Agent/Task 実行を著しく遅延させない — 根拠: 実装挙動

## 5. テストレビュー判断基準（パッケージ固有）

- EV-07・EV-10（after-plan の詳細トリガー条件）はドキュメント未記載で実装のみが根拠のため、テストが「現在の閾値・キーワードリスト」を無批判にコピーした実装追認になっていないか重点確認する
- EV-11 は hook スクリプトの pytest では検証できない（オーケストレーター/エージェントの遵守判断のため）。この観点をカバーすると称するテストがある場合、対象が hook 単体テストなのか統合テスト・レビューなのかを明確にし、hook 単体テストで typo 判定を検証しようとしていないか確認する
- `SIMPLE_EDIT_PATTERNS`（EV-04）・`DESIGN_INDICATORS`（EV-01/EV-02）の具体的な文字列リストは実装のみが根拠。リストの内容そのものを固定的な仕様として厳密比較するテストは、リスト変更のたびに壊れる「実装追認」になっていないか確認し、リスト変更が意図的な仕様変更かどうかのレビューを優先する
- 例外条項（typo 修正・セッション内相談済み・`tool: codex` の implementation agent 内での Edit/Write 等）の適用判断は hook の責務ではなく、提案を受けたオーケストレーター側（`codex-suggestion-compliance` ルール）の責務である。hook 側のテストに例外判定を求めない
