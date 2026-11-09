"""実リポジトリやユーザースキルに触れない hook テスト環境。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from candidates_ingest import CandidatesContext

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "packages" / "skill-evolution"


@pytest.fixture(autouse=True)
def no_real_claude(tmp_path):
    executable = tmp_path / "blocked-claude"
    executable.write_text(f"#!{sys.executable}\nraise SystemExit(97)\n")
    executable.chmod(0o700)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CLAUDE_CODE_EXECPATH", str(executable))
        yield


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


@pytest.fixture(scope="session")
def candidate_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("candidate-template")
    package = project / "packages/skill-evolution"
    for directory in ("lib", "scripts", "config"):
        shutil.copytree(
            PACKAGE / directory, package / directory, ignore=shutil.ignore_patterns("__pycache__")
        )
    (project / "packages/core").symlink_to(REPO / "packages/core", target_is_directory=True)
    manifest = project / ".claude/orchestra.json"
    manifest.parent.mkdir()
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
    return project


def build_candidate_context(project: Path, patch: pytest.MonkeyPatch) -> CandidatesContext:
    import candidates_safety
    from candidates_ingest import build_context

    patch.setattr(
        candidates_safety,
        "__file__",
        str(project / "packages/skill-evolution/lib/candidates_safety.py"),
    )
    patch.setenv("AI_ORCHESTRA_DIR", str(project))
    patch.setenv("CLAUDE_PROJECT_DIR", str(project))
    return build_context(str(project))


@pytest.fixture
def candidate_context_factory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[Path], CandidatesContext]:
    def create(template: Path) -> CandidatesContext:
        # hardlink ではテスト中の書き換えが共有元にも伝わるため、通常のコピーで隔離する。
        shutil.copytree(
            template,
            project,
            symlinks=True,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        return build_candidate_context(project, monkeypatch)

    return create


@pytest.fixture
def candidate_context(candidate_context_factory, candidate_template):
    return candidate_context_factory(candidate_template)


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


@pytest.fixture(scope="session")
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


@pytest.fixture(scope="session")
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


@pytest.fixture(scope="session")
def scenario_texts():
    import yaml

    def make(*, suffix="", **fields):
        scenarios = []
        for split in ("train", "holdout"):
            scenario = {
                "schema_version": "1.0",
                "id": f"sevo-00000001-{split}{suffix}",
                "target": "skill:review",
                "description": "人工的な手順不足の再現",
                "prompt": f"人工的な {split} の入力 {suffix} を確認してください。",
                "critical": [
                    {
                        "id": "artifact",
                        "text": "結果を保存",
                        "oracle": "artifact_exists",
                        "path": "result.json",
                    }
                ],
                "holdout": split == "holdout",
                "allowed_tools": ["Read"],
                **fields,
            }
            scenarios.append(yaml.safe_dump(scenario, allow_unicode=True, sort_keys=False))
        return scenarios

    return make


@pytest.fixture(scope="session")
def scenario_template(
    tmp_path_factory, candidate_template, observation_event, candidate_event, scenario_texts
):
    import yaml

    project = tmp_path_factory.mktemp("scenario-template")
    shutil.copytree(candidate_template, project, symlinks=True, dirs_exist_ok=True)
    directory = project / "packages/meta-harness"
    for name in ("schemas", "config"):
        shutil.copytree(REPO / "packages/meta-harness" / name, directory / name)
    scenario_root = directory / "scenarios"
    scenario_root.mkdir()
    (scenario_root / "fixtures").symlink_to(
        REPO / "packages/meta-harness/scenarios/fixtures", target_is_directory=True
    )
    shutil.copytree(REPO / "packages/meta-harness/scenarios/skill", scenario_root / "skill")
    skill = project / ".claude/skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("レビューの手順を確かめる\n")
    suite = directory / "scenarios/skill/review"
    suite.mkdir()
    baseline = yaml.safe_load(scenario_texts()[0])
    baseline.update(id="existing-train", allowed_tools=["Read", "Write"])
    (suite / "existing.yaml").write_text(yaml.safe_dump(baseline, allow_unicode=True))
    with pytest.MonkeyPatch.context() as patch:
        context = build_candidate_context(project, patch)
        context.store.write(lambda _: [observation_event(), candidate_event()])
    return project


@pytest.fixture
def scenario_context(candidate_context_factory, scenario_template):
    return candidate_context_factory(scenario_template)


@pytest.fixture(scope="session")
def fake_claude_factory():
    def make(directory, scenarios=None, *, raw=None, cost=0.01, delay=0, exit_code=0):
        executable = directory / "fake-claude"
        capture = directory / "claude-call.json"
        output = (
            raw
            if raw is not None
            else json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "total_cost_usd": cost,
                    "structured_output": {"scenarios": scenarios},
                }
            )
        )
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            f"Path({str(capture)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'entries': os.listdir('.'), 'env': dict(os.environ), 'stdin': sys.stdin.read()}}))\n"
            f"time.sleep({delay!r})\nprint({output!r})\nraise SystemExit({exit_code!r})\n"
        )
        executable.chmod(0o700)
        return str(executable), capture

    return make


@pytest.fixture
def fake_claude(tmp_path, fake_claude_factory):
    def make(scenarios=None, **options):
        return fake_claude_factory(tmp_path, scenarios, **options)

    return make


@pytest.fixture(scope="session")
def drafted_template(tmp_path_factory, scenario_template, scenario_texts, fake_claude_factory):
    from candidates_draft import draft

    project = tmp_path_factory.mktemp("drafted-template")
    shutil.copytree(scenario_template, project, symlinks=True, dirs_exist_ok=True)
    executable, _ = fake_claude_factory(
        tmp_path_factory.mktemp("drafted-template-cli"), scenario_texts()
    )
    with pytest.MonkeyPatch.context() as patch:
        context = build_candidate_context(project, patch)
        draft(context, "C-00000001", executable=executable)
    return project


@pytest.fixture(scope="module")
def meta_evaluator():
    from tests.module_loader import load_module

    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(REPO / "packages/meta-harness/lib"))
        yield load_module(
            "skill_evolution_contract_evaluator",
            str(REPO / "packages/meta-harness/lib/evaluator.py"),
        )
