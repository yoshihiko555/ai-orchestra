# codex-suggestions 評価セット

**パッケージ**: `packages/codex-suggestions`
**類型**: hook 型
**作成日**: 2026-07-03
**最終レビュー日**: 2026-09-25（Issue #452 レビュー指摘対応: EV-18 を新設。after-plan は `tool_response` が async 起動メタデータのとき発火しないことを追加（判定は `hook_common.is_async_launch` に集約）。前回: Issue #452: EV-17 を新設し after-plan 発火条件を「暫定仕様（subagent_type のみ）」として確定、prompt 部分一致（plan_keywords）を廃止。正式な発火条件と Claude-only 環境を前提にした設計原則の見直しは Issue #456 で行う。前回レビュー 2026-07-03: EV-15 を仕様確定、EV-07・EV-10 を欠番化。裁定内容は §3 参照）／EV-19（before-write のプロジェクト外パス除外、2026-09-25 追加）は未レビュー（人間レビュー待ち）
**仕様更新**: 2026-10-28（ユーザーによる A 案・追加レビュー対応承認、[ADR-060](../adr/ADR-20261028-060.md)）。編集前の任意助言・発火条件・通知抑制を更新する。after-plan の発火条件は変更しない。新しい期待値は未検証のためチェックを付けない。
**情報源**: docs/reference/packages.md（codex-suggestions セクション）, docs/reference/hooks.md, facets/instructions/codex-suggestion-compliance.md, facets/instructions/codex-delegation.md, .claude/rules/config-loading.md（補助: packages/codex-suggestions/manifest.json, hooks/check-codex-before-write.py, hooks/check-codex-after-plan.py, packages/core/hooks/hook_common.py の実装挙動）

## 1. 責務定義

本パッケージは、Edit/Write によるファイル変更前と、Plan 系サブエージェントタスク完了後の 2 箇所で、Codex CLI への相談を促す非拘束的な提案（advisory suggestion）を `additionalContext` として注入する。提案は `cli-tools.yaml`（+ `.local.yaml`）の `codex.enabled` に従って有効/無効を切り替えられ、いかなる場合もツール実行やエージェント実行そのものをブロックしない。ただし after-plan（Plan 系タスク完了後）の発火条件および hook の存置可否は 2026-07-03 レビューで再検討対象となった（EV-07・EV-10 欠番、Issue #129）。2026-09-25（Issue #452）で、発火条件は「暫定仕様（`tool_input.subagent_type` のみで判定、prompt 部分一致は廃止）」として EV-17 に確定した。正式な発火条件と Claude-only 環境を前提にした設計原則の見直しは Issue #456 で行う。

### Non-Goals

- Codex CLI を実際に実行すること（`codex exec` の呼び出し自体はオーケストレーター/ユーザーの判断に委ねる）
- 提案によって編集の保留・追加相談・採否の毎回の説明を強制すること（助言は任意。ただし明示 routing とスキル固有の委譲義務は別であり維持する）
- Antigravity CLI に関する提案（`antigravity-suggestions` パッケージの責務）
- typo 修正等の意味的な軽微性や相談済みを hook 自身が判定すること（後述 EV-11・「5. テストレビュー判断基準」参照）。通知済みは相談済みを意味せず、transcript は解析しない

## 2. 期待する入出力・副作用

before-write の config・対象範囲・marker に使う project / worktree は、環境変数 `CLAUDE_PROJECT_DIR` が非空ならそのルートを使い、未設定または空の場合だけ hook 入力の `cwd` で解決する。after-plan は今回の変更対象外とする。

| 構成要素                      | 入力                                                                                                                                                                                                | 期待する出力                                                                                                              | 副作用                                                           |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `check-codex-before-write.py` | PreToolUse(Edit\|Write) stdin JSON（`tool_input.file_path`, `content` / `old_string` / `new_string`, `cwd`, `session_id`, トップレベルの `agent_id`）                                                                                                         | 条件成立時: `hookSpecificOutput.additionalContext` に `[Codex Suggestion] ...` を含む JSON を stdout へ／非該当時: 無出力 | `session_id` があれば hash 化した空の通知 marker のみ保存。ブロックせず常に exit 0 |
| `check-codex-after-plan.py`   | PostToolUse(Agent\|Task) stdin JSON（`tool_name`, `tool_input`, `tool_response`, `cwd`）。発火は `tool_input.subagent_type` のみで判定し、`tool_response` が async 起動メタデータの場合は発火しない | 条件成立時: `[Codex Review Suggestion] ...` を含む JSON を stdout へ／非該当時: 無出力                                    | なし（ファイル書き込み・ブロックなし。exit 0）                                                     |

## 3. 評価観点

