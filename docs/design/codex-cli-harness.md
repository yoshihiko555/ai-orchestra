---
codd:
  node_id: "design:codex-cli-harness"
  kind: design
  status: draft
  depends_on:
    - id: "design:distribution-sync-flow"
      relation: references
  owner: ai-orchestra
---

# Codex CLI Harness Design v0.1

作成日: 2026-07-03  
対象: Codex CLI を主たる利用面とするリポジトリ単位のハーネス設計  
ステータス: Draft

---

## 0. このリポジトリでの採用状況（導入注記）

本ドキュメントは外部ドラフト v0.1 を 2026-07-04 に取り込んだもの。ai-orchestra では本設計をベースに
`packages/codex-harness`（Stage 0〜2）を実装する。実装計画と設計判断は `.claude/Plans.md` を参照。

実機検証（codex-cli 0.142.5、2026-07-04）で判明した本文への補正点:

- **hooks**: 公式仕様確認済みでデフォルト有効。イベントは本文想定の 4 種を含む全 10 種。非対話実行では
  hash ベースの trust モデルにより `--dangerously-bypass-hook-trust` が必要（ハーネス側で SHA-256 検証
  通過時のみ付与する fail-closed 設計とする）。
- **permission profiles**: `[permissions.*]` 記法は有効（0.142.0 以降）だが、`codex exec` に明示指定フラグは
  存在しない。`default_permissions` または `--profile` の config レイヤー切替で代替する。
- **scripts**: 本文の bash 例は、このリポジトリでは pytest 慣習に合わせ Python 実装に読み替える。
- **trust ledger の限界**: trust 検証（`verify_hooks_trust`）は「台帳（`.claude/orchestra.json`
  の `codex_file_hashes`）と実ファイルのハッシュ一致」のみを保証する。台帳自体の正当性は
  git 履歴と人間レビューでのみ担保され、ハーネスはこれを暗号学的に検証しない。セッション内で
  台帳・フック・ルールを改ざんされるリスクは `[permissions.*]` の deny 設定（`.codex/hooks/**`,
  `.codex/rules/**`, `.claude/orchestra.json` 等、§5.5 参照）で緩和するに留まる。署名付き配布や
  改ざん検知の強化は将来課題とする。
- **hooks は `codex exec` で発火しない（0.142.5 実測）**: 正式スキーマの `.codex/hooks.json`・
  インライン TOML・`--enable hooks`・trust 設定・`--dangerously-bypass-hook-trust` のすべてを
  満たしても、exec 経由では SessionStart / UserPromptSubmit / PreToolUse / Stop のいずれも
  発火しなかった。公式ドキュメントに exec での hooks 動作保証の記載はなく、upstream の
  hook dispatch 実装バグ（openai/codex PR #26434 と同系統）の可能性が高い。このため本ハーネスは
  hooks を「対話 TUI 向けの補助層」と位置づけ、**exec 経路の防御は rules + sandbox +
  `codex_run.py` 自身による実行後 validation** で成立させる（§4.5 の deterministic validation は
  Codex の Stop hook に依存しない）。upstream 修正後に hooks 発火を E2E で再検証する。

---

## 1. Executive Summary

本ドキュメントでは、Codex CLI を API プロキシとして包むのではなく、**各リポジトリに配置する repo-local な実行境界・指示・検証・観測の設計パターン**として「Codex CLI Harness」を定義する。

Codex CLI Harness は、Codex CLI 自体を置き換えるものではない。人間が Codex と対話する通常利用は `codex` TUI に任せ、自動実行・評価・再現性が必要な場面では `codex exec` / `codex exec --json` を使う。ハーネスの責務は、Codex の標準機能である `AGENTS.md`、`.codex/config.toml`、permission profiles、rules、hooks、MCP、subagents、JSONL event stream を組み合わせ、プロジェクトごとの安全な運用単位を作ることである。

最初から自作 CLI を作る必要はない。まずは各 repo に `AGENTS.md` と `.codex/` 配下の設定・rules・hooks・validation script を置く。複数 repo へ展開したくなった段階で、任意の bootstrap tool、たとえば `repo-ai-harness init --target codex` のような自作ツールを追加する。

---

## 2. Terminology

### 2.1 Codex CLI

OpenAI の Codex をローカルターミナルから使うための CLI。対話 TUI と非対話実行の両方を持つ。

### 2.2 Harness

本ドキュメントでの Harness は、モデル API や会話 UI の代替ではなく、以下を repo 単位で定義する運用レイヤを指す。

