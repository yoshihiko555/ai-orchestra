# codex-harness 評価セット

**パッケージ**: `packages/codex-harness`
**類型**: 主: hook 型、副: CLI ツール型
**作成日**: 2026-07-05
**最終レビュー日**: 未レビュー（人間レビュー待ち）
**情報源**: docs/design/codex-cli-harness.md（§0 導入注記, §9 Run Lifecycle, §10 Policy Model, §15 Readiness Checklist）, .claude/Plans.md（Project: Codex CLI Harness Stage 0-2, Decisions/Notes）, packages/codex-harness/manifest.json, packages/codex-harness/codex/{hooks.json, hooks/_.py, schemas/_.json, rules/codex-harness.rules, config-harness.toml, validation.json}, packages/codex-harness/scripts/{harness_common.py, codex_run.py, codex_review.py}, scripts/lib/sync_engine.py（apply_codex_harness_config / sync_codex_files / collect_facet_build_targets）, scripts/lib/toml_merge.py, scripts/lib/gitignore_sync.py, scripts/orchestra-manager.py（uninstall）

## 1. 責務定義

本パッケージは、Codex CLI を主たる利用面とするこのリポジトリに対し、実行前後のガードレール（prompt secret 検出・危険コマンド禁止・Stop 時検証）、`.codex/` 配下への hooks/schemas/rules/config の repo-local 配布、および非対話実行（run）・レビュー実行（review）の 2 スクリプトによる機械可読な artifact 一式（events/diff/validation/report）の生成を保証する。ハーネスは Codex CLI 自体を置き換えるものではなく、Codex ネイティブの hooks・rules・`config.toml`・permission profiles を組み合わせた repo-local な実行境界を提供する（設計 §1, §2.2, §4）。

### Non-Goals

- Codex CLI の代替 TUI やモデル API プロキシを作ること（設計 §3.2 (1)(2)）
- Codex の agent loop を自前実装すること、`danger-full-access` を前提にした自動化（設計 §3.2 (3)(4)）
- CI からの本番 deploy / merge / release の自動化（設計 §3.2 (5)）。`codex_run.py` / `codex_review.py` は patch / findings artifact の生成のみを行う。`gh pr merge` / `gh release create` / publish 系は rules・hook 双方で禁止する（承認しても実行不可）。一方 `git push` / `gh pr create` は rules で `prompt`（対話 Codex で人間承認時のみ実行）とし、ハードブロックはしない（Issue #161 フォローアップ）
- 全 repo への同一設定の強制（設計 §3.2 (6)）。config マージは add-if-missing / upsert のレイヤ判断でユーザー設定と共存する
- `AGENTS.md` および `.codex/config.toml` の新規作成・初期所有。これらは `codex-suggestions` パッケージの責務であり、本パッケージは既存の `.codex/config.toml` への設定マージのみを行う（Plans.md Decisions 2026-07-04）
- `cocoindex` 等 MCP サーバーの `.codex/config.toml` `[mcp_servers.*]` 設定管理（`cocoindex` パッケージの責務）。本パッケージの config マージはこのセクションに触れない
- hooks による完全なセキュリティ境界の代替。OS サンドボックス・コンテナ隔離・CI ポリシーの代替にはならない（設計 §16.2）
- `.codex/runs/` `.codex/reports/` の `.gitignore` エントリ管理。これは `scripts/lib/gitignore_sync.py` の共通 ENTRIES が担当し、本パッケージの manifest では宣言しない

## 2. 期待する入出力・副作用

