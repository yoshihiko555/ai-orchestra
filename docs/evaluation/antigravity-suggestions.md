# antigravity-suggestions 評価セット

**パッケージ**: `packages/antigravity-suggestions`
**類型**: hook 型
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-04（両キー競合時は Antigravity 優先を EV-13 で確定。実装ギャップは Issue #125 で解消済み — `normalize_cli_tools_config` が antigravity.enabled 明示設定を優先するよう修正、EV-13/EV-10 テスト追加済み。PR #220 レビュー指摘への追加対応: base/local を merge してから正規化すると base の既定値 antigravity.enabled: true のせいで移行済みプロジェクトの local 限定 gemini.enabled: false フォールバックが機能しない regression が判明したため、base/local をレイヤーごとに正規化してから merge する `load_cli_tools_config`（packages/core/hooks/hook_common.py）に置き換え済み）
**仕様更新**: 2026-10-28（ユーザーによる A 案・追加レビュー対応承認、[ADR-060](../adr/ADR-20261028-060.md)）。任意助言・調査意図による発火・通知抑制を更新する。新しい期待値は未検証のためチェックを付けない。
**情報源**: docs/reference/packages.md（antigravity-suggestions セクション）, docs/reference/hooks.md, facets/instructions/antigravity-suggestion-compliance.md, facets/instructions/antigravity-delegation.md（補助・後方互換の根拠のみ）, packages/antigravity-suggestions/manifest.json（構成要素列挙のみ）, packages/antigravity-suggestions/hooks/suggest-antigravity-research.py（構成要素列挙のみ、期待値導出には未使用）

## 1. 責務定義

WebSearch の `query` / WebFetch の `prompt` に比較・選定・移行等の調査意図がある場合に、Antigravity CLI（`agy`）でのリサーチを任意提案する PreToolUse hook。`[Antigravity Suggestion]` は実行をブロックせず、オーケストレーターにも操作の保留・追加相談・採否の毎回の説明を義務付けない。相談済み・軽微・適用外ならそのまま続行できる。一方、`[Resolved Routing]` / `agents.<name>.tool` の明示 routing とスキル固有の委譲義務は維持する。

### Non-Goals

- Antigravity CLI（`agy`）自体の呼び出し実行（それはサブエージェント/オーケストレーターの責務であり、hook はテキスト提案を出すのみ）
- WebSearch/WebFetch 以外のツール呼び出しへの提案（matcher が `WebSearch|WebFetch` に限定）
- 提案による強制ブロック・保留・追加相談（助言の採否は任意）
- 意味的な相談済み判断（オーケストレーターの責務。通知済みは相談済みではなく、hook は transcript を解析しない）

## 2. 期待する入出力・副作用

config・対象範囲・marker に使う project / worktree は、環境変数 `CLAUDE_PROJECT_DIR` が非空ならそのルートを使い、未設定または空の場合だけ hook 入力の `cwd` で解決する。

| 構成要素                                  | 入力                                                                       | 期待する出力                                                                            | 副作用                                             |
| ----------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `suggest-antigravity-research.py`（hook） | PreToolUse の stdin JSON（`tool_name` が `WebSearch` / `WebFetch`、`tool_input.query` / `prompt`, `cwd`, `session_id`, トップレベルの `agent_id`）        | `[Antigravity Suggestion]` を含む提案テキスト（追加コンテキストとして注入、非ブロック） | `session_id` があれば hash 化した空の通知 marker のみ保存 |
| `antigravity-system`（skill）             | ユーザー/エージェントからの Antigravity CLI 利用に関する自然言語リクエスト | Antigravity CLI 利用ガイダンス                                                          | なし                                               |

## 3. 評価観点

