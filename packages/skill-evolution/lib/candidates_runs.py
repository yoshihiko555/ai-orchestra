"""月別の記録から実行範囲と帰属の条件を組み立てる。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import skill_evolution_common as common
from candidates_safety import ValidationError, open_regular, timestamp, validate_directory


class RunHistory:
    def __init__(self, events: list[dict]) -> None:
        valid = []
        for event in events:
            try:
                if common._valid_event(event):
                    valid.append(event)
            except (KeyError, TypeError, ValueError):
                continue
        self.events = sorted(valid, key=lambda event: timestamp(event["ts"]))
        self.scopes: list[dict] = []
        seen_runs: set[str] = set()
        seen_tools: set[tuple] = set()
        for event in self.events:
            if event["type"] != "run.started":
                continue
            tool_key = (event["sid"], event["tool_use_id"])
            if event["run_id"] in seen_runs or (tool_key[1] and tool_key in seen_tools):
                continue
            seen_runs.add(event["run_id"])
            seen_tools.add(tool_key)
            boundary = self._boundary(event)
            self.scopes.append(
                {
                    **event,
                    "end": boundary["ts"] if boundary else None,
                    "boundary": boundary["type"] if boundary else "区切り不明",
                }
            )

    @classmethod
    def load(cls, directory: Path, root: Path, months: set[str] | None = None) -> RunHistory:
        if not directory.exists():
            return cls([])
        validate_directory(directory, root)
        events = []
        for path in sorted(directory.glob("runs-*.jsonl")):
            match = re.fullmatch(r"runs-(\d{4}(?:0[1-9]|1[0-2]))\.jsonl", path.name)
            if match is None or (months is not None and match[1] not in months):
                continue
            with open_regular(path) as stream:
                for line in stream:
                    if not line.endswith(b"\n"):
                        continue
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            events.append(event)
                    except (ValueError, UnicodeError):
                        continue
        return cls(events)

    def _boundary(self, run: dict) -> dict | None:
        start = timestamp(run["ts"])
        for event in self.events:
            if event["sid"] != run["sid"] or timestamp(event["ts"]) <= start:
                continue
            if run["agent_id"] is not None:
                if event["type"] == "agent.stopped" and event["agent_id"] == run["agent_id"]:
                    return event
            elif event["type"] == "turn.stopped" or (
                event["type"] == "turn.started" and event["prompt_id"] != run["prompt_id"]
            ):
                return event
        return None

    def for_session(self, sid: str) -> list[dict]:
        return [scope for scope in self.scopes if scope["sid"] == sid]

    @classmethod
    def for_evidence(cls, directory: Path, root: Path, failures: list[dict]) -> RunHistory:
        if not failures:
            return cls([])
        latest_month = max(timestamp(failure["ts"]).strftime("%Y%m") for failure in failures)
        months = sorted(
            path.name[5:11]
            for path in directory.glob("runs-*.jsonl")
            if re.fullmatch(r"runs-\d{4}(?:0[1-9]|1[0-2])\.jsonl", path.name)
        )
        # 過去月を飛ばすと、月をまたいで動くサブエージェントの始まりを見落とす。
        selected = {month for month in months if month <= latest_month}
        history = cls.load(directory, root, selected)
        for month in months:
            if month <= latest_month:
                continue
            needs_boundary = any(
                scope["end"] is None and timestamp(scope["ts"]) <= timestamp(failure["ts"])
                for failure in failures
                for scope in history.for_session(failure["sid"])
            )
            if not needs_boundary:
                break
            following = cls.load(directory, root, {month})
            history = cls(history.events + following.events)
        return history

    def running_subagents(self, sid: str, at: datetime) -> list[str]:
        running: set[str] = set()
        for event in self.events:
            if event["sid"] != sid or timestamp(event["ts"]) > at:
                continue
            if event["type"] == "agent.started" and event["agent_id"]:
                running.add(event["agent_id"])
            elif event["type"] == "agent.stopped":
                running.discard(event["agent_id"])
        return sorted(running)

    def confirmed(self, scope: dict) -> bool:
        if scope["trigger"] == "direct":
            return True
        return any(
            event["type"] == "run.confirmed"
            and event["sid"] == scope["sid"]
            and event["tool_use_id"] == scope["tool_use_id"]
            and timestamp(event["ts"]) >= timestamp(scope["ts"])
            for event in self.events
        )

    def human_choices(self, sid: str | None, at: str) -> list[dict]:
        return sorted(
            [scope for scope in self.for_session(sid) if timestamp(scope["ts"]) < timestamp(at)],
            key=lambda scope: timestamp(scope["ts"]),
            reverse=True,
        )

    def attribute_failure(self, sid: str, at: str) -> dict:
        moment = timestamp(at)
        scopes = self.for_session(sid)
        containing = [
            scope
            for scope in scopes
            if timestamp(scope["ts"]) <= moment
            and (scope["end"] is None or moment < timestamp(scope["end"]))
        ]
        scope = containing[0] if len(containing) == 1 else None
        conditions = {
            "unique_scope": scope is not None,
            "recorded_boundary": bool(scope and scope["end"]),
            "no_nested_scope": bool(scope)
            and not any(
                other["run_id"] != scope["run_id"]
                and timestamp(scope["ts"]) <= timestamp(other["ts"])
                and (scope["end"] is None or timestamp(other["ts"]) < timestamp(scope["end"]))
                for other in scopes
            ),
            "no_running_subagent": not self.running_subagents(sid, moment),
            "confirmed_scope": bool(scope and self.confirmed(scope)),
        }
        if all(conditions.values()):
            return {
                "attribution": "run",
                "run_id": scope["run_id"],
                "skill": scope["skill"],
                "attributed_by": "auto",
                "conditions": conditions,
            }
        return {
            "attribution": "unknown",
            "run_id": None,
            "skill": None,
            "attributed_by": None,
            "conditions": conditions,
        }

    def require_human_scope(self, run_id: str, sid: str | None, at: str) -> dict:
        for scope in self.human_choices(sid, at):
            if scope["run_id"] == run_id:
                return scope
        raise ValidationError("同じセッションで証拠より前に始まった実行範囲を指定してください")
