# Context Sharing Rule

**CLI 間コンテキスト共有基盤のルール。**

## 概要

`.claude/context/` ディレクトリを通じて、セッション内サブエージェント間およびCLI間（Claude Code / Codex CLI / Antigravity CLI）で作業コンテキストを共有する。

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

| イベント               | hook                         | 動作                                                                     |
| ---------------------- | ---------------------------- | ------------------------------------------------------------------------ |
| セッション開始         | `load-task-state.py`         | `init_context_dir()` でディレクトリ初期化                                |
| サブエージェント起動前 | `inject-shared-context.py`   | 解決済みルーティング + 既存エントリー + working-context を prompt に注入 |
| サブエージェント完了後 | `capture-task-result.py`     | 結果サマリーを `session/entries/` に書き出し                             |
| ファイル編集後         | `update-working-context.py`  | 変更ファイルを `working-context.json` に追記                             |
| セッション終了         | `cleanup-session-context.py` | `session/` と `working-context.json` を削除                              |

## 注入形式

サブエージェント起動時に prompt 末尾に以下が自動追加される:

```
[Shared Context]
## Previous Agent Results
- {agent_id} ({task_name}): {summary}

## Working Context
- Modified files: file1.py, file2.py
- Current phase: implementation

[Resolved Routing]
Resolved by hook from cli-tools.yaml + cli-tools.local.yaml (merged). Follow these values; do not re-read the config files to decide tool or sandbox. Call only the CLIs that have lines below.
- agent: debugger
- tool: codex
- codex.model: {codex.model}
- codex.sandbox: read-only
- codex.flags: (none)
- codex.requires_sandbox_disable: true
```

`[Resolved Routing]` は、起動するエージェント（`subagent_type`。省略時は `general-purpose`）が
`cli-tools.yaml` の `agents` に定義され、project が `cli-tools.yaml` または `cli-tools.local.yaml` を
持つ場合にだけ付く。値は base と `.local.yaml` をマージした設定から hook が解決したもので、
サブエージェントは config を読み直さずにこれに従う。sandbox は `agents.<name>.sandbox` →
`codex.sandbox.analysis` の順で解決し（`tool: auto` でエージェント別の指定がなければ analysis 用と
implementation 用の両方を示す）、`read-only` / `workspace-write` 以外の値や、sandbox を無効化・上書きする
フラグ（`--dangerously-bypass-approvals-and-sandbox` / `--full-auto` / `--sandbox` 等）があれば Codex を使わない
（`tool: codex` は `claude-direct` に、`tool: auto` は codex の行を出さない。理由は `note` に書く）。
`[Shared Context]` は前回の結果か working-context がある場合にだけ付く。
`[Resolved Routing]` は常に最後に置き、`[Shared Context]` に展開する値は 1 行に畳む
（過去のサブエージェント出力に偽のルーティングブロックを紛れ込ませないため）。

## 制限

- エントリーは最新 5 件まで注入
- 各エントリーの summary は 200 文字にトランケート
- modified_files は最新 20 件まで表示
- `.claude/` 配下のファイル変更は working-context に記録しない

## セッション間記憶

セッション終了時に `session/` はクリーンアップされる。セッション間の記憶永続化は claude-mem に委任する。
