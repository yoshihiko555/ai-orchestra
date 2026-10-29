---
codd:
  node_id: "design:hooks"
  kind: design
  status: active
  depends_on:
    - id: "design:architecture"
      relation: references
  owner: ai-orchestra
---

# Hook リファレンス

**更新日**: 2026-10-28
AI Orchestra の全フックの動作と設定の詳細。

---

## 概要

Hook は Claude Code のライフサイクルイベントに応じて自動実行される Python スクリプト。`$AI_ORCHESTRA_DIR/packages/{pkg}/hooks/` に配置され、`.claude/settings.local.json` に登録される。

### フックイベント一覧

| イベント             | 発火タイミング                     |
| -------------------- | ---------------------------------- |
| `SessionStart`       | Claude Code セッション開始時       |
| `SessionEnd`         | Claude Code セッション終了時       |
| `UserPromptSubmit`   | ユーザーがプロンプトを送信した直後 |
| `InstructionsLoaded` | 指示書ファイルの読み込み完了時     |
| `PreToolUse`         | ツール実行前                       |
| `PostToolUse`        | ツール実行後                       |
| `Stop`               | Claude Code が応答を返す直前       |
| `ExitPlanMode`       | プランモード終了時                 |
| `SubagentStart`      | サブエージェント起動時             |
| `SubagentStop`       | サブエージェント停止時             |

### フックの入出力

- **入力**: stdin から JSON を受け取る（イベントに応じた構造）
- **出力**: stdout に出力した文字列がセッションのコンテキストに注入される
- **終了コード**: 0 で正常終了。非ゼロでもセッションは継続する

---

## パッケージ別フック一覧

### core

| フック                       | イベント         | 対象       | 説明                                                                                   |
| ---------------------------- | ---------------- | ---------- | -------------------------------------------------------------------------------------- |
| `load-task-state.py`         | SessionStart     | —          | plan ファイル（`.claude/plans/*.md`）からタスク状態を読み込みサマリーを出力            |
| `set-plan-gate.py`           | PostToolUse      | Agent/Task | プラン完了後にプランゲートを設定                                                       |
| `check-plan-gate.py`         | PreToolUse       | Agent/Task | プランゲート確認（実装エージェントをブロック）                                         |
| `clear-plan-gate.py`         | UserPromptSubmit | —          | ユーザー入力時にプランゲートをクリア                                                   |
| `inject-shared-context.py`   | PreToolUse       | Agent/Task | サブエージェントに解決済みルーティング（`[Resolved Routing]`）と共有コンテキストを注入 |
| `capture-task-result.py`     | PostToolUse      | Agent/Task | サブエージェント結果を `.claude/context/session/entries/` に記録                       |
| `update-working-context.py`  | PostToolUse      | Edit/Write | 変更ファイルを `working-context.json` に追記                                           |
| `cleanup-session-context.py` | SessionEnd       | —          | `.claude/context/session/` をクリーンアップ                                            |

### agent-routing

| フック            | イベント         | 対象 | 説明                                                        |
| ----------------- | ---------------- | ---- | ----------------------------------------------------------- |
| `agent-router.py` | UserPromptSubmit | —    | プロンプトからエージェントを検出し `[Agent Routing]` を出力 |

### quality-gates

| フック                          | イベント    | 対象            | 説明                                         |
| ------------------------------- | ----------- | --------------- | -------------------------------------------- |
| `check-context-optimization.py` | PreToolUse  | Read/Grep/Bash  | 大きすぎる読み込みや `cat` 利用を抑制        |
| `post-implementation-review.py` | PostToolUse | Edit/Write      | 一定量の変更後にレビューを提案               |
| `post-test-analysis.py`         | PostToolUse | Bash            | テスト実行結果を分析し `quality_gate` を記録 |
| `lint-on-save.py`               | PostToolUse | Edit/Write      | ファイル種別ごとの自動 lint / format 実行    |
| `test-tampering-detector.py`    | PostToolUse | Edit/Write/Bash | skip/disable 追加やテスト削除を警告          |
| `test-gate-checker.py`          | PostToolUse | Edit/Write      | テスト品質ゲートチェック                     |
| `turn-end-summary.py`           | Stop        | —               | 次ターン向け `systemMessage` を生成          |

### audit

