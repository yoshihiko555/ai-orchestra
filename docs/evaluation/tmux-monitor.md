# tmux-monitor 評価セット

**パッケージ**: `packages/tmux-monitor`
**類型**: hook 型
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-03（人間レビュー完了・指摘なし。評価観点の変更なし。テストギャップは Issue #137 で追跡）
**情報源**: docs/reference/packages.md（tmux-monitor セクション）, docs/reference/hooks.md（tmux-monitor フック一覧）
**補助参照（構成要素の列挙のみ。期待値の導出には未使用）**: packages/tmux-monitor/manifest.json, packages/tmux-monitor/hooks/\*.py の docstring 冒頭

## 1. 責務定義

tmux-monitor は、tmux がインストールされた環境において、Claude Code のサブエージェント（Task/Agent）の起動・停止を tmux ペインでリアルタイムに可視化する。SessionStart/SessionEnd で監視用 tmux セッションのライフサイクルを管理し、PreToolUse/SubagentStart/SubagentStop で各サブエージェントの状態をペイン単位で表示する。専用の config ファイルは持たず、`tmux` バイナリの有無のみで有効/無効が決まる（docs/reference/packages.md）。tmux が未インストールの環境やペイン/セッションが見つからない状況でも、Claude Code 本体のセッション進行を妨げないことを保証する。

### Non-Goals

- tmux 自体のインストール・環境構築は行わない（前提条件として扱う）
- サブエージェントの出力内容の解析・要約は行わない（`tmux-format-output.py` は表示整形のみ）
- 専用の enabled フラグや `*.local.yaml` による有効/無効切り替えは提供しない（tmux バイナリ検出のみで判定）

## 2. 期待する入出力・副作用

| 構成要素                 | 入力                                                    | 期待する出力               | 副作用                                                                                                          |
| ------------------------ | ------------------------------------------------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `tmux-session-start.py`  | SessionStart hook 入力（`cwd`, `session_id`）           | なし（stdout 出力なし）    | tmux セッションの作成/再利用、`/tmp/claude-session-info/{session_id}.*` 書き込み、孤児セッション/ファイルの削除 |
| `tmux-session-end.py`    | SessionEnd hook 入力（`session_id`）                    | なし                       | 同一 PID に紐づく session info ファイルの一括削除、共有コンテキストストアの削除                                 |
| `tmux-pre-task.py`       | PreToolUse(Agent/Task) 入力（`tool_input.description`） | なし                       | `{session_id}.task-queue` に description を追記（flock で排他制御）                                             |
| `tmux-subagent-start.py` | SubagentStart 入力（`agent_id`, `session_id`）          | なし                       | tmux ペインの追加/再利用、pane info ファイルの書き込み                                                          |
| `tmux-subagent-stop.py`  | SubagentStop 入力（`agent_id`, `session_id`）           | なし                       | ペインタイトルへの `DONE:` 付与、ペイン境界色の変更、pane info ファイルの削除                                   |
| `tmux-format-output.py`  | stdin（Claude Code transcript 形式の JSONL）            | 整形済みテキスト（stdout） | なし（`tail -f \| ./tmux-format-output.py` として利用される表示専用ユーティリティ）                             |

## 3. 評価観点