| 構成要素                                                    | 入力                                                                           | 期待する出力                                                                                                                                                                                                                            | 副作用                                                                                   |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `user_prompt_secret_scan.py`（UserPromptSubmit hook）       | stdin JSON `{prompt}`                                                          | 検出時: stderr に検出パターン名（値は非表示）+ exit 2（ブロック） / 非検出時: 無出力 + exit 0                                                                                                                                           | なし                                                                                     |
| `pre_tool_use_policy.py`（PreToolUse hook）                 | stdin JSON `{tool_input: {command\|cmd\|script}}`                              | 検出時: stderr に違反パターン名 + exit 2（ブロック） / 非該当時: 無出力 + exit 0                                                                                                                                                        | なし                                                                                     |
| `stop_validate.py`（Stop hook）                             | stdin JSON `{cwd}` + `.codex/validation.json`                                  | 常に exit 0。失敗時のみ stdout に `{continue:true, systemMessage}`                                                                                                                                                                      | `.codex/reports/validation-<timestamp>.log` を書き込み                                   |
| `codex_run.py`（非対話 run スクリプト）                     | argv: `task`, `--project`, `--sandbox`, `--allow-untrusted-hooks`, `--timeout` | `.codex/runs/<run_id>/` 一式（prompt.md / metadata.json / git-status.before.txt / git-status.after.txt / diff-stat.txt / diff.patch / validation.log / final.json / report.md）。exit code は `codex exec` の returncode をそのまま返す | `.codex/runs/` 配下へのファイル作成、`codex exec` サブプロセス実行（workspace-write 可） |
| `codex_review.py`（review スクリプト）                      | argv: `--base`, `--project`, `--allow-untrusted-hooks`, `--timeout`            | `.codex/runs/<ts>-review/` 一式（input.diff / events.jsonl / progress.log / review.json / report.md）。diff が空なら `codex exec` を呼ばず exit 0                                                                                       | 同上（sandbox は read-only 固定）                                                        |
| `harness_common.verify_hooks_trust` / `resolve_trust_flags` | `project_root`、`.claude/orchestra.json` の `codex_file_hashes`                | `TrustResult(trusted, reasons)` / `codex exec` への追加フラグ、または `None`（中断）                                                                                                                                                    | なし（読み取りのみ）                                                                     |
| `sync_engine.sync_codex_files`（配布）                      | `manifest.codex_files`、project 既存ファイル、`orch["codex_file_hashes"]`      | 同期したファイル数                                                                                                                                                                                                                      | `.codex/` 配下へのファイルコピー、`orch["codex_file_hashes"]` 更新                       |
| `sync_engine.apply_codex_harness_config`（配布）            | `config-harness.toml`、project `.codex/config.toml`                            | 変更有無（bool）                                                                                                                                                                                                                        | `.codex/config.toml` のマージ書き込み                                                    |
| `sync_engine.collect_facet_build_targets`（配布）           | `installed_packages`、各 manifest の `facet_targets`                           | facet ビルド対象リスト（`"claude"` を必ず含む）                                                                                                                                                                                         | なし                                                                                     |

## 3. 評価観点

### 配布基盤