| フック                         | イベント           | 対象           | 説明                                                   |
| ------------------------------ | ------------------ | -------------- | ------------------------------------------------------ |
| `audit-bootstrap.py`           | SessionStart       | —              | セッションログ初期化 + `session_start` 記録            |
| `audit-session-end.py`         | SessionEnd         | —              | セッション集計 + `session_end` 記録                    |
| `audit-prompt.py`              | UserPromptSubmit   | —              | 期待ルートを予測し `prompt` を記録                     |
| `audit-route.py`               | PostToolUse        | 全 PostToolUse | 実ルート照合 + `route_decision` 記録                   |
| `audit-cli.py`                 | PostToolUse        | Bash           | Codex/Antigravity CLI 呼び出しを `cli_call` として記録 |
| `audit-subagent-start.py`      | SubagentStart      | —              | サブエージェント開始を記録                             |
| `audit-subagent-end.py`        | SubagentStop       | —              | サブエージェント終了を記録                             |
| `audit-instructions-loaded.py` | InstructionsLoaded | —              | 読み込まれた指示書を記録                               |

### codex-suggestions

| フック                        | イベント    | 対象       | 説明                                             |
| ----------------------------- | ----------- | ---------- | ------------------------------------------------ |
| `check-codex-before-write.py` | PreToolUse  | Edit/Write | `[Codex Suggestion]` を出力して Codex 相談を促す |
| `check-codex-after-plan.py`   | PostToolUse | Agent/Task | プラン完了後に Codex レビューを提案              |

### antigravity-suggestions

| フック                            | イベント   | 対象               | 説明                                                             |
| --------------------------------- | ---------- | ------------------ | ---------------------------------------------------------------- |
| `suggest-antigravity-research.py` | PreToolUse | WebSearch/WebFetch | `[Antigravity Suggestion]` を出力して Antigravity リサーチを促す |

### codd

| フック                       | イベント    | 対象       | 説明                                                                                                                      |
| ---------------------------- | ----------- | ---------- | ------------------------------------------------------------------------------------------------------------------------- |
| `codd-scan-postedit.py`      | PostToolUse | Edit/Write | scope 内ファイル編集時に `codd scan` を実行し graph を再構築（常に非ブロック、`hooks.scan_on_edit` opt-in・既定 `false`） |
| `codd-validate-precommit.py` | PreToolUse  | Bash       | `git commit` 検出時に `codd validate` を実行し warn/block（`hooks.validate_on_commit` opt-in・既定 `warn`）               |

---

## 詳細

### load-task-state.py（core）

plan ファイル（`.claude/plans/<slug>.md`。Project ごとに 1 ファイル）からタスク状態を読み込み、セッション開始時にサマリーを出力する（ADR-20261027-059）。

**処理フロー:**

1. `task-memory.yaml` から設定を読み込み（マーカー定義・表示上限等）
2. `.claude/plans/` 直下の `<slug>.md` を列挙（symlink と命名規則に合わないファイルは読まない）
3. ファイルごとに状態マーカー（`cc:TODO` / `cc:WIP` / `cc:done` / `cc:blocked`）と AC を解析し、完了かどうかを判定
4. 未完了のファイルごとに Goal / Open Questions / WIP / 次の TODO / blocked を stdout に出力。完了したファイルは件数と `/task-state close` の案内だけを出す
5. 旧形式の `.claude/Plans.md` があれば、移行を促す警告を 1 行出す（中身は読まない）

plan ファイルは読むだけで書き換えない（ADR-20260928-058）。表示するタスクの合計は `max_display_tasks` までで、上限を超えたファイルは件数だけの 1 行になる。

**出力例:**

```
[task-memory] .claude/plans/: 未完了 2 / 完了 1
[task-memory] 完了したファイルは /task-state close <slug> で片付けられます
■ plans-dir-split (12 tasks: done: 3, WIP: 1, TODO: 8)
  Goal: Plans.md をタスクごとのファイルに分割する
  WIP:
    - パーサの対応
  Next TODO:
    - スキルの対応
■ codd-ack — WIP: 0, TODO: 4, blocked: 0 (表示上限のため省略)
```

### agent-router.py（agent-routing）

ユーザーのプロンプトからエージェントを検出し、ルーティング提案を出力する。

**処理フロー:**

