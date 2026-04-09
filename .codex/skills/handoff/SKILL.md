---
name: handoff
description: 'Generate a task handoff file so Codex CLI can continue work when Claude
  Code

  hits rate limits. Use this skill when the user says "handoff", "引き継ぎ",

  "Codex に渡して", "rate limit", "レート制限", "continue in codex",

  or wants to transfer in-progress work to Codex CLI.

  '
metadata:
  short-description: Transfer in-progress tasks to Codex CLI via handoff file
---

# CLI Language Policy

**外部 CLI（Codex CLI / Gemini CLI）と連携するスキルで守るべき共通ルール。**

## 言語プロトコル

| 対象 | 言語 |
|------|------|
| Codex / Gemini への質問 | **英語** |
| Codex / Gemini からの回答 | **英語** |
| ユーザーへの報告 | **日本語** |

## Config-Driven ルーティング

CLI ツールの利用可否と設定は `cli-tools.yaml` で一元管理する。

### 読み込み手順

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `{tool}.enabled` を確認する（`false` なら `claude-direct` にフォールバック）
4. `agents.{name}.tool` で実行先を決定する

### ルーティング規則

| `agents.{name}.tool` | 動作 |
|----------------------|------|
| `codex` | Codex CLI を使用 |
| `gemini` | Gemini CLI を使用 |
| `claude-direct` | 外部 CLI を呼ばず Claude で処理 |
| `auto` | タスク種別に応じて選択（深い推論 → Codex、調査 → Gemini、単純作業 → Claude） |

## サンドボックス実行

外部 CLI（Codex / Gemini）は sandbox 内で直接実行する。
エラー時は `claude-direct` にフォールバックする。

---

# Handoff — Codex CLI タスク引き継ぎ

**Claude Code のレート制限時に、作業中タスクのコンテキストを Codex CLI が引き継げる指示書ファイルを生成する。**

## checkpointing との違い

| | checkpointing | handoff |
|---|---|---|
| 目的 | 過去の作業を記録 | 未完了タスクを Codex に委譲 |
| 視点 | 過去（何をやったか） | 未来（何をやるべきか） |
| 消費者 | 次セッションの Claude/人間 | Codex CLI |

## 使い方

```
/handoff
/handoff --message "Phase 2 の実装を続けて"
```

## ワークフロー

このスキルは以下のステップを **順番に** 実行する。

### Step 1: データ収集（Python スクリプト）

`scripts/handoff.py` を実行して構造化データを収集する:

```bash
python .claude/skills/handoff/scripts/handoff.py
```

スクリプトが JSON を stdout に出力する。内容:
- Plans.md の WIP/TODO/blocked タスク
- 未コミット diff のサマリー（`git diff --stat`）
- ブランチ名、最近のコミット
- Decisions セクション

### Step 2: ��話要約の生成

ここが **スキル実行時に Claude が担当する部分**。Step 1 の出力 JSON に加えて、
現在の会話コンテキストから以下を要約する（英語で、500文字以内）:

- 何に取り組んでいたか（What was being worked on）
- どこまで��んだか（Progress so far）
- 次にやるべきこと（What needs to be done next）
- 注意点や判断のコンテキスト（Important context or decisions）

### Step 3: 引き継ぎファイル生成

Step 1 の JSON + Step 2 の要約を組み合わせ��、以下のフォーマットで
`.claude/handoffs/{timestamp}.md` に書き出す。

**ファイルは英語で記述する**（Codex への指示は英語ルール準拠）。

```markdown
# Task Handoff

**Generated**: {YYYY-MM-DD HH:MM:SS UTC}
**Branch**: {branch_name}
**Project**: {project directory}

## Conversation Summary

{Step 2 で生成した会話要約}

## Current Task State

### In Progress (WIP)
- {task 1}
- {task 2}

### Next Up (TODO)
- {task 1}
- {task 2}

### Blocked
- {task} — Reason: {reason}

## Recent Changes

### Uncommitted Changes
{git diff --stat output}

### Recent Commits
- {hash} {message}

## Design Decisions
- {decision 1}
- {decision 2}

## Instructions for Codex

You are continuing work that was started in Claude Code.
Focus on the WIP tasks listed above. The conversation summary
provides context on what has been done and what remains.

Key files to review:
- .claude/Plans.md — Full task state (update markers as you complete tasks)
- {other relevant files from working context}

When you complete a task, update its marker in Plans.md from `cc:WIP` to `cc:done`.
```

### Step 4: ユーザーへの案内

生成後、以下をユーザーに **日本語で** 表示する:

1. 生成され���ファイルのパス
2. Codex 起動コマンド:
   ```
   codex -c .claude/handoffs/{timestamp}.md
   ```
3. 引き継ぎ内容のサマリー（WIP タスク数、TODO タスク数）

## オプション

| フラグ | 説明 |
|--------|------|
| `--message "..."` | Codex への追加指示メッセージを引き継ぎファイルに含める |

## 注意事項

- 引き継ぎファイルは `.claude/handoffs/` に蓄積される（git 管理推奨）
- Plans.md が存在しない場合はエラーメッセージを表示して終了
- diff が大きすぎる場合（100行超）は `--stat` のみに切り詰める
- 機密情報（.env 等）は diff に含めない
