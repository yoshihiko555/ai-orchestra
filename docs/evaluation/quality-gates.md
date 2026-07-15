# quality-gates 評価セット

**パッケージ**: `packages/quality-gates`
**類型**: hook 型
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-03（EV-11/12/19/21/22/24 は同日の裁定で仕様確定）
**情報源**: docs/reference/packages.md（quality-gates セクション）, .claude/rules/skill-review-policy.md, .claude/config/audit/audit-flags.json, packages/quality-gates/manifest.json, packages/quality-gates/hooks/\*.py（docstring・実装。`packages/quality-gates/README.md` は存在しないため未参照）, 実装 `packages/quality-gates/hooks/evaluation-set-checker.py` および `packages/quality-gates/tests/test_evaluation_set_checker.py`（Issue #123: hook 実体はコードベースに着地済み）

## 1. 責務定義

quality-gates は実装後の品質チェックを自動化する hook 群と、レビュー/TDD/リリース前確認のスキル群を提供する。編集直後の formatter/lint 実行、変更規模に応じたレビュー・テスト実行の提案、テスト実行結果の分析と Codex への相談提案、テスト改ざん（skip 追加・抑制コメント・テストファイル削除）の検知、テストファイル変更時の評価セット（`docs/evaluation/<pkg>.md`）突合案内、ターン終了時の軽量サマリー通知を、セッションを止めずに（fail-open で）行う。

### Non-Goals

- 実際のテスト実行そのものは行わない（実行を提案するのみで、pytest/npm test 等の起動はユーザー/エージェント側の責務）
- コードレビューの実施主体ではない（`review` スキルはサブエージェントへの委譲であり、hook 自体は判定を下さない）
- CI/CD レベルのマージブロッキングゲートではない（`post-test-analysis.py` の exit code 2 はローカルセッションの当該 PostToolUse 呼び出しのみに影響する）
- マージ可否の最終判断は行わない（`release-readiness` スキルは人間の確認を前提とする）
- 評価セットとの突合作業そのもの（マトリクス生成・must 観点のカバレッジ判定・ギャップの Issue 追記）は行わない（`evaluation-set-checker.py` は確認を促す案内のみで、突合の実施は `evaluation-set-policy` ルールに従いオーケストレーターが行う）

## 2. 期待する入出力・副作用