1. stdin から `{ "prompt": "..." }` を受け取る（ハーネスの通知は何もしない）
2. `cli-tools.yaml`（+ `.local.yaml`）を読み込む
3. エージェントを検出する。`router.engine: keyword`（既定）は、入力のうち依頼の形をした文だけでキーワードを照合する。`router.engine: jev` は Jev の判定を使い、使えないときはキーワード判定に戻る（[packages.md の「提案の判定」](./packages.md#提案の判定routerengine)）
4. `agents.{name}.tool` でルーティング先を決定
5. CLI 呼び出しコマンド例と `Task(...)` 形式を stdout に出力
6. project が audit を導入していれば、判定を監査ログに `routing_suggestion` として残す

**出力例:**

```
[Codex CLI] Agent 'backend-python-dev' ('Python') uses Codex:
`codex exec --model gpt-5.3-codex --sandbox read-only "..." < /dev/null 2>/dev/null`

[Agent Routing] 'Python' → `backend-python-dev` (tool: codex):
Task(subagent_type="backend-python-dev", prompt="...")
```

### check-codex-before-write.py（codex-suggestions）

ファイル編集前に `[Codex Suggestion]` を含む追加コンテキストで Codex への相談を任意提案する。

**発火条件（設計・契約シグナルに限定）:**

- basename が `DESIGN.md` / `ARCHITECTURE.md`、または `design/`・`architecture/`・`schema/`・`schemas/`・`interfaces/`・`contracts/` 等の明確な設計パスへの変更
- `interface` / `abstract class` / `Protocol` / `ABC` / `@abstractmethod` 等の契約宣言の追加・削除・変更。ただし TS/JS の `interface` は通常パスではコードシグナルとせず、設計パス内の変更としてのみ対象とする。`Props` という名前だけでは除外しない

Edit は `old_string` / `new_string` の契約宣言が同じならコードシグナルを抑制する。設計パス以外のソース（`.py` / `.pyi` / `.ts` / `.tsx` / `.js` / `.jsx` / `.java`）への Write はディスク上の旧本文と新本文の宣言を比較し、新規ファイルの旧本文は空とする。旧本文が読取不能・大き過ぎ・非通常ファイル・非 UTF-8 の場合は任意通知を抑制し、exit 0 とする。

設計パスは旧本文比較と独立に判定し、宣言が同じでも対象となり得る。`core` / `config` / `model`、通常の `class`、文字数、`src/` だけでは発火しない。ヘッダが同じ契約内のメソッド変更等は検出範囲を拡大せず、[評価セットの境界](../evaluation/codex-suggestions.md)に従う。

**例外（発火しないケース）:**

- `codex.enabled: false` の場合（project-local config による明示 opt-in がない場合も従来どおり無効）
- プロジェクト外のファイル（`CLAUDE_PROJECT_DIR` → hook 入力の `cwd` の順で解決したプロジェクトルートの外。scratchpad・メモリ等）
- 以下の共通条件でサブエージェント／インプロセス teammate 内、または同一 session / file の通知済みと判定した場合

`check-codex-after-plan.py` は今回の絞り込み・通知抑制の対象外であり、既存の発火条件と非拘束的な advisory を維持する。

### suggest-antigravity-research.py（antigravity-suggestions）

WebSearch/WebFetch の前に Antigravity CLI でのリサーチを任意提案する。

**発火条件:**

WebSearch の `tool_input.query` / WebFetch の `tool_input.prompt` に比較・選定・移行等の調査意図がある場合に限定する。明確な選択比較（`vs` / `vs.` / `versus` / `違い` / `どっち` / `which is better`）も対象とする。文字列等の演算比較や単純な migration エラー検索は除外するが、明確な選択比較が同居すればそちらを優先する。URL や質問の長さ、`documentation` / `library` / `guide` / `example` / `best practice` 等の一般語だけでは発火しない。

**出力例:**

```
[Antigravity Suggestion] Consider using Antigravity CLI for research:
`agy -p "..." --model gemini-4-argon-high 2>/dev/null`
```

**例外:**

- `antigravity.enabled: false` の場合（旧 `gemini.enabled: false` も後方互換で尊重）
- 以下の共通条件でサブエージェント／インプロセス teammate 内、または同一 session / 正規化した同一質問の通知済みと判定した場合

### 編集前・Web 調査前の提案の共通契約

対象は `check-codex-before-write.py` と `suggest-antigravity-research.py`。共通 helper で次を扱う:

- **実行中エージェントの判定**: トップレベルの `agent_id` が非空の文字列（`str`）の場合だけ、サブエージェント／インプロセス teammate 内と判定して提案を抑制する。`agent_type` は `--agent` で起動したメインにも付くため、単独では抑制しない。`tool_input.subagent_type` は呼出先であり判定に使わない（出典: [Claude Code 公式仕様 — Common input fields](https://code.claude.com/docs/en/hooks#common-input-fields)）。
- **project / worktree の解決**: 両 hook の config・対象範囲・marker は、環境変数 `CLAUDE_PROJECT_DIR` が非空ならそのルートを使い、未設定または空の場合だけ hook 入力の `cwd` を使う。同じルートに統一し、別 project / worktree の設定や通知状態を混同しない。
- **通知の重複抑制**: `session_id` があれば、project / worktree 別に同一 session・同一ファイル（Codex）または正規化した同一質問（Antigravity）への通知を 1 度にする。
- **最小限の状態**: 各 project / worktree の `.claude/state/suggestions/` に session / 対象を hash 化した空 marker のみを保存する。質問本文・URL・生の session ID や対象パスは保存しない。marker の自動削除は今回追加せず、終了後も session ディレクトリが残る。cleanup は将来課題とする。
- **状態を使えない場合**: 状態書き込み失敗時は無出力・exit 0。`session_id` がなければ state を作らず、その呼び出しごとの助言とする。
- **責務境界**: 通知済みは相談済みではない。意味的な相談済み判定はオーケストレーターが行い、hook は transcript を解析しない。

`[Codex Suggestion]` / `[Antigravity Suggestion]` は操作の保留・追加相談を義務付けない。相談済み・軽微・適用外ならそのまま続行でき、採否を毎回説明する義務もない。ただし `[Resolved Routing]` / `agents.<name>.tool` の明示的な routing とスキル固有の委譲義務は維持する。相談時も既存の config 解決（base + `.local.yaml`、解決済み routing の尊重）と CLI ごとの sandbox 安全条件に従う（[ADR-060](../adr/ADR-20261028-060.md)）。

次回監査では researcher の指定 CLI 試行率と、メインの任意提案の採用率を分離する。サブエージェント内の通知は `agent_id` で抑制し、researcher の `agy` 実行は `researcher.md` の指示が担うため、hook 通知数を researcher の遵守率の代用にしない。

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

| 関数                                          | 説明                                                                    |
| --------------------------------------------- | ----------------------------------------------------------------------- |
| `read_hook_input()`                           | stdin から JSON を読み取り dict を返す                                  |
| `get_field(data, key)`                        | dict からフィールドを安全に取得                                         |
| `load_package_config(pkg, file, project_dir)` | パッケージ config を読み込み `.local` があればマージ                    |
| `find_package_config(pkg, file, project_dir)` | パッケージ config パスを解決                                            |
| `deep_merge(base, override)`                  | dict を再帰的にマージ                                                   |
| `read_json_safe(path)`                        | JSON ファイルを安全に読み込み                                           |
| `write_json(path, data)`                      | dict を JSON ファイルに書き出し                                         |
| `append_jsonl(path, record)`                  | dict を JSONL ファイルに追記                                            |
| `find_first_text(node, keys)`                 | ネスト構造から最初の非空文字列を検索                                    |
| `find_first_int(node, keys)`                  | ネスト構造から最初の整数値を検索                                        |
| `ensure_package_path(pkg, subdir)`            | `$AI_ORCHESTRA_DIR/packages/{pkg}/{subdir}` を sys.path に追加          |
| `safe_hook_execution(func)`                   | Hook の main() を安全にラップ（例外時は stderr にログ出力して exit(0)） |
| `try_append_event(...)`                       | 統一イベントログへの追記（失敗しても例外を上げない）                    |

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

### SessionEnd

- `audit-session-end.py`（audit）

### UserPromptSubmit

- `clear-plan-gate.py`（core）
- `audit-prompt.py`（audit）
- `agent-router.py`（agent-routing）

### PreToolUse

- `Agent/Task`: `check-plan-gate.py`, `inject-shared-context.py`
- `Edit/Write`: `check-codex-before-write.py`
- `Read/Grep/Bash`: `check-context-optimization.py`
- `WebSearch/WebFetch`: `suggest-antigravity-research.py`
- `Bash`: `codd-validate-precommit.py`（opt-in、`hooks.validate_on_commit` 既定 `warn`）

### PostToolUse

- `Agent/Task`: `set-plan-gate.py`, `capture-task-result.py`, `check-codex-after-plan.py`
- `Edit/Write`: `post-implementation-review.py`, `lint-on-save.py`, `test-tampering-detector.py`, `test-gate-checker.py`, `update-working-context.py`, `codd-scan-postedit.py`（opt-in、`hooks.scan_on_edit` 既定 `false`）
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
  enabled: false # check-codex-before-write.py の [Codex Suggestion] が抑制される
antigravity:
  enabled: false # suggest-antigravity-research.py の [Antigravity Suggestion] が抑制される
```