- [ ] EV-01（正常 / must）: `sync_codex_files` — target が未存在の場合はコピーし、`codex_file_hashes` 台帳にハッシュを記録する — 根拠: 設計 §7.1 + Plans.md Decisions（配布は `codex_files` + hash 保護方式）
- [ ] EV-02（境界 / must）: `sync_codex_files` — target は存在するが台帳に記録がない（配布前からの既存ファイル）場合、force なしでは上書きせず warn する。force ありなら上書きし台帳に記録する — 根拠: 実装挙動（`_sync_codex_file` の分岐）
- [ ] EV-03（異常 / must）: `sync_codex_files` — 現在のハッシュが台帳ハッシュと不一致（配布後にユーザーが改変）の場合、force なしでは上書きせず warn する。force ありなら上書きする — 根拠: 実装挙動 + Plans.md Decisions（ユーザー改変検出時は skip + warn、force 引数対応）
- [ ] EV-04（正常 / should）: `sync_codex_files` — 現ハッシュが台帳ハッシュと一致し、かつ配布元ファイルにも変更がない場合は no-op（コピー0件）とする — 根拠: 実装挙動
- [ ] EV-05（正常 / must）: `sync_codex_files` — 現ハッシュが台帳ハッシュと一致し、配布元ファイルが更新されている場合は新しい内容で上書きし台帳を更新する — 根拠: 実装挙動
- [ ] EV-06（正常 / must）: CLI `install --force` が `OrchestraManager.install(..., force=True)` → `run_initial_sync(force=True)` → `sync_codex_files(..., force=True)` まで配線され、EV-02 / EV-03 の抑制を解除する — 根拠: Plans.md Phase 1（`--force` の CLI フラグ配線）
- [ ] EV-07（正常 / must）: `collect_facet_build_targets` は常に `"claude"` を含み、`installed_packages` の各 manifest の `facet_targets` を集約し重複を除去する（パッケージ名決め打ちではなく capability 判定） — 根拠: Plans.md Phase 1（facet build ゲートの capability 判定改修）
- [ ] EV-08（正常 / must）: `apply_codex_harness_config` は `default_permissions`（トップレベルキー）と `[features].hooks` を add-if-missing で扱い、既存のユーザー値を上書きしない — 根拠: packages/codex-harness/codex/config-harness.toml 冒頭コメント + 設計 §5.5
- [ ] EV-09（正常 / must）: `apply_codex_harness_config` は `[permissions.*]` セクションを upsert する（harness 所有、ユーザーが編集していても harness 側の値に揃える） — 根拠: 同上
- [ ] EV-10（境界 / must）: `apply_codex_harness_config` は次のいずれかの場合に何もせず `False` を返す: `codex-harness` が `installed_packages` に無い／プロジェクトの `.codex/config.toml` が存在しない（新規作成しない）／ `config-harness.toml` が存在しない — 根拠: 実装挙動（config.toml の初期所有は codex-suggestions のため新規作成しない）
- [ ] EV-11（境界 / should）: `apply_codex_harness_config` は2回目実行で差分がなければ `False` を返す（冪等） — 根拠: 実装挙動
- [ ] EV-12（境界 / must）: `apply_codex_harness_config` は `[mcp_servers.*]` 等、harness が所有しない既存セクションを変更しない — 根拠: 実装挙動（`_iter_toml_sections` は `permissions.` prefix のみ走査）

### hooks

- [ ] EV-13（正常 / must）: `user_prompt_secret_scan.py` は `OPENAI_API_KEY` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `GITHUB_TOKEN` / `ghp_` / `github_pat_` / `sk-` / PEM private key block の各パターンを検出し exit 2 でブロックする — 根拠: 設計 §10.4（secret policy 検出文字列例）
- [ ] EV-14（異常 / must）: `user_prompt_secret_scan.py` は stdin が不正 JSON、または `prompt` フィールドが欠如する場合 fail-open（exit 0）とする — 根拠: 実装挙動（ファイル冒頭 docstring の fail-open 明記）
- [ ] EV-15（境界 / should）: `user_prompt_secret_scan.py` は最小長未満の `sk-` のような文字列や秘密情報を含まない通常プロンプトを誤検知しない — 根拠: 実装挙動
- [ ] EV-16（正常 / must）: `pre_tool_use_policy.py` は `gh pr merge` / `gh release create` / `npm publish` / `pnpm publish` / `docker push` / `kubectl apply` / `terraform apply` / `rm -rf /` / `rm -rf ~` / `chmod -R 777` / curl・wget パイプ の各禁止コマンドを検出し exit 2 でブロックする — 根拠: 設計 §10.3（常に禁止コマンド一覧）。**注**: `git push` は本 hook のハードブロック対象から除外し（rules 層で `prompt` = 人間承認に委譲、hook は allow/block の二値で `prompt` を表現できないため）、`git push` 入力に対しては exit 0（allow）を返す（Issue #161 フォローアップ）
- [ ] EV-17（境界 / should）: `pre_tool_use_policy.py` は `rm -rf ./build` のような狭い相対パス、および `git status` / `git diff` / `pytest -q` / `git push` / `gh pr create` 等の非ブロック対象コマンドを誤検知しない（`git push` / `gh pr create` は rules 層 `prompt` 管理のため hook では allow） — 根拠: 実装挙動（word boundary によるナローイング。rules ファイル側の広い `rm -rf` prefix rule とは責務分担）
- [ ] EV-18（異常 / must）: `pre_tool_use_policy.py` は stdin が不正、または `tool_input` が欠如する場合 fail-open（exit 0）とする — 根拠: 実装挙動
- [ ] EV-19（正常 / must）: `stop_validate.py` は `.codex/validation.json` のコマンドを順次実行し、結果（passed/failed）を集計してログファイルに書き込む — 根拠: 設計 §5.7（Stop: lint/typecheck/test/secret scan を実行しログとして残す）
- [ ] EV-20（異常 / must）: `stop_validate.py` は検証コマンドが失敗しても Stop をブロックしない（常に exit 0）。失敗時のみ `systemMessage` で summary を出力する — 根拠: 設計 §4.5（deterministic validation はログとして残すが、Stop hook 自体は本パッケージの設計判断としてブロックしない実装挙動）
- [ ] EV-21（境界 / should）: `stop_validate.py` は全コマンド成功時は無出力、`.codex/validation.json` 不在時は何もしない（`.codex/reports/` も作成しない） — 根拠: 実装挙動

