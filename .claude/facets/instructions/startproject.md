# Start Project

**マルチエージェント協調でプロジェクトを開始する。**

> **Note**: CLI のモデル名・オプションは `.claude/config/agent-routing/cli-tools.yaml` で一元管理。
> `.claude/config/agent-routing/cli-tools.local.yaml` が存在する場合はそちらの値を優先する。

## Overview

このスキルはマルチエージェント協調で、プロジェクト開始から実装後レビューまでをカバーする。

### 運用メモ（v2）

- `/preflight` で事前計画済みの場合は、Phase 2 で差分確認を中心に進める
- Phase 6 完了時点で **最小テストゲート**（changed tests + smoke）を通す
- `/tdd` は軽量必須（最低 1 サイクル）。Phase 7 前後で実施する
- セッションが重い場合は `/checkpointing --full` を実行してからレビューへ進む

## Workflow

```
Phase 1: Research (Config-Driven)
    ↓
Phase 2: Requirements & Planning (Claude)
    ↓
Phase 3: Design Review (architect)
    ↓
Phase 4: Task Creation (Claude)
    ↓
Phase 5: Plans.md Update (Claude)
    ↓
Phase 6: Implementation (Implementation Agents via Subagent)
    ↓
Phase 7: Multi-Session Review (code-reviewer / security-reviewer)
```

---

## Agent Routing

各フェーズでエージェントを呼び出す際は、`cli-tools.yaml` の `agents.{name}.tool` を参照してルーティングする。

| `tool` 設定 | 呼び出し方法 |
|-------------|-------------|
| `claude-direct` | `Task(subagent_type="{agent}", prompt="...")` |
| `codex` | `Task(subagent_type="{agent}", prompt="... Codex CLI (workspace-write) で実装すること ...")` |
| `gemini` | `Task(subagent_type="general-purpose", prompt="Gemini CLI で実行: gemini -m <gemini.model> -p '...'")` |

**例**: `agents.frontend-dev.tool` が `codex` なら `Task(subagent_type="frontend-dev", prompt="... Codex CLI で実装すること ...")`。named agent はドメイン固有の知識（コーディング規約、テックスタック）を保持するため、`general-purpose` ではなく直接呼び出す。

---

## Phase 1: Research (Background)

**Task tool でサブエージェントを起動し、`agents.researcher.tool` に従って調査を実行。**

```
Task tool parameters:
- subagent_type: "researcher"
- run_in_background: true
- prompt: |
    Research for: {feature}

    Resolve route from cli-tools.yaml (`agents.researcher.tool`).

    If tool == gemini:
      sandbox 内で gemini を実行する。エラー時は claude-direct にフォールバック。
      Bash timeout: 180000 を指定すること。
      gemini -m <gemini.model> -p "Analyze this repository for: {feature}

       Provide:
       1. Repository structure and architecture
       2. Relevant existing code and patterns
       3. Library recommendations
       4. Technical considerations

       IMPORTANT: Do not ask any clarifying questions. Provide your best answer
       based on the available information. If you need assumptions, state them.
       " --include-directories . < /dev/null 2>/dev/null

      リトライ: タイムアウトや質問検出時は gemini-delegation.md のリトライプロトコルに従う。

    If tool == claude-direct:
      Read/Grep/Glob で同等の調査を実施し、同形式で要約を作成する。

    Save full output to: .claude/docs/research/{feature}.md
    Return CONCISE summary (5-7 bullet points)
```

---

## Phase 2: Requirements Gathering (Claude)

**ユーザーに質問して要件を明確化。**

Ask in Japanese:

1. **目的**: 何を達成したいですか？
2. **スコープ**: 含めるもの・除外するものは？
3. **技術的要件**: 特定のライブラリ、制約は？
4. **成功基準**: 完了の判断基準は？

**Draft implementation plan based on research output + user answers.**

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

## Phase 5: Plans.md Update

**プロジェクト固有のコンテキストを `.claude/Plans.md` に記録する。**

Plans.md が未作成の場合は `/task-state` スキルで初期化するか、以下のフォーマットで作成する。
既に Plans.md が存在する場合は、該当セクションに追記する。

Update `.claude/Plans.md`:

