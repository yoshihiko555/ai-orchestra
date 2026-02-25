---

name: context-loader
description: Load project context from AGENTS and .claude directories

---

## Trigger

- "load context"
- "project context"
- "check context"

## Actions

1. Read `AGENTS.md` for Codex behavior instructions
2. Read `.claude/config/agent-routing/cli-tools.yaml` for CLI tool settings
3. Read `.claude/agents/` only if agent behavior is relevant
4. Check `.codex/rules/*.rules` for project execution-policy constraints (optional)
5. Check `~/.codex/rules/*.rules` for user-level fallback constraints (optional)
6. Check `.claude/logs/cli-tools.jsonl` for past Codex/Gemini interactions (optional)
7. Read `CLAUDE.md` for project overview

## Output

Summarize relevant context for the current task.
