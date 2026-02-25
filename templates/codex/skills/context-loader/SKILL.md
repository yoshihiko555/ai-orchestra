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
2. Read `.claude/docs/DESIGN.md` for architecture decisions
3. Check `.claude/docs/research/` for Gemini's findings
4. Review `.claude/docs/libraries/` for library constraints
5. Read `.claude/config/agent-routing/cli-tools.yaml` for runtime tool settings
6. Check `.codex/rules/*.rules` for project execution-policy constraints (optional)
7. Check `~/.codex/rules/*.rules` for user-level fallback constraints (optional)

## Output

Summarize relevant context for the current task.