- Codex に渡す project instruction
- Codex が使える filesystem / network / shell / MCP の境界
- 実行前・実行中・実行後の deterministic validation
- Codex 実行ログ、変更差分、検証ログ、最終レポートの保存
- 対話実行と非対話実行の共通運用ルール
- Claude Code など他エージェント向け harness と接続できる抽象化

### 2.3 Repo-local Harness

各リポジトリの中に置かれる Codex 用設定一式。

```text
repo/
  AGENTS.md
  .codex/
    config.toml
    rules/
    hooks.json
    hooks/
    schemas/
    agents/
    prompts/
    reports/
    runs/
```

### 2.4 Global Harness Defaults

`~/.codex/` 配下に置く個人または組織共通の設定。repo-local なハーネスの代替ではなく、全 repo 共通の薄い基盤として扱う。

### 2.5 HarnessInit

Codex CLI に存在する標準コマンドではない。本ドキュメントでは、repo-local harness files を初期配置する任意の bootstrap action を指す。最初は shell script、`just` task、Makefile、テンプレートコピーで十分。複数 repo 展開や drift detection が必要になった段階で自作 CLI に昇格する。

---

## 3. Goals / Non-goals

### 3.1 Goals

1. **Repo ごとに再現可能な Codex 運用を定義する**
   - どの指示が読み込まれるか
   - どの sandbox / permission が使われるか
   - どの検証が実行されるか
   - どのログが保存されるか

2. **Codex の実行境界を明示する**
   - workspace 外の読み書き禁止
   - secret / credential の保護
   - destructive command の禁止
   - network domain の制限
   - 本番環境操作の明示的禁止

3. **対話実行と非対話実行を両立する**
   - 人間との通常作業は `codex` TUI
   - 自動化・CI・レポート生成は `codex exec --json`

4. **結果を機械処理可能にする**
   - JSONL event stream
   - final output schema
   - git diff / patch
   - validation logs
   - run summary

5. **Claude Code Harness との共通抽象に寄せる**
   - agent provider の違いを隠蔽しすぎない
   - 共通化すべきなのは task lifecycle / run record / validation / reporting
   - provider-specific な policy hooks は残す

### 3.2 Non-goals

1. Codex CLI の代替 UI を作ること
2. Responses API ベースの会話プロキシを必須にすること
3. Codex の agent loop を自前実装すること
4. `danger-full-access` を前提にした自動化を行うこと
5. CI から本番 deploy / merge / release まで自動化すること
6. すべての repo に同一設定を強制すること

---

## 4. Design Principles

### 4.1 Repo-first, Global-light

Codex の harness はグローバルに厚く置かない。理由は、開発コマンド、危険操作、検証コマンド、許可 network domain、AGENTS.md の内容が repo ごとに異なるため。

Global は薄くする。

```text
~/.codex/
  config.toml      # 個人 default / provider / profile
  AGENTS.md        # 全 repo 共通の作業姿勢
  rules/           # 全 repo で禁止したい操作
  hooks.json       # 個人または組織で共通の最低限 hooks
```

Repo-local に主戦場を置く。

```text
repo/
  AGENTS.md
  .codex/
    config.toml
    rules/default.rules
    hooks.json
    hooks/*.py
    schemas/*.schema.json
```

### 4.2 Codex-native primitives first

独自ツールを先に作らず、Codex CLI がすでに持っている以下の機構を優先する。

- `AGENTS.md`
- `.codex/config.toml`
- permission profiles
- rules
- hooks
- `codex exec --json`
- `--output-schema`
- MCP tool allowlist
- subagents

### 4.3 Thin wrapper, not proxy

Codex とのやり取りそのものを外部 API プロキシが仲介する設計は避ける。人間の対話は `codex` TUI に残し、ハーネスは以下を担当する。

- 起動前 preflight
- 起動 profile の選択
- project config の配置
- hooks / rules / permission の整備
- 実行後 validation
- run artifacts の保存

### 4.4 Least privilege by default

標準モードは read-only または workspace-write。bypass / full-access は、外部コンテナや一時 worktree など、Codex の外側で強く隔離されている場合に限定する。

### 4.5 Deterministic validation over prompt promises

「テストして」と AGENTS.md に書くだけでは不十分。Stop hook、script、CI、`codex exec --json` の後処理で実際に検証を実行し、ログとして残す。

### 4.6 Observability by default

非対話モードでは、最低限以下を保存する。

- run metadata
- prompt
- config snapshot
- JSONL events
- stderr progress log
- final response
- git diff / patch
- validation result
- summary report

