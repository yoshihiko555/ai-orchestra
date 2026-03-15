# Archived Plans

## Archived: 2026-03-14

## Project: cocoindex v2 — mcp-proxy による MCP 共有化

### Phase 1: 調査・検証 `cc:done`

#### mcp-proxy 実動作確認

- `cc:done` mcp-proxy インストール・起動テスト（SSE / streamable-http エンドポイントの有無確認）
- `cc:done` Claude Code から HTTP/SSE 接続で cocoindex-code が使えるか検証
- `cc:done` Gemini CLI から HTTP/SSE 接続で cocoindex-code が使えるか検証（同一 /mcp エンドポイント）
- `cc:done` Codex CLI の streamable-http 対応状況を確認 → POST /mcp で tools/list 成功

### Phase 2: config 設計・proxy_manager 実装 `cc:done`

#### config 拡張

- `cc:done` config/cocoindex.yaml に proxy セクション追加（enabled, port, port_range, host, pid_file, startup_timeout）
- `cc:done` targets に force_stdio フィールド追加（全 CLI 対象）

#### proxy_manager.py

- `cc:done` start_proxy(): mcp-proxy を subprocess.Popen で起動、PID ファイル書き出し、ポート開放待機
- `cc:done` stop_proxy(): PID ファイルから読み込み、SIGTERM → SIGKILL
- `cc:done` is_proxy_running(): PID 生死確認 + ポートチェック
- `cc:done` orphan 対策: 前回の残存プロセス検出・クリーンアップ
- `cc:done` hash-based ポート導出: project_dir の MD5 ハッシュで port_range 内の固定ポートを自動割り当て

### Phase 3: provision hook 拡張 `cc:done`

#### 各 CLI の v2 エントリ生成

- `cc:done` _build_claude_entry: proxy_active 時に type="sse", url="http://{host}:{port}/sse"
- `cc:done` _build_gemini_entry: proxy_active 時に url="http://{host}:{port}/sse"
- `cc:done` _build_toml_section (Codex): proxy_active 時に url="http://{host}:{port}/mcp"
- `cc:done` provision-mcp-servers.py の main() に proxy 起動ロジックを統合

#### SessionEnd hook

- `cc:done` stop-mcp-proxy.py: セッション終了時に proxy を停止（cwd fallback 対応済み）
- `cc:done` manifest.json に SessionEnd hook を追加（version 0.2.0）

### Phase 4: テスト・ドキュメント `cc:done`

#### テスト

- `cc:done` proxy_manager の単体テスト（35 テスト: 起動/停止/冪等性/orphan/ポート導出）
- `cc:done` v2 config 時の各 CLI エントリ形式テスト（7 テスト: proxy/force_stdio/fallback）
- `cc:done` E2E 検証: proxy 起動 → SSE 接続 → ツール呼び出し → Gemini 並列アクセス
- `cc:done` cocoindex-usage.md を v2 対応に更新
- `cc:done` proxy セクションの設定方法・有効化手順を追記

---

## Archived: 2026-03-14

## Project: CLI 間コンテキスト共有基盤

### Phase 0: 基盤（共通ユーティリティ） `cc:done`

#### context_store.py

- `cc:done` `packages/core/hooks/context_store.py` 作成 — コンテキスト操作の共通関数
  - `init_context_dir(project_dir)` — `.claude/context/{session,shared}` ディレクトリ初期化
  - `write_entry(project_dir, agent_id, data)` — `session/entries/{agent_id}.json` 書き込み
  - `read_entries(project_dir)` — 全エントリの一括読み込み
  - `update_working_context(project_dir, updates)` — `shared/working-context.json` 更新
  - `cleanup_session(project_dir)` — `session/` のクリーンアップ
- `cc:done` `.claudeignore` テンプレートに `.claude/context/session/` を追加

### Phase 1: 並列サブエージェント間共有 `cc:done`

#### SessionStart — コンテキスト初期化

- `cc:done` 既存 SessionStart hook に `init_context_dir()` 呼び出しを追加（`session/meta.json` 作成）

#### PostToolUse(Task) — 結果キャプチャ

- `cc:done` `capture-task-result.py` 新規作成 — Task 完了時に `tool_response` サマリを `session/entries/{agent_id}.json` に書き出し
- `cc:done` エントリ形式: `{agent_id, task_name, timestamp, status, summary}`（サマリは先頭 500 トークン相当にトランケート）

#### PreToolUse(Task) — コンテキスト注入

- `cc:done` 既存エントリがある場合、prompt に `[Shared Context]` セクションとして注入する hook 拡張

#### テスト

- `cc:done` ユニットテスト + hook 統合テスト（65テスト通過）

### Phase 2: CLI 間コンテキスト自動注入 `cc:done`

#### working-context.json 自動更新

- `cc:done` PostToolUse(Edit/Write) hook — 変更ファイルリストを `shared/working-context.json` に追記
- `cc:done` working-context.json 形式: `{modified_files, current_phase, recent_decisions, updated_at}`

#### Codex/Gemini 委譲時のコンテキスト注入

- `cc:done` PreToolUse(Task) hook 拡張 — Codex/Gemini 委譲時に `working-context.json` の内容を prompt 末尾に自動追加
- `cc:done` 注入テンプレート: `[Working Context]\n- Modified files: ...\n- Current phase: ...\n- Recent decisions: ...`

#### agent-router.py 拡張

- `cc:TODO` ルーティング提案にコンテキストヒントを付加

#### テスト

- `cc:done` Codex 委譲時にコンテキストが注入されることを確認（inject-shared-context hook テスト済み）

### Phase 3: クリーンアップ + 統合 `cc:done`

#### SessionEnd — セッション終了処理

- `cc:done` `cleanup-session-context.py` 新規作成 — `session/` のクリーンアップ（セッション間記憶は claude-mem に委任）

#### 統合

- `cc:done` `packages/core/manifest.json` に新規 hook を登録（v0.4.0）
- `cc:done` 統合テスト: 全フェーズの E2E 動作確認（65テスト通過）
- `cc:done` `.claude/rules/context-sharing.md` にコンテキスト共有ルールを追加

---

## Archived: 2026-03-15

## Project: Plans.md 自動アーカイブ

### Phase 1: アーカイブロジック実装 `cc:done`

#### load-task-state.py 拡張

- `cc:done` detect_completed_projects(content) — PJ 単位の完了判定（全フェーズ `cc:done`）
- `cc:done` archive_projects(plans_path, archive_path, projects) — 完了 PJ を archive に追記 + Plans.md から除去
- `cc:done` main() のタスク解析前にアーカイブ実行

### Phase 2: テスト `cc:done`

- `cc:done` 完了 PJ 検出テスト
- `cc:done` アーカイブ実行テスト（Plans.md → Plans.archive.md 移動）
- `cc:done` 混在ケース（done + TODO 混在 PJ は残る）
- `cc:done` Decisions/Notes の移動テスト（全 PJ 完了時にまとめてアーカイブ）

### Phase 3: ルール/テンプレート更新 `cc:done`

- `cc:done` task-memory-usage.md にアーカイブ仕様追記
- `cc:done` .gitignore/.claudeignore テンプレート更新（Plans.archive.md 追加）

### Phase 4: 検証 `cc:done`

- `cc:done` 既存 Plans.md をアーカイブ実行して検証（SessionStart で自動実行を確認）

---
