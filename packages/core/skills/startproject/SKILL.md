---
name: startproject
description: |
  Start a new project/feature implementation with multi-agent collaboration.
  Includes multi-session review workflow for quality assurance.
metadata:
  short-description: Project kickoff with multi-agent collaboration
---

# Start Project

**マルチエージェント協調でプロジェクトを開始する。**

> **Note**: CLI のモデル名・オプションは `.claude/config/agent-routing/cli-tools.yaml` で一元管理。
> `.claude/config/agent-routing/cli-tools.local.yaml` が存在する場合はそちらの値を優先する。

## Overview

このスキルはマルチエージェント協調で、プロジェクト開始から実装後レビューまでをカバーする。

## Workflow

```
Phase 1: Research (Gemini via Subagent)
    ↓
Phase 2: Requirements & Planning (Claude)
    ↓
Phase 3: Design Review (architect)
    ↓
Phase 4: Task Creation (Claude)
    ↓
Phase 5: CLAUDE.md Update (Claude)
    ↓
[Implementation...]
    ↓
Phase 6: Multi-Session Review (code-reviewer / security-reviewer)
```

---

## Agent Routing

各フェーズでエージェントを呼び出す際は、`cli-tools.yaml` の `agents.{name}.tool` を参照してルーティングする。

| `tool` 設定 | 呼び出し方法 |
|-------------|-------------|
| `claude-direct` | `Task(subagent_type="{agent}", prompt="...")` |
| `codex` | `Task(subagent_type="general-purpose", prompt="Codex CLI で実行: codex exec --model <codex.model> --sandbox <codex.sandbox.*> <codex.flags> '...'")` |
| `gemini` | `Task(subagent_type="general-purpose", prompt="Gemini CLI で実行: gemini -m <gemini.model> -p '...'")` |

**例**: `agents.architect.tool` が `codex` なら general-purpose + Codex CLI、`claude-direct` なら直接 `Task(subagent_type="architect", ...)`。

---

## Phase 1: Gemini Research (Background)

**Task tool でサブエージェントを起動し、Gemini でリポジトリ分析。**

```
Task tool parameters:
- subagent_type: "general-purpose"
- run_in_background: true
- prompt: |
    Research for: {feature}

    1. Call Gemini CLI:
       gemini -p "Analyze this repository for: {feature}

       Provide:
       1. Repository structure and architecture
       2. Relevant existing code and patterns
       3. Library recommendations
       4. Technical considerations
       " --include-directories . 2>/dev/null

    2. Save full output to: .claude/docs/research/{feature}.md

    3. Return CONCISE summary (5-7 bullet points)
```

---

## Phase 2: Requirements Gathering (Claude)

**ユーザーに質問して要件を明確化。**

Ask in Japanese:

1. **目的**: 何を達成したいですか？
2. **スコープ**: 含めるもの・除外するものは？
3. **技術的要件**: 特定のライブラリ、制約は？
4. **成功基準**: 完了の判断基準は？

**Draft implementation plan based on Gemini research + user answers.**

---

## Phase 3: Design Review

**`architect` エージェントで設計レビューを実施。**

> ルーティング: `agents.architect.tool` の値に基づき、Agent Routing セクションの方法で呼び出す。

```
Agent: architect
run_in_background: true
prompt: |
    Review this implementation plan for: {feature}

    Draft plan: {plan from Phase 2}

    Analyze:
    1. Approach assessment - is this the right architecture?
    2. Risk analysis - what could go wrong?
    3. Implementation order - what should be built first?
    4. Improvements - any better approaches?

    Return CONCISE summary:
    - Top 3-5 recommendations
    - Key risks
    - Suggested implementation order
```

---

## Phase 4: Task Creation (Claude)

**サブエージェントの要約を統合し、タスクリストを作成。**

Use TodoWrite to create tasks:

```python
{
    "content": "Implement {specific feature}",
    "activeForm": "Implementing {specific feature}",
    "status": "pending"
}
```

---

## Phase 5: CLAUDE.md Update (IMPORTANT)

**プロジェクト固有の情報を CLAUDE.md に追記する。**

Add to CLAUDE.md:

```markdown
---

## Current Project: {feature}

### Context

- Goal: {1-2 sentences}
- Key files: {list}
- Dependencies: {list}

### Decisions

- {Decision 1}: {rationale}
- {Decision 2}: {rationale}

### Notes

- {Important constraints or considerations}
```

**This ensures context persists across sessions.**

---

## Phase 6: Multi-Session Review (Post-Implementation)

**実装完了後、別セッションでレビューを実施。**

### Option A: New Claude Session

1. Start new Claude Code session
2. Run: `git diff main...HEAD` to see all changes
3. Ask Claude to review the implementation

### Option B: Review Subagent

> ルーティング: 各レビュアーの `agents.{name}.tool` の値に基づき、Agent Routing セクションの方法で呼び出す。
> skill-review-policy.md に従い、変更内容に応じたレビュアーを選定する。

```
# 変更ファイルのパスパターンに基づきレビュアーを選定（skill-review-policy.md 参照）
# 例: code-reviewer + security-reviewer（並列実行）

Agent: code-reviewer
prompt: |
    Review implementation for: {feature}

    Changes: {git diff main...HEAD output}

    Check:
    1. Code quality and patterns
    2. Potential bugs
    3. Missing edge cases

    Report only Critical/High findings.

Agent: security-reviewer
prompt: |
    Security review for: {feature}

    Changes: {git diff main...HEAD output}

    Check:
    1. Security concerns
    2. Input validation
    3. Authentication/authorization

    Report only Critical/High findings.
```

### Why Multi-Session Review?

- **Fresh perspective**: New session has no bias from implementation
- **Different context**: Can focus purely on review, not implementation details
- **Specialized reviewers**: Each reviewer focuses on their domain of expertise

---

## User Confirmation

Present final plan to user (in Japanese):

```markdown
## プロジェクト計画: {feature}

### 調査結果 (Gemini)

{Key findings - 3-5 bullet points}

### 設計方針 (設計レビュー済み)

{Approach with refinements}

### タスクリスト ({N}個)

{Task list}

### リスクと注意点

{From design review}

### 次のステップ

1. この計画で進めてよろしいですか？
2. 実装完了後、別セッションでレビューを行います

---

この計画で進めてよろしいですか？
```

---

## Output Files

| File                         | Purpose                      |
| ---------------------------- | ---------------------------- |
| `docs/research/{feature}.md` | Gemini research output       |
| `CLAUDE.md`                  | Updated with project context |
| Task list (internal)         | Progress tracking            |

---

## Tips

- **All external CLI / review work through subagents** to preserve main context
- **Agent routing is config-driven** — `cli-tools.yaml` の変更が自動的に反映される
- **Update CLAUDE.md** to persist context across sessions
- **Use multi-session review** for better quality assurance
- **Ctrl+T**: Toggle task list visibility