```markdown
# Plans

## Project: {feature}

### Phase 1: {フェーズ名} `cc:TODO`

#### {タスクグループ名}

- `cc:TODO` {タスク}

---

## Decisions

- {YYYY-MM-DD}: {Decision 1} — 理由: {rationale}
- {YYYY-MM-DD}: {Decision 2} — 理由: {rationale}

## Notes

- Goal: {1-2 sentences}
- Key files: {list}
- Dependencies: {list}
- {Important constraints or considerations}
```

**SessionStart はタスク行（`cc:` マーカー付き）のみ自動抽出する。**
Decisions / Notes のコンテキストは Plans.md を直接読むか、`.claude/docs/research/{feature}.md` を参照すること。

---

## Phase 6: Implementation (Implementation Agents)

**タスクリストの各タスクを implementation agents にサブエージェント経由で委譲する。**

> **重要: オーケストレーターは実装コードを直接 Edit/Write しない。**
> タスクリストの管理とサブエージェント呼び出しに専念する。

### 原則

1. **Phase 4 で作成したタスクリストを順に処理する**
2. **各タスクを適切な implementation agent に委譲する**（`frontend-dev`, `backend-python-dev`, `backend-go-dev`, `ai-dev`, `tester` 等）
3. **オーケストレーター自身は Edit/Write で実装コードを書かない** — サブエージェントに全て任せる
4. **implementation agents は `cli-tools.yaml` の `agents.{name}.tool` 設定に従い、自動的に Codex CLI 経由で実装する**

### 実行パターン

```
# 1. タスクリストから次のタスクを取得
# 2. 適切な implementation agent を選定

# ルーティング: agents.{name}.tool の値に基づき、Agent Routing セクションの方法で呼び出す

# 例: Python バックエンド実装
Task(subagent_type="backend-python-dev", prompt="""
タスク: {タスク内容}

コンテキスト:
- プロジェクト: {feature}
- 関連ファイル: {files}
- 設計方針: {design decisions from Phase 3}

IMPORTANT: cli-tools.yaml の設定に従い、Codex CLI (workspace-write) で実装すること。
エラー時は claude-direct にフォールバック。

実装してください。
""")

# 例: フロントエンド実装
Task(subagent_type="frontend-dev", prompt="""
タスク: {タスク内容}

コンテキスト:
- プロジェクト: {feature}
- 関連ファイル: {files}

IMPORTANT: cli-tools.yaml の設定に従い、Codex CLI (workspace-write) で実装すること。
エラー時は claude-direct にフォールバック。

実装してください。
""")

# 例: テスト作成
Task(subagent_type="tester", prompt="""
タスク: {タスク内容}

コンテキスト:
- 関連ファイル: {files}

IMPORTANT: cli-tools.yaml の設定に従い実装すること。

テストを作成してください。
""")
```

### 禁止事項

- オーケストレーターが直接 `Edit` / `Write` で実装コードを変更すること
- 「軽微な変更」を理由にサブエージェント委譲をスキップすること
- implementation agent を経由せずに Codex CLI を直接呼び出すこと（Agent Routing に従う）

### 許容事項

- 設定ファイルの軽微な修正（`.env.example` 等）はオーケストレーターが直接行ってよい
- タスクリストの更新（TaskCreate/TaskUpdate）はオーケストレーターが行う
- サブエージェントの結果確認と次タスクへの遷移判断

### Phase 6 Gate（追加）

実装完了後、最低限以下を確認する:

1. changed tests が成功
2. 主要導線の smoke テストが成功

失敗がある場合は修正して再実行する。
その後 `/tdd {feature}` で最低 1 サイクル（Red → Green → Refactor）を実施する。

---

## Phase 7: Multi-Session Review (Post-Implementation)

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

### 調査結果 (Research)

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
| `.claude/docs/research/{feature}.md` | Research output (config-driven) |
| `.claude/Plans.md`           | Project context & task tracking |

---

## Tips

- **All external CLI / review work through subagents** to preserve main context
- **Agent routing is config-driven** — `cli-tools.yaml` の変更が自動的に反映される
- **Update Plans.md** to persist context across sessions
- **Use multi-session review** for better quality assurance
- **Ctrl+T**: Toggle task list visibility
