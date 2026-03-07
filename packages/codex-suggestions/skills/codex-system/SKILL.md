---
name: codex-system
description: |
  Use Codex CLI with config-driven routing.
  Always resolve behavior from `.claude/config/agent-routing/cli-tools.yaml`
  (`codex.enabled`, `agents.<name>.tool`, model/sandbox/flags), instead of
  hardcoding Codex as design-only or implementation-only.
  Explicit triggers: "consult codex", "codexで", "think deeper", "analyze".
metadata:
  short-description: Claude Code ↔ Codex CLI collaboration (config-driven)
---

# Codex System — Config-Driven Integration

**Codex の役割は固定しない。`cli-tools.yaml` を SSOT として解決する。**

> **Note**: CLI のモデル名・オプションは `.claude/config/agent-routing/cli-tools.yaml` で一元管理。
> `.claude/config/agent-routing/cli-tools.local.yaml` が存在する場合はそちらの値を優先する。

> **詳細ルール**: `.claude/rules/codex-delegation.md`, `.claude/rules/config-loading.md`

## Source of Truth (MUST)

1. `.claude/config/agent-routing/cli-tools.yaml` を読む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用
3. `codex.enabled` と `agents.<target-agent>.tool` で実行先を解決
4. Codex を呼ぶ場合のみ `codex.model` / `codex.sandbox.*` / `codex.flags` を展開
5. `codex.enabled: false` の場合は Codex 呼び出しを行わず、設定されたフォールバックに従う

## Routing Rules

| 条件 | 動作 |
|------|------|
| `agents.<target>.tool == "codex"` | Codex CLI を使用（analysis / implementation を用途で選択） |
| `agents.<target>.tool == "claude-direct"` | Codex を強制しない |
| `agents.<target>.tool == "gemini"` | Gemini を使用 |
| `agents.<target>.tool == "auto"` | タスク特性で選択（深い推論・デバッグ・比較・レビューは Codex 候補） |

**重要**: 「Codex は設計専用」「Codex は実装専用」などの固定役割を前提にしない。  
役割は `cli-tools.yaml` の変更で切り替わる。

## When to Consult Codex

- ユーザーが明示的に Codex 利用を指示したとき
- ルーティング解決結果が `tool: codex` のとき
- `tool: auto` で深い推論が必要な分析（設計・デバッグ・比較検討・レビュー）を行うとき

## How to Consult

### Subagent Pattern (推奨)

**Use Task tool with `subagent_type='general-purpose'` to preserve main context.**

```
Task tool parameters:
- subagent_type: "general-purpose"
- run_in_background: true (optional)
- prompt: |
    Resolve target agent/tool from cli-tools.yaml first.
    If tool resolves to codex, run:

    まず sandbox 内で codex を実行する。sandbox エラーが出たら
    dangerouslyDisableSandbox: true で再試行。それも拒否されたら claude-direct にフォールバック。

    codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "
    {question}
    " 2>/dev/null

    Return CONCISE summary (recommendation + rationale).
```

### Direct Call (Short Questions)

For quick questions:

```bash
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "Brief question" 2>/dev/null
```

### Implementation Task (when route == codex)

```bash
codex exec --model <codex.model> --sandbox <codex.sandbox.implementation> <codex.flags> "{implementation task}" 2>/dev/null
```

### Sandbox Modes

| Mode | Use Case |
|------|----------|
| `read-only` | 分析、レビュー、デバッグ助言 |
| `workspace-write` | 実装、修正、リファクタリング |

## Language Protocol

1. Ask Codex in **English**
2. Receive response in **English**
3. Report to user in **Japanese**

## Integration with Gemini

| Task | Use |
|------|-----|
| 外部調査が必要 | Gemini → (必要なら) Codex |
| 実装タスクで route が codex | Codex |
| 実装タスクで route が claude-direct | Claude direct |
| route が auto | タスク特性で選択 |

## Why This Skill

- config 変更だけで Codex の役割を切り替えられる
- エージェント定義とスキル文書の責務齟齬を防げる
- 将来のモデル評価変化（実装担当の入れ替え）に追従しやすい