### trust 検証 / run / review

- [ ] EV-22（異常 / must）: `verify_hooks_trust` は次のいずれかで fail-closed（`trusted=False`）となる: `orchestra.json` 不在、台帳に hooks 対象エントリなし、ハッシュ不一致、対象ファイル欠如、対象が symlink — 根拠: 設計 §0（非対話実行では hash ベースの trust モデルにより `--dangerously-bypass-hook-trust` が必要。ハーネス側で SHA-256 検証通過時のみ付与する fail-closed 設計）
- [ ] EV-23（正常 / must）: `verify_hooks_trust` は台帳記録済みの全 hooks ファイルがハッシュ一致する場合のみ `trusted=True` となり、`resolve_trust_flags` は `--dangerously-bypass-hook-trust` を返す — 根拠: 同上
- [ ] EV-24（異常 / must）: `resolve_trust_flags` は untrusted かつ `allow_untrusted=False` の場合 `None` を返し、`codex_run.main()` / `codex_review.main()` はこれを受けて exit 1 で中断する — 根拠: 設計 §0 + 実装挙動
- [ ] EV-25（境界 / should）: `resolve_trust_flags` は untrusted でも `allow_untrusted=True` の場合、空リストを返し（bypass フラグなしで）実行を継続する — 根拠: 実装挙動
- [ ] EV-26（正常 / must）: `codex_run.main()` は run artifact 一式（prompt.md / metadata.json / git-status.before.txt / git-status.after.txt / diff-stat.txt / diff.patch / validation.log / final.json / report.md）を `.codex/runs/<run_id>/` に保存し、`codex exec` の exit code をそのまま返す — 根拠: 設計 §9.2（Run artifact model）
- [ ] EV-27（異常 / must）: `codex_run` は必須 `.codex` ファイル欠如、または `--project` が git リポジトリ外の場合 preflight で中断する（exit 1） — 根拠: 設計 §9.1（Preflight: repo root detection / `.codex/` files existence）
- [ ] EV-28（正常 / must）: `codex_run` / `codex_review` は `metadata.json` / `final.json` または `review.json` / `report.md` に `redact_secrets` を適用してから書き込む — 根拠: 設計 §10.4（secret policy）+ §4.6（Observability の一環として保存する artifact の秘密保護）
- [ ] EV-29（境界 / must）: `codex_run` の `codex exec` 呼び出しは `stdin=subprocess.DEVNULL` を指定し、非対話実行を保証する — 根拠: 設計 §5.2（`codex exec` は TUI を開かない非対話実行）+ .claude/rules/codex-delegation.md（stdin を封じる）と同種の運用原則
- [ ] EV-30（正常 / must）: `codex_run` の exit code は `codex exec` の returncode をそのまま返す（タイムアウト時は 124） — 根拠: 実装挙動
- [ ] EV-31（正常 / must）: `codex_review` は sandbox を read-only 固定で実行し、diff を stdin 経由で渡す（workspace-write にはならない） — 根拠: 設計 §8.3（Review mode: sandbox は read-only）
- [ ] EV-32（境界 / must）: `codex_review` は base...HEAD の diff が空の場合、`codex exec` を呼ばず exit 0 で中断する — 根拠: 実装挙動（無駄な review 実行を避ける設計判断）
- [ ] EV-33（境界 / should）: `codex_review` は read-only 実行中に `git status` が変化した場合、report に警告を含める — 根拠: 設計 §8.3（review mode は read-only を要求する）の逸脱検知として実装された挙動
- [ ] EV-34（正常 / must）: `parse_events`（run/review 共通パターン）はスキーマ形状のペイロードを優先抽出し、無ければ最後の agent メッセージ text にフォールバックし、不明/不正な JSONL 行は無視して例外を出さない — 根拠: 設計 §16.3（JSONL event schema は versioned input として扱い、未知イベントを許容する。パーサー失敗で artifact を削除しない）
- [ ] EV-35（境界 / should）: `write_atomic` は tmp ファイル経由でアトミックに書き込み、tmp ファイルを残さず、既存ファイルを正しく上書きする — 根拠: 実装挙動
- [ ] EV-36（正常 / must）: `task_result.schema.json` / `review_result.schema.json` は `status` の enum（success/partial/failed）と必須フィールド（files_changed/validation/risks または findings）を定義通りに要求する — 根拠: 設計 §9.3（Final output schema）