対話モードでも、終了後に git diff と validation report を保存できる状態を目指す。

---

## 5. Codex-native Building Blocks

### 5.1 `codex` interactive TUI

用途:

- 人間が直接 Codex と会話しながら実装する
- approval を見ながら判断する
- diff を確認しながら進める
- サブエージェントや resume を人間が操作する

Harness 側の責務:

- 起動 profile を固定する
- repo が trusted / clean か確認する
- `.codex/` が期待通りか確認する
- 終了後に diff / validation を収集する

例:

```bash
codex --cd . --profile project-safe --ask-for-approval on-request
```

### 5.2 `codex exec`

用途:

- CI / script / scheduled job
- 機械処理可能なレポート生成
- task replay
- review-only job
- failure repair proposal

`codex exec` は TUI を開かず、最終応答を stdout に出す。進捗は stderr に流れる。

例:

```bash
codex exec \
  --sandbox workspace-write \
  --ask-for-approval never \
  "Fix the failing unit tests. Keep the change minimal."
```

### 5.3 `codex exec --json`

`--json` を使うと stdout が JSONL stream になる。Harness はこれを run artifact として保存し、agent message、reasoning、command execution、file change、MCP call、web search、plan update、token usage などを集計する。

例:

```bash
RUN_ID="$(date +%Y%m%d-%H%M%S)"
mkdir -p ".codex/runs/$RUN_ID"

codex exec --json \
  --sandbox workspace-write \
  --output-schema .codex/schemas/task_result.schema.json \
  "Implement the requested change, run relevant validation, and report risks." \
  > ".codex/runs/$RUN_ID/events.jsonl" \
  2> ".codex/runs/$RUN_ID/progress.log"

git diff --binary > ".codex/runs/$RUN_ID/change.patch"
```

### 5.4 `AGENTS.md`

Harness における instruction layer。repo の作業ルール、禁止事項、validation command、出力形式、設計原則を書く。

推奨構成:

```md
# AGENTS.md

## Mission

最小差分で、既存設計を尊重して変更する。

## Required workflow

1. 変更前に関連ファイルを読む。
2. 実装前に短い plan を提示する。
3. 変更後に該当する lint / typecheck / test を実行する。
4. 実行できなかった検証は理由を明記する。

## Do not

- `.env`、秘密鍵、認証情報を読まない・表示しない。
- `git push`、deploy、release、destructive migration を実行しない。
- 依頼範囲外の大規模リファクタリングをしない。

## Validation commands

- npm run lint
- npm run typecheck
- npm test

## Final response format

- Summary
- Files changed
- Validation
- Risks / follow-ups
```

### 5.5 `.codex/config.toml`

Harness の config layer。model、approval、sandbox、permission profile、MCP、hooks、features などを定義する。

最小例:

```toml
model = "gpt-5.5"
# 対話 codex は on-failure（sandbox 拒否時に承認要求）。詳細は §10.2。
# 非対話 runner は -c approval_policy=never で上書きし厳格運用する。
approval_policy = "on-failure"
sandbox_mode = "workspace-write"
model_reasoning_effort = "high"

[features]
hooks = true

[shell_environment_policy]
include_only = ["PATH", "HOME", "SHELL", "TMPDIR", "LANG", "LC_ALL"]
```

現行の `codex-harness` は repo-local permission profile を配布しない。
過去に配布していた `project-edit` profile は、git worktree / GitHub CLI / network の
通常ワークフローを阻害したため、同期時に legacy generated shape と一致する場合だけ
削除する（Issue #161 フォローアップ）。sandbox / approval posture はユーザーの
Codex config に委譲し、ハーネス固有の制御は hooks / rules / trust ledger で担う。

注: Codex の permission profile / sandbox / config syntax は変化し得るため、導入時点の `codex doctor`、公式 docs、実際の CLI version で確認する。

### 5.6 Rules

Sandbox 外で実行しようとする shell command に対して、allow / prompt / forbidden を設定する policy layer。

例:

```python
# .codex/rules/default.rules

prefix_rule(
    pattern = ["git", "status"],
    decision = "allow",
    match = ["git status"],
)

prefix_rule(
    pattern = ["git", "diff"],
    decision = "allow",
    match = ["git diff"],
)

prefix_rule(
    pattern = ["git", "push"],
    decision = "prompt",
    justification = "Pushing a branch is allowed only after explicit human approval.",
)

prefix_rule(
    pattern = ["gh", "pr", "merge"],
    decision = "forbidden",
    justification = "PR merge is outside Codex harness scope.",
)

prefix_rule(
    pattern = ["rm", "-rf"],
    decision = "forbidden",
    justification = "Destructive recursive deletion is blocked by default.",
)
```