- [ ] EV-01（正常 / must）: before-write: basename が `DESIGN.md` / `ARCHITECTURE.md`、または `design/`・`architecture/`・`schema/`・`schemas/`・`interfaces/`・`contracts/` 等の明確な設計パスへの変更、または `interface` / `abstract class` / `Protocol` / `ABC` / `@abstractmethod` 等の契約宣言の追加・削除・変更で `[Codex Suggestion]` を出力する（config・対象範囲・通知抑制の条件も適用）。TS/JS の `interface` は設計パス内の変更としてのみ対象とし、`Props` という名前だけでは除外しない — 根拠: ADR-060、docs/reference/packages.md
- ~~EV-02~~（**欠番**, 2026-10-28）: サイズだけによる発火を廃止する（ADR-060）。旧 500 文字超・`src/` 内 200 文字超の期待値やサイズ別の理由文は固定化しない。否定側は EV-05 で確認する
- [ ] EV-03（異常 / must）: before-write: `codex.enabled: false`（`cli-tools.yaml` または `.local.yaml` 上書き）のとき、他条件に関わらず `[Codex Suggestion]` を出力しない — 根拠: facets/instructions/codex-suggestion-compliance.md（`codex.enabled: false` の場合は hook 自体が提案を抑制する）
- [ ] EV-04（境界 / should）: before-write: `SIMPLE_EDIT_PATTERNS`（`.gitignore` / `README.md` / `CHANGELOG.md` / `requirements.txt` / `package.json` / `pyproject.toml` / `.env.example`）に該当するファイルパスでは、他条件を満たしても提案を出力しない — 根拠: 実装挙動
- [ ] EV-05（境界 / must）: before-write: 明確な設計パス・契約宣言の変更がない通常の Edit/Write では、`core` / `config` / `model`、通常の `class`、文字数、`src/` だけを理由に `additionalContext` を出力しない。通常パスの TS/JS の `interface` もコードシグナルから除外する — 根拠: ADR-060、docs/reference/packages.md
- [ ] EV-06（異常 / must）: before-write: `file_path` が空・4096 文字超、`content` が 1,000,000 文字超、または `file_path` に `..` を含む場合は提案を出力せず exit code 0 で終了する（入力バリデーション） — 根拠: 実装挙動
- ~~EV-07~~（**欠番**, 2026-07-03 レビュー）: after-plan の発火条件は「あるべき仕様」として未確定と判定（下記注記・Issue #129）
- [ ] EV-08（異常 / must）: after-plan: `tool_response` が構造化フィールド（`is_error` / `error`）でエラーを示す場合は提案を抑制する（`str(tool_response)` の部分一致では判定しない設計） — 根拠: 実装挙動
- [ ] EV-09（異常 / must）: after-plan: `codex.enabled: false` のとき提案を出力しない — 根拠: .claude/rules/codex-suggestion-compliance.md + 実装挙動
- ~~EV-10~~（**欠番**, 2026-07-03 レビュー）: after-plan の非該当時無出力条件は EV-07 と一体で未確定と判定（下記注記・Issue #129）
- [ ] EV-11（境界 / should）: `[Codex Suggestion]` は任意助言であり、編集の保留・追加相談・採否の毎回の説明を義務付けない。相談済み・軽微・適用外なら続行できる。変更の累積数だけで一律の実装委譲義務を発生させない — 根拠: ADR-060、facets/instructions/codex-suggestion-compliance.md。**スコープ注記**: 意味的な軽微性・相談済みの判断はオーケストレーターの責務。hook 単体の pytest ではなく、エージェント挙動の統合テスト/レビューで確認する

- [ ] EV-17（境界 / must）: after-plan: **暫定仕様** — `tool_input.subagent_type` が `plan` / `planner`（大文字小文字不問）のときのみ発火し、prompt の部分一致（`plan` / `計画` 等）では発火しない（`Plans.md` を含むだけのプロンプトで誤発火するため廃止）— 根拠: Issue #452。正式な発火条件と Claude-only 環境を前提にした設計原則の見直しは Issue #456 で行う — 自動テスト: 否定側 `packages/codex-suggestions/tests/test_check_codex_after_plan.py -k substring`、肯定側 `test_is_plan_agent_task_true_for_subagent_type_only`（同ファイル）
- [ ] EV-18（境界 / must）: after-plan: `tool_response` が async 起動メタデータ（`isAsync: true` または `status: async_launched`）のときは発火しない（判定は `hook_common.is_async_launch` に集約）— 根拠: Issue #452（バックグラウンド起動時点では計画がまだ完了していないため）— 自動テスト: `packages/codex-suggestions/tests/test_check_codex_after_plan.py -k async`
- [ ] EV-19（境界 / must）[2026-09-25 追加]: before-write: `file_path` がプロジェクトルート（`CLAUDE_PROJECT_DIR` → hook 入力の `cwd` の順で解決。環境変数が非空ならそのルートを使い、未設定または空の場合だけ `cwd` を使う）配下に解決できない場合（scratchpad・メモリ・他リポジトリ等）は、他の発火条件を満たしても `[Codex Suggestion]` を出力しない。相対パスはプロジェクトルート基準で解決し、シンボリックリンク等で解決後にプロジェクト外を指す場合も対象外とする。プロジェクトルートを解決できない場合は従来どおり判定する（fail-open: 提案を抑制しすぎない） — 根拠: プロジェクト外のファイルは Codex 相談の対象にならず、提案はノイズになる（`update-working-context` がプロジェクト外の変更を記録しないのと同じ方針）

