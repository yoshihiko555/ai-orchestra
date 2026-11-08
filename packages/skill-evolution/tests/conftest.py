"""実リポジトリやユーザースキルに触れない hook テスト環境。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "packages" / "skill-evolution"


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / ".claude").mkdir()
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(directory))
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO))
    monkeypatch.setenv("HOME", str(home))
    return directory


@pytest.fixture
def write_config(project):
    def write(text, *, local=False):
        directory = project / ".claude" / "config" / "skill-evolution"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("skill-evolution.local.yaml" if local else "skill-evolution.yaml")
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture
def run_hook(project):
    def run(filename, payload, *, raw=None):
        completed = subprocess.run(
            [sys.executable, str(PACKAGE / "hooks" / filename)],
            input=raw if raw is not None else json.dumps(payload).encode(),
            capture_output=True,
            env=os.environ.copy(),
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr.decode()
        assert completed.stdout == b""
        return completed

    return run


@pytest.fixture
def read_events(project):
    def read():
        return [
            json.loads(line)
            for path in sorted(
                (project / ".claude" / "logs" / "skill-evolution").glob("runs-*.jsonl")
            )
            for line in path.read_text().splitlines()
        ]

    return read


@pytest.fixture
def skill_dir(project):
    directory = project / ".claude" / "skills" / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("レビューの手順\n", encoding="utf-8")
    return directory


sys.path.insert(0, str(PACKAGE / "lib"))


@pytest.fixture
def candidate_context(project, monkeypatch):
    import candidates_safety
    from candidates_ingest import build_context

    package = project / "packages/skill-evolution"
    for directory in ("lib", "scripts", "config"):
        shutil.copytree(PACKAGE / directory, package / directory)
    (project / "packages/core").symlink_to(REPO / "packages/core", target_is_directory=True)
    monkeypatch.setattr(candidates_safety, "__file__", str(package / "lib/candidates_safety.py"))
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(project))
    manifest = project / ".claude/orchestra.json"
    manifest.write_text(json.dumps({"installed_packages": ["fail-logs", "skill-evolution"]}))
    scripts = project / "packages/fail-logs/scripts"
    scripts.mkdir(parents=True)
    (scripts / "lessons-fixture.json").write_text('{"lessons": []}')
    (scripts / "lessons_cli.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "base = Path(__file__).parent\n"
        "exit_file = base / 'exit-fixture'\n"
        "if exit_file.exists(): sys.exit(int(exit_file.read_text()))\n"
        "payload = json.loads((base / 'lessons-fixture.json').read_text())\n"
        "if sys.argv[1] == 'show':\n"
        "    payload = {'lesson': next(x for x in payload['details'] if x['lid'] == sys.argv[2])}\n"
        "print(json.dumps(payload))\n"
    )
    masking = project / "packages/fail-logs/lib/masking.py"
    masking.parent.mkdir()
    masking.write_text(
        "import re\ndef load_masker():\n"
        "    def mask(text):\n"
        "        return re.sub(r'sk-[A-Za-z0-9]+|(?<=password=)[^ ]+|(?<=Bearer )[^ ]+', '[MASKED]', text)\n"
        "    return mask\n"
    )
    composition = project / "facets/compositions/skills/review.yaml"
    composition.parent.mkdir(parents=True)
    composition.write_text("name: review\n")
    return build_context(str(project))


@pytest.fixture
def scope_event():
    import skill_evolution_common as common

    def make(
        kind="run.started",
        at="2026-11-01T10:00:00Z",
        *,
        sid="session-1234",
        number=1,
        prompt="prompt-1",
        agent=None,
        **fields,
    ):
        payload = {"session_id": sid, "prompt_id": prompt, "agent_id": agent, "agent_type": None}
        record = common.build_scope_event(kind, payload)
        record["ts"] = at
        if kind == "run.started":
            record.update(
                run_id=f"R-{number:012x}",
                skill="review",
                trigger="direct",
                tool_use_id=None,
                model=None,
                skill_hash=None,
                project="project",
                src={
                    "tool_use_id": "absent",
                    "agent_id": "hook_input" if agent else "absent",
                    "model": "absent",
                    "skill_hash": "unresolved",
                },
            )
        record.update(fields)
        return record

    return make


@pytest.fixture
def write_runs(candidate_context):
    from candidates_safety import timestamp

    def write(events):
        directory = candidate_context.store.directory
        directory.mkdir(parents=True, exist_ok=True)
        for record in events:
            path = directory / f"runs-{timestamp(record['ts']):%Y%m}.jsonl"
            with path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")

    return write


@pytest.fixture
def observation_event():
    import candidates_model as model

    def make(number=1, *, skill="review", kind="failure", failure_eids=None, **fields):
        payload = {
            "evidence": {
                "kind": kind,
                "ref": f"{number:012x}" if kind == "failure" else f"L-{number:08x}",
            },
            "sid": "session-1234",
            "ts": "2026-11-01T10:01:00Z",
            "excerpt": "pytest collection failed missing imports",
            "excerpt_status": "",
            "failure_kind": "test_failure",
            "failure_eids": failure_eids or [],
            "contexts": [{"kind": "session", "ref": "session-1234"}],
            "skill": skill,
            "run_id": None,
            "attribution": "skill" if skill else "unknown",
            "attributed_by": "human" if skill else None,
            "conditions": {},
        }
        payload.update(fields)
        return model.event("observation.created", f"O-{number:08x}", payload, actor="ingest")

    return make


@pytest.fixture
def candidate_event():
    import candidates_model as model

    def make(number=1, *, observations=None, skill="review", **fields):
        payload = {
            "skill": skill,
            "hypothesis": {"title": "手順の不足", "description": "検証の手順を追加する"},
            "observations": observations or [f"O-{number:08x}"],
            "handoff_eligible": True,
        }
        payload.update(fields)
        return model.event("candidate.created", f"C-{number:08x}", payload)

    return make
