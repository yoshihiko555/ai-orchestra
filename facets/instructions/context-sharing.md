# Context Sharing Rule

**CLI 間コンテキスト共有基盤のルール。**

## 概要

`.claude/context/` ディレクトリを通じて、セッション内サブエージェント間およびCLI間（Claude Code / Codex CLI / Gemini CLI）で作業コンテキストを共有する。

## ストレージ構造

```
.claude/context/
  session/                    # セッションスコープ（終了時クリーンアップ）
    meta.json                 # セッション ID、開始時刻
    entries/                  # サブエージェント結果（Map-Reduce）
      {agent_id}_{timestamp}.json
  shared/                     # CLI 間共有
    working-context.json      # 作業中ファイル・設計判断・フェーズ
```

## 自動動作

| イベント | hook | 動作 |
|---------|------|------|
| セッション開始 | `load-task-state.py` | `init_context_dir()` でディレクトリ初期化 |
| サブエージェント起動前 | `inject-shared-context.py` | 既存エントリー + working-context を prompt に注入 |
| サブエージェント完了後 | `capture-task-result.py` | 結果サマリーを `session/entries/` に書き出し |
| ファイル編集後 | `update-working-context.py` | 変更ファイルを `working-context.json` に追記 |
| セッション終了 | `cleanup-session-context.py` | `session/` と `working-context.json` を削除 |

## 注入形式

サブエージェント起動時に prompt 末尾に以下が自動追加される:

```
[Shared Context]
## Previous Agent Results
- {agent_id} ({task_name}): {summary}

## Working Context
- Modified files: file1.py, file2.py
- Current phase: implementation
```

## 制限

- エントリーは最新 5 件まで注入
- 各エントリーの summary は 200 文字にトランケート
- modified_files は最新 20 件まで表示
- `.claude/` 配下のファイル変更は working-context に記録しない

## セッション間記憶

セッション終了時に `session/` はクリーンアップされる。セッション間の記憶永続化は claude-mem に委任する。