| 構成要素                                                                    | 入力                                                                                            | 期待する出力                                                                                                                                                                                            | 副作用                                                                                                    |
| --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `check-context-optimization.py` (PreToolUse Read/Grep/Bash)                 | `tool_name`, `tool_input`（file_path/offset/limit, output_mode/head_limit/pattern, command）    | 閾値超過時のみ `hookSpecificOutput.additionalContext` に提案文言                                                                                                                                        | なし（読み取り専用、状態ファイル書き込みなし）                                                            |
| `post-implementation-review.py` (PostToolUse Edit/Write)                    | `tool_input.file_path`, `content`/`new_string`                                                  | 3ファイル以上または100行以上の変更でレビュー提案（`additionalContext`）                                                                                                                                 | `.claude/state/post-implementation-review.json` にプロジェクトスコープの状態を更新（Issue #154 で worktree 分離のため /tmp から移行）                                   |
| `post-test-analysis.py` (PostToolUse Bash)                                  | `tool_input.command`, `tool_response.exit_code`/`stdout`                                        | 失敗検知時に Codex 相談コマンドを `additionalContext` に提示。失敗時は既定で stderr + exit 2（`block_on_failed_test=false` の明示 opt-out で解除）                                                      | `.claude/state/test-gate-checker.json` を更新（成功時カウンタリセット）、audit イベント `quality_gate` を記録（Issue #154 で worktree 分離のため /tmp から移行） |
| `lint-on-save.py` (PostToolUse Edit/Write)                                  | `tool_input.file_path`                                                                          | 実行結果を `[Lint OK/Issues found]` として `additionalContext` に整形                                                                                                                                   | 対象ファイルへの formatter/linter 実行（`--fix`/`--write` 等でファイル内容が書き換わる場合あり）          |
| `test-tampering-detector.py` (PostToolUse Edit/Write/Bash/Delete/MultiEdit) | `tool_input`, git diff（追加行・削除ファイル）                                                  | 新規検知時のみ `[Warning] Potential test tampering detected` を `additionalContext` に出力                                                                                                              | `.claude/state/test-tampering-detector.json` に報告済み finding を記録（再警告防止。Issue #154 で worktree 分離のため /tmp から移行し、flock 排他ロックを追加）                           |
| `test-gate-checker.py` (PostToolUse Edit/Write)                             | `tool_input.file_path`/`content`                                                                | 3ファイル以上または100行以上の未テスト変更でテスト実行提案（`additionalContext`）                                                                                                                       | `.claude/state/test-gate-checker.json` を更新（`post-test-analysis.py` と共有・連携。Issue #154 で worktree 分離のため /tmp から移行）                         |
| `turn-end-summary.py` (Stop)                                                | working-context（modified_files）, `.claude/Plans.md`                                           | 変更ファイル数・Plans.md の WIP/TODO/blocked 件数を `systemMessage` として出力                                                                                                                          | audit イベント `turn_end` を記録（`decision: block` は不使用）                                            |
| `evaluation-set-checker.py` (PostToolUse Edit/Write)                        | テストファイル（`packages/<pkg>/tests/`, `tests/unit/`, `tests/e2e/`）の `tool_input.file_path` | 対象パッケージ特定時は `docs/evaluation/<pkg>.md` との突合確認を `additionalContext` に案内。評価セット不在時は「未整備」警告（`_template.md` による新規作成を提案）。パッケージ特定不能時は汎用案内1行 | `.claude/state/evaluation-set-checker.json` に session_id×パッケージの通知済み状態を記録（重複抑制）      |
| `review` / `tdd` / `design-tracker` / `release-readiness` スキル            | ユーザーの明示的実行                                                                            | `review` はパスパターンに応じたレビュアー選定 + Tiered Output（Critical/High/Medium/Low）                                                                                                               | 選定されたレビュアーをサブエージェントとして起動（Task 呼び出し）                                         |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `check-context-optimization.py` は Read の offset/limit が未指定かつ行数が閾値（既定200行）を超える場合のみ提案し、offset/limit 指定時や閾値以下では何も出力しない — 根拠: 実装挙動
- [ ] EV-02（正常 / must）: `lint-on-save.py` は編集ファイルの拡張子から対応する formatter/linter を判定して実行し、結果を `additionalContext` にまとめて報告する — 根拠: docs/reference/packages.md
- [ ] EV-03（正常 / must）: `post-implementation-review.py` は変更ファイル数3以上または変更行数100以上でレビュー提案を1回出し、以後 24時間（TTL）は再提案しない — 根拠: 実装挙動
- [ ] EV-04（正常 / must）: `test-gate-checker.py` はコード変更ファイル数3以上または変更行数100以上、かつ未警告状態でテスト実行を提案する — 根拠: 実装挙動
- [ ] EV-05（正常 / must）: `post-test-analysis.py` はテストコマンド（pytest/npm test 等）実行後、失敗を検知した場合に Codex へのデバッグ相談コマンドを `additionalContext` に提示する — 根拠: docs/reference/packages.md
- [ ] EV-06（正常 / must）: `test-tampering-detector.py` は追加行に `it.skip`/`test.skip`/`describe.skip` または `@pytest.mark.skip`/`@unittest.skip` を検知した場合に警告する — 根拠: docs/reference/packages.md
- [ ] EV-07（正常 / must）: `test-tampering-detector.py` はテストファイル内の追加行に `eslint-disable`/`noqa`/`type: ignore` を検知した場合に警告する（テストファイル以外は対象外） — 根拠: 実装挙動
- [ ] EV-08（正常 / must）: `test-tampering-detector.py` は git 管理下のテストファイルが `rm`/`git rm`（`bash -c` 経由含む）で削除された場合に警告する — 根拠: docs/reference/packages.md
- [ ] EV-09（正常 / should）: `turn-end-summary.py` は Stop 時に working-context の変更ファイル数と Plans.md の WIP/TODO/blocked 件数を要約し `systemMessage` として出力する — 根拠: 実装挙動
- [ ] EV-10（異常 / must）: 全 hook は `main()` 内の例外を捕捉して stderr にログを出し、exit code 0 を返す（内部エラーでセッションを継続不能にしない fail-open 設計） — 根拠: 実装挙動
- [ ] EV-11（異常 / must）: `quality_gate.block_on_failed_test` の既定値は `true`（ブロック有効）であり、`false` を明示設定（opt-out）した場合のみテスト失敗時も `additionalContext` での提案に留める — 根拠: 2026-07-03 人間レビュー裁定
- [ ] EV-12（異常 / must）: `post-test-analysis.py` はテスト失敗検知時、既定でブロックする（stderr に理由を出力し exit code 2 で PostToolUse をブロック。`block_on_failed_test=false` の明示的な opt-out でのみ解除） — 根拠: 2026-07-03 人間レビュー裁定
- [ ] EV-13（異常 / must）: `test-gate-checker.py` は `audit-flags.json` の `quality_gate.enabled=false` のとき、テスト未実行警告を一切出さない — 根拠: 実装挙動
- [ ] EV-14（境界 / must）: `lint-on-save.py` の各 formatter/linter コマンドは 15秒でタイムアウトし、未導入（`FileNotFoundError`）やタイムアウト時は次の候補コマンド（pnpm→npm→yarn→npx→直接実行）にフォールバックしてハングしない — 根拠: 実装挙動
- [ ] EV-15（境界 / should）: `test-tampering-detector.py` は同一 finding（同一 file_path/label/snippet または同一削除ファイル）を一度警告した後、状態ファイルに記録し再警告しない — 根拠: 実装挙動
- [ ] EV-16（境界 / should）: 状態ファイルの read-modify-write は flock 排他ロック + 一時ファイル + `os.replace` で行われ、複数 worktree/セッションからの並行書き込みでも状態が破損・競合しない。`quality_gate_config.update_project_scoped_state`（test-gate-checker.py / post-test-analysis.py / post-implementation-review.py が使用）と `quality_gate_config.update_locked_json_state`（test-tampering-detector.py が使用。Issue #154 で追加）の双方が対象 — 根拠: 実装挙動
- [ ] EV-26（正常 / must）: `evaluation-set-checker.py` はテストファイル（`packages/<pkg>/tests/` はパスからパッケージ名を抽出、`tests/unit/`・`tests/e2e/` 配下はファイル名と実在パッケージ名の最長一致）の Edit/Write を検知し、対象パッケージを特定できた場合は `docs/evaluation/<pkg>.md` との突合確認を `additionalContext` に案内し、特定できない場合は汎用案内1行のみを出力する — 根拠: Issue #123 仕様。**拡張（Issue #237、実装済み・未レビュー）**: 上記2方式より前段で `.claude/config/quality-gates/evaluation-set-mapping.yaml`（`packages/quality-gates/config/` が既定配布元、`*.local.yaml` 上書き対応）の明示マッピング（評価セット ID → テストパス glob）を優先判定する。`packages/` 配下に実体を持たない SSOT（例: orchex CLI）のテストを正しい評価セットへ誘導し、`test_orchestra_manager_core.py` のような偶発的なトークン一致（`core`）による誤マッチも回避する（`packages/quality-gates/hooks/evaluation-set-checker.py` の `match_explicit_mapping`/`identify_package`）
- [ ] EV-27（異常 / must）: 対象パッケージは特定できたが `docs/evaluation/<pkg>.md` が存在しない場合、`evaluation-set-checker.py` は「評価セット未整備」警告を出し `_template.md` による新規作成を提案する — 根拠: Issue #123 仕様
- [ ] EV-28（異常 / must）: `audit-flags.json`（または `.local.json` 上書き）の `features.evaluation_set_check.enabled=false` のとき、`evaluation-set-checker.py` は突合案内・未整備警告を含む一切の出力を行わない — 根拠: Issue #123 仕様
- [ ] EV-29（境界 / should）: 同一 session_id かつ同一パッケージへの通知は `.claude/state/evaluation-set-checker.json` に記録され、以後同一セッション内では重複通知しない。パッケージを特定できない場合は `unknown:<相対ファイルパス>` をキーとしたファイル単位の dedup となり、特定不能な別ファイルはそれぞれ再通知される — 根拠: Issue #123 仕様
- [ ] EV-30（正常 / must）: 編集対象が `packages/<pkg>/tests/`・`tests/unit/`・`tests/e2e/` のいずれにも該当しないファイルの場合、`evaluation-set-checker.py` は突合案内・警告を一切出力しない（PostToolUse: Edit|Write で発火するが非テストファイルには反応しない） — 根拠: Issue #123 仕様