- [ ] EV-01（正常 / must）: WebSearch の `tool_input.query` に比較・選定・移行等の調査意図がある場合、config・通知抑制条件を適用して `[Antigravity Suggestion]` を出力する。明確な選択比較（`vs` / `vs.` / `versus` / `違い` / `どっち` / `which is better`）も対象とする — 根拠: ADR-060、docs/reference/packages.md
- [ ] EV-02（正常 / must）: WebFetch の `tool_input.prompt` に EV-01 と同じ調査意図・明確な選択比較がある場合、config・通知抑制条件を適用して `[Antigravity Suggestion]` を出力する。URL だけでは発火しない — 根拠: ADR-060、docs/reference/packages.md
- [x] EV-03（異常 / must）: `antigravity.enabled: false` のとき、hook 自体が提案を抑制する（発火しない） — 根拠: .claude/rules/antigravity-suggestion-compliance.md
- [x] EV-04（異常 / should）: `.local.yaml` に旧 `gemini.enabled: false` が残っており、かつ `antigravity.enabled` が明示設定されていない場合は、後方互換フォールバックとして有効な無効化設定と尊重され提案が抑制される — 根拠: .claude/rules/antigravity-delegation.md（両キー競合時の優先は EV-13）
- [ ] EV-05（正常 / must）: `[Antigravity Suggestion]` は任意助言であり、WebSearch/WebFetch の保留・追加相談・採否の毎回の説明を義務付けない — 根拠: ADR-060、facets/instructions/antigravity-suggestion-compliance.md（hook 単体テスト対象外。統合テスト/レビューで確認、5節参照）
- [ ] EV-06（正常 / must）: 相談を選ぶ場合は既存の config 解決（`[Resolved Routing]` があれば尊重、なければ base + `.local.yaml`）、model allowlist・sandbox・非対話実行・リトライ/フォールバック条件に従う。明示 routing とスキル固有の委譲義務は助言の採否によらず必須 — 根拠: ADR-060、facets/instructions/antigravity-suggestion-compliance.md（hook 単体テスト対象外）
- [ ] EV-07（正常 / should）: 任意相談した場合は結果を踏まえて調査を続行する。相談済み・軽微・適用外の場合を含め、追加相談なしでも続行できる — 根拠: ADR-060、facets/instructions/antigravity-suggestion-compliance.md（hook 単体テスト対象外）
- [ ] EV-08（境界 / should）: ユーザーが明示的にスキップを指示した場合、Antigravity 相談をスキップしてよい — 根拠: .claude/rules/antigravity-suggestion-compliance.md（hook 単体テスト対象外。手動チェックリスト/統合テストで確認、5節参照）
- [ ] EV-09（境界 / should）: 同一セッション内で同じトピックについて既に Antigravity 相談済みの場合、再度の相談をスキップしてよい — 根拠: .claude/rules/antigravity-suggestion-compliance.md（hook 単体テスト対象外。手動チェックリスト/統合テストで確認、5節参照）
- [x] EV-10（境界 / must）: hook の matcher は `WebSearch|WebFetch` に限定され、それ以外のツール呼び出し（Read/Bash/Edit 等）では発火しない — 根拠: packages/antigravity-suggestions/manifest.json, docs/reference/packages.md

- [ ] EV-14（境界 / must）: `query` / `prompt` に調査意図がない場合は、URL・文字数・`documentation` / `library` / `guide` / `example` / `best practice` 等の一般語だけで発火しない。文字列等の演算比較や単純な migration エラー検索も除外するが、EV-01 の明確な選択比較が同居すればそちらを優先する — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-15（境界 / must）: トップレベルの `agent_id` が非空の文字列（`str`）の場合だけ、サブエージェント／インプロセス teammate 内として提案を抑制する。`agent_id` の欠落・空文字列・非文字列や、`--agent` メインにも付く `agent_type` 単独では抑制しない。`tool_input.subagent_type` は呼出先であり判定に使わない — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-16（境界 / must）: `session_id` があれば、同じ project / worktree 内の同一 session・正規化した同一質問に 1 度だけ通知する。別 session / 質問や別 project / worktree とは状態を混同しない。意味的な相談済みはオーケストレーターが判断し、hook は transcript を解析しない — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-17（異常 / must）: 状態書き込み失敗時は無出力・exit 0。`session_id` がない場合は state を作らず、その呼び出しごとの助言とする — 根拠: ADR-060、docs/reference/hooks.md
- [ ] EV-18（境界 / must）: 各 project / worktree の `.claude/state/suggestions/` に保存するのは、session / 対象を hash 化した空 marker のみ。質問本文・URL・生の session ID は保存しない。marker の自動削除は今回追加せず、終了後も session ディレクトリが残る。cleanup は将来課題とする — 根拠: ADR-060、docs/reference/hooks.md

## 4. 類型別観点