> **after-plan 発火条件の再検討（EV-07・EV-10 欠番, 2026-07-03 → EV-17 で暫定仕様確定, 2026-09-25）**: 当初 after-plan hook は「計画・レビュー系のサブエージェントタスク完了後に Codex レビューを促す」用途を想定していたが、現状その用途はほぼ使われない見込み。Plan 系タスクの判定条件・非該当時の無出力が「あるべき仕様」かは未確定だったため、Issue #452 で prompt 部分一致による誤発火（`Plans.md` 等を含むだけのプロンプト）を解消する暫定仕様（EV-17: `subagent_type` のみで判定）を確定した。正式な発火条件の再定義（または after-plan hook の廃止）と Claude-only 環境を前提にした設計原則の見直しは Issue #456 で検討する。

- [ ] EV-20（境界 / must）: before-write: トップレベルの `agent_id` が非空の文字列（`str`）の場合だけ、サブエージェント／インプロセス teammate 内として提案を抑制する。`agent_id` の欠落・空文字列・非文字列や、`--agent` メインにも付く `agent_type` 単独では抑制しない。`tool_input.subagent_type` は呼出先のため判定に使わない — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-21（境界 / must）: before-write: `session_id` があれば同一 session / file に 1 度だけ通知する。別 session / file、別 project / worktree の通知状態とは混同しない。通知済みは相談済みではなく、transcript を解析しない — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-22（異常 / must）: before-write: 状態書き込み失敗時は無出力・exit 0。`session_id` がない場合は state を作らず、その呼び出しごとの助言とする — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-23（境界 / must）: before-write: Edit の `old_string` / `new_string` で契約宣言が同じならコードシグナルを抑制する。設計パスの判定とは独立し、契約宣言の変更と通常のメソッド実装変更を区別する — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-25（正常 / must）: 任意助言の採否とは独立して `[Resolved Routing]` / `agents.<name>.tool` の指定とスキル固有の実装委譲義務を守る。相談を選ぶ場合も既存の config 解決・sandbox 安全条件・非対話実行条件に従う — 根拠: ADR-060、facets/instructions/codex-suggestion-compliance.md（hook 単体テスト対象外）
- [ ] EV-26（境界 / must）: before-write: 設計パス以外のソース（`.py` / `.pyi` / `.ts` / `.tsx` / `.js` / `.jsx` / `.java`）への Write は、ディスク上の旧本文と新本文の契約宣言を比較し、同じならコードシグナルを抑制する。新規ファイルの旧本文は空とする。旧本文が読取不能・大き過ぎ・非通常ファイル・非 UTF-8 の場合は任意通知を抑制し、exit 0 とする。設計パスは旧本文比較と独立に判定する — 根拠: ADR-060、docs/reference/hooks.md

### 検出しないもの・既知の境界

以下は設計パス以外のコードシグナルでは検出しない。設計パスの判定は独立し、今回は検出範囲を拡大しない。

- 同じ `Protocol` / `interface` ヘッダ内のメソッド追加・変更（ヘッダ自体が同じ場合）
- 同じ `@abstractmethod` の 2 個目の追加（宣言は set で比較し、個数は比較しない）
- `metaclass=ABCMeta` による抽象クラスや Go の `type X interface` 宣言
- 通常パスの TS/JS の `interface`（EV-05）。`Props` だけを名前で除外する仕様にはしない
- Edit の部分断片が解析できず、契約宣言を抽出できない場合

## 4. 類型別観点

