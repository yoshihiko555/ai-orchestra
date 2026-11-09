"""EV-83: hook と CLI の通常の操作は v1 のデータを消さない。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

V1_PATHS = (
    ".claude/skill-evolution/lessons/review.md",
    ".claude/logs/skill-evolution/metrics/review.jsonl",
    ".claude/logs/skill-evolution/pending/session/review.json",
    ".claude/logs/skill-evolution/locks/review.lock",
)


def write_files(directory: Path, names: tuple[str, ...]) -> dict[Path, bytes]:
    snapshot = {}
    for name in names:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"旧データを保持: {name}\n".encode()
        path.write_bytes(content)
        snapshot[path] = content
    return snapshot


def assert_preserved(snapshot: dict[Path, bytes]) -> None:
    assert {path: path.read_bytes() for path in snapshot} == snapshot


def test_ev83_hooks_and_cli_commands_preserve_every_v1_file(
    candidate_context, observation_event, candidate_event, run_hook, read_events, skill_dir
):
    project = candidate_context.project
    candidate_context.store.write(lambda _: [observation_event(), candidate_event()])
    snapshot = write_files(project, V1_PATHS)
    base = {"session_id": "session-1", "prompt_id": "prompt-1", "tool_use_id": "toolu_1"}
    for filename, fields in (
        ("inject-lessons.py", {"tool_name": "Skill", "tool_input": {"skill": "review"}}),
        ("capture-skill-telemetry.py", {"tool_name": "Skill", "tool_input": {"skill": "review"}}),
        ("capture-skill-stop.py", {"hook_event_name": "Stop"}),
        ("capture-subagent-skill.py", {"hook_event_name": "SubagentStop", "agent_id": "agent-1"}),
        (
            "record-skill-scope.py",
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_name": "review",
            },
        ),
        ("record-skill-scope.py", {"hook_event_name": "UserPromptSubmit"}),
        (
            "record-skill-scope.py",
            {"hook_event_name": "SubagentStart", "agent_id": "agent-1", "agent_type": "Explore"},
        ),
    ):
        run_hook(filename, {**base, **fields})
        assert_preserved(snapshot)
    assert {event["type"] for event in read_events()} == {
        "run.started",
        "run.confirmed",
        "turn.stopped",
        "agent.stopped",
        "turn.started",
        "agent.started",
    }
    for filename, arguments, code in (
        ("skill_candidates.py", ["list"], 0),
        ("skill_candidates.py", ["show", "C-00000001"], 0),
        ("skill_candidates.py", ["show", "O-00000001"], 0),
        ("skill_candidates.py", ["runs"], 0),
        ("skill_candidates.py", ["runs", "--sid", "session-1"], 0),
        ("skill_candidates_admin.py", ["prune-runs", "--before", "202601"], 0),
    ):
        completed = subprocess.run(
            [
                sys.executable,
                str(project / "packages/skill-evolution/scripts" / filename),
                "--project",
                str(project),
                *arguments,
            ],
            input="{}",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode == code, completed.stderr
        assert_preserved(snapshot)
