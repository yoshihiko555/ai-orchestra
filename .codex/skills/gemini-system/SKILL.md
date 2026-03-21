---
name: gemini-system
description: 'PROACTIVELY consult Gemini CLI for research, large codebase comprehension,

  and multimodal data processing. Gemini excels at: massive context windows (1M tokens),

  Google Search grounding, video/audio/PDF analysis, and repository-wide understanding.

  Use for pre-implementation research, documentation analysis, and multimodal tasks.

  Explicit triggers: "research", "investigate", "analyze video/audio/PDF", "understand
  codebase".

  '
metadata:
  short-description: Claude Code ↔ Gemini CLI collaboration (research & multimodal)
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

# Gemini System — Research & Multimodal Specialist

**Gemini CLI is your research specialist with 1M token context.**

> **詳細ルール**: `.claude/rules/gemini-delegation.md`

## Context Management (CRITICAL)

**サブエージェント経由を推奨。** Gemini出力は大きくなりがちなため。

| 状況 | 方法 |
|------|------|
| コードベース分析 | サブエージェント経由（推奨） |
| ライブラリ調査 | サブエージェント経由（推奨） |
| マルチモーダル | サブエージェント経由（推奨） |
| 短い質問 (1-2文回答) | 直接呼び出しOK |

## Tool Selection (Config-Aware)

**固定マッピングではなく、`cli-tools.yaml` の解決結果を優先する。**

| ケース | 推奨 |
|--------|------|
| 外部調査、最新ドキュメント、マルチモーダル | Gemini 候補 |
| 設計判断、デバッグ、実装 | `agents.<target>.tool` で解決 |
| `tool: auto` の場合 | 深い推論は Codex 候補、調査は Gemini 候補 |

## When to Consult (MUST)

| Situation | Trigger Examples |
|-----------|------------------|
| **Research** | 「調べて」「リサーチ」 / "Research" "Investigate" |
| **Library docs** | 「ライブラリ」「ドキュメント」 / "Library" "Docs" |
| **Codebase analysis** | 「コードベース全体」 / "Entire codebase" |
| **Multimodal** | 「PDF」「動画」「音声」 / "PDF" "Video" "Audio" |

## When NOT to Consult

- `agents.<target>.tool` の解決結果が `gemini` でない場合
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

    sandbox 内で gemini を実行する。エラー時は claude-direct にフォールバック。

    gemini -m <gemini.model> -p "{research question}

    IMPORTANT: Do not ask any clarifying questions. Provide your best answer
    based on the available information." < /dev/null 2>/dev/null

    タイムアウト: Bash timeout パラメータに 180000 を指定すること。
    リトライ: タイムアウトや質問検出時は gemini-delegation.md のリトライプロトコルに従う。

    Save full output to: .claude/docs/research/{topic}.md
    Return CONCISE summary (5-7 bullet points).
```

### Direct Call (Short Questions Only)

For quick questions expecting brief answers:

```bash
gemini -m <gemini.model> -p "Brief question

IMPORTANT: Do not ask any clarifying questions." < /dev/null 2>/dev/null
```

### CLI Options Reference

> **Non-Interactive 実行**: 全コマンドに `< /dev/null` と no-questions 指示を追加すること。
> 詳細は `gemini-delegation.md` の「Non-Interactive 実行（MUST）」セクション参照。

```bash
# Codebase analysis
gemini -m <gemini.model> -p "{question}

IMPORTANT: Do not ask any clarifying questions." --include-directories . < /dev/null 2>/dev/null

# Multimodal (PDF/video/audio — stdin をファイルで使用するため < /dev/null 不要)
gemini -m <gemini.model> -p "{prompt}

IMPORTANT: Do not ask any clarifying questions." < /path/to/file.pdf 2>/dev/null

# JSON output
gemini -m <gemini.model> -p "{question}

IMPORTANT: Do not ask any clarifying questions." --output-format json < /dev/null 2>/dev/null
```

### Workflow (Subagent)

1. **Spawn subagent** with Gemini research prompt
2. **Continue your work** → Subagent runs in parallel
3. **Receive summary** → Subagent returns key findings
4. **Full output saved** → `.claude/docs/research/{topic}.md`

## Output Location

Save Gemini research results to:
```
.claude/docs/research/{topic}.md
```

This allows Claude and Codex to reference the research later.

## Task Templates

### Pre-Implementation Research

```bash
gemini -p "Research best practices for {feature} in Python 2025.
Include:
- Common patterns and anti-patterns
- Library recommendations (with comparison)
- Performance considerations
- Security concerns
- Code examples

IMPORTANT: Do not ask any clarifying questions. Provide your best answer
based on the available information." < /dev/null 2>/dev/null
```

### Repository Analysis

```bash
gemini -p "Analyze this repository:
1. Architecture overview
2. Key modules and responsibilities
3. Data flow between components
4. Entry points and extension points
5. Existing patterns to follow

IMPORTANT: Do not ask any clarifying questions." --include-directories . < /dev/null 2>/dev/null
```

### Library Research

See: `references/lib-research-task.md`

### Multimodal Analysis

```bash
# Video (stdin をファイルで使用 — < /dev/null 不要)
gemini -p "Analyze video: main concepts, key points, timestamps

IMPORTANT: Do not ask any clarifying questions." < tutorial.mp4 2>/dev/null

# PDF
gemini -p "Extract: API specs, examples, constraints

IMPORTANT: Do not ask any clarifying questions." < api-docs.pdf 2>/dev/null

# Audio
gemini -p "Transcribe and summarize: decisions, action items

IMPORTANT: Do not ask any clarifying questions." < meeting.mp3 2>/dev/null
```

## Integration with Codex

| Workflow | Steps |
|----------|-------|
| **New feature** | Gemini research → (`agents.<target>.tool` に応じて) 設計レビュー |
| **Library choice** | Gemini comparison → (`tool: auto` なら) Codex 候補で意思決定 |
| **Bug investigation** | Gemini codebase search → (`tool: auto` なら) Codex 候補でデバッグ |

## Why Gemini?

- **1M token context**: Entire repositories at once
- **Google Search**: Latest information and docs
- **Multimodal**: Native PDF/video/audio processing
- **Fast exploration**: Quick overview before deep work
- **Shared context**: Results saved for Claude/Codex
