---

name: context-loader
description: Load project context from .claude directory

---

## Trigger

- "load context"
- "project context"
- "check context"

## Actions

1. Read `.claude/config/agent-routing/cli-tools.yaml` for CLI tool settings
2. Read `.claude/rules/` for coding principles and delegation rules
3. Check `.claude/logs/cli-tools.jsonl` for past Codex/Gemini interactions
4. Read `CLAUDE.md` for project overview

## Output

Summarize relevant context for the current research task.
