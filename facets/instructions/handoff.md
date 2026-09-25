# Handoff — Codex CLI タスク引き継ぎ

**Claude Code のレート制限時に、作業中タスクのコンテキストを Codex CLI が引き継げる指示書ファイルを生成する。**

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
python3 .claude/skills/handoff/scripts/handoff.py
```

スクリプトが JSON を stdout に出力する。内容:

- Plans.md の WIP/TODO/blocked タスク
- Plans.md の Project ごとの Goal / Context / Constraints と、その Project に属する WIP / TODO / blocked タスク（`order_markdown` として整形済み。Project が複数あってもタスクは所属 Project の下に出る）
- 未コミット diff のサマリー（`git diff --stat`）
- ブランチ名、最近のコミット
- Decisions セクション

### Step 2: 会話要約の生成

ここが **スキル実行時に Claude が担当する部分**。Step 1 の出力 JSON に加えて、
現在の会話コンテキストから以下を要約する（英語で、500文字以内）:

- 何に取り組んでいたか（What was being worked on）
- どこまで進んだか（Progress so far）
- 次にやるべきこと（What needs to be done next）
- 注意点や判断のコンテキスト（Important context or decisions）

### Step 3: 引き継ぎファイル生成

Step 1 の JSON + Step 2 の要約を組み合わせて、以下のフォーマットで
`.claude/handoffs/{timestamp}.md` に書き出す。

**ファイルは英語で記述する**（Codex への指示は英語ルール準拠）。

```markdown
# Task Handoff

**Generated**: {YYYY-MM-DD HH:MM:SS UTC}
**Branch**: {branch_name}
**Project**: {project directory (absolute path)}

## Conversation Summary

{Step 2 で生成した会話要約}

## Order

### {project name}

#### Goal

- {Goal の行}

#### Context

- {Context の行}

#### Constraints

- {Constraints の行}

#### Tasks

- WIP: {この Project の WIP タスク}
- TODO: {この Project の TODO タスク}
- Blocked: {この Project の blocked タスク} — Reason: {reason}

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
Treat the Order section as the specification: for each task, follow the Goal,
Context and Constraints of the Project it is listed under (Constraints of one
Project do not apply to another Project's tasks); read the files listed under
Context before changing code.

Key files to review:

- .claude/Plans.md — Full task state (update markers as you complete tasks)
- {other relevant files from working context}

Never commit on an integration branch. Before the first commit, run
`git branch --show-current`: if it prints nothing (detached HEAD) or one of
main / master / develop / staging / stage or the repository's default branch, create
a feature branch first (`git switch -c <type>/<short-name>`).
When you complete a task, update its marker in Plans.md from `cc:WIP` to `cc:done`.
When a Phase's Acceptance Criteria are met, check them (`- [ ]` → `- [x]`) only after
you actually ran the `verify:` command and it passed, or confirmed the `judge:` criterion;
never check an unverified criterion. Then commit that task's changes with a descriptive
message. Check `git status --porcelain` first ("Uncommitted Changes" lists tracked
changes only): stage by path (`git add <paths>`) only what belongs to the WIP tasks,
never use `git add -A`, and before each commit review `git diff --cached`; if unrelated
changes are already staged, unstage them (`git restore --staged <path>`) first. Do not stage `.claude/Plans.md` or `.claude/handoffs/` (local working
files, not part of the change). Do not push; Claude Code creates the PR with
`/pr-create` from the committed work.
```

### Step 4: ユーザーへの案内

生成後、以下をユーザーに **日本語で** 表示する:

1. 生成されたファイルのパス
2. Codex 起動コマンド（引き継ぎファイルを新規セッションのプロンプトとして渡す。`-c` は config 上書き用で
   ファイルは渡せない）。`<codex.model>` と `<codex.sandbox.implementation>` は
   `.claude/config/agent-routing/cli-tools.yaml`（+ `.local.yaml`）の実効値で置換して表示する。
   実装のための引き継ぎなので、実効値の `codex.enabled` が `false`、
   `codex.sandbox.implementation` が `workspace-write` でない、または `codex.model` が routing hook と
   同じ安全文字集合 `[A-Za-z0-9_.,:/@+=-]` 以外の文字を含む場合は、起動コマンドを案内せず設定の見直しか
   Claude Code での続行を案内する。引き継ぎファイルとプロジェクトは絶対パスで書き、`-C` で作業
   ディレクトリを固定する（この検証と生成は後続 PR で `handoff.py` に機械化する。ADR-056 §決定 6 の 3）:
   ```
   codex -C '<project absolute path>' --model '<codex.model>' --sandbox '<codex.sandbox.implementation>' "$(cat '<project absolute path>/.claude/handoffs/{timestamp}.md')"
   ```
3. 引き継ぎ内容のサマリー（WIP タスク数、TODO タスク数）

## オプション

| フラグ            | 説明                                                   |
| ----------------- | ------------------------------------------------------ |
| `--message "..."` | Codex への追加指示メッセージを引き継ぎファイルに含める |

## 注意事項

- 引き継ぎファイルは `.claude/handoffs/` に蓄積される（ローカル管理。`.gitignore` に自動追加される（gitignore 同期の対象）。コミットに含めない）
- Plans.md が存在しない場合はエラーメッセージを表示して終了
- diff が大きすぎる場合（100行超）は `--stat` のみに切り詰める
- 機密情報（.env 等）は diff に含めない
