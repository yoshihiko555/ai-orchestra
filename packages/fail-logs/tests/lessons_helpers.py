"""教訓候補のテストで共有する部品（台帳の用意・候補の追加・CLI の実行）。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from tests.module_loader import REPO_ROOT, load_module

LIB_DIR = REPO_ROOT / "packages" / "fail-logs" / "lib"
CORE_HOOKS = REPO_ROOT / "packages" / "core" / "hooks"
for _path in (str(CORE_HOOKS), str(LIB_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import distill_signals as signals_lib  # noqa: E402, F401  （テストから h.signals_lib で使う）
import lessons_model as model  # noqa: E402
import lessons_store as store_lib  # noqa: E402
import masking  # noqa: E402
import transcript_reader as reader  # noqa: E402, F401  （テストから h.reader で使う）

cli = load_module("lessons_cli_under_test", "packages/fail-logs/scripts/lessons_cli.py")
admin = load_module("lessons_admin_under_test", "packages/fail-logs/scripts/lessons_admin.py")

LOG_REL = Path(".claude") / "logs" / "fail-logs"
API_KEY_SAMPLE = "sk-" + "abcdefghijklmnopqrstuvwxyz012345"


def make_project(
    tmp_path: Path, *, installed: bool = True, local_config: str | None = None
) -> Path:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    packages = ["core", "fail-logs"] if installed else ["core"]
    (project / ".claude" / "orchestra.json").write_text(
        json.dumps({"installed_packages": packages}), encoding="utf-8"
    )
    if local_config is not None:
        config_dir = project / ".claude" / "config" / "fail-logs"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "fail-logs.local.yaml").write_text(local_config, encoding="utf-8")
    return project


def log_dir(project: Path) -> Path:
    return project / LOG_REL


def strong_masker():
    found = masking.load_masker(
        str(REPO_ROOT / "packages" / "audit" / "hooks" / ("secret_" + "masking.py"))
    )
    assert found is not None
    return found


def lesson_payload(**overrides: object) -> dict:
    payload: dict = {
        "title": "sandbox では mktemp を引数なしで使わない",
        "event": "mktemp が失敗した",
        "cause": "sandbox の書き込み許可の外",
        "avoidance": "TMPDIR をテンプレートに渡す",
        "scope": "claude",
        "severity": "normal",
        "signals": ["tool_error"],
    }
    payload.update(overrides)
    return payload


def created_event(
    lid: str | None = None,
    *,
    session: str = "sess-aaaa-0001",
    actor: str = "worker",
    **overrides: object,
) -> dict:
    data = model.build_lesson_data(
        lesson_payload(**overrides),
        strong_masker(),
        source="distill",
        evidence={"kind": "session", "ref": session},
    )
    return model.make_event(model.EV_CREATED, lid or model.new_lid(), actor, session, data)


def evidence_event(
    lid: str,
    *,
    kind: str = "session",
    ref: str = "sess-bbbb-0002",
    actor: str = "worker",
    **overrides: object,
) -> dict:
    source = "review" if kind == "pr" else "distill"
    data = model.build_lesson_data(
        lesson_payload(**overrides),
        strong_masker(),
        source=source,
        evidence={"kind": kind, "ref": ref},
    )
    return model.make_event(model.EV_EVIDENCE, lid, actor, "", data)


def state_event(snapshot: dict, lid: str, to: str, target: dict | None = None) -> dict:
    lesson = snapshot["lessons"][lid]
    return model.make_event(
        model.EV_STATE, lid, "skill", "", model.build_state_data(lesson["state"], to, target, "")
    )


def new_store(project: Path) -> store_lib.LessonStore:
    return store_lib.LessonStore(str(log_dir(project)), lock_wait_seconds=2)


def append(store: store_lib.LessonStore, *events: dict) -> dict:
    snapshot, _ = store.append_events(lambda _snap: list(events))
    return snapshot


def run_cli(
    module,
    monkeypatch,
    project: Path,
    argv: list[str],
    stdin: str = "",
    session: str | None = None,
    *,
    transcript: bool = True,
) -> int:
    """CLI を同じプロセスで実行する。session を渡すと、そのセッションの記録（transcript）も用意する。"""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    config_dir = project.parent / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    if session is None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
        if transcript:
            projects = config_dir / "projects" / "proj"
            projects.mkdir(parents=True, exist_ok=True)
            (projects / f"{session}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8"))))
    return module.main(argv)