## 4. 類型別観点

### hook 型（主）

- [ ] EV-37（境界 / must）: stdin/stdout 契約 — `UserPromptSubmit` / `PreToolUse` hook は stdin JSON を読み、判定結果を exit code（0=allow / 2=block）で返す。Claude Code hook のような `hookSpecificOutput.additionalContext` 注入は行わない（Codex hooks は exit code ベースのプロトコルであり、Claude Code hook とはプロトコルが異なる） — 根拠: .claude/Plans.md Notes（2026-07-04, "stdin JSON、exit code 2 でブロック"）+ 実装挙動
- [ ] EV-38（正常 / must）: exit code 規約 — block は exit 2、allow は exit 0（UserPromptSubmit/PreToolUse）。Stop hook は常に exit 0（ブロック不可の設計） — 根拠: 上記 Notes + 設計 §5.7（Stop hook の用途は validation/report 生成でありブロックではない）
- [ ] EV-39（異常 / must）: fail-safe 方針 — `user_prompt_secret_scan.py` / `pre_tool_use_policy.py` は stdin パース失敗・必須フィールド欠如時に fail-open（exit 0）とする。`stop_validate.py` は検証失敗時も fail-open（常に exit 0、`systemMessage` のみで通知） — 根拠: 実装挙動（各 docstring に fail-open 明記）
- [ ] EV-40（境界 / should）: 冪等性 — hooks 自体は状態を持たない。`stop_validate.py` のみタイムスタンプ付きの `.codex/reports/validation-<timestamp>.log` を都度生成するため、再実行しても既存ログを上書き・二重注入しない（ログの累積は許容設計であり、クリーンアップは対象外） — 根拠: 実装挙動
- N/A: config 駆動 — 本パッケージの hooks は `cli-tools.yaml` の `enabled` フラグ経由のランタイムトグルを持たない。有効/無効の単位はパッケージ install/uninstall（`.codex/hooks.json` の配置有無）であり、`codex-suggestions` 等 Claude 側 hook パッケージが持つ `codex.enabled` ランタイム参照とは設計が異なる（design にも per-hook ランタイムトグルの記述はない）
- [ ] EV-41（境界 / should）: 秘匿情報 — `redact_secrets` は `metadata.json` / `final.json` / `review.json` / `report.md` に適用される一方、`events.jsonl` / `progress.log` は「ライブ subprocess ストリームであり意図的に redact 対象外」と `codex_run.py` 実装コメントで明示されている。`validation.log`（`run_validation` が書き込む検証コマンドの生出力）には `redact_secrets` が適用されていない — 根拠: 実装挙動（`codex_run.py` コメント）+ 設計 §4.6/§10.4。**要人間レビュー**: `validation.log` への `redact_secrets` 適用要否は明示的な設計判断ではなく、Issue 化候補
- [ ] EV-42（境界 / should）: 性能 — `stop_validate.py` は lint/test 実行を伴うため `hooks.json` の `Stop` timeout を 300 秒に設定し、長時間実行を前提としている（他の同期・軽量 hook とは異なる設計判断） — 根拠: packages/codex-harness/codex/hooks.json（`Stop` の `timeout: 300`）

