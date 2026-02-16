from __future__ import annotations

import json
import os
import sys

from tests.module_loader import REPO_ROOT, load_module

os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)
core_hooks = REPO_ROOT / "packages" / "core" / "hooks"
if str(core_hooks) not in sys.path:
    sys.path.insert(0, str(core_hooks))

hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")
orchestration_bootstrap = load_module(
    "orchestration_bootstrap", "packages/route-audit/hooks/orchestration-bootstrap.py"
)
orchestration_expected_route = load_module(
    "orchestration_expected_route",
    "packages/route-audit/hooks/orchestration-expected-route.py",
)
orchestration_route_audit = load_module(
    "orchestration_route_audit",
    "packages/route-audit/hooks/orchestration-route-audit.py",
)


# ---------------------------------------------------------------------------
# hook_common: read_json_safe / write_json / append_jsonl
# ---------------------------------------------------------------------------


def test_read_json_safe_returns_dict(tmp_path) -> None:
    path = tmp_path / "data.json"
    path.write_text('{"a": 1}', encoding="utf-8")

    assert hook_common.read_json_safe(str(path)) == {"a": 1}


def test_read_json_safe_returns_empty_on_invalid_input(tmp_path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{invalid", encoding="utf-8")
    non_dict = tmp_path / "list.json"
    non_dict.write_text("[1,2,3]", encoding="utf-8")

    assert hook_common.read_json_safe(str(invalid)) == {}
    assert hook_common.read_json_safe(str(non_dict)) == {}
    assert hook_common.read_json_safe(str(tmp_path / "missing.json")) == {}


def test_write_json_and_read_back(tmp_path) -> None:
    json_path = tmp_path / "out.json"
    hook_common.write_json(str(json_path), {"x": "y"})
    assert json.loads(json_path.read_text(encoding="utf-8")) == {"x": "y"}


def test_append_jsonl(tmp_path) -> None:
    jsonl_path = tmp_path / "trace.jsonl"
    hook_common.append_jsonl(str(jsonl_path), {"event": "x"})
    assert json.loads(jsonl_path.read_text(encoding="utf-8").strip()) == {"event": "x"}


# ---------------------------------------------------------------------------
# hook_common: find_first_text / find_first_int
# ---------------------------------------------------------------------------


def test_find_first_text_from_nested_data() -> None:
    payload = {
        "a": {"message": ""},
        "b": [{"x": 1}, {"text": "  selected prompt  "}],
    }
    assert hook_common.find_first_text(payload, {"prompt", "text"}) == "  selected prompt  "


def test_find_first_text_and_int() -> None:
    payload = {
        "a": [{"command": ""}, {"nested": {"command": "pytest -q"}}],
        "b": {"status": "2"},
    }
    assert hook_common.find_first_text(payload, {"command", "cmd"}) == "pytest -q"
    assert hook_common.find_first_int(payload, {"exit_code", "status"}) == 2


# ---------------------------------------------------------------------------
# orchestration-bootstrap: _touch
# ---------------------------------------------------------------------------


def test_bootstrap_touch(tmp_path) -> None:
    touch_path = tmp_path / "state.jsonl"
    orchestration_bootstrap._touch(str(touch_path))
    assert touch_path.exists()
    touch_path.write_text("keep", encoding="utf-8")
    orchestration_bootstrap._touch(str(touch_path))
    assert touch_path.read_text(encoding="utf-8") == "keep"


# ---------------------------------------------------------------------------
# orchestration-expected-route
# ---------------------------------------------------------------------------


def test_expected_route_selects_rule_by_priority() -> None:
    config = {"agents": {}}
    policy = {
        "default_route": "claude-direct",
        "rules": [
            {
                "id": "low-priority",
                "priority": 10,
                "keywords_any": ["batch"],
                "expected_route": "task:tester",
            },
            {
                "id": "high-priority",
                "priority": 100,
                "keywords_any": ["batch"],
                "expected_route": "task:debugger",
            },
        ],
    }
    route, rule_id = orchestration_expected_route.select_expected_route(
        "run the batch job now", config, policy
    )
    assert route == "task:debugger"
    assert rule_id == "high-priority"


def test_expected_route_returns_default_when_no_rule_matches() -> None:
    config = {"agents": {}}
    route, rule_id = orchestration_expected_route.select_expected_route(
        "hello world", config, {"default_route": "claude-direct", "rules": []}
    )
    assert route == "claude-direct"
    assert rule_id is None


def test_expected_route_project_root_precedence(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/env/project")

    assert orchestration_expected_route.project_root({"cwd": "/data/project"}) == "/data/project"
    assert orchestration_expected_route.project_root({}) == "/env/project"


# ---------------------------------------------------------------------------
# orchestration-route-audit
# ---------------------------------------------------------------------------


def test_route_audit_detect_route_for_bash_and_task() -> None:
    route, excerpt = orchestration_route_audit.detect_route(
        {"tool_name": "Bash", "tool_input": {"command": "codex exec --full-auto 'x'"}}
    )
    assert route == "bash:codex"
    assert "codex exec" in excerpt

    route, excerpt = orchestration_route_audit.detect_route(
        {"tool_name": "Bash", "tool_input": {"command": "gemini -p 'x'"}}
    )
    assert route == "bash:gemini"
    assert "gemini -p" in excerpt

    route, excerpt = orchestration_route_audit.detect_route(
        {"tool_name": "Task", "tool_input": {"subagent_type": "tester"}}
    )
    assert route == "task:tester"
    assert excerpt == ""

    route, excerpt = orchestration_route_audit.detect_route(
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
    )
    assert route is None
    assert excerpt == "ls -la"


def test_route_audit_is_match_supports_alias() -> None:
    policy = {"aliases": {"task:tester": ["task:unit-test-agent"]}}

    assert orchestration_route_audit.is_match("task:tester", "task:tester", policy)
    assert orchestration_route_audit.is_match("task:tester", "task:unit-test-agent", policy)
    assert not orchestration_route_audit.is_match("task:tester", "bash:codex", policy)
    assert not orchestration_route_audit.is_match("", "task:tester", policy)
    assert not orchestration_route_audit.is_match("task:tester", "", policy)


def test_route_audit_project_root_precedence(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/env/project")

    assert orchestration_route_audit.project_root({"cwd": "/data/project"}) == "/data/project"
    assert orchestration_route_audit.project_root({}) == "/env/project"
