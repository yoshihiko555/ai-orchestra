---
name: tdd
description: Implement features using Test-Driven Development (TDD) with Red-Green-Refactor
  cycle. Use this skill when the user asks to implement something with TDD, write
  tests first, or mentions "tdd", "test-driven", "red green refactor". Also trigger
  when the user says "テスト駆動", "TDDで実装", or wants test-first development.
disable-model-invocation: true
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

# Test-Driven Development

Implement $ARGUMENTS using Test-Driven Development (TDD).

## Phase 0: Setup — Language & Config Resolution

Before writing any code, resolve two things:

### 1. Detect Project Language & Test Framework

Scan the project root for language markers and select the appropriate test framework and runner:

| Marker File | Language | Test Framework | Run Command |
|-------------|----------|---------------|-------------|
| `pyproject.toml`, `setup.py` | Python | pytest | Project runner (uv/poetry/pip) + `pytest` |
| `package.json` | TypeScript/JavaScript | vitest / jest | `npm test` or `npx vitest` / `npx jest` |
| `go.mod` | Go | testing (stdlib) | `go test ./...` |
| `Cargo.toml` | Rust | cargo test | `cargo test` |
| `*.csproj`, `*.sln` | C# | xUnit / NUnit | `dotnet test` |

If multiple markers exist, prefer the one closest to the target module. If the project already has tests, follow the existing test conventions (directory structure, naming, framework).

Store the resolved values mentally as `$LANG`, `$TEST_FRAMEWORK`, `$TEST_CMD` for use throughout.

### 2. Resolve Agent Routing from Config

Read `.claude/config/agent-routing/cli-tools.yaml` (and `.local.yaml` if present) to determine which tool each agent should use. This is mandatory — the TDD skill must respect the project's routing configuration.

Key agents used in TDD:

| Phase | Agent | Config Key | Typical Routing |
|-------|-------|-----------|----------------|
| Test writing | `tester` | `agents.tester.tool` | codex |
| Implementation | `backend-python-dev`, `frontend-dev`, etc. | `agents.<lang-dev>.tool` | codex |
| Refactor review | `code-reviewer` | `agents.code-reviewer.tool` | claude-direct |

Select the implementation agent based on `$LANG`:
- Python → `backend-python-dev`
- TypeScript/JavaScript → `frontend-dev`
- Go → `backend-go-dev`
- Other → `general-purpose`

**Routing enforcement rule**: If `agents.<name>.tool` is `codex`, the work MUST be delegated to a subagent that executes via Codex CLI. Do NOT write code directly with Edit/Write when the config says `codex`. If `codex.enabled: false`, fall back to `claude-direct` (subagent without Codex).

---

## Phase 1: Test Design

1. **Confirm Requirements**
   - What is the input
   - What is the output
   - What are the edge cases

2. **List Test Cases**
   ```
   - [ ] Happy path: Basic functionality
   - [ ] Happy path: Boundary values
   - [ ] Error case: Invalid input
   - [ ] Error case: Error handling
   ```

Present the test case list to the user for confirmation before proceeding.

---

## Phase 2: Red-Green-Refactor

Repeat the following cycle for each test case.

### Step 1: Write Failing Test (Red)

Delegate to the `tester` agent per config routing:

```
Task(subagent_type="tester", prompt="""
Write a failing test for: {test case description}

Target module: {module path}
Test file: {test file path}
Test framework: $TEST_FRAMEWORK
Language: $LANG

Write ONLY the test — do not implement the production code.
The test must fail when run (Red phase of TDD).

After writing, run: $TEST_CMD {test file}
Confirm the test FAILS and report the failure message.
""")
```

### Step 2: Minimal Implementation (Green)

Delegate to the implementation agent per config routing:

```
Task(subagent_type="$IMPL_AGENT", prompt="""
Make this failing test pass with MINIMAL code:

Test file: {test file path}
Target module: {module path}

Rules:
- Write the minimum code to make the test pass
- Don't aim for perfection — hardcoding is OK at this stage
- Don't implement anything beyond what the test requires

After writing, run: $TEST_CMD {test file}
Confirm the test PASSES and report the result.
""")
```

### Step 3: Refactor

After Green, assess whether refactoring is needed. If the code is already clean, skip to the next test.

If refactoring is needed, the approach depends on `agents.code-reviewer.tool`:

- **claude-direct**: Review and refactor inline (no subagent needed)
- **codex**: Delegate refactoring to a subagent via Codex

```
# Review what to refactor
# Then apply changes while ensuring tests still pass:
$TEST_CMD {test file}
```

Refactoring targets:
- Remove duplication
- Improve naming
- Simplify structure
- Extract functions if needed

### Step 4: Next Test

Return to Step 1 with the next test case from the list.

---

## Phase 3: Completion Check

Run the full test suite and check coverage:

```bash
# Full test suite
$TEST_CMD

# Coverage (if available for $LANG)
# Python: pytest --cov={module} --cov-report=term-missing
# JS/TS: npx vitest --coverage / npx jest --coverage
# Go: go test -cover ./...
```

Target: 80%+ line coverage on the new module.

---

## Report Format

```markdown
## TDD Complete: {Feature Name}

### Environment
- Language: $LANG
- Test Framework: $TEST_FRAMEWORK
- Routing: tester=$TESTER_TOOL, impl=$IMPL_TOOL

### Test Cases
- [x] {test1}: {description}
- [x] {test2}: {description}
...

### Coverage
{Coverage report}

### Implementation Files
- `{source file}`: {description}
- `{test file}`: {N} tests
```

---

## Key Principles

- Write tests **first** — never write production code without a failing test
- Keep each Red-Green-Refactor cycle **small** — one behavior per cycle
- Refactor **only after** tests pass
- Respect `cli-tools.yaml` routing — if config says Codex, use Codex via subagent
- Adapt to the project's language and test conventions, don't force a specific framework

### Integration Notes

- After `startproject`, run at least 1 full TDD cycle (Red → Green → Refactor)
- When used with `issue-fix`, the test case list should be derived from the issue's acceptance criteria
