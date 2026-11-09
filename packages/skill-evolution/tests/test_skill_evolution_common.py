"""EV-25、EV-44〜48、EV-84: 設定、ハッシュ、追記の契約。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from tests.module_loader import load_module

se = load_module(
    "skill_evolution_common_unit", "packages/skill-evolution/lib/skill_evolution_common.py"
)
REPO = Path(__file__).resolve().parents[3]


def test_ev25_config_defaults_and_local_deep_merge(project, write_config):
    write_config("enabled: false\nruns:\n  hash_max_files: 3\n")
    write_config(
        "enabled: true\nruns:\n  hash_max_bytes: 42\nledger:\n  excerpt_max_chars: 1\n", local=True
    )
    config = se.load_config(str(project))
    assert config["enabled"] is True
    assert config["runs"] == {"hash_max_files": 3, "hash_max_bytes": 42}
    assert config["ledger"] == {"excerpt_max_chars": 1, "lock_wait_seconds": 5}
    assert config["draft"] == {
        "model": "sonnet",
        "max_budget_usd": 1.0,
        "timeout_seconds": 300,
        "max_files": 4,
        "max_file_bytes": 16384,
        "max_attempts_per_candidate": 3,
    }


def test_ev84_v1_keys_are_absent_and_legacy_local_config_loads(project, write_config):
    distributed = REPO / "packages/skill-evolution/config/skill-evolution.yaml"
    for defaults in (se.DEFAULTS, yaml.safe_load(distributed.read_text())):
        assert not {"lessons", "pending", "offline", "trigger"} & defaults.keys()
        assert "dir" not in defaults["storage"]

    legacy = {
        "storage": {"dir": ".claude/legacy"},
        "lessons": {"max_lines": 1},
        "pending": {"stale_after_seconds": 1},
        "offline": {"max_iterations": 1},
        "trigger": {"lessons_threshold": 1},
    }
    write_config(yaml.safe_dump(legacy), local=True)
    config = se.load_config(str(project))
    assert config == {
        **se.DEFAULTS,
        **legacy,
        "storage": {**se.DEFAULTS["storage"], **legacy["storage"]},
    }
    assert se.logs_dir(str(project), config) == str(project / ".claude/logs/skill-evolution")


def test_ev25_config_copies_and_ledger_match_defaults():
    distributed = REPO / "packages/skill-evolution/config/skill-evolution.yaml"
    synced = REPO / ".claude/config/skill-evolution/skill-evolution.yaml"
    assert distributed.read_bytes() == synced.read_bytes()
    assert yaml.safe_load(distributed.read_text()) == se.DEFAULTS
    ledger = json.loads((REPO / ".claude/orchestra.json").read_text())
    assert (
        ledger["file_hashes"]["skill-evolution"]["config/skill-evolution/skill-evolution.yaml"]
        == hashlib.sha256(synced.read_bytes()).hexdigest()
    )


def test_ev25_config_and_core_import_fallback(project, monkeypatch):
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
    assert se._load_hook_common() is not None
    monkeypatch.setattr(se, "_load_hook_common", lambda: None)
    assert se.load_config(str(project)) == se.DEFAULTS


def test_ev47_hash_encoding_and_order_are_deterministic():
    files = [("references/例.md", "参照".encode()), ("SKILL.md", b"steps")]
    expected = hashlib.sha256()
    for relative, content in sorted(files):
        expected.update(relative.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    assert se.hash_skill_files(files) == "sha256:" + expected.hexdigest()
    assert se.hash_skill_files(reversed(files)) == se.hash_skill_files(files)
    assert se.hash_skill_files([("renamed.md", b"steps")]) != se.hash_skill_files(
        [("SKILL.md", b"steps")]
    )


def test_ev47_hash_excludes_hidden_cache_and_internal_symlinks(skill_dir, tmp_path):
    baseline = se.hash_skill_directory(skill_dir)
    (skill_dir / ".hidden").write_text("除外")
    (skill_dir / "__pycache__").mkdir()
    (skill_dir / "__pycache__" / "cache.pyc").write_bytes(b"cache")
    (skill_dir / ".git").mkdir()
    (skill_dir / ".git" / "config").write_text("除外")
    target = tmp_path / "outside"
    target.mkdir()
    (target / "outside.md").write_text("外部")
    (skill_dir / "linked-dir").symlink_to(target, target_is_directory=True)
    (skill_dir / "linked-file").symlink_to(target / "outside.md")
    (skill_dir / "broken-link").symlink_to(tmp_path / "missing")
    os.mkfifo(skill_dir / "pipe")
    assert se.hash_skill_directory(skill_dir) == baseline
    assert (
        se.hash_skill_files(
            [
                ("SKILL.md", (skill_dir / "SKILL.md").read_bytes()),
                (".hidden", b"ignore"),
                ("__pycache__/cache.pyc", b"ignore"),
                ("references/.hidden/x", b"ignore"),
            ]
        )
        == baseline
    )
    references = skill_dir / "references"
    references.mkdir()
    (references / "guide.md").write_text("追加手順")
    changed = se.hash_skill_directory(skill_dir)
    assert changed != baseline
    (references / "guide.md").write_text("変更した手順")
    assert se.hash_skill_directory(skill_dir) != changed


def test_ev47_root_skill_symlink_and_project_precedence(project, skill_dir, tmp_path):
    linked = tmp_path / "linked"
    linked.symlink_to(skill_dir, target_is_directory=True)
    assert se.hash_skill_directory(linked) == se.hash_skill_directory(skill_dir)
    user = Path.home() / ".claude/skills/review"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text("ユーザー版")
    assert se.resolve_skill_directory(str(project), "review") == (
        skill_dir.resolve(),
        "project_skill_dir",
    )
    (skill_dir / "SKILL.md").unlink()
    skill_dir.rmdir()
    assert se.resolve_skill_directory(str(project), "review") == (user.resolve(), "user_skill_dir")


@pytest.mark.parametrize(
    "max_files,max_bytes,expected",
    [(2, 3, True), (1, 3, False), (2, 2, False), (0, 3, False), (2, 0, False)],
)
def test_ev48_hash_cap_boundaries(max_files, max_bytes, expected):
    digest = se.hash_skill_files(
        [("a", b"a"), ("b", b"bc")], max_files=max_files, max_bytes=max_bytes
    )
    assert (digest is not None) == expected


@pytest.mark.parametrize("reason", ["files", "bytes", "missing", "plugin"])
def test_ev48_unresolved_skill_hash_is_null(
    project, skill_dir, write_config, run_hook, read_events, reason
):
    skill = "review"
    if reason == "files":
        write_config("runs:\n  hash_max_files: 0\n")
    elif reason == "bytes":
        write_config("runs:\n  hash_max_bytes: 1\n")
    elif reason == "missing":
        skill = "missing"
    else:
        skill = "plugin:review"
    run_hook("inject-lessons.py", {"tool_name": "Skill", "tool_input": {"skill": skill}})
    [event] = read_events()
    assert event["skill_hash"] is None and event["src"]["skill_hash"] == "unresolved"


def test_ev48_resolved_direct_skill_with_exceeded_cap_still_records(
    skill_dir, write_config, run_hook, read_events
):
    write_config("runs:\n  hash_max_files: 0\n")
    run_hook(
        "record-skill-scope.py",
        {
            "hook_event_name": "UserPromptExpansion",
            "expansion_type": "slash_command",
            "command_name": "review",
        },
    )
    [event] = read_events()
    assert event["trigger"] == "direct" and event["skill_hash"] is None


def test_ev45_event_writer_uses_utc_month_and_private_permissions(project):
    event = se.build_scope_event("turn.started", {"session_id": "s", "prompt_id": "p"})
    event["ts"] = "2026-12-01T00:30:00+09:00"
    assert se.append_run_event(str(project), event)
    directory = project / ".claude/logs/skill-evolution"
    path = directory / "runs-202611.jsonl"
    assert json.loads(path.read_text()) == event
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes().endswith(b"\n")


def test_ev45_writer_rejects_file_symlink_and_returns_false_on_lock_timeout(project, tmp_path):
    directory = project / ".claude/logs/skill-evolution"
    directory.mkdir(parents=True)
    target = tmp_path / "outside.jsonl"
    target.write_bytes(b"untouched")
    path = directory / f"runs-{datetime.now(UTC):%Y%m}.jsonl"
    path.symlink_to(target)
    event = se.build_scope_event("turn.stopped", {"session_id": "s"})
    assert not se.append_run_event(str(project), event)
    assert target.read_bytes() == b"untouched"
    path.unlink()
    with path.open("ab") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not se.append_run_event(str(project), event, {"ledger": {"lock_wait_seconds": 0}})
    assert path.read_bytes() == b""


def test_ev45_concurrent_writers_keep_complete_json_lines(project):
    def append(index):
        event = se.build_scope_event("turn.started", {"session_id": "s", "prompt_id": f"p-{index}"})
        return se.append_run_event(str(project), event)

    se._resolve_log_root_cached(str(project))
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(append, range(32)))
    path = next((project / ".claude/logs/skill-evolution").glob("runs-*.jsonl"))
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert {event["prompt_id"] for event in records} == {f"p-{index}" for index in range(32)}
    assert len(records) == 32


def test_ev45_writer_retries_after_lock_release(project):
    event = se.build_scope_event("turn.started", {"session_id": "s"})
    directory = project / ".claude/logs/skill-evolution"
    directory.mkdir(parents=True)
    path = directory / f"runs-{datetime.now(UTC):%Y%m}.jsonl"
    script = """import fcntl, sys, time