- [x] EV-12（境界 / must）: stdin/stdout 契約 — 両 hook とも PreToolUse/PostToolUse の stdin JSON を読み込み、提案時のみ `hookSpecificOutput.additionalContext` を含む JSON を stdout に出力する。非該当時は何も print しない — 根拠: 実装挙動
- [ ] EV-13（正常 / must）: exit code 規約 — 両 hook とも常に exit code 0 で終了し、ブロック（非ゼロ exit）は行わない — 根拠: 実装挙動 + .claude/rules/codex-suggestion-compliance.md（提案は advisory のみでツール実行を妨げない）
- [ ] EV-14（異常 / must）: fail-safe 方針 — hook 内部で例外が発生しても exit code 0 で終了し、Claude Code のツール実行・エージェント実行を妨げない（fail-open）。一般の内部エラーは従来どおり stderr に出力するが、before-write の状態書き込み失敗や旧本文を安全に読めない場合は無出力とする（EV-22 / EV-26）— 根拠: 実装挙動、ADR-060
- 冪等性: before-write の通知抑制は EV-21 / EV-22 で確認する。after-plan は従来どおり状態を持たない
- [x] EV-15（境界 / must）: config 駆動 — `codex.enabled` は `.claude/config/agent-routing/cli-tools.yaml` をベースに `cli-tools.local.yaml` の上書きを適用する。`codex` セクション自体が未定義の場合は**デフォルト無効（false）**として扱い、明示的に有効化されない限り提案を発火しない — 根拠: 2026-07-03 人間レビュー裁定（Issue #129 で実装追従済み。`hook_common.is_cli_enabled` に呼び出し元限定の `default` 引数を追加し、codex-suggestions の 2 hook のみ `is_cli_enabled("codex", config, default=False)` として呼び出す形で解消。共有デフォルト自体は変更せず、agent-routing / antigravity-suggestions / image-generation など他パッケージの呼び出し（`default` 省略、従来どおり True）には影響しない）。**「明示的に有効化」は project 自身が導入した config に限る**（PR #247 レビュー指摘の追加是正）: agent-routing を導入していない project（`.claude/config/agent-routing/cli-tools.yaml` も `.local.yaml` も project 側に存在しない）では、`load_package_config` が `$AI_ORCHESTRA_DIR/packages/agent-routing/config/cli-tools.yaml`（パッケージ同梱フォールバック）にフォールバックする。このフォールバック内の `codex.enabled: true` は project の明示 opt-in ではないため、`hook_common.has_project_config("agent-routing", "cli-tools.yaml", project_dir)` が False の間は `load_package_config` を呼ぶ前に no-op（無出力・exit 0）とする。project-local な config が実在する通常環境（agent-routing 導入済み）では従来どおり `codex.enabled` の値に従う
- [ ] EV-24（境界 / must）: 秘匿情報 — before-write が作る状態は各 project / worktree の `.claude/state/suggestions/` 配下の session / 対象を hash 化した空 marker のみ。質問本文・生の session ID・対象パスを状態に保存しない。提案はユーザー入力コンテンツの本文を含めず、外部送信もしない。marker の自動削除は今回追加せず、終了後も session ディレクトリが残る。cleanup は将来課題とする — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-16（境界 / should）: 性能 — 判定と通知 marker の処理はローカルで完結し、ネットワーク I/O や外部プロセス起動を行わず、Edit/Write・Agent/Task 実行を著しく遅延させない — 根拠: docs/reference/hooks.md

## 5. テストレビュー判断基準（パッケージ固有）

- EV-07・EV-10 は 2026-07-03 レビューで欠番化された（after-plan 発火条件が仕様未確定）。Issue #452 で暫定仕様（EV-17: `subagent_type` のみで判定）を確定したため、`subagent_type` 判定は固定化テストの対象とする。正式な発火条件の再定義（または after-plan hook の廃止）と設計原則の見直しは Issue #456 で行う
- EV-11 / EV-25 は hook 単体の pytest では検証できない（オーケストレーター/エージェントの判断・routing・委譲義務のため）。統合テスト・レビューで確認し、hook に意味的な typo 判定や相談済み判定を求めない
- `SIMPLE_EDIT_PATTERNS`（EV-04）の具体的な文字列リストは従来どおり実装が根拠。EV-01 / EV-05 / EV-23 は定数リストの厳密比較ではなく、設計パス・契約宣言の変更と通常編集の入出力で確認する。旧サイズ閾値や一般キーワードだけの発火を固定化しない
- EV-20〜EV-24 は before-write と共通 helper のテストで確認する。非空文字列の `agent_id` によるサブエージェント／インプロセス teammate の識別、`agent_id` の欠落・空文字列・非文字列や `agent_type` 単独・呼出先の `subagent_type` では抑制しないこと、session / 対象 / project / worktree の分離、空 marker のみの保存、状態書き込み失敗・session 欠落を含める。after-plan に新しい通知抑制条件を適用しない
- EV-26 は既存ファイルの宣言不変・変更、新規ファイル、旧本文の読取不能・大き過ぎ・非通常ファイル・非 UTF-8、設計パスの独立判定を確認する。config・対象範囲・marker が同じルートを使うことと、非空の `CLAUDE_PROJECT_DIR` を優先し、未設定または空の場合だけ hook 入力の `cwd` にフォールバックすることも確認する
- 次回監査では researcher の指定 CLI 試行率とメインの任意提案の採用率を分離する。サブエージェント内の通知は `agent_id` で抑制し、researcher の `agy` 実行は `researcher.md` の指示が担うため、hook 通知数を researcher の遵守率の代用にしない
