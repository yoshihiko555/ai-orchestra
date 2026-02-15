import json

from tests.module_loader import load_module

log_common = load_module("log_common", "packages/core/hooks/log_common.py")


def test_parents_returns_ancestors_in_order(tmp_path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    parents = log_common._parents(str(nested))

    assert parents[0] == str(nested.parent)
    assert str(tmp_path) in parents


def test_find_project_root_returns_nearest_claude_parent(tmp_path) -> None:
    root = tmp_path / "project"
    (root / ".claude").mkdir(parents=True)
    child = root / "src" / "api"
    child.mkdir(parents=True)

    assert log_common.find_project_root(str(child)) == str(root)


def test_find_project_root_falls_back_to_start_dir(tmp_path) -> None:
    start = tmp_path / "orphan" / "work"
    start.mkdir(parents=True)

    assert log_common.find_project_root(str(start)) == str(start)


def test_get_events_log_path_builds_expected_path(tmp_path) -> None:
    root = tmp_path / "project"
    expected = root / ".claude" / "logs" / "orchestration" / "events.jsonl"

    assert log_common.get_events_log_path(str(root)) == str(expected)


def test_truncate_text_respects_max_length() -> None:
    assert log_common.truncate_text("abc", max_length=3) == "abc"
    assert log_common.truncate_text("abcdefgh", max_length=3) == "abc... [8 chars]"


def test_append_jsonl_creates_parent_and_writes_json(tmp_path) -> None:
    out = tmp_path / "logs" / "events.jsonl"
    record = {"event_type": "test", "data": {"ok": True}}

    log_common.append_jsonl(str(out), record)

    line = out.read_text(encoding="utf-8").strip()
    assert json.loads(line) == record


def test_append_event_writes_standard_record(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    log_common.append_event(
        "session_start",
        {"foo": "bar"},
        session_id="s-1",
        hook_name="hook-a",
        project_dir=str(project),
    )

    out = project / ".claude" / "logs" / "orchestration" / "events.jsonl"
    line = out.read_text(encoding="utf-8").strip()
    record = json.loads(line)

    assert record["session_id"] == "s-1"
    assert record["event_type"] == "session_start"
    assert record["hook"] == "hook-a"
    assert record["data"] == {"foo": "bar"}
    assert record["timestamp"]

