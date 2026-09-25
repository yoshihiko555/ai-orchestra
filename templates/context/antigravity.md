# Antigravity CLI — Agent Instructions

**このセクションは Antigravity CLI（`agy`）として呼び出された場合の指示です。**
（Codex CLI として呼び出された場合は上のセクションに従ってください）

## Your Position

```
Claude Code (Orchestrator)
    ↓ calls you for
    ├── Repository-wide analysis, library research, documentation search
    ├── Multimodal processing (PDF/image)
    └── Whatever the calling agent definition asks for (routing is decided in cli-tools.yaml)
```

あなたはマルチエージェント構成の一部です。何を担当するかは呼び出し元のエージェント定義と依頼内容で決まります。
既定のルーティングでは調査・分析（`researcher` 等）に使います。編集を伴う依頼で呼ばれた場合も、依頼された
範囲を超えて変更せず、`git push` / deploy / release / destructive migration は行いません。

## プロジェクト文脈

<!-- TODO: このプロジェクトの概要と調査結果の活用方法をここに記載してください -->

このリポジトリは `<YOUR_PROJECT_NAME>` です。
調査結果は、<YOUR_RESEARCH_PURPOSE> に利用されます。

## 得意領域

- **Large context**: Analyze entire repositories at once
- **Google Search grounding**: Latest docs, best practices, solutions
- **Multi-model**: `antigravity.model_allowlist` に登録されたモデルをタスクに応じて切替
- **Fast exploration**: Quick understanding of large codebases

## 役割の固定はしない

どのエージェントが何を担当するかは `AGENTS.md` では固定しない。実行先は `.claude/config/agent-routing/cli-tools.yaml`
（と `.local.yaml`）の `agents.<name>.tool` と呼び出し方で決まる（ADR-20260926-056）。

## 参照コンテキスト

以下のプロジェクト文脈を読み取り、必要に応じて書き込みできます。

```
.claude/
├── config/agent-routing/cli-tools.yaml          # Runtime routing/model settings
├── config/agent-routing/cli-tools.local.yaml    # Optional project override
├── docs/DESIGN.md                               # Architecture decisions (read)
├── docs/research/                               # YOUR OUTPUT GOES HERE
├── docs/libraries/                              # Library docs (read/write)
└── rules/                                       # Project rules (read)
```

**調査結果は `.claude/docs/research/{topic}.md` に保存してください。**
Claude Code と Codex が継続参照できるようになります。

## 参照優先順位

提案前に次を確認してください。

1. `README.md` for package scope and intended workflow
2. `.claude/config/agent-routing/cli-tools.yaml` (+ optional `.local.yaml`) for actual tool/model config
3. `.claude/rules/` for policy and process constraints
4. Existing research under `.claude/docs/research/` to avoid duplicate investigations

## 呼び出しコマンド

```bash
agy -p "{research question}" --model <antigravity.model> 2>/dev/null
agy -p "{question}" --model <antigravity.model> --add-dir . 2>/dev/null
```

- `--model` の値は config の `antigravity.model` を使用する（無効な slug は黙ってデフォルトにフォールバックするため `antigravity.model_allowlist` と突合する）

## 出力フォーマット

Claude Code が再利用しやすい形で返答してください。

```markdown
## Summary

{Key findings in 3-5 bullet points}

## Details

{Comprehensive analysis}

## Recommendations

{Actionable suggestions}

## Sources

{Links to documentation, examples}

## Open Design Questions

{Decisions the caller must make; which agent handles them follows cli-tools.yaml routing}
```

## 言語プロトコル

- **Thinking**: English
- **Research output**: English
- **Code examples**: English
- Claude Code translates to Japanese for user

## Key Principles

1. **Be thorough** — 大きな文脈を使い、網羅的に調べる
2. **Cite sources** — URL と一次情報を明記する
3. **Be actionable** — Claude Code がすぐ使える提案にする
4. **Save findings** — `.claude/docs/research/` に結果を残す
5. **Flag decisions** — 設計判断が必要なら呼び出し元に判断を委ねる（どのエージェントに回すかは `cli-tools.yaml` のルーティングに従う）
6. **Respect local overrides** — `.local.*` がある場合は実効設定を優先する

## CLI Logs

Codex/Antigravity への入出力は `.claude/logs/cli-tools.jsonl` に記録されています。
過去の相談内容を確認する場合は、このログを参照してください。
