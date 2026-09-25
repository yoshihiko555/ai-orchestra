---
name: tdd
description:
  Implement features using Test-Driven Development (TDD) with Red-Green-Refactor
  cycle. Use this skill when the user asks to implement something with TDD, write
  tests first, or mentions "tdd", "test-driven", "red green refactor". Also trigger
  when the user says "テスト駆動", "TDDで実装", or wants test-first development.
disable-model-invocation: true
---

# CLI Language Policy

**外部 CLI（Codex CLI / Antigravity CLI）と連携するスキルで守るべき共通ルール。**

## 言語プロトコル

| 対象                           | 言語       |
| ------------------------------ | ---------- |
| Codex / Antigravity への質問   | **英語**   |
| Codex / Antigravity からの回答 | **英語**   |
| ユーザーへの報告               | **日本語** |

## Config-Driven ルーティング

CLI ツールの利用可否と設定は `cli-tools.yaml` で一元管理する。

### 読み込み手順

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `{tool}.enabled` を確認する（`false` なら `claude-direct` にフォールバック）
4. `agents.{name}.tool` で実行先を決定する

### ルーティング規則

| `agents.{name}.tool` | 動作                                                                              |
| -------------------- | --------------------------------------------------------------------------------- |
| `codex`              | Codex CLI を使用                                                                  |
| `antigravity`        | Antigravity CLI（`agy`）を使用（旧値 `gemini` は読み替え）                        |
| `claude-direct`      | 外部 CLI を呼ばず Claude で処理                                                   |
| `auto`               | タスク種別に応じて選択（深い推論 → Codex、調査 → Antigravity、単純作業 → Claude） |

## サンドボックス実行

Antigravity CLI（`agy`）は sandbox 内で直接実行する。
Codex CLI は sandbox 内で動作しないため、base + `.local.yaml` マージ後の実効値で
`codex.requires_sandbox_disable` が `true`（既定値）の場合に限り、呼び出し側で sandbox を
無効化して実行する。`false` に上書きされた環境では sandbox 内で実行する
（安全条件の詳細は `codex-delegation.md` 参照）。
エラー時は `claude-direct` にフォールバックする。

---

# Test-Driven Development

Implement $ARGUMENTS using Test-Driven Development (TDD).

## Phase 0: Setup — Language & Config Resolution

Before writing any code, resolve two things:

### 1. Detect Project Language & Test Framework

Scan the project root for language markers and select the appropriate test framework and runner:

| Marker File                  | Language              | Test Framework   | Run Command                               |
| ---------------------------- | --------------------- | ---------------- | ----------------------------------------- |
| `pyproject.toml`, `setup.py` | Python                | pytest           | Project runner (uv/poetry/pip) + `pytest` |
| `package.json`               | TypeScript/JavaScript | vitest / jest    | `npm test` or `npx vitest` / `npx jest`   |
| `go.mod`                     | Go                    | testing (stdlib) | `go test ./...`                           |
| `Cargo.toml`                 | Rust                  | cargo test       | `cargo test`                              |
| `*.csproj`, `*.sln`          | C#                    | xUnit / NUnit    | `dotnet test`                             |

If multiple markers exist, prefer the one closest to the target module. If the project already has tests, follow the existing test conventions (directory structure, naming, framework).

Store the resolved values mentally as `$LANG`, `$TEST_FRAMEWORK`, `$TEST_CMD` for use throughout.

### 2. Resolve Agent Routing from Config

Read `.claude/config/agent-routing/cli-tools.yaml` (and `.local.yaml` if present) to determine which tool each agent should use. This is mandatory — the TDD skill must respect the project's routing configuration.

Key agents used in TDD:

| Phase                      | Agent                                      | Config Key                  |
| -------------------------- | ------------------------------------------ | --------------------------- |
| Test writing               | `tester`                                   | `agents.tester.tool`        |
| Implementation             | `backend-python-dev`, `frontend-dev`, etc. | `agents.<lang-dev>.tool`    |
| Refactor (apply)           | `$IMPL_AGENT` (same as Implementation)     | `agents.<lang-dev>.tool`    |
| Refactor review (optional) | `code-reviewer`                            | `agents.code-reviewer.tool` |

Select the implementation agent based on `$LANG`:

- Python → `backend-python-dev`
- TypeScript/JavaScript → `frontend-dev`
- Go → `backend-go-dev`
- Other → `general-purpose`

**Routing enforcement rule**: Delegate each phase with `Task(subagent_type="{agent}", prompt="...")` regardless of the `agents.<name>.tool` value. Do NOT write CLI names, sandbox modes, or model names in the prompt: the hook appends `[Resolved Routing]` (tool / sandbox / model, merged from `cli-tools.yaml` and `.local.yaml`) to the subagent prompt and the agent definition follows it. The orchestrator must not write test or implementation code itself in a delegated phase.

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

If refactoring is needed, delegate it to the implementation agent (the same `$IMPL_AGENT` as
Step 2: it has Edit/Write and a `workspace-write` sandbox, unlike `code-reviewer`, whose agent
definition has no Edit/Write tools). The hook's `[Resolved Routing]` decides the tool; do not write CLI names in the prompt:

```
Task(subagent_type="$IMPL_AGENT", prompt="""
Refactor {file} without changing behavior: remove duplication, improve naming and structure
introduced while making the tests pass. Do not add features.
Tests: $TEST_CMD {test file} must stay green. Report what you changed and the test result.
""")
```

If you want an independent opinion on what to refactor first, ask `code-reviewer` (read-only)
and pass its list to `$IMPL_AGENT`. Do not refactor inline in the orchestrator. After the
subagent returns, re-run `$TEST_CMD {test file}` yourself to confirm the tests still pass.

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
