#!/usr/bin/env python3
"""UserPromptSubmit hook: cli-tools.yaml 駆動のエージェントルーティング提案。"""

import json
import sys

from route_config import (
    RESEARCHER_FALLBACK_TRIGGERS,
    build_cli_suggestion,
    detect_agent,
    get_agent_tool,
    load_config,
)


def main():
    try:
        data = json.load(sys.stdin)
        prompt = data.get("prompt", "")
        if len(prompt) < 5:
            sys.exit(0)

        config = load_config(data)
        messages = []

        agent, trigger = detect_agent(prompt)
        if agent:
            tool = get_agent_tool(agent, config)
            cli_msg = build_cli_suggestion(tool, agent, trigger, config)
            if cli_msg:
                messages.append(cli_msg)
            messages.append(
                f"[Agent Routing] '{trigger}' → `{agent}` (tool: {tool}):\n"
                f'Task(subagent_type="{agent}", prompt="...")'
            )
        else:
            prompt_lower = prompt.lower()
            for trig in RESEARCHER_FALLBACK_TRIGGERS.get(
                "ja", []
            ) + RESEARCHER_FALLBACK_TRIGGERS.get("en", []):
                if trig.lower() in prompt_lower:
                    fallback_agent = "researcher"
                    tool = get_agent_tool(fallback_agent, config)
                    cli_msg = build_cli_suggestion(tool, fallback_agent, trig, config)
                    if cli_msg:
                        messages.append(cli_msg)
                    messages.append(
                        f"[Agent Routing] '{trig}' → `{fallback_agent}` (tool: {tool}):\n"
                        f'Task(subagent_type="{fallback_agent}", prompt="...")'
                    )
                    break

        if messages:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": "\n\n".join(messages),
                        }
                    }
                )
            )
        sys.exit(0)
    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
