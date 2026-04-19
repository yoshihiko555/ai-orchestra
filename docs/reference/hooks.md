# Hook リファレンス

**更新日**: 2026-04-14
AI Orchestra の全フックの動作と設定の詳細。

---

## 概要

Hook は Claude Code のライフサイクルイベントに応じて自動実行される Python スクリプト。`$AI_ORCHESTRA_DIR/packages/{pkg}/hooks/` に配置され、`.claude/settings.local.json` に登録される。

### フックイベント一覧

| イベント | 発火タイミング |
|---------|--------------|
| `SessionStart` | Claude Code セッション開始時 |
| `SessionEnd` | Claude Code セッション終了時 |
| `UserPromptSubmit` | ユーザーがプロンプトを送信した直後 |
| `InstructionsLoaded` | 指示書ファイルの読み込み完了時 |
| `PreToolUse` | ツール実行前 |
| `PostToolUse` | ツール実行後 |
| `Stop` | Claude Code が応答を返す直前 |
| `ExitPlanMode` | プランモード終了時 |
| `SubagentStart` | サブエージェント起動時 |
| `SubagentStop` | サブエージェント停止時 |

### フックの入出力

- **入力**: stdin から JSON を受け取る（イベントに応じた構造）
- **出力**: stdout に出力した文字列がセッションのコンテキストに注入される
- **終了コード**: 0 で正常終了。非ゼロでもセッションは継続する

---

## パッケージ別フック一覧

### core

| フック | イベント | 対象 | 説明 |
|-------|---------|------|------|
| `load-task-state.py` | SessionStart | — | Plans.md からタスク状態を読み込みサマリーを出力 |
| `set-plan-gate.py` | PostToolUse | Agent/Task | プラン完了後にプランゲートを設定 |
| `check-plan-gate.py` | PreToolUse | Agent/Task | プランゲート確認（実装エージェントをブロック） |
| `clear-plan-gate.py` | UserPromptSubmit | — | ユーザー入力時にプランゲートをクリア |
| `inject-shared-context.py` | PreToolUse | Agent/Task | サブエージェントに共有コンテキストを注入 |
| `capture-task-result.py` | PostToolUse | Agent/Task | サブエージェント結果を `.claude/context/session/entries/` に記録 |
| `update-working-context.py` | PostToolUse | Edit/Write | 変更ファイルを `working-context.json` に追記 |
| `cleanup-session-context.py` | SessionEnd | — | `.claude/context/session/` をクリーンアップ |

### agent-routing

| フック | イベント | 対象 | 説明 |
|-------|---------|------|------|
| `agent-router.py` | UserPromptSubmit | — | プロンプトからエージェントを検出し `[Agent Routing]` を出力 |

### quality-gates

| フック | イベント | 対象 | 説明 |
|-------|---------|------|------|
| `check-context-optimization.py` | PreToolUse | Read/Grep/Bash | 大きすぎる読み込みや `cat` 利用を抑制 |
| `post-implementation-review.py` | PostToolUse | Edit/Write | 一定量の変更後にレビューを提案 |
| `post-test-analysis.py` | PostToolUse | Bash | テスト実行結果を分析し `quality_gate` を記録 |
| `lint-on-save.py` | PostToolUse | Edit/Write | ファイル種別ごとの自動 lint / format 実行 |
| `test-tampering-detector.py` | PostToolUse | Edit/Write/Bash | skip/disable 追加やテスト削除を警告 |
| `test-gate-checker.py` | PostToolUse | Edit/Write | テスト品質ゲートチェック |
| `turn-end-summary.py` | Stop | — | 次ターン向け `systemMessage` を生成 |

### audit

| フック | イベント | 対象 | 説明 |
|-------|---------|------|------|
| `audit-bootstrap.py` | SessionStart | — | セッションログ初期化 + `session_start` 記録 |
| `audit-session-end.py` | SessionEnd | — | セッション集計 + `session_end` 記録 |
| `audit-prompt.py` | UserPromptSubmit | — | 期待ルートを予測し `prompt` を記録 |
| `audit-route.py` | PostToolUse | 全 PostToolUse | 実ルート照合 + `route_decision` 記録 |
| `audit-cli.py` | PostToolUse | Bash | Codex/Gemini CLI 呼び出しを `cli_call` として記録 |
| `audit-subagent-start.py` | SubagentStart | — | サブエージェント開始を記録 |
| `audit-subagent-end.py` | SubagentStop | — | サブエージェント終了を記録 |
| `audit-instructions-loaded.py` | InstructionsLoaded | — | 読み込まれた指示書を記録 |