- [ ] EV-11（正常 / must）: hook は WebSearch/WebFetch の実行自体をブロックせず、`[Antigravity Suggestion]` を含む追加コンテキストを注入する形で任意提案する。hook の非ブロックだけでなく、オーケストレーターにも保留・追加相談を義務付けない（EV-05）— 根拠: ADR-060、docs/reference/packages.md
- [x] EV-12（境界 / should）: EV-11 の非ブロック方針の帰結として、hook は正常系の exit code（0 等の非ブロックコード）で終了し WebSearch/WebFetch 自体の実行を妨げない — 根拠: 同上（ドキュメントからの論理的導出）
- [x] EV-13（異常 / must）: config 駆動 - `antigravity.enabled` と旧 `gemini.enabled` が同時に設定され値が矛盾する場合、`antigravity.enabled` を優先する（新キーが正。旧 `gemini.enabled` は `antigravity` 未設定時のみ後方互換フォールバックとして作用する） — 根拠: 2026-07-04 人間レビュー裁定。`normalize_cli_tools_config`（packages/core/hooks/hook_common.py）を修正済み（Issue #125）
- （config 駆動）EV-03 / EV-04 / EV-13 で担保
- fail-safe: 状態書き込み失敗時は EV-17 で確認する
- 冪等性: 通知の重複抑制は EV-16 / EV-17 で確認する。EV-09 の意味的な相談済み判断とは区別する
- 秘匿情報: 状態に本文等を残さないことは EV-18 で確認する
- N/A: 性能（同期 hook が WebSearch/WebFetch 実行を著しく遅延させないか） — ドキュメントに記載なし

## 5. テストレビュー判断基準（パッケージ固有）

- EV-05〜EV-09 は「hook の出力」ではなく「hook 出力を受けたオーケストレーターの振る舞い」を規定する観点である。hook 単体のユニットテストでは検証できないため、統合テストまたは手順ドキュメントとの突合で確認する。任意助言への保留・追加相談を強制する旧期待値は固定化しない。
- EV-14〜EV-18 は hook と共通 helper のテストで確認する。調査意図・明確な選択比較と一般語・URL・長さだけの区別、演算比較・単純な migration エラー検索の除外と選択比較の優先、config・対象範囲・marker での非空の `CLAUDE_PROJECT_DIR` 優先と、未設定または空の場合だけ hook 入力の `cwd` へのフォールバック、非空文字列の `agent_id` によるサブエージェント／インプロセス teammate の識別、`agent_id` の欠落・空文字列・非文字列や `agent_type` 単独・呼出先の `subagent_type` では抑制しないこと、正規化した質問の重複、session / project / worktree の分離、空 marker のみの保存、状態書き込み失敗・session 欠落を含める。
- EV-03 / EV-04 / EV-13 の抑制テストは、両キー競合時に `antigravity.enabled` を優先する（EV-13, 2026-07-04 裁定）ことを期待値とする。`normalize_cli_tools_config` は Issue #125 でこの優先順位に修正済み（`tests/unit/test_agent_routing_consistency.py::TestLegacyGeminiCompat`, `tests/unit/test_hook_common.py::TestLoadCliToolsConfig` で検証）。
- EV-04 の「移行済みプロジェクト」ケース（base が antigravity.enabled を明示し、local に旧 gemini.enabled: false のみが残る）は、base/local を merge してから正規化すると base の既定値に紛れて誤判定されるため、`load_cli_tools_config`（base/local を個別に正規化してから merge）で検証する（`tests/unit/test_hook_common.py::TestLoadCliToolsConfig::test_migrated_project_legacy_gemini_disables_antigravity` で検証。PR #220 レビュー指摘対応）。
- EV-10 は関数レベルでは `packages/antigravity-suggestions/tests/test_suggest_antigravity_research.py` の manifest matcher 値検証 + 非対象 tool_name での非発火検証でカバーする（実際の matcher ディスパッチは Claude Code 本体の責務であり hook 内では再現できないため、defense-in-depth の確認に留まる）。
- 次回監査では researcher の指定 CLI 試行率（指定 CLI の実行を要する researcher タスクに対する実際の試行）と、メインの任意提案の採用率（通知された助言に対する任意相談）を分離する。サブエージェント内の通知は `agent_id` で抑制し、researcher の `agy` 実行は `researcher.md` の指示が担うため、hook 通知数を researcher の遵守率の代用にしない。