Rules は deterministic な command policy として扱い、複雑な文脈判断は hooks に逃がす。

### 5.7 Hooks

Codex lifecycle に deterministic script を差し込む拡張点。Harness では以下に使う。

- prompt secret scan
- dangerous command check
- command execution logging
- validation enforcement
- final report generation

推奨 hook use:

```text
UserPromptSubmit:
  - prompt に秘密情報が含まれていないか検査
  - 本番 DB / deploy / key rotation など危険 intent を検出

PreToolUse:
  - Bash / apply_patch / MCP tool call の検査
  - rules では表現しづらい条件付き policy を補完

PostToolUse:
  - command result / duration / exit code / stderr size を記録
  - test failure を分類

Stop:
  - git diff を収集
  - lint / typecheck / test / secret scan を実行
  - report を生成
```

hooks.json 例:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 .codex/hooks/user_prompt_secret_scan.py",
            "timeout": 10,
            "statusMessage": "Scanning prompt"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "^Bash$",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .codex/hooks/pre_tool_use_policy.py",
            "timeout": 30,
            "statusMessage": "Checking command policy"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash .codex/hooks/stop_validate.sh",
            "timeout": 120,
            "statusMessage": "Running validation"
          }
        ]
      }
    ]
  }
}
```

注意:

- Hooks は policy enforcement の補助であり、完全な sandbox 境界ではない。
- Project-local hooks は trust model の対象になる。
- チームで強制したい hooks は、将来的には managed hooks に寄せる。

### 5.8 MCP

Codex に外部 context / tool を与える仕組み。Harness では最小権限で使う。

推奨方針:

- documentation lookup は許可しやすい
- issue tracker / design docs は read-only から始める
- write capability は個別 approval 必須
- production system / secret manager / deploy は原則禁止
- `enabled_tools` / `disabled_tools` を明示する

例:

```toml
[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
enabled = true
required = false
enabled_tools = ["resolve-library-id", "get-library-docs"]
default_tools_approval_mode = "prompt"
```

### 5.9 Subagents

複雑な探索・レビュー・観点分離に使う。実装を並列編集させるより、read-heavy / review-heavy な用途から導入する。

推奨用途:

- PR review: security / correctness / test / maintainability
- 大規模 repo の探索
- migration impact analysis
- generated diff の独立レビュー

非推奨用途:

- 複数 subagent に同じファイルを同時編集させる
- approval が必要な操作を非対話で多用する
- max depth / thread を無制限にする

---

## 6. Reference Architecture

```text
Human / CI / Script
  │
  ▼
Harness Entry Layer
  ├─ scripts/codex-tui.sh
  ├─ scripts/codex-run.sh
  ├─ scripts/codex-review.sh
  └─ optional repo-ai-harness CLI
  │
  ▼
Codex Native Layer
  ├─ codex interactive TUI
  ├─ codex exec
  ├─ codex exec --json
  └─ codex resume / fork / doctor
  │
  ▼
Repo Policy Layer
  ├─ AGENTS.md
  ├─ .codex/config.toml
  ├─ .codex/rules/*.rules
  ├─ .codex/hooks.json
  ├─ .codex/hooks/*
  ├─ .codex/schemas/*
  └─ .codex/agents/*
  │
  ▼
Validation / Observation Layer
  ├─ git diff / patch
  ├─ lint / typecheck / test
  ├─ secret scan
  ├─ JSONL event parser
  ├─ run summary
  └─ risk report
```

---

## 7. Repository Layout

推奨 layout:

```text
repo/
  AGENTS.md
  .codex/
    config.toml
    hooks.json
    rules/
      default.rules
    hooks/
      user_prompt_secret_scan.py
      pre_tool_use_policy.py
      post_tool_use_log.py
      stop_validate.sh
    schemas/
      task_result.schema.json
      review_result.schema.json
    prompts/
      implementation.md
      review.md
      triage.md
    agents/
      reviewer.config.toml
      explorer.config.toml
    runs/
      .gitignore
    reports/
      .gitignore
  scripts/
    codex-tui.sh
    codex-run.sh
    codex-review.sh
    codex-report.sh
  justfile
```

### 7.1 Commit するもの

```text
AGENTS.md
.codex/config.toml
.codex/hooks.json
.codex/rules/*.rules
.codex/hooks/*.py
.codex/hooks/*.sh
.codex/schemas/*.json
.codex/prompts/*.md
.codex/agents/*.toml
scripts/codex-*.sh
justfile
```

### 7.2 Commit しないもの

```text
.codex/runs/*
.codex/reports/*
.codex/tmp/*
*.patch generated by a local run
local secrets / credentials / auth files
```

`.codex/runs/.gitignore`:

```gitignore
*
!.gitignore
```

`.codex/reports/.gitignore`:

```gitignore
*
!.gitignore
```

---

## 8. Operating Modes

### 8.1 Interactive implementation mode

人間が Codex と対話して実装する通常モード。

```bash
./scripts/codex-tui.sh
```

責務:

- preflight で git status / branch / config を確認
- `codex --cd . --profile project-safe --ask-for-approval on-request` を起動
- 終了後に `git diff --stat` と validation suggestion を出す

### 8.2 Non-interactive task mode

明確な task prompt を `codex exec --json` に渡し、artifact を保存する。

```bash
TASK="Fix failing tests in packages/foo"
./scripts/codex-run.sh "$TASK"
```

責務:

- run id を作る
- events.jsonl / progress.log / final.json を保存
- patch を保存
- validation を実行
- report を生成

### 8.3 Review mode

現在 branch と base branch の差分を読み、read-only でレビューする。

```bash
./scripts/codex-review.sh main
```

責務:

- `git diff main...HEAD` を context として渡す
- sandbox は read-only
- final output は review schema に合わせる
- 指摘は severity / file / rationale / suggested fix で構造化する

### 8.4 CI repair proposal mode

CI 失敗時に Codex が修正 patch を提案する。ただし merge / push / deploy はしない。

責務:

- checkout は read credentials 最小
- Codex API key を repo-controlled code に露出しない
- patch artifact のみ生成
- PR 作成は別 job / human-controlled flow

### 8.5 Triage / exploration mode

大規模 repo の調査や影響範囲分析。基本は read-only。subagent を使ってもよい。

---

## 9. Run Lifecycle

### 9.1 Lifecycle Overview

```text
1. Preflight
   - git status
   - repo root detection
   - Codex version / doctor
   - .codex/ files existence
   - current branch / base branch

2. Prompt assembly
   - task prompt
   - optional prompt template
   - optional piped context
   - output schema

3. Codex execution
   - codex TUI or codex exec
   - sandbox / approval / profile selection
   - JSONL capture for exec mode

4. Artifact collection
   - events.jsonl
   - progress.log
   - final response
   - git diff
   - changed files
   - validation logs

5. Validation
   - lint
   - typecheck
   - unit test
   - secret scan
   - policy checks

6. Report
   - summary
   - changed files
   - validation result
   - risks
   - follow-ups
```

### 9.2 Run artifact model

```text
.codex/runs/<run_id>/
  metadata.json
  prompt.md
  events.jsonl
  progress.log
  final.json
  git-status.before.txt
  git-status.after.txt
  diff.patch
  diff-stat.txt
  validation.log
  report.md
```

`metadata.json` example:

```json
{
  "run_id": "20260703-153000-fix-tests",
  "agent_provider": "codex-cli",
  "mode": "noninteractive",
  "repo_root": "/path/to/repo",
  "base_ref": "main",
  "head_ref": "feature/foo",
  "model": "gpt-5.5",
  "profile": "project-safe",
  "sandbox": "workspace-write",
  "approval_policy": "never",
  "started_at": "2026-07-03T15:30:00+09:00"
}
```

### 9.3 Final output schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "summary", "files_changed", "validation", "risks"],
  "properties": {
    "status": {
      "type": "string",
      "enum": ["success", "partial", "failed"]
    },
    "summary": {
      "type": "string"
    },
    "files_changed": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["path", "change_type", "notes"],
        "properties": {
          "path": { "type": "string" },
          "change_type": { "type": "string" },
          "notes": { "type": "string" }
        }
      }
    },
    "validation": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["command", "status", "summary"],
        "properties": {
          "command": { "type": "string" },
          "status": {
            "type": "string",
            "enum": ["passed", "failed", "skipped"]
          },
          "summary": { "type": "string" }
        }
      }
    },
    "risks": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["severity", "description", "mitigation"],
        "properties": {
          "severity": { "type": "string", "enum": ["low", "medium", "high"] },
          "description": { "type": "string" },
          "mitigation": { "type": "string" }
        }
      }
    }
  }
}
```

---

## 10. Policy Model

### 10.1 Layers

```text
Policy =
  AGENTS.md instructions
  + config.toml defaults
  + permission profiles
  + rules
  + hooks
  + validation scripts
  + CI rules
  + human approvals
```

### 10.2 Permission defaults

推奨:

```text
read-only:
  - review
  - triage
  - impact analysis
  - security audit

workspace-write:
  - local implementation
  - test fixing
  - refactoring inside repo

full access / yolo:
  - 原則使わない
  - 使う場合は外部コンテナ / disposable VM / throwaway worktree 内のみ
```

#### 対話 vs 非対話の承認方針（Issue #161 フォローアップ）

対話 `codex` と非対話 runner（`codex exec`）で承認の扱いを分ける。

| 実行形態                                            | approval_policy            | filesystem / network の扱い                                                                        |
| --------------------------------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------- |
| 対話 `codex`（`.codex/config.toml` 既定）           | `on-failure`               | sandbox が拒否した操作を「sandbox 外で実行してよいか」人間へ承認要求し、承認時に実行（escalation） |
| 非対話 runner（`codex_run.py` / `codex_review.py`） | `never`（`-c` で明示固定） | 承認エスカレーションなし。read-only / workspace-write の sandbox 境界を厳格に維持する              |

これにより、次の2つを事前に許可を広げずに対話で通せる（いずれも人間承認が前提）:

- **worktree の Git 操作**: git worktree の実体 Git dir（`<repo>/.git/worktrees/<name>`）は workspace root の外にあり、workspace-write では書き込めない。そのため `git add` / `git commit` は sandbox で失敗するが、`on-failure` により承認を経て実行される。config に機器固有の絶対パスを writable root として埋め込まない方針。
- **ネットワーク**: workspace-write は network 既定オフのため `gh` / `git fetch` 等は sandbox で失敗するが、同様に承認を経て実行される。network を profile で恒常的に開放しない。

非対話 runner は人間が承認ゲートになれないため、上記いずれも通さず（`approval_policy=never`）、生成する artifact（patch / findings）の範囲に実行を限定する。

### 10.3 Command policy

常に禁止（`forbidden`。承認しても実行不可。rules・PreToolUse hook 双方で強制）:

```text
gh pr merge
gh release create
npm publish
pnpm publish
docker push
kubectl apply
terraform apply
rm -rf /
rm -rf ~
chmod -R 777
curl ... | sh
wget ... | sh
```

原則 prompt（対話 Codex で人間承認時のみ実行。plain 形のみ対象）:

```text
git push
gh pr create
gh issue comment
git commit
npm install
pnpm add
pip install
brew install
docker build
```

> Issue #161 フォローアップ: `git push` は「常に禁止」から「prompt（人間承認付き許可）」へ変更した。
> 対話 Codex は人間が承認ゲートになるため、plain な branch push / PR 作成はハードブロックせず承認で通す。
> 一方 merge / release / publish / cluster/infra 適用 / 破壊的削除は不可逆・本番影響のため forbidden を維持する。
> force-push と option 挿入で native prefix rule を迂回する形は、PreToolUse hook（`pre_tool_use_policy.py`）で hard block する。

許可しやすい:

```text
git status
git diff
git log
ls
cat project files
npm test
npm run lint
npm run typecheck
```

### 10.4 Secret policy

保護対象:

```text
.env
*.env
.env.*
*.pem
*.key
id_rsa
id_ed25519
.aws/
.gcp/
.azure/
.ssh/
```

Hook / validation で検出する文字列例:

```text
OPENAI_API_KEY
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
GITHUB_TOKEN
ghp_
sk-
-----BEGIN PRIVATE KEY-----
```

---

## 11. Scripts

### 11.1 `scripts/codex-tui.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

echo "[codex-harness] repo: $ROOT"
echo "[codex-harness] branch: $(git branch --show-current)"
echo "[codex-harness] status:"
git status --short

if [[ ! -f AGENTS.md ]]; then
  echo "[codex-harness] warning: AGENTS.md not found"
fi

if [[ ! -f .codex/config.toml ]]; then
  echo "[codex-harness] warning: .codex/config.toml not found"
fi

codex --cd "$ROOT" --profile project-safe --ask-for-approval on-request

echo "[codex-harness] diff summary after session:"
git diff --stat || true
```

### 11.2 `scripts/codex-run.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

TASK="${1:?usage: scripts/codex-run.sh '<task>'}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

RUN_ID="$(date +%Y%m%d-%H%M%S)"
RUN_DIR=".codex/runs/$RUN_ID"
mkdir -p "$RUN_DIR"

cat > "$RUN_DIR/prompt.md" <<EOF_PROMPT
$TASK
EOF_PROMPT

git status --porcelain > "$RUN_DIR/git-status.before.txt"

codex exec --json \
  --sandbox workspace-write \
  --ask-for-approval never \
  --output-schema .codex/schemas/task_result.schema.json \
  "$TASK" \
  > "$RUN_DIR/events.jsonl" \
  2> "$RUN_DIR/progress.log"

git status --porcelain > "$RUN_DIR/git-status.after.txt"
git diff --stat > "$RUN_DIR/diff-stat.txt" || true
git diff --binary > "$RUN_DIR/diff.patch" || true

bash .codex/hooks/stop_validate.sh > "$RUN_DIR/validation.log" 2>&1 || true

python3 .codex/hooks/parse_events.py "$RUN_DIR/events.jsonl" > "$RUN_DIR/report.md" || true

echo "[codex-harness] run artifacts: $RUN_DIR"
```

### 11.3 `scripts/codex-review.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE="${1:-main}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

RUN_ID="$(date +%Y%m%d-%H%M%S)-review"
RUN_DIR=".codex/runs/$RUN_ID"
mkdir -p "$RUN_DIR"

git diff "$BASE...HEAD" > "$RUN_DIR/input.diff"

codex exec --json \
  --sandbox read-only \
  --ask-for-approval never \
  --output-schema .codex/schemas/review_result.schema.json \
  "Review the diff in stdin. Focus on correctness, security, test coverage, and maintainability. Return only structured findings." \
  < "$RUN_DIR/input.diff" \
  > "$RUN_DIR/events.jsonl" \
  2> "$RUN_DIR/progress.log"

echo "[codex-harness] review artifacts: $RUN_DIR"
```

---

## 12. Claude Code Harness Compatibility

### 12.1 Common abstraction

Provider-neutral harness interface:

```text
AgentHarnessProvider
  init(repo)
  run(task, mode, profile)
  review(base_ref, head_ref)
  collect(run_id)
  validate(run_id)
  report(run_id)
```

Common `RunRecord`:

```json
{
  "run_id": "string",
  "provider": "codex-cli | claude-code | other",
  "repo": "string",
  "mode": "interactive | noninteractive | review | ci-repair",
  "task": "string",
  "base_ref": "string",
  "head_ref": "string",
  "policy": {
    "sandbox": "string",
    "approval": "string",
    "network": "string"
  },
  "artifacts": {
    "events": "path",
    "transcript": "path",
    "patch": "path",
    "validation": "path",
    "report": "path"
  },
  "result": {
    "status": "success | partial | failed",
    "summary": "string",
    "risks": []
  }
}
```

### 12.2 Do not over-unify provider internals

Codex と Claude Code で同じにすべきもの:

- repo-local policy の考え方
- run lifecycle
- artifact naming
- validation commands
- final report schema
- CI integration pattern

同じにしないほうがよいもの:

- hooks のイベント名
- permission / sandbox の具体 syntax
- transcript format
- subagent の起動方法
- rules DSL
- CLI flags

Provider-specific adapter を切る。

```text
harness-core/
  run_record.py
  validation.py
  report.py
  policy_model.py

providers/
  codex_cli/
    init_template/
    run.py
    parse_jsonl.py
    config_model.py
  claude_code/
    init_template/
    run.py
    parse_transcript.py
    config_model.py
```

---

## 13. HarnessInit Design

### 13.1 Phase 1: Manual template

最初は template directory を repo にコピーするだけ。

```bash
cp -R templates/codex/.codex ./
cp templates/codex/AGENTS.md ./AGENTS.md
```

### 13.2 Phase 2: Repo-local script

```bash
./scripts/setup-codex-harness.sh
```

実行内容:

- `.codex/` がなければ作成
- `AGENTS.md` がなければ作成
- 既存ファイルは上書きしない
- diff を表示する
- `.gitignore` に runs / reports を追加

### 13.3 Phase 3: Generic CLI

複数 repo 展開が必要になったら任意の自作 CLI を作る。

```bash
repo-ai-harness init --target codex
repo-ai-harness check --target codex
repo-ai-harness report --run .codex/runs/20260703-153000
repo-ai-harness doctor
```

`init` の仕様:

```text
input:
  - target provider: codex
  - repo root
  - language/runtime profile: node | python | rust | mixed
  - safety profile: strict | standard | permissive

output:
  - AGENTS.md
  - .codex/config.toml
  - .codex/rules/default.rules
  - .codex/hooks.json
  - .codex/hooks/*
  - .codex/schemas/*
  - scripts/codex-*.sh

safety:
  - default no overwrite
  - show diff before write
  - support --dry-run
  - support --force only with explicit flag
```

---

## 14. Adoption Plan

### Stage 0: One repo experiment

- Add `AGENTS.md`
- Add `.codex/config.toml`
- Add minimal rules
- Add Stop validation script
- Use `codex` TUI normally
- Manually run validation after sessions

Success criteria:

- Codex reads expected instructions
- Dangerous command prompts / blocks work
- Validation commands are known and documented

### Stage 1: Non-interactive runs

- Add `scripts/codex-run.sh`
- Save JSONL events
- Save patch / diff / validation
- Add final output schema

Success criteria:

- One task can be replayed
- Run artifacts are preserved
- `status`, `validation`, `risks` are machine-readable

### Stage 2: Review harness

- Add review schema
- Add read-only review script
- Use subagents only for review / exploration

Success criteria:

- Review output is structured
- Findings include file / severity / rationale
- No write operations happen in review mode

### Stage 3: Multi-repo template

- Extract common template
- Add setup script
- Define drift check
- Map Claude Code harness concepts to shared RunRecord

Success criteria:

- New repo setup takes minutes, not hours
- Repo-specific overrides are small
- Provider-specific code is isolated

### Stage 4: CI integration

- Use `codex exec` in CI only for review or patch proposal
- Never auto-merge
- Never expose sensitive tokens to repo-controlled code
- Store patch artifact

Success criteria:

- CI can generate useful proposal patches
- Human can inspect patch and logs
- No credentials are exposed to Codex-run code

---

## 15. Readiness Checklist

A repo is Codex-harness-ready when all of the following are true.

```text
[ ] AGENTS.md exists and documents workflow, validation, and do-not rules
[ ] .codex/config.toml exists
[ ] default sandbox / approval / permission profile is explicit
[ ] .codex/rules/default.rules exists
[ ] destructive commands are forbidden
[ ] project-local hooks are reviewed and trusted
[ ] Stop validation exists
[ ] secret scan exists or is delegated to CI
[ ] codex-run script captures JSONL events
[ ] output schema exists for non-interactive runs
[ ] .codex/runs and .codex/reports are gitignored
[ ] review mode is read-only
[ ] MCP tools are allowlisted, not broadly enabled
[ ] CI mode cannot deploy, merge, publish, or push without human-controlled job
[ ] Claude Code compatibility is expressed through RunRecord, not through forced identical internals
```

---

## 16. Known Risks / Open Questions

### 16.1 Codex config surface may evolve

Codex CLI, permission profiles, rules, hooks, and subagents are active surfaces. Some are explicitly experimental or may change. Version pinning and `codex doctor` checks should be part of the harness.

### 16.2 Hooks are not a complete security boundary

Hooks are useful for guardrails and deterministic validation, but they should not replace OS sandboxing, container isolation, code review, or CI policy.

### 16.3 JSONL event schema should be treated as versioned input

The harness should parse known event types but tolerate unknown ones. Parser failures should not delete run artifacts.

### 16.4 Repo-local policy can drift

When multiple repositories copy the same template, changes drift over time. A future `repo-ai-harness check` should compare repo files against template version and report drift.

### 16.5 MCP expands the attack surface

MCP tools should start read-only and require explicit allowlists. Write-capable tools should have prompt approval and audit logs.

---

## 17. Recommended Initial Implementation

Start with this minimal repo-local harness.

```text
repo/
  AGENTS.md
  .codex/
    config.toml
    hooks.json
    rules/
      default.rules
    hooks/
      user_prompt_secret_scan.py
      pre_tool_use_policy.py
      stop_validate.sh
    schemas/
      task_result.schema.json
    runs/
      .gitignore
    reports/
      .gitignore
  scripts/
    codex-tui.sh
    codex-run.sh
    codex-review.sh
```

Avoid building a full custom CLI until at least three repositories need the same template. Before that, a repo-local script and a copied template are cheaper and safer.

---

## 18. Source References

Official Codex documentation consulted on 2026-07-03:

- https://developers.openai.com/codex/cli
- https://developers.openai.com/codex/cli/reference
- https://developers.openai.com/codex/noninteractive
- https://developers.openai.com/codex/guides/agents-md
- https://developers.openai.com/codex/config-basic
- https://developers.openai.com/codex/permissions
- https://developers.openai.com/codex/rules
- https://developers.openai.com/codex/hooks
- https://developers.openai.com/codex/mcp
- https://developers.openai.com/codex/subagents
