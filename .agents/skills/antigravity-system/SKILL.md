---
name: antigravity-system
description: 'PROACTIVELY consult Antigravity CLI for research and large codebase
  comprehension.

  Antigravity excels at: large context windows, Google Search grounding,

  and repository-wide understanding.

  Use for pre-implementation research, documentation analysis, and codebase exploration.

  Explicit triggers: "research", "investigate", "agy", "antigravity", "understand
  codebase".

  '
metadata:
  short-description: Claude Code ↔ Antigravity CLI collaboration (research)
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

外部 CLI（Codex / Antigravity）は sandbox 内で直接実行する。
エラー時は `claude-direct` にフォールバックする。

---

# Antigravity System — Research Specialist

**Antigravity CLI (`agy`) is your research specialist with a large context window.**

> **詳細ルール**: `.claude/rules/antigravity-delegation.md`

## Context Management (CRITICAL)

**サブエージェント経由を推奨。** Antigravity 出力は大きくなりがちなため。

| 状況                 | 方法                         |
| -------------------- | ---------------------------- |
| コードベース分析     | サブエージェント経由（推奨） |
| ライブラリ調査       | サブエージェント経由（推奨） |
| 短い質問 (1-2文回答) | 直接呼び出しOK               |

## Tool Selection (Config-Aware)

**固定マッピングではなく、`cli-tools.yaml` の解決結果を優先する。**

| ケース                     | 推奨                                           |
| -------------------------- | ---------------------------------------------- |
| 外部調査、最新ドキュメント | Antigravity 候補                               |
| 設計判断、デバッグ、実装   | `agents.<target>.tool` で解決                  |
| `tool: auto` の場合        | 深い推論は Codex 候補、調査は Antigravity 候補 |

## When to Consult (MUST)

| Situation             | Trigger Examples                                  |
| --------------------- | ------------------------------------------------- |
| **Research**          | 「調べて」「リサーチ」 / "Research" "Investigate" |
| **Library docs**      | 「ライブラリ」「ドキュメント」 / "Library" "Docs" |
| **Codebase analysis** | 「コードベース全体」 / "Entire codebase"          |

## When NOT to Consult

- `agents.<target>.tool` の解決結果が `antigravity` でない場合
- 実装作業で `tool: codex` / `tool: claude-direct` が指定されている場合
- 単純なファイル操作（直接処理で十分）
- テスト・lint 実行のみの作業

## How to Consult

### Recommended: Subagent Pattern

**Use Task tool with `subagent_type='general-purpose'` to preserve main context.**

```
Task tool parameters:
- subagent_type: "general-purpose"
- run_in_background: true (optional, for parallel work)
- prompt: |
    Research: {topic}

    sandbox 内で agy を実行する。エラー時は claude-direct にフォールバック。

    agy -p "{research question}

    IMPORTANT: Do not ask any clarifying questions. Provide your best answer
    based on the available information." --model <antigravity.model> 2>/dev/null

    タイムアウト: Bash timeout パラメータに 300000 を指定すること。
    リトライ: タイムアウトや質問検出時は antigravity-delegation.md のリトライプロトコルに従う。

    Save full output to: .claude/docs/research/{topic}.md
    Return CONCISE summary (5-7 bullet points).
```

### Direct Call (Short Questions Only)

For quick questions expecting brief answers:

```bash
agy -p "Brief question

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> 2>/dev/null
```

### CLI Options Reference

> **Note**: `agy -p` モードは stdin を待たないため `< /dev/null` は不要。
> 詳細は `antigravity-delegation.md` の「Non-Interactive 実行」セクション参照。

```bash
# Codebase analysis
agy -p "{question}

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir . 2>/dev/null

# Model allowlist check
# antigravity.model が antigravity.model_allowlist に含まれない場合は [WARN] を出力してから実行すること。
agy -p "{question}

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> 2>/dev/null
```

### Workflow (Subagent)

1. **Spawn subagent** with Antigravity research prompt
2. **Continue your work** → Subagent runs in parallel
3. **Receive summary** → Subagent returns key findings
4. **Full output saved** → `.claude/docs/research/{topic}.md`

## Output Location

Save Antigravity research results to:

```
.claude/docs/research/{topic}.md
```

This allows Claude and Codex to reference the research later.

## Task Templates

### Pre-Implementation Research

```bash
agy -p "Research best practices for {feature} in Python 2025.
Include:
- Common patterns and anti-patterns
- Library recommendations (with comparison)
- Performance considerations
- Security concerns
- Code examples

IMPORTANT: Do not ask any clarifying questions. Provide your best answer
based on the available information." --model <antigravity.model> 2>/dev/null
```

### Repository Analysis

```bash
agy -p "Analyze this repository:
1. Architecture overview
2. Key modules and responsibilities
3. Data flow between components
4. Entry points and extension points
5. Existing patterns to follow

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir . 2>/dev/null
```

### Library Research

See: `references/lib-research-task.md`

## Integration with Codex

| Workflow              | Steps                                                                  |
| --------------------- | ---------------------------------------------------------------------- |
| **New feature**       | Antigravity research → (`agents.<target>.tool` に応じて) 設計レビュー  |
| **Library choice**    | Antigravity comparison → (`tool: auto` なら) Codex 候補で意思決定      |
| **Bug investigation** | Antigravity codebase search → (`tool: auto` なら) Codex 候補でデバッグ |

## Why Antigravity?

- **Large context window**: Entire repositories at once
- **Google Search grounding**: Latest information and docs
- **Multiple models**: Gemini 3.5 Flash / 3.1 Pro / Claude 4.6 / GPT-OSS switching
- **Fast exploration**: Quick overview before deep work
- **Shared context**: Results saved for Claude/Codex

---

## Additional resources

- For lib-research-task details, see [references/lib-research-task.md](references/lib-research-task.md)
- For use-cases details, see [references/use-cases.md](references/use-cases.md)
