# core 評価セット

**パッケージ**: `packages/core`
**類型**: 主: hook 型、副: 共通ライブラリ
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-04（precompact-dump / log_common / handoff の未文書化を「文書化すべき」と裁定し Issue #130 へ。Non-Goals の failure_detector 責務境界は今回対象外・現配置のまま）
**情報源**: docs/reference/packages.md（core セクション）, docs/design/architecture.md（4.3 / 5 / 9 章）, .claude/rules/task-memory-usage.md, .claude/rules/context-sharing.md
**補助参照（構成要素の列挙のみ）**: packages/core/manifest.json, packages/core/hooks/ 配下のファイル名・docstring 冒頭

## 1. 責務定義

core は全パッケージが依存する共通基盤であり、(1) `Plans.md` によるタスク状態管理（状態マーカーの解析・自動アーカイブ・サマリー注入）、(2) plan gate によるサブエージェント実行フロー制御、(3) `.claude/context/` を介した CLI 間・サブエージェント間のコンテキスト共有（作業ファイル・前回結果の集約と注入）、(4) 全 hook が共有する設定読み込み・JSON I/O・ログ出力ユーティリティ（`hook_common.py` / `log_common.py` / `context_store.py`）を提供する。他パッケージに依存せず、他の全パッケージから依存される最下層のパッケージである。

### Non-Goals

- エージェントルーティングの判断ロジック（`agent-routing` パッケージの責務）
- lint/format の自動実行やテスト品質ゲート判定（`quality-gates` パッケージの責務）
- 失敗イベントの記録・集計そのもの（`fail-logs` パッケージの責務。ただし検知ロジック `failure_detector.py` は物理的に `packages/core/hooks/` に配置されている — 責務境界は情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）
- Codex/Antigravity 呼び出し要否の提案（`codex-suggestions` / `antigravity-suggestions` パッケージの責務）
- MCP プロキシのライフサイクル管理（`cocoindex` パッケージの責務）

## 2. 期待する入出力・副作用

