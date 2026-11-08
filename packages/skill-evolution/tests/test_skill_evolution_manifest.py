"""EV-84: 既存 hook の互換性と追加イベントの同期・削除。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from tests.module_loader import load_module

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
sync = load_module("skill_evolution_sync_test", "scripts/lib/sync_engine.py")
manager_module = load_module("skill_evolution_manager_test", "scripts/orchestra-manager.py")


def test_ev84_manifest_preserves_existing_hooks_and_adds_scope_hooks():
    manifest = json.loads((REPO / "packages/skill-evolution/manifest.json").read_text())
    assert manifest["version"] == "0.2.0"
    # v1 と同じファイル名・matcher を保ち、root の解決（git）とロック待ちが収まる timeout を付ける
    assert manifest["hooks"] == {
        "PreToolUse": [{"file": "inject-lessons.py", "matcher": "Skill", "timeout": 10}],
        "PostToolUse": [{"file": "capture-skill-telemetry.py", "matcher": "Skill", "timeout": 10}],
        "SubagentStop": [{"file": "capture-subagent-skill.py", "timeout": 10}],
        "Stop": [{"file": "capture-skill-stop.py", "timeout": 10}],
        "UserPromptExpansion": [{"file": "record-skill-scope.py", "timeout": 10}],
        "UserPromptSubmit": [{"file": "record-skill-scope.py", "timeout": 10}],
        "SubagentStart": [{"file": "record-skill-scope.py", "timeout": 10}],
    }
    assert {path for path in manifest["files"] if path.startswith("hooks/")} == {
        "hooks/inject-lessons.py",
        "hooks/capture-skill-telemetry.py",
        "hooks/capture-subagent-skill.py",
        "hooks/capture-skill-stop.py",
        "hooks/record-skill-scope.py",
    }


def test_ev84_sync_and_uninstall_support_new_events_with_legacy_local_config(project, write_config):
    local = write_config(
        "lessons: {max_lines: 1}\npending: {stale_after_seconds: 1}\noffline: {}\ntrigger: {lessons_threshold: 1}\n",
        local=True,
    )
    before = local.read_bytes()
    config_key = "config/skill-evolution/skill-evolution.yaml"
    distributed = REPO / "packages/skill-evolution/config/skill-evolution.yaml"
    config = project / ".claude" / config_key
    config.write_bytes(distributed.read_bytes())
    ledger = project / ".claude/orchestra.json"
    ledger.write_text(
        json.dumps(
            {
                "installed_packages": ["skill-evolution"],
                "file_hashes": {
                    "skill-evolution": {config_key: hashlib.sha256(config.read_bytes()).hexdigest()}
                },
            }
        )
    )
    settings = project / ".claude/settings.local.json"
    settings.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "custom-stop"}]}]}}
        )
    )
    assert sync.sync_hooks(project, REPO, ["skill-evolution"]) == 7
    hooks = json.loads(settings.read_text())["hooks"]
    assert set(hooks) == {
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStop",
        "UserPromptExpansion",
        "UserPromptSubmit",
        "SubagentStart",
    }
    for event in ("UserPromptExpansion", "UserPromptSubmit", "SubagentStart"):
        assert any(
            "record-skill-scope.py" in hook["command"]
            for entry in hooks[event]
            for hook in entry["hooks"]
        )
    assert all(
        hook.get("timeout") == 10
        for entries in hooks.values()
        for entry in entries
        for hook in entry["hooks"]
        if "skill-evolution" in hook["command"]
    )
    assert sync.sync_hooks(project, REPO, ["skill-evolution"]) == 0
    assert local.read_bytes() == before

    manager_module.OrchestraManager(REPO).uninstall("skill-evolution", str(project))

    hooks = json.loads(settings.read_text())["hooks"]
    assert all(
        "skill-evolution" not in hook["command"]
        for entries in hooks.values()
        for entry in entries
        for hook in entry["hooks"]
    )
    assert any(
        hook["command"] == "custom-stop" for entry in hooks["Stop"] for hook in entry["hooks"]
    )
    assert json.loads(ledger.read_text())["installed_packages"] == []
    assert not config.exists()
    assert local.read_bytes() == before