### codex-suggestions

| フック | イベント | 対象 | 説明 |
|-------|---------|------|------|
| `check-codex-before-write.py` | PreToolUse | Edit/Write | `[Codex Suggestion]` を出力して Codex 相談を促す |
| `check-codex-after-plan.py` | PostToolUse | Agent/Task | プラン完了後に Codex レビューを提案 |

### gemini-suggestions

| フック | イベント | 対象 | 説明 |
|-------|---------|------|------|
| `suggest-gemini-research.py` | PreToolUse | WebSearch/WebFetch | `[Gemini Suggestion]` を出力して Gemini リサーチを促す |

### cocoindex

| フック | イベント | 対象 | 説明 |
|-------|---------|------|------|
| `provision-mcp-servers.py` | SessionStart | — | 各 CLI の MCP サーバー設定を生成 |
| `stop-mcp-proxy.py` | SessionEnd | — | proxy を停止（v2 モード時） |

### tmux-monitor

| フック | イベント | 対象 | 説明 |
|-------|---------|------|------|
| `tmux-session-start.py` | SessionStart | — | tmux ペインのセットアップ |
| `tmux-session-end.py` | SessionEnd | — | tmux ペインのクリーンアップ |
| `tmux-pre-task.py` | PreToolUse | Agent/Task | タスク実行前の準備表示 |
| `tmux-subagent-start.py` | SubagentStart | — | サブエージェント起動表示 |
| `tmux-subagent-stop.py` | SubagentStop | — | サブエージェント停止表示 |

---

## 詳細

### load-task-state.py（core）

Plans.md からタスク状態を読み込み、セッション開始時にサマリーを出力する。

**処理フロー:**

1. `.claude/Plans.md` の存在を確認
2. `task-memory.yaml` から設定を読み込み（マーカー定義等）
3. 状態マーカー（`cc:TODO` / `cc:WIP` / `cc:done` / `cc:blocked`）を解析
4. 全フェーズが完了したプロジェクトを `.claude/Plans.archive.md` にアーカイブ
5. WIP / 次の TODO / blocked タスクをサマリーとして stdout に出力

**出力例:**

```
[task-memory] 11 tasks (done: 9, TODO: 2)
  Next TODO:
    - タスク1
    - タスク2
```

### agent-router.py（agent-routing）

ユーザーのプロンプトからエージェントを検出し、ルーティング提案を出力する。

**処理フロー:**

1. stdin から `{ "prompt": "..." }` を受け取る
2. `cli-tools.yaml`（+ `.local.yaml`）を読み込む
3. プロンプト内のキーワードからエージェントを検出
4. `agents.{name}.tool` でルーティング先を決定
5. CLI 呼び出しコマンド例と `Task(...)` 形式を stdout に出力

**出力例:**

```
[Codex CLI] Agent 'backend-python-dev' ('Python') uses Codex:
`codex exec --model gpt-5.3-codex --sandbox workspace-write --full-auto "..." 2>/dev/null`

[Agent Routing] 'Python' → `backend-python-dev` (tool: codex):
Task(subagent_type="backend-python-dev", prompt="...")
```

### check-codex-before-write.py（codex-suggestions）

ファイル編集前に Codex への相談を提案する。

**発火条件:**

- `core/` を含むファイルパスへの変更
- `config` や `class` 等のキーワードを含む変更内容
- 大きなコンテンツを含む新規ファイル作成

**出力例:**

```
[Codex Suggestion] File path contains 'config'. Consider consulting Codex before this change:
`codex exec --model gpt-5.3-codex --sandbox read-only --full-auto '...'`
```

**例外（発火しないケース）:**

- `codex.enabled: false` の場合
- 既に Codex 相談済みのファイル

### suggest-gemini-research.py（gemini-suggestions）

WebSearch/WebFetch の前に Gemini CLI でのリサーチを提案する。

**出力例:**

```
[Gemini Suggestion] Consider using Gemini CLI for research:
`gemini -m gemini-3.1-pro-preview -p "..." 2>/dev/null`
```

**例外:**

- `gemini.enabled: false` の場合

### lint-on-save.py（quality-gates）

Edit/Write 後に、編集したファイル種別に応じて formatter / linter を実行する。

**処理フロー:**

