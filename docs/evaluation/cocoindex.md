# cocoindex 評価セット

**パッケージ**: `packages/cocoindex`
**類型**: hook 型（MCP サーバー設定のプロビジョニング）
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-04（proxy warmup 挙動・uninstall クリーンアップ仕様の明文化を要と裁定し Issue #127 で定義。他観点は指摘なし）
**情報源**: `.claude/rules/cocoindex-usage.md`, `docs/reference/packages.md`（cocoindex セクション）, `docs/design/distribution-sync-flow.md`（config 同期の一般ルール部分）。補助参照: `packages/cocoindex/manifest.json`, `packages/cocoindex/hooks/*.py`（ファイル名・docstring 冒頭のみ、構成要素列挙用）

## 1. 責務定義

cocoindex パッケージは、cocoindex-code MCP サーバーの接続設定を Claude Code（`.mcp.json`）/ Codex CLI（`.codex/config.toml`）/ Antigravity CLI agy（`.gemini/settings.json`）の 3 CLI に対して自動的にプロビジョニングし、`enabled` フラグや `targets` 設定の変更に追従してエントリを reconcile（追加・更新・削除）する。proxy モード（v2, opt-in）が有効な場合は、SessionStart で mcp-proxy をセッションをまたいで永続化するように起動し、SessionEnd では停止せず次セッションでの再利用を可能にすることで、v1（stdio）モードで起こり得る複数 CLI 間の SQLite ロック競合を回避する経路を提供する。旧設定キー `targets.gemini` からの移行時も、既存の `.local.yaml` 上書きを壊さず `targets.antigravity` として読み替えて尊重する。

### Non-Goals

- v1（stdio）モードにおける複数 CLI 同時起動時の SQLite ロック競合の完全自動解消（回避策の提供に留まり、根本解決は proxy モード（v2）に委ねる）
- cocoindex-code MCP サーバー自体の検索・インデックス機能の実装（本パッケージは設定のプロビジョニングのみを担当する）
- `uvx` / `cocoindex` パッケージ本体のバージョン管理ポリシー策定（バージョン固定はユーザーが `.local.yaml` で明示的に行う運用に委ねる）

## 2. 期待する入出力・副作用

| 構成要素                                            | 入力                                                            | 期待する出力                                                                                     | 副作用                                                                                                                                      |
| --------------------------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `provision-mcp-servers.py`（SessionStart）          | hook 入力 JSON（cwd 等）＋ `cocoindex.yaml`（＋ `.local.yaml`） | 各 CLI の MCP エントリが `enabled` / `targets` 設定と一致した状態になる（reconcile）             | `.mcp.json` / `.codex/config.toml` / `.gemini/settings.json` への書き込み。`proxy.enabled: true` 時はバックグラウンドで proxy warmup を開始 |
| `notify-proxy-reconnect.py`（UserPromptSubmit）     | hook 入力 JSON ＋ proxy session state                           | proxy が ready/idle になった後、1 セッションにつき 1 回だけ reconnect を促す `additionalContext` | session state への通知済みフラグ書き込み                                                                                                    |
| `stop-mcp-proxy.py`（SessionEnd）                   | hook 入力 JSON                                                  | ログ出力のみ（proxy プロセスは停止しない）                                                       | session state のクリア                                                                                                                      |
| `cocoindex.yaml` / `cocoindex.local.yaml`（config） | —                                                               | `enabled` / server 定義 / `targets`（claude, codex, antigravity）/ `proxy` 設定のソース          | なし（設定ソースのみ、直接の副作用はない）                                                                                                  |

## 3. 評価観点

- [ ] EV-01（正常 / must）: Claude Code 向けに `.mcp.json` の `mcpServers` キーへ cocoindex-code サーバー定義を書き込む — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-02（正常 / must）: Codex CLI 向けに `.codex/config.toml` の `[mcp_servers.{name}]` セクションへ書き込む — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-03（正常 / must）: Antigravity CLI（agy）向けに `.gemini/settings.json` の `mcpServers` キーへ書き込む（agy は Gemini CLI と同じ設定ファイルを継続利用する仕様） — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-04（異常 / must）: `cocoindex.enabled: false`（トップレベル）を設定すると、3 CLI すべての設定ファイルから cocoindex-code のエントリが自動削除される（クリーンアップモード） — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-05（異常 / must）: `targets.<cli>.enabled: false` を設定した CLI のみエントリが削除され、他の CLI の設定ファイルは変更されない — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-06（境界 / must）: `.local.yaml` に旧キー `targets.gemini.enabled: false` が残存している場合でも `targets.antigravity` の設定として読み替えられ、有効な上書きとして尊重される — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-07（正常 / should）: `.claude/config/cocoindex/cocoindex.local.yaml` で `args`（例: cocoindex / cocoindex-code のバージョン固定）を上書きできる — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-08（正常 / must）: `proxy.enabled: true` のとき、SessionStart で `start_proxy()` が冪等に呼び出され、既に起動済みならスキップする — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-09（正常 / must）: `proxy.enabled: true` のとき、SessionEnd では proxy プロセスを停止せず、次セッションで再利用するために起動状態を維持する — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-10（正常 / should）: proxy の手動停止は `orchestra-manager.py proxy stop --project .` の実行によってのみ行われる（hook からは停止されない） — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-11（境界 / must）: proxy 未起動状態からの初回セッションでは MCP 接続が確立されず、ユーザーが `/mcp` で手動リコネクトするまで cocoindex-code ツールが利用できない — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-12（正常 / should）: 2 回目以降のセッションでは、永続化された proxy に対して自動的に MCP 接続が確立される — 根拠: `.claude/rules/cocoindex-usage.md`
- [ ] EV-13（正常 / should）: `notify-proxy-reconnect.py` は proxy が ready/idle になった後、1 セッションにつき 1 回だけ reconnect を促す（同一セッション内で繰り返し通知しない） — 根拠: `docs/reference/packages.md`
- [ ] EV-14（境界 / should）: v1（stdio）モードで複数 CLI が同一プロジェクトの MCP サーバーを同時起動すると SQLite ロック競合が発生し得る（本パッケージが自動解消することは保証しない既知の制限） — 根拠: `.claude/rules/cocoindex-usage.md`