| 構成要素                                                                          | 入力                                                 | 期待する出力                                                                                             | 副作用                                                                                                                                                   |
| --------------------------------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `load-task-state.py`（SessionStart）                                              | SessionStart イベント JSON、`.claude/Plans.md`       | WIP タスク一覧 / 次の TODO / blocked タスク一覧のサマリーをコンテキストに注入                            | 全フェーズ `cc:done` のプロジェクトを `.claude/Plans.archive.md` に日付付きで追記し `Plans.md` から除去。`.claude/context/`（session/, shared/）を初期化 |
| `set-plan-gate.py`（PostToolUse: Agent\|Task）                                    | PostToolUse イベント JSON                            | —                                                                                                        | プランゲート状態を設定                                                                                                                                   |
| `check-plan-gate.py`（PreToolUse: Agent\|Task）                                   | PreToolUse イベント JSON                             | ゲート pending 時: exit code 2 でツール呼び出しをブロック                                                | 実装系エージェント呼び出しの中断（core hook 群で唯一 fail-open ではなく意図的にブロックする）                                                            |
| `clear-plan-gate.py`（UserPromptSubmit）                                          | UserPromptSubmit イベント JSON                       | —                                                                                                        | プランゲート状態を解除                                                                                                                                   |
| `inject-shared-context.py`（PreToolUse: Agent\|Task）                             | PreToolUse イベント JSON（`tool_input.prompt` 含む） | 直近のサブエージェント結果＋working-context を `[Shared Context]` 形式で prompt 末尾に付加               | なし（読み取りのみ）                                                                                                                                     |
| `capture-task-result.py`（PostToolUse: Agent\|Task）                              | PostToolUse イベント JSON（`tool_response` 含む）    | —                                                                                                        | 結果サマリー（先頭 2000 文字）を `session/entries/{agent_id}_{timestamp}.json` に書き出し                                                                |
| `update-working-context.py`（PostToolUse: Edit\|Write）                           | PostToolUse イベント JSON（`file_path` 含む）        | —                                                                                                        | 変更ファイルパスを `working-context.json` の modified_files に追記（`.claude/` 配下は除外）                                                              |
| `cleanup-session-context.py`（SessionEnd）                                        | SessionEnd イベント JSON                             | —                                                                                                        | `session/` ディレクトリと `working-context.json` を削除                                                                                                  |
| `precompact-dump.py`（PreCompact）                                                | manifest.json にのみ記載                             | 未文書化（2026-07-04 裁定: 文書化すべき）                                                                | 未文書化 → packages.md / architecture.md へ文書化する（Issue #130）                                                                                      |
| `hook_common.py`（util）                                                          | 呼び出し元 hook からの設定名・パッケージ名           | base 設定と `*.local.yaml` を deep_merge した dict                                                       | なし                                                                                                                                                     |
| `context_store.py`（util）                                                        | session / shared エントリーの読み書きリクエスト      | エントリー一覧・working-context dict                                                                     | `.claude/context/` 配下のファイル読み書き（fcntl ファイルロック付き、architecture.md 4.3）                                                               |
| `log_common.py`（util）                                                           | イベント種別・メタ情報                               | —                                                                                                        | 統一イベントログへの書き出し（詳細フォーマット未文書化 → 2026-07-04 裁定: 文書化すべき・Issue #130）                                                     |
| `task-memory.yaml`（config）                                                      | —                                                    | `Plans.md` のパス・マーカー定義                                                                          | なし                                                                                                                                                     |
| `preflight` / `startproject` / `checkpointing` / `task-state` / `design`（skill） | ユーザー対話                                         | packages.md 記載の一行責務（計画策定/新規開発協調/セッション保存復元/Plans.md作成更新/要件設計文書作成） | 詳細フローは情報源に記載なく本評価セットの対象外（スキル型チェックリストは別評価セットで扱う）                                                           |
| `handoff`（skill、manifest.json のみ記載）                                        | —                                                    | 未文書化（2026-07-04 裁定: 文書化すべき）                                                                | 未文書化 → 文書化する（Issue #130）                                                                                                                      |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `load-task-state.py` が SessionStart 時に `Plans.md` の状態マーカー（`cc:TODO`/`cc:WIP`/`cc:done`/`cc:blocked`）を解析し、WIP タスク一覧・次の TODO タスク・blocked タスク一覧をコンテキストに注入する — 根拠: task-memory-usage.md
- [ ] EV-02（正常 / must）: 全フェーズが `cc:done` のプロジェクトは `Plans.archive.md` に日付付きで追記され、`Plans.md` から該当プロジェクトセクション（区切り線含む）が除去される — 根拠: task-memory-usage.md
- [ ] EV-03（境界 / should）: Decisions / Notes セクションは**全プロジェクトが完了した場合のみ**アーカイブへ移動し、一部プロジェクトのみ完了時は `Plans.md` に残存する — 根拠: task-memory-usage.md
- [ ] EV-04（正常 / should）: `cc:blocked` マーカーには `— 理由: {ブロック理由}` の付記があるものとして解析・表示される — 根拠: task-memory-usage.md
- [ ] EV-05（正常 / must）: サブエージェント（Agent/Task）完了の PostToolUse でプランゲートが設定される — 根拠: docs/reference/packages.md, architecture.md 5.1
- [ ] EV-06（異常 / must）: プランゲートが pending の状態で実装系エージェントが呼び出された場合、`check-plan-gate.py` が exit code 2 でツール呼び出しをブロックする — 根拠: architecture.md 5.1 / 5.2
- [ ] EV-07（正常 / must）: UserPromptSubmit（ユーザーの次メッセージ送信）でプランゲートが解除される — 根拠: docs/reference/packages.md, architecture.md 5.1
- [ ] EV-08（正常 / must）: SessionStart 時に `.claude/context/`（`session/`, `shared/`）が初期化される — 根拠: context-sharing.md
- [ ] EV-09（正常 / must）: サブエージェント（Agent/Task）起動前に、直近のサブエージェント結果と working-context が `[Shared Context]`（`## Previous Agent Results` + `## Working Context`）形式で prompt 末尾に注入される — 根拠: context-sharing.md
- [ ] EV-10（境界 / should）: 注入されるサブエージェント結果エントリーは最新 5 件までに制限され、各エントリーの summary は 200 文字にトランケートされる — 根拠: context-sharing.md
- [ ] EV-11（境界 / should）: 注入される modified_files は最新 20 件までに制限される — 根拠: context-sharing.md
- [ ] EV-12（正常 / must）: サブエージェント完了後、結果サマリー（`tool_response` 先頭 2000 文字）が `session/entries/{agent_id}_{timestamp}.json` として書き出される — 根拠: architecture.md 9.2
- [ ] EV-13（正常 / must）: Edit/Write 完了後、変更ファイルパスが `working-context.json` の modified_files に追記される — 根拠: context-sharing.md, architecture.md 9.2
- [ ] EV-14（境界 / must）: `.claude/` 配下のファイル変更は `working-context.json` の modified_files に記録されない — 根拠: context-sharing.md
- [ ] EV-15（正常 / must）: SessionEnd 時に `session/` ディレクトリと `working-context.json` が削除され、セッション間で状態を持ち越さない — 根拠: context-sharing.md

