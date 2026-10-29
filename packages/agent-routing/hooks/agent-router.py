#!/usr/bin/env python3
"""UserPromptSubmit hook: cli-tools.yaml 駆動のエージェントルーティング提案。

バックグラウンドタスクの完了通知（`<task-notification>`）とサブエージェントのハンドバック・
別セッションからのメッセージ（`<agent-message>`）はユーザー入力ではないため提案しない。

判定は `router.engine` で切り替える（ADR-20261029-062）。既定の `keyword` は依頼の形をした節だけで
トリガー語を照合する。`jev` は Jev の判定で提案し、Jev が使えないときはキーワード判定に戻る。
"""

import json
import re
import sys
from pathlib import Path

import jev_router
from route_config import (
    RESEARCHER_FALLBACK_TRIGGERS,
    _project_dir_from_data,
    _routing_request_clauses,
    build_cli_suggestion,
    detect_agent,
    get_agent_tool,
    has_project_config,
    is_harness_prompt,
    load_config,
    load_package_config,
)

# `/name` または `/plugin:name` の後に空白か本文末尾が続くもの（audit-prompt.py と同じ判定）
SLASH_COMMAND_RE = re.compile(r"^/([A-Za-z0-9][\w.-]*(?::[\w.-]+)?)(?=\s|$)")
STATUS_SLASH_COMMAND = "slash_command"
ROUTING_EVENT_TYPE = "routing_suggestion"
NO_AGENT = "none"


def _installed_agents(project_dir: str) -> set[str] | None:
    """`.claude/agents/<name>.md` が実在するエージェント名。ディレクトリが無ければ None。"""
    agents_dir = Path(project_dir) / ".claude" / "agents"
    if not project_dir or not agents_dir.is_dir():
        return None
    return {path.stem for path in agents_dir.glob("*.md") if path.is_file()}


def _keyword_suggestion(prompt: str, allowed_agents: set[str] | None) -> tuple[str | None, str]:
    """依頼の形をした節だけでキーワード判定する。researcher へのフォールバックも同じ節を使う。"""
    request_text = "\n".join(_routing_request_clauses(prompt))
    agent, trigger = detect_agent(request_text, allowed_agents=allowed_agents)
    if agent:
        return agent, trigger
    if allowed_agents is not None and "researcher" not in allowed_agents:
        return None, ""
    request_lower = request_text.lower()
    for trig in RESEARCHER_FALLBACK_TRIGGERS.get("ja", []) + RESEARCHER_FALLBACK_TRIGGERS.get(
        "en", []
    ):
        if trig.lower() in request_lower:
            return "researcher", trig
    return None, ""


def _suggestion_messages(agent: str, trigger: str, config: dict) -> list[str]:
    tool = get_agent_tool(agent, config)
    messages = []
    cli_msg = build_cli_suggestion(tool, agent, trigger, config)
    if cli_msg:
        messages.append(cli_msg)
    messages.append(
        f"[Agent Routing] '{trigger}' → `{agent}` (tool: {tool}):\n"
        f'Task(subagent_type="{agent}", prompt="...")'
    )
    return messages


def _is_installed(agent: str | None, allowed_agents: set[str] | None) -> bool | None:
    if agent is None or agent == NO_AGENT:
        return None
    return allowed_agents is None or agent in allowed_agents


def _route(
    prompt: str, config: dict, allowed_agents: set[str] | None
) -> tuple[str | None, str, dict]:
    """提案するエージェントと trigger、監査ログに残す判定内容を返す。"""
    keyword_agent, keyword_trigger = _keyword_suggestion(prompt, allowed_agents)
    engine = jev_router.router_engine(config)
    record: dict = {
        "engine": engine,
        "status": None,
        "decided_by": "keyword",
        "config_id": None,
        "model": None,
        "request_hash": None,
        "probabilities": None,
        "p_none": None,
        "choice": None,
        "installed": None,
        "keyword": {"agent": keyword_agent or NO_AGENT, "trigger": keyword_trigger or None},
        "latency_ms": None,
        "input_tokens": None,
    }
    if engine != jev_router.ENGINE_JEV:
        return keyword_agent, keyword_trigger, record
    if SLASH_COMMAND_RE.match(prompt.lstrip()):
        # スラッシュコマンドは Jev に送らない
        return keyword_agent, keyword_trigger, {**record, "status": STATUS_SLASH_COMMAND}

    judgment = jev_router.judge(prompt, config)
    record = {
        **record,
        "status": judgment.status,
        "config_id": jev_router.CONFIG_ID,
        "model": judgment.model,
        "request_hash": judgment.request_hash,
        "latency_ms": judgment.latency_ms,
    }
    if judgment.status != jev_router.STATUS_OK:
        return keyword_agent, keyword_trigger, record

    installed = _is_installed(judgment.choice, allowed_agents)
    record = {
        **record,
        "decided_by": "jev",
        "probabilities": judgment.probabilities,
        "p_none": judgment.p_none,
        "choice": judgment.choice,
        "installed": installed,
        "input_tokens": judgment.input_tokens,
    }
    # 未導入のエージェントを選んだときは次の候補に繰り下げず、何も提案しない
    if judgment.agent is None or not installed:
        return None, "", record
    return judgment.agent, f"jev p_none={judgment.p_none:.2f}", record


def _route_audit_enabled(flags: object) -> bool:
    """`features.route_audit.enabled` を audit-prompt.py と同じ規則で読む。

    値が空なら既定（有効）とし、object でない値があるときは記録しない。
    """
    features = (flags.get("features") or {}) if isinstance(flags, dict) else None
    route_audit = (features.get("route_audit") or {}) if isinstance(features, dict) else None
    if not isinstance(route_audit, dict):
        return False
    return bool(route_audit.get("enabled", True))


def _emit_routing_event(data: dict, project_dir: str, record: dict) -> None:
    """判定を audit の監査ログに残す。audit を導入していない project では何もしない。"""
    session_id = str(data.get("session_id") or "")
    if not session_id or not project_dir:
        return
    try:
        if not has_project_config("audit", "audit-flags.json", project_dir):
            return
        if not _route_audit_enabled(load_package_config("audit", "audit-flags.json", project_dir)):
            return
        from event_logger import emit_event  # type: ignore[import-not-found]

        emit_event(ROUTING_EVENT_TYPE, record, session_id=session_id, project_dir=project_dir)
    except Exception as exc:
        # 記録に失敗しても提案は変えない（例外文には環境のパスが入りうるため種別だけ出す）
        print(f"agent-router: failed to record routing: {type(exc).__name__}", file=sys.stderr)


def main():
    try:
        data = json.load(sys.stdin)
        prompt = data.get("prompt", "")
        if len(prompt) < 5:
            sys.exit(0)
        if is_harness_prompt(prompt):
            sys.exit(0)

        config = load_config(data)
        project_dir = _project_dir_from_data(data)
        allowed_agents = _installed_agents(project_dir)
        agent, trigger, record = _route(prompt, config, allowed_agents)
        messages = _suggestion_messages(agent, trigger, config) if agent else []

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
            sys.stdout.flush()
        _emit_routing_event(data, project_dir, {**record, "suggested": agent or NO_AGENT})
        sys.exit(0)
    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