## 4. 類型別観点

- [ ] EV-15（異常 / must）: hook（`provision-mcp-servers.py` / `notify-proxy-reconnect.py` / `stop-mcp-proxy.py`）内部で例外が発生した場合も exit code 0 で終了し、Claude Code のセッション進行をブロックしない（fail-open） — 根拠: 実装挙動（`packages/core/hooks/hook_common.py` の `safe_hook_execution`。各 cocoindex hook がこのデコレータを使用している）
- N/A: stdin/stdout 契約 — cocoindex 固有の追加スキーマは一次情報源に定義がなく、Claude Code 標準 hook 入力（cwd 等）と `hook_common` 共通ユーティリティに従うのみのため、パッケージ固有の観点として追加すべき仕様が確認できない
- N/A: 冪等性 — 「現在の状態と一致していれば書き込みをスキップする」という記述は hook docstring にのみあり、一次情報源（`cocoindex-usage.md`）に明記がないため期待仕様として確定できない（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）
- config 駆動 → EV-04 / EV-05 / EV-06 / EV-07 で担保（enabled フラグ・`*.local.yaml` 上書きの尊重。新規 ID は起こさずクロス参照）
- N/A: 秘匿情報 — `cocoindex.yaml` が扱うのはサーバー起動コマンド・バージョン指定・ポート番号のみで、API キー等の秘密情報を含まない（`.claude/rules/cocoindex-usage.md` のバージョン固定例より）ため、マスキング観点の対象外と考えられる
- [ ] EV-18（正常 / should）: `provision-mcp-servers.py` は proxy 未 ready のとき `start_proxy_background()` を呼び出すが、これは helper プロセスを `subprocess.Popen(..., start_new_session=True)` で起動する非同期（fire-and-forget）処理であり、proxy warmup の完了を同期的に待たない（SessionStart hook は warmup 完了を待たずに終了する） — 根拠: `.claude/rules/cocoindex-usage.md`「warmup は非同期（バックグラウンド）実行」（仕様確定 2026-07-15, Issue #127）
- [ ] EV-16（正常 / must）: sync 実行時、`cocoindex.local.yaml` のようなプロジェクト固有上書きファイルは同期・削除の対象外として保持される（`*.local.yaml` は絶対に削除しない） — 根拠: `docs/design/distribution-sync-flow.md`
- [ ] EV-17（正常 / should）: `docs/reference/packages.md` に記載された hook 一覧（`provision-mcp-servers.py` / `notify-proxy-reconnect.py` / `stop-mcp-proxy.py`）と `packages/cocoindex/manifest.json` の `hooks` 定義が一致する — 根拠: `docs/reference/packages.md` ＋ `manifest.json`（構成要素の列挙としての整合確認）
- [ ] EV-19（正常 / should）: `orchestra-manager.py uninstall cocoindex` は cocoindex 固有の状態（各 CLI 設定ファイルへ書き込み済みの MCP エントリ・起動中の mcp-proxy プロセス・`.claude/state/cocoindex-sessions/` のセッション state）をクリーンアップの対象外として扱う（意図的な仕様。完全なクリーンアップは手動手順に従う） — 根拠: `.claude/rules/cocoindex-usage.md`「uninstall 時のクリーンアップ」（仕様確定 2026-07-15, Issue #127）

## 5. テストレビュー判断基準（パッケージ固有）

- 3 CLI（Claude Code / Codex / Antigravity）それぞれについて、書き込み形式（JSON の `mcpServers` / TOML の `[mcp_servers.*]` / JSON の `mcpServers`）を個別に検証しているか。1 テストで 3 CLI の検証を混在させて差異を見落としていないか
- `enabled: false` のクリーンアップ検証で、cocoindex 以外の既存 MCP サーバーエントリ（他パッケージ・ユーザー手動追加分）を誤って削除していないか確認しているか
- 旧キー `targets.gemini` の読み替えテストが、`.local.yaml` 由来の上書きシナリオ（ベース `cocoindex.yaml` のみでは再現しない状況）で書かれているか
- proxy 永続化ライフサイクルのテストが、SessionStart → SessionEnd → 再 SessionStart という複数セッションのシーケンスを跨いで検証しているか（単一セッションのモックだけでは「停止しない」ことを検証できない）