1. 変更されたファイルのパスを取得
2. 拡張子や shebang からファイル種別を判定
3. Python は `ruff`、JS/TS は `biome` / `prettier` / `eslint`、Shell は `shfmt` / `shellcheck`、Go は `gofmt`、Rust は `rustfmt` などを順に試行
4. 実行結果があれば stdout に出力

---

## 共通ユーティリティ: hook_common.py

全フックから参照される共通ライブラリ。

### 主要関数

| 関数 | 説明 |
|------|------|
| `read_hook_input()` | stdin から JSON を読み取り dict を返す |
| `get_field(data, key)` | dict からフィールドを安全に取得 |
| `load_package_config(pkg, file, project_dir)` | パッケージ config を読み込み `.local` があればマージ |
| `find_package_config(pkg, file, project_dir)` | パッケージ config パスを解決 |
| `deep_merge(base, override)` | dict を再帰的にマージ |
| `read_json_safe(path)` | JSON ファイルを安全に読み込み |
| `write_json(path, data)` | dict を JSON ファイルに書き出し |
| `append_jsonl(path, record)` | dict を JSONL ファイルに追記 |
| `find_first_text(node, keys)` | ネスト構造から最初の非空文字列を検索 |
| `find_first_int(node, keys)` | ネスト構造から最初の整数値を検索 |
| `ensure_package_path(pkg, subdir)` | `$AI_ORCHESTRA_DIR/packages/{pkg}/{subdir}` を sys.path に追加 |
| `safe_hook_execution(func)` | Hook の main() を安全にラップ（例外時は stderr にログ出力して exit(0)） |
| `try_append_event(...)` | 統一イベントログへの追記（失敗しても例外を上げない） |

### 使用例

```python
#!/usr/bin/env python3
import sys
import os

# core hooks を sys.path に追加
sys.path.insert(0, os.path.join(os.environ.get("AI_ORCHESTRA_DIR", ""), "packages", "core", "hooks"))
from hook_common import read_hook_input, load_package_config, safe_hook_execution

@safe_hook_execution
def main():
    data = read_hook_input()
    config = load_package_config("agent-routing", "cli-tools.yaml", os.getcwd())
    # ... 処理 ...

if __name__ == "__main__":
    main()
```

---

## フックの代表的な組み合わせ

同一イベントに複数のフックが登録されている場合、`.claude/settings.local.json` の登録順に実行される。以下は現行 manifest に基づく代表的な組み合わせ。

### SessionStart

- `sync-orchestra.py`（同期スクリプト）
- `load-task-state.py`（core）
- `audit-bootstrap.py`（audit）
- `provision-mcp-servers.py`（cocoindex）
- `tmux-session-start.py`（tmux-monitor）

### SessionEnd

- `audit-session-end.py`（audit）
- `tmux-session-end.py`（tmux-monitor）

### UserPromptSubmit

- `clear-plan-gate.py`（core）
- `audit-prompt.py`（audit）
- `agent-router.py`（agent-routing）

### PreToolUse

- `Agent/Task`: `check-plan-gate.py`, `inject-shared-context.py`, `tmux-pre-task.py`
- `Edit/Write`: `check-codex-before-write.py`
- `Read/Grep/Bash`: `check-context-optimization.py`
- `WebSearch/WebFetch`: `suggest-gemini-research.py`

### PostToolUse

- `Agent/Task`: `set-plan-gate.py`, `capture-task-result.py`, `check-codex-after-plan.py`
- `Edit/Write`: `post-implementation-review.py`, `lint-on-save.py`, `test-tampering-detector.py`, `test-gate-checker.py`, `update-working-context.py`
- `Bash`: `post-test-analysis.py`, `test-tampering-detector.py`, `audit-cli.py`
- `全 PostToolUse`: `audit-route.py`

### InstructionsLoaded

- `audit-instructions-loaded.py`（audit）

### Stop

- `turn-end-summary.py`（quality-gates）

---

## フックの有効化/無効化

### パッケージ単位

```bash
# パッケージを無効化（hooks の登録を解除）
orchex disable codex-suggestions --project .

# パッケージを有効化（hooks を再登録）
orchex enable codex-suggestions --project .
```

### 個別フック

`.claude/settings.local.json` から該当フックのエントリを手動で削除する。

### CLI 単位の無効化

`cli-tools.yaml` で CLI を無効化すると、関連する提案フックが自動的に抑制される:

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
codex:
  enabled: false    # check-codex-before-write.py の [Codex Suggestion] が抑制される
gemini:
  enabled: false    # suggest-gemini-research.py の [Gemini Suggestion] が抑制される
```
