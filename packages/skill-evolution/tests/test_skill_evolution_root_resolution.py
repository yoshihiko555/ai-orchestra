"""EV-49: 実行範囲記録を root に集約し、安全な保存先だけを使う。"""

from __future__ import annotations

import json

import pytest

from tests.module_loader import load_module

se = load_module(
    "skill_evolution_common_root", "packages/skill-evolution/lib/skill_evolution_common.py"
)


@pytest.fixture(autouse=True)
def clear_root_cache():
    se._resolve_log_root_cached.cache_clear()
    yield
    se._resolve_log_root_cached.cache_clear()


def test_ev49_linked_worktree_events_are_written_to_root(project, tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / ".claude").mkdir(parents=True)
    core = se._load_hook_common()
    calls = []

    def resolve(path, **kwargs):
        calls.append(path)
        return str(root)

    monkeypatch.setattr(core, "resolve_root_worktree", resolve)
    config = {"storage": {"logs_dir": ".claude/logs/custom-runs"}}
    payload = {"session_id": "s", "prompt_id": "p"}
    assert se.record_run_started(str(project), payload, "review", "skill_tool", config)
    assert se.append_run_event(str(project), se.build_scope_event("turn.stopped", payload), config)
    paths = list((root / ".claude/logs/custom-runs").glob("runs-*.jsonl"))
    [event, stop] = [json.loads(line) for line in paths[0].read_text().splitlines()]
    assert event["project"] == "root"
    assert stop["type"] == "turn.stopped"
    assert not (project / ".claude/logs").exists()
    assert calls == [str(project)]


@pytest.mark.parametrize("failure", ["none", "exception", "no-core", "no-claude"])
def test_ev49_git_failure_falls_back_to_project(project, tmp_path, monkeypatch, failure):
    core = se._load_hook_common()
    if failure == "no-core":
        monkeypatch.setattr(se, "_load_hook_common", lambda: None)
    elif failure == "exception":

        def fail(*args, **kwargs):
            raise OSError("git 失敗")

        monkeypatch.setattr(core, "resolve_root_worktree", fail)
    else:
        monkeypatch.setattr(
            core,
            "resolve_root_worktree",
            lambda *args, **kwargs: str(tmp_path / "no-claude") if failure == "no-claude" else None,
        )
    assert se.logs_dir(str(project)) == str(project / ".claude/logs/skill-evolution")
    assert se.append_run_event(
        str(project), se.build_scope_event("turn.started", {"session_id": "s"})
    )
    assert len(list((project / ".claude/logs/skill-evolution").glob("runs-*.jsonl"))) == 1


@pytest.mark.parametrize("relative", ["../../../outside", "/outside", "escape/runs"])
def test_ev49_traversal_and_symlink_escape_fall_back_to_default(
    project, tmp_path, monkeypatch, relative
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(se, "_resolve_log_root_cached", lambda path: str(project))
    config = {"storage": {"logs_dir": relative}}
    assert se.logs_dir(str(project), config) == str(project / ".claude/logs/skill-evolution")
    assert se.append_run_event(str(project), se.build_scope_event("turn.started", {}), config)
    assert list(outside.iterdir()) == []


def test_ev49_default_directory_escape_fails_without_writing(project, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".claude/logs").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(se, "_resolve_log_root_cached", lambda path: str(project))
    with pytest.raises(ValueError, match="安全な保存先"):
        se.logs_dir(str(project))
    assert not se.append_run_event(str(project), se.build_scope_event("turn.stopped", {}))
    assert list(outside.iterdir()) == []