### CLI ツール型（副）

- [ ] EV-43（正常 / must）: コマンド契約 — `codex_run.py` / `codex_review.py` の引数（`task` / `--project` / `--sandbox` / `--allow-untrusted-hooks` / `--timeout`, `--base`）と exit code（0=成功, 1=preflight失敗, 124=タイムアウト, それ以外=`codex exec` 準拠）が安定している — 根拠: 実装挙動
- [ ] EV-44（異常 / must）: 入力バリデーション — `--project` が git リポジトリ外、または必須 `.codex` ファイル欠如の場合にエラーメッセージと exit 1 を返す。壊れた `validation.json` / `events.jsonl` は例外を出さず空扱い・フォールバックする — 根拠: 実装挙動
- [ ] EV-45（正常 / must）: 破壊的操作の安全策 — hooks trust 検証が fail-closed であり、`--allow-untrusted-hooks` を明示しない限り改変された `.codex/hooks/` 配下での `codex exec` 実行（bypass フラグ付与）を許可しない — 根拠: 設計 §0
- [ ] EV-46（正常 / must）: 出力の安定性 — `final.json` / `review.json` は `task_result.schema.json` / `review_result.schema.json` の必須キー・enum に準拠する — 根拠: 設計 §9.3
- [ ] EV-47（境界 / must）: 設定レイヤリング — `config-harness.toml` のマージは add-if-missing（`default_permissions` / `[features].hooks`）と upsert（`[permissions.*]`）を明確に区別する（EV-08/EV-09 と同一観点の CLI ツール断面での確認） — 根拠: 設計 §5.5
- N/A: スキル型固有項目（対話規約・非対話完結性・フォールバック・ルーティング尊重・成果物規約） — 理由: 本パッケージはスキル指示書ではなく hook + CLI スクリプト配布パッケージであり、AskUserQuestion 等の対話フェーズを持たない

### 共通（全類型）