## 4. 類型別観点

<!-- docs/evaluation/README.md の hook 型チェックリストを本パッケージの実情で具体化する -->

- [ ] EV-17（境界 / must）: stdin/stdout 契約 — 各 hook は stdin の JSON（tool_name/tool_input/tool_response/cwd 等）をパースし、提案がある場合のみ `hookSpecificOutput.additionalContext`（Stop は `systemMessage`）を出力、無い場合は標準出力しない — 根拠: 実装挙動
- [ ] EV-18（境界 / must）: exit code 規約 — 8 hook（`evaluation-set-checker.py` 含む）のうち exit code 2（ブロック）を使うのは `post-test-analysis.py`（EV-12）のみで、他の 7 hook は常に exit 0 で終わる — 根拠: 実装挙動
- [ ] EV-19（異常 / must）: fail-safe 方針 — hook の内部エラーはセッションを壊さない fail-open（EV-10）を維持する一方、品質ゲート違反（テスト失敗）の検知時は明示的な opt-in なしに既定でブロックする（opt-out 方式。EV-11/EV-12 と連動） — 根拠: 2026-07-03 人間レビュー裁定
- [ ] EV-20（境界 / should）: 冪等性 — `post-implementation-review.py` の TTL 再武装（EV-03）と `test-tampering-detector.py` の既報告 finding 抑制（EV-15）により、同一入力の繰り返しで二重提案が起きない — 根拠: 実装挙動
- [ ] EV-21（異常 / must）: config 駆動 — `quality_gate.enabled` 配下の 7 hook は `quality_gate.enabled=false` のとき、提案・警告・ブロック・audit イベント記録を含む全動作を行わない（`evaluation-set-checker.py` は独立フラグ `features.evaluation_set_check.enabled` を持つため対象外。EV-28 参照） — 根拠: 2026-07-03 人間レビュー裁定
- [ ] EV-22（境界 / must）: 秘匿情報 — `additionalContext` に出力するコマンド文字列・テスト出力は、秘匿情報パターン（API キー・トークン・秘密鍵等。`packages/audit/hooks/secret_masking.py` の共通パターンに準拠）をマスキングしてから出力する。200 文字切り詰めはマスキングの代替としない — 根拠: 2026-07-03 人間レビュー裁定; `.claude/rules/coding-principles.md`（セキュリティ）
- [ ] EV-23（境界 / should）: 性能 — PostToolUse 系 hook は git コマンド（5秒 timeout）や formatter（15秒 timeout）を同期的に subprocess 実行するため、対象ファイルが多い操作直後は数秒〜十数秒の遅延が生じ得る — 根拠: 実装挙動
- [ ] EV-24（正常 / must）: ドキュメント整合 — `packages/quality-gates/README.md` が存在し、独自 README を持つ他パッケージ（audit / fail-logs 等）と同様に責務・hook 一覧・設定キー（`quality_gate.*`）を記述している — 根拠: 2026-07-03 人間レビュー裁定
- [ ] EV-25（境界 / should）: 後方互換性 — `audit-flags.json` の `quality_gate.*` キー（enabled/block_on_failed_test/test_file_threshold/test_line_threshold）が未定義・欠落していてもコード側デフォルト値（`QUALITY_GATE_ENABLED_DEFAULT=True` 等）にフォールバックし、config 未同期環境でも動作する — 根拠: 実装挙動
- N/A: 生成物の同期 — quality-gates は hook スクリプトとスキル指示書のみで構成され、`templates/context/` 経由で生成される正本ファイルを持たない
- N/A: 配布ライフサイクル（install/update/uninstall） — install/sync 機構自体は core パッケージの責務であり、quality-gates 固有のアンインストール時クリーンアップ処理（`.claude/state/*.json` 等の削除）はコード上確認できないため対象外とする（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡。Issue #154 で状態ファイルの保存先が /tmp からプロジェクト内 `.claude/state/` に変わったため、`.gitignore` 済みの当該ディレクトリはプロジェクト単位で自然に隔離される）

## 5. テストレビュー判断基準（パッケージ固有）

- fail-safe 観点のテストは本書の裁定を正とする: config 未定義時のデフォルト値は `quality_gate.enabled=true` かつ `block_on_failed_test=true`（ブロック既定有効）。hook 内部エラーの fail-open（EV-10）と品質ゲートの既定ブロック（EV-11/12/19）を混同しない
- 状態ファイル（`.claude/state/*.json`。Issue #154 で /tmp から移行し worktree = project_dir 配下に閉じ込めた）を扱うテストは、project_dir を実在する書き込み可能ディレクトリにし、`project_key`（git-common-dir）によるプロジェクト間分離が機能していることを確認する
- `lint-on-save.py` 等の外部コマンド実行系テストは、実ツール未導入環境でも `is_missing_tool_output` によるフォールバックが機能することを確認する（CI 環境依存で不安定化させない）
- EV-11/12/19/21/22/24 は 2026-07-03 の人間レビューで仕様確定済み。現実装はこの裁定と差分がある（既定 false・enabled 不均一・マスキング未実装・README 不在）ため、テストは現実装ではなく本書を正として書き、実装側の追従は Issue #134 で行う