with open(sys.argv[1], 'ab') as stream:
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    print('locked', flush=True)
    time.sleep(0.1)
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE, text=True
    ) as holder:
        assert holder.stdout.readline().strip() == "locked"
        assert se.append_run_event(str(project), event, {"ledger": {"lock_wait_seconds": 1}})
        holder.wait(timeout=3)
    assert json.loads(path.read_text()) == event


def test_ev44_writer_rejects_invalid_event_without_creating_logs(project):
    event = se.build_scope_event("agent.started", {"session_id": "s", "agent_type": "invalid\n"})
    assert not se.append_run_event(str(project), event)
    event = se.build_scope_event("turn.started", {"session_id": "s"})
    event["prompt"] = "記録禁止"
    assert not se.append_run_event(str(project), event)
    assert not (project / ".claude/logs").exists()


def test_ev48_unreadable_directory_is_unresolved(skill_dir, monkeypatch):
    original_walk = os.walk

    def fail_to_scan(directory, **kwargs):
        kwargs["onerror"](PermissionError("読取り不可"))
        return original_walk(directory, **kwargs)

    monkeypatch.setattr(se.os, "walk", fail_to_scan)
    assert se.hash_skill_directory(skill_dir) is None


def test_ev45_writer_starts_a_new_line_after_a_truncated_previous_write(project):
    directory = project / ".claude/logs/skill-evolution"
    directory.mkdir(parents=True)
    path = directory / f"runs-{datetime.now(UTC):%Y%m}.jsonl"
    path.write_bytes(b'{"v": 2, "type": "turn.st')  # 前回の書き込みが途中で失敗した断片
    event = se.build_scope_event("turn.stopped", {"session_id": "s", "prompt_id": "p"})
    assert se.append_run_event(str(project), event)
    lines = path.read_bytes().split(b"\n")
    assert lines[0] == b'{"v": 2, "type": "turn.st'
    assert json.loads(lines[1]) == event
    assert lines[2] == b""


def test_ev45_writer_returns_false_for_malformed_ledger_config(project):
    event = se.build_scope_event("turn.started", {"session_id": "s", "prompt_id": "p"})
    assert se.append_run_event(str(project), event, {"ledger": "not-a-mapping"}) is False


def test_ev48_malformed_hash_limits_resolve_to_unresolved(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("x", encoding="utf-8")
    assert se.hash_skill_directory(skill_dir, {"runs": {"hash_max_files": "many"}}) is None
    assert se.hash_skill_directory(skill_dir, {"runs": "not-a-mapping"}) is None


def test_ev49_regular_checkout_resolves_root_without_git(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)

    def must_not_load_hook_common():
        raise AssertionError("通常の checkout では git の解決を呼ばない")

    monkeypatch.setattr(se, "_load_hook_common", must_not_load_hook_common)
    se._resolve_log_root_cached.cache_clear()
    try:
        assert se._resolve_log_root_cached(str(checkout)) == str(checkout)
    finally:
        se._resolve_log_root_cached.cache_clear()