- [ ] EV-48（異常 / must）: 配布ライフサイクル — `uninstall()` 時に `codex_files`（`.codex/hooks/*.py`, `.codex/schemas/*.json`, `.codex/rules/codex-harness.rules`, `.codex/validation.json`, `.codex/hooks.json`）および `codex_file_hashes` 台帳エントリが削除されること — 根拠: docs/evaluation/README.md（配布ライフサイクル: install/update/uninstall を通じて壊れた中間状態を残さない）。**現状ギャップ**: `scripts/orchestra-manager.py` の `uninstall()` は `pkg.config` / `pkg.agents` のみ処理し、`codex_files` を扱っていない（実装未対応）
- [ ] EV-49（正常 / must）: 後方互換性 — `codex_file_hashes` 台帳（本パッケージ用、フラット構造）と `file_hashes` 台帳（agents/config 用、`pkg_name` ネスト構造）は別キーとして共存し、互いを破壊しない — 根拠: 実装挙動（`sync_engine.py` の docstring でも別台帳と明記）
- [ ] EV-50（境界 / should）: 生成物の同期 — `config-harness.toml` はソースであり、`.codex/config.toml` への反映は必ず `apply_codex_harness_config()` のマージ経由でのみ行われる（直接コピーしない） — 根拠: packages/codex-harness/codex/config-harness.toml 冒頭コメント（"This file is never copied verbatim into a project"）
- [ ] EV-51（境界 / must）: ドキュメント整合 — README.md に `packages/codex-harness` の記載が追加され、実際の配布物（manifest の `codex_files` 8 件、`scripts` 2 件）と一致していること — 根拠: CLAUDE.md 変更ガードレール（仕様変更時は README.md と必要なテストを同時更新する）。**現状ギャップ**: 本評価セット作成時点で README.md に `codex-harness` の記載なし（.claude/Plans.md Phase 5 TODO「README / CHANGELOG（Unreleased）更新」が未完了）
- [ ] EV-52（正常 / must）: rules 層の decision 分類 — `codex-harness.rules` は `git push` / `gh pr create` を `decision="prompt"`（人間承認付き許可）とし、`gh pr merge` / `gh release create` / `npm publish` / `pnpm publish` / `docker push` / `kubectl apply` / `terraform apply` / `rm -rf` 各種を `decision="forbidden"`（承認不可のハード禁止）とする — 根拠: 設計 §5.6 / §10.3 + Issue #161 フォローアップ（対話は承認ベース、公開/破壊系は禁止維持）
- [ ] EV-53（境界 / must）: 非対話 runner の承認固定 — `codex_run.py` / `codex_review.py` は `codex exec` に `-c approval_policy=never` を付与し、対話向け既定（`config.toml` の `approval_policy="on-failure"`）に依存せず、承認エスカレーションを行わない厳格 sandbox 動作を維持する — 根拠: 実装挙動（runner は非対話でプロンプト不可）+ 設計 §5.2

## 5. テストレビュー判断基準（パッケージ固有）

- EV-41（validation.log の redact 対象外）・EV-48（uninstall 未対応）・EV-51（README 未更新）は現時点で「あるべき仕様」に対する実装ギャップまたは未確認事項である。これらを「現状の実装が正しい」と追認するテストを書かず、対応方針（Issue 化 or 実装修正 or 設計として許容）が決まるまでテスト未整備のまま明示的に残すこと
- EV-37/EV-38（hooks の exit code プロトコル）は Claude Code hook の `hookSpecificOutput.additionalContext` パターンと混同しやすい。テストが Codex hooks 特有の「exit code のみで判定し JSON 注入をしない」契約を正しく区別しているか確認する（`.claude/rules/` 配下の Claude 向け hook 契約をそのまま流用していないか）
- `FORBIDDEN_PATTERNS`（EV-16）・`SECRET_PATTERNS`（EV-13）の具体的な正規表現・最小長閾値（`MIN_TOKEN_LENGTH` 等）は実装のみが根拠。閾値や文字列リストそのものを固定的な仕様として厳密比較するテストは、リスト変更のたびに壊れる「実装追認」になっていないか確認し、変更が意図的な仕様変更かどうかのレビューを優先する
- `verify_hooks_trust`（EV-22/EV-23）は SHA-256 比較・symlink 拒否・path traversal 拒否という fail-closed 設計の中核であるため、各失敗理由（ハッシュ不一致／ファイル欠如／symlink／台帳なし）が独立したテストケースとして検証されているかを重点確認する（複合ケースで one-off に丸めていないか）
- `parse_events`（EV-34）のフォールバック処理は「Codex の JSONL イベントスキーマは将来変化しうる」という設計判断（設計 §16.3）に基づく。未知イベント形状を許容するテストが「たまたま今のイベント形状で通っている」だけになっていないか、意図的に未知キーを含むイベント行のテストケースがあるか確認する
- `codex_run.py` / `codex_review.py` の `main()` エンドツーエンドテストは `execute_codex` / `execute_codex_review`（実際の `codex exec` 呼び出し）をモックしている。モックが「非対話・stdin=DEVNULL・sandbox 指定」等の呼び出し契約（EV-29, EV-31）まで検証せず、後続処理（artifact 生成）だけを検証する空洞化になっていないか確認する
