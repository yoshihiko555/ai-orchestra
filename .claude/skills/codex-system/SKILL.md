---
name: codex-system
description: 'Use Codex CLI with config-driven routing.

  Always resolve behavior from `.claude/config/agent-routing/cli-tools.yaml`

  (`codex.enabled`, `agents.<name>.tool`, model/sandbox/flags), instead of

  hardcoding Codex as design-only or implementation-only.

  Explicit triggers: "consult codex", "codexで", "think deeper", "analyze".

  '
metadata:
  short-description: Claude Code ↔ Codex CLI collaboration (config-driven)
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

### 前提条件（ユーザー設定）

以下の設定が `~/.claude/settings.json` に必要:

```json
{
  "env": {
    "NODE_OPTIONS": "--use-env-proxy"
  },
  "sandbox": {
    "enableWeakerNetworkIsolation": true,
    "network": {
      "allowedDomains": ["generativelanguage.googleapis.com", "accounts.google.com"]
    },
    "filesystem": {
      "allowWrite": ["~/.gemini"]
    }
  }
}
```

### フォールバック

- sandbox 内で実行エラーが発生した場合は `claude-direct` にフォールバックする
- `dangerouslyDisableSandbox` は使用しない（サブエージェントでは承認プロンプトが自動拒否されるため）

---

# Codex System — Config-Driven Integration

**Codex の役割は固定しない。`cli-tools.yaml` を SSOT として解決する。**

> **詳細ルール**: `.claude/rules/codex-delegation.md`, `.claude/rules/config-loading.md`

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

    sandbox 内で codex を実行する。エラー時は claude-direct にフォールバック。

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
