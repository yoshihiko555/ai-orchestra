"""routing_suggestion を含むログで kpi-report / log-viewer が壊れないことのテスト（audit EV-30）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.module_loader import REPO_ROOT

SCRIPTS_DIR = REPO_ROOT / "packages" / "audit" / "scripts"
SESSION_ID = "routing-session"

BASE_EVENTS = [
    {
        "v": 1,
        "ts": "2026-10-29T10:00:00+00:00",
        "sid": SESSION_ID,
        "type": "prompt",
        "tid": "t1",
        "data": {"expected_route": "claude-direct", "user_input_excerpt": "テストを追加して"},
    },
    {
        "v": 1,
        "ts": "2026-10-29T10:00:05+00:00",
        "sid": SESSION_ID,
        "type": "route_decision",
        "tid": "t1",
        "data": {"expected": "claude-direct", "actual": "claude-direct", "matched": True},
    },
]
ROUTING_EVENT = {
    "v": 1,
    "ts": "2026-10-29T10:00:00.200000+00:00",
    "sid": SESSION_ID,
    "type": "routing_suggestion",
    "tid": "t2",
    "data": {
        "engine": "jev",
        "status": "ok",
        "decided_by": "jev",
        "suggested": "tester",
        "p_none": 0.2,
        "latency_ms": 180,
        "probabilities": {"none": 0.2, "tester": 0.8},
        "keyword": {"agent": "tester", "trigger": "テスト"},
    },
}


def _write_log(project_dir: Path, events: list[dict]) -> None:
    sessions = project_dir / ".claude" / "logs" / "audit" / "sessions"
    sessions.mkdir(parents=True)
    lines = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    (sessions / f"{SESSION_ID}.jsonl").write_text(lines, encoding="utf-8")


def _run_script(name: str, project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), "--project", str(project_dir), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_kpi_report_ignores_routing_suggestion(tmp_path: Path) -> None:
    without = tmp_path / "without"
    with_routing = tmp_path / "with"
    _write_log(without, BASE_EVENTS)
    _write_log(with_routing, [*BASE_EVENTS, ROUTING_EVENT])

    base = _run_script("kpi-report.py", without)
    result = _run_script("kpi-report.py", with_routing)

    assert base.returncode == 0, base.stderr
    assert result.returncode == 0, result.stderr
    # イベント総数だけが 1 増え、ルーティング・CLI・サブエージェント等の集計は変わらない
    assert "**Total events**: 3" in result.stdout
    assert result.stdout.replace("**Total events**: 3", "**Total events**: 2") == base.stdout


def test_log_viewer_shows_routing_suggestion(tmp_path: Path) -> None:
    _write_log(tmp_path, [*BASE_EVENTS, ROUTING_EVENT])

    result = _run_script("log-viewer.py", tmp_path, "--type", "routing_suggestion")

    assert result.returncode == 0, result.stderr
    assert "routing_suggestion" in result.stdout
    assert "engine=jev status=ok suggested=tester p_none=0.2 latency_ms=180" in result.stdout


def test_log_viewer_lists_all_events_with_routing_suggestion(tmp_path: Path) -> None:
    _write_log(tmp_path, [*BASE_EVENTS, ROUTING_EVENT])

    result = _run_script("log-viewer.py", tmp_path)

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip().splitlines()) == 3