- [ ] EV-01（正常 / must）: tmux がインストールされた環境で、SessionStart hook が `claude-{project_name}-{session_key}` 形式の tmux セッションを作成または再利用する — 根拠: 実装挙動
- [ ] EV-02（異常 / must）: tmux バイナリが見つからない環境では、全 hook（SessionStart/SessionEnd/PreToolUse/SubagentStart/SubagentStop）が no-op として即座に終了し、Claude Code 本体のセッション進行に影響を与えない — 根拠: docs/reference/packages.md（有効化）
- [ ] EV-03（異常 / must）: tmux はインストール済みだが対象の tmux セッション/ペインが存在しない（未起動・削除済み等）場合、SubagentStart/SubagentStop hook はエラーを発生させず何もせず終了する — 根拠: 実装挙動
- [ ] EV-04（異常 / must）: tmux/ps コマンド呼び出しがタイムアウトした場合も hook は例外を発生させず処理を継続する（`returncode=1` として扱われる） — 根拠: 実装挙動
- [ ] EV-05（正常 / should）: SubagentStart hook は PreToolUse hook が保存した description をキューから FIFO で取得し、ペインタイトルに反映する — 根拠: 実装挙動
- [ ] EV-06（正常 / should）: SubagentStop hook はペインを kill せず、タイトルに `DONE:` を付与し境界色を変更するのみで、`tail -f` による出力表示を維持する — 根拠: 実装挙動（docstring: tmux-subagent-stop.py）
- [ ] EV-07（正常 / should）: SessionStart hook は `/resume` や `/clear` 等で同一 PID の tmux セッションが既に存在する場合、既存セッションを kill せず維持し、待機ペインのみ再生成する — 根拠: 実装挙動（コード内コメント: tmux-session-start.py）
- [ ] EV-08（境界 / should）: SessionEnd hook は現在の `session_id` だけでなく、同一 PID に紐づく全 session info ファイルを一括削除する（`/resume` で `session_id` が変わっても旧ファイルが残らない） — 根拠: 実装挙動（docstring: tmux-session-end.py）
- [ ] EV-09（境界 / must）: SessionStart hook の孤児セッション削除は PID の生存確認（`os.kill(pid, 0)`）に基づいて行われ、生存中の別プロジェクトの tmux セッションを誤って削除しない — 根拠: 実装挙動

## 4. 類型別観点

<!-- fail-safe 方針は EV-02 / EV-03 で担保（tmux 未インストール・対象セッション不在のいずれも no-op）。重複のため新規 ID は起こさない -->

- [ ] EV-10（exit code規約 / 正常 / should）: 各 hook はブロック用の exit code（Claude Code の block 相当）を出力せず、常に処理継続扱いとして完了する（tmux-monitor は表示専用で Claude Code の動作を止めない） — 根拠: 実装挙動
- [ ] EV-11（冪等性 / 境界 / should）: 同一 `session_id` で SessionStart / SubagentStart hook が繰り返し実行されても、既存の tmux セッション・ペインを再利用し重複作成しない — 根拠: 実装挙動
- [ ] EV-12（性能 / 境界 / should）: 外部プロセス呼び出し（`ps`, `tmux`）には 5 秒のタイムアウトが設定されており、hook が無制限にブロックしない — 根拠: 実装挙動（tmux_common.py: `_SUBPROCESS_TIMEOUT`）
- [ ] EV-13（秘匿情報 / 境界 / should）: PreToolUse で保存されたタスク description はそのままペインタイトルに表示され、マスキング処理は行われない — 根拠: 実装挙動（要人間レビュー: 意図した仕様か実装追認かを重点確認）
- N/A: config 駆動 — 専用の config ファイルおよび `*.local.yaml` / `*.local.json` 上書きは提供されない。`tmux` バイナリの有無のみで有効/無効を判定する仕様のため対象外（根拠: docs/reference/packages.md「有効化」）
- N/A: stdin/stdout 契約 — tmux-monitor 固有の追加スキーマ定義はなく、Claude Code 本体の標準 hook 入力（`cwd`/`session_id`/`agent_id`/`tool_input` 等）をそのまま利用するため対象外

## 5. テストレビュー判断基準（パッケージ固有）

- EV-02・EV-03・EV-04 は環境依存（tmux 有無、セッション有無、subprocess タイムアウト）のテストであるため、実際の `tmux` バイナリに依存せずモック/フェイクで再現できているかを確認する（CI 環境で `tmux` が入っていないケースを含む）
- EV-09（孤児セッション削除）のテストは、他プロジェクトの生存中セッションを削除しないことを明示的に検証しているか確認する（誤削除は実害が大きいため、正常系のみで満足しない）
- EV-13 は「現状の実装がそうなっている」ことの確認に留め、マスキングが必要かどうかの仕様判断（要 / 不要）をテストの期待値に断定的に埋め込まない