## 4. 類型別観点

<!-- docs/evaluation/README.md の hook 型チェックリストを core の実情で具体化する -->

- [ ] EV-16（境界 / must）: stdin/stdout 契約 — 各 hook は Claude Code の hook イベント JSON を stdin から受け取り、コンテキスト注入結果は `hookSpecificOutput.additionalContext` 形式で返す（Claude Code hook 仕様に準拠） — 根拠: architecture.md 5.2
- [ ] EV-17（異常 / must）: fail-safe 方針 — `check-plan-gate.py` を除く全 core hook は内部例外発生時に exit code 0 で正常終了しセッション/ツール実行をブロックしない（fail-open）。`check-plan-gate.py` のみ意図的に exit code 2 でブロック可能な設計上の例外である — 根拠: architecture.md 5.2
- [ ] EV-18（境界 / should）: 冪等性 — 全フェーズ完了プロジェクトの自動アーカイブは、初回実行で `Plans.md` から該当セクションが除去済みのため、同一セッション内で再実行しても二重アーカイブされない（構造的冪等性） — 根拠: task-memory-usage.md
- [ ] EV-19（正常 / must）: config 駆動 — `hook_common.load_package_config()` は `{package}/{name}.yaml`（base）と `{name}.local.yaml`（local）を deep_merge し、local の値が base を上書きする — 根拠: architecture.md 4.3
- [ ] EV-20（境界 / should）: config 駆動（部分上書き） — base 設定にのみ存在するキーは local が存在してもそのまま base の値が使われる（local はキー単位の上書きであり全置換ではない） — 根拠: architecture.md 4.3
- N/A: 秘匿情報（マスキング） — core が扱う注入対象（ファイルパス・タスクサマリー・エントリー summary）に対する秘密情報マスキング処理は情報源に記載がなく、該当する取り扱い自体が定義されていないため対象外
- N/A: 性能 — 同期 hook（特に SessionStart の `load-task-state.py`）がセッション開始を遅延させないための定量的な性能要件が packages.md / architecture.md / task-memory-usage.md / context-sharing.md のいずれにも記載がないため、本評価セットでは具体化しない

## 5. テストレビュー判断基準（パッケージ固有）

- Plans.md の自動アーカイブに関するテストは「一部プロジェクトのみ完了」（EV-03 の境界）と「全プロジェクト完了」の両ケースを分けて検証しているか確認する。単一ケースのみのテストは gap として扱う
- `check-plan-gate.py` の exit code（0 か 2 か）が明示的にアサートされているか確認する。fail-open 原則からの逸脱は critical 相当のバグとみなす
- `context_store.py` は fcntl ファイルロック付きと明記されている（architecture.md 4.3）が、ロック競合時の具体的挙動（待機/エラー等）は情報源に記載がない。ロック競合ケースをテストする場合、期待値が実装追認になっていないか重点確認する
- 注入・保存時のトランケーション境界値（5 件目/6 件目、200 文字/201 文字、2000 文字/2001 文字、20 件目/21 件目）をテストしているか確認する。境界値を跨がないテストは EV-10/EV-11/EV-12/EV-14 の観点をカバーしたとみなさない
