"""API プロファイルの実効値を route_config と hook の入口から検証する。"""

from __future__ import annotations

import importlib
import io
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module

hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")
route_config = load_module("api_route_config", "packages/agent-routing/hooks/route_config.py")
inject = load_module("api_inject_context", "packages/core/hooks/inject-shared-context.py")


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def api_config() -> dict:
    return {
        "agents": {"researcher": {"tool": "api", "api_profile": "research"}},
        "api": {
            "providers": {
                "gemini": {
                    "enabled": True,
                    "model": "provider-model",
                    "model_allowlist": ["provider-model", "profile-model"],
                }
            },
            "profiles": {
                "research": {
                    "provider": "gemini",
                    "model": "profile-model",
                    "features": {"web_search": True, "attachments": True},
                    "limits": {"timeout_sec": 23, "max_output_tokens": 321},
                }
            },
        },
    }


def _profiles():
    return importlib.import_module("api_profiles")


def _available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-routing-private-marker")
    monkeypatch.setattr(_profiles(), "provider_dependency_available", lambda provider: True)


def test_route_config_resolves_profile_values_without_secret_values(
    api_config, monkeypatch
) -> None:
    _available(monkeypatch)

    routing = route_config.resolve_agent_routing("researcher", api_config)

    assert routing["tool"] == "api"
    assert route_config.get_agent_tool("researcher", api_config) == "api"
    assert set(routing["api"]) == {
        "profile",
        "provider",
        "model",
        "web_search",
        "attachments",
        "timeout_sec",
        "max_output_tokens",
    }
    assert routing["api"]["profile"] == "research"
    assert routing["api"]["provider"] == "gemini"
    assert routing["api"]["model"] == "profile-model"
    assert routing["api"]["web_search"] is True
    assert routing["api"]["attachments"] is True
    assert routing["api"]["timeout_sec"] == 23
    assert routing["api"]["max_output_tokens"] == 321
    assert "dummy-routing" not in json.dumps(routing)
    assert "task:researcher" in route_config.build_aliases(api_config)["api"]


def test_profile_uses_provider_model_when_not_overridden(api_config, monkeypatch) -> None:
    _available(monkeypatch)
    del api_config["api"]["profiles"]["research"]["model"]

    routing = route_config.resolve_agent_routing("researcher", api_config)

    assert routing["tool"] == "api"
    assert routing["api"]["model"] == "provider-model"


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("key_missing", r"key|キー"),
        ("provider_disabled", r"disabled|無効"),
        ("dependency_missing", r"dependenc|SDK|依存|llm"),
        ("profile_missing", r"profile|プロファイル"),
    ],
)
def test_unavailable_api_falls_back_with_reason(api_config, monkeypatch, condition, reason) -> None:
    _available(monkeypatch)
    if condition == "key_missing":
        monkeypatch.delenv("GEMINI_API_KEY")
    elif condition == "provider_disabled":
        api_config["api"]["providers"]["gemini"]["enabled"] = False
    elif condition == "dependency_missing":
        monkeypatch.setattr(_profiles(), "provider_dependency_available", lambda provider: False)
    else:
        api_config["agents"]["researcher"]["api_profile"] = "undefined"

    routing = route_config.resolve_agent_routing("researcher", api_config)

    assert routing["tool"] == "claude-direct"
    assert route_config.get_agent_tool("researcher", api_config) == "claude-direct"
    assert "api" not in routing
    notes = "\n".join(routing["notes"])
    assert re.search(reason, notes, re.IGNORECASE)
    if condition == "key_missing":
        assert "orchex auth set gemini" in notes
    assert "dummy-routing" not in notes
    assert "task:researcher" in route_config.build_aliases(api_config)["claude-direct"]


@pytest.mark.parametrize("invalid_profile", [None, [], {"provider": "unknown"}])
def test_invalid_profile_falls_back_instead_of_advertising_api(
    api_config, monkeypatch, invalid_profile
) -> None:
    _available(monkeypatch)
    api_config["api"]["profiles"]["research"] = invalid_profile

    routing = route_config.resolve_agent_routing("researcher", api_config)

    assert routing["tool"] == "claude-direct"
    assert "api" not in routing
    assert routing["notes"]


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("features", "web_search", "false"),
        ("features", "attachments", "true"),
        ("limits", "timeout_sec", 0),
        ("limits", "max_output_tokens", -1),
    ],
)
def test_invalid_profile_features_and_limits_fall_back(
    api_config, monkeypatch, section, field, value
) -> None:
    _available(monkeypatch)
    api_config["api"]["profiles"]["research"][section][field] = value

    routing = route_config.resolve_agent_routing("researcher", api_config)

    assert routing["tool"] == "claude-direct"
    assert "api" not in routing
    assert routing["notes"]


def test_invalid_model_cannot_inject_routing_lines(api_config, monkeypatch) -> None:
    _available(monkeypatch)
    api_config["api"]["profiles"]["research"]["model"] = "bad\n- tool: codex"

    routing = route_config.resolve_agent_routing("researcher", api_config)
    lines = inject.build_routing_value_lines(routing)

    assert routing["tool"] == "claude-direct"
    assert "api" not in routing
    assert all(len(line.splitlines()) == 1 for line in lines)
    assert all("bad" not in line for line in lines)


def test_disabled_provider_is_reason_when_key_and_dependency_are_also_missing(
    api_config, monkeypatch
) -> None:
    api_config["api"]["providers"]["gemini"]["enabled"] = False
    monkeypatch.setattr(_profiles(), "provider_dependency_available", lambda provider: False)

    routing = route_config.resolve_agent_routing("researcher", api_config)

    assert routing["tool"] == "claude-direct"
    assert len(routing["notes"]) == 1
    assert re.search(r"disabled|無効", routing["notes"][0], re.I)


def test_missing_google_namespace_is_unavailable_instead_of_import_error(monkeypatch) -> None:
    profiles = _profiles()

    def missing_namespace(name):
        raise ModuleNotFoundError("google は未導入")

    monkeypatch.setattr(importlib.util, "find_spec", missing_namespace)

    assert profiles.provider_dependency_available("gemini") is False


@pytest.mark.parametrize("event", ["SubagentStart", "Agent", "Task"])
@pytest.mark.parametrize(
    ("profile_name", "expected_command"),
    [
        ("research", "orchex llm ask --profile research"),
        ("-research", "orchex llm ask --profile=-research"),
    ],
)
def test_hook_delivers_local_profile_values_without_secrets(
    tmp_path, api_config, monkeypatch, event, profile_name, expected_command
) -> None:
    _available(monkeypatch)
    api_config["api"]["profiles"][profile_name] = api_config["api"]["profiles"].pop("research")
    api_config["agents"]["researcher"]["api_profile"] = profile_name
    config_dir = tmp_path / ".claude/config/agent-routing"
    config_dir.mkdir(parents=True)
    (config_dir / "cli-tools.yaml").write_text(yaml.safe_dump(api_config), encoding="utf-8")
    (config_dir / "cli-tools.local.yaml").write_text(
        yaml.safe_dump({"api": {"profiles": {profile_name: {"model": "local-model"}}}}),
        encoding="utf-8",
    )
    payload = {"cwd": str(tmp_path)}
    if event == "SubagentStart":
        payload.update(hook_event_name=event, agent_type="researcher")
    else:
        payload.update(
            hook_event_name="PreToolUse",
            tool_name=event,
            tool_input={"subagent_type": "researcher", "prompt": "資料を調査してください"},
        )
    output = io.StringIO()

    with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
        with patch.object(sys, "stdout", output):
            inject.main()

    result = json.loads(output.getvalue())["hookSpecificOutput"]
    assert set(result) == {"hookEventName", "additionalContext"}
    context = result["additionalContext"]
    fields = dict(line[2:].split(": ", 1) for line in context.splitlines() if line.startswith("- "))
    assert fields["tool"] == "api"
    assert fields["api.profile"] == profile_name
    assert fields["api.provider"] == "gemini"
    assert fields["api.model"] == "local-model"
    assert fields["api.web_search"] == "true"
    assert fields["api.attachments"] == "true"
    assert float(fields["api.timeout_sec"]) == 23
    assert int(fields["api.max_output_tokens"]) == 321
    assert fields["api.command"] == expected_command
    assert all("dummy-routing" not in value for value in fields.values())
    assert "dummy-routing" not in context
    if event == "SubagentStart":
        assert "[Resolved Routing]" in context.splitlines()
    else:
        assert context.startswith("[Subagent Context Preview]")


def test_default_profiles_route_only_research_to_gemini(monkeypatch) -> None:
    _available(monkeypatch)
    config = yaml.safe_load(
        (REPO_ROOT / "packages/agent-routing/config/cli-tools.yaml").read_text(encoding="utf-8")
    )

    researcher = hook_common.resolve_agent_routing("researcher", config)

    assert researcher["tool"] == "api"
    assert researcher["api"]["profile"] == "research"
    assert researcher["api"]["provider"] == "gemini"
    assert researcher["api"]["web_search"] is True
    assert researcher["api"]["attachments"] is True
    assert set(config["api"]["profiles"]) == {"research", "repo-reader"}
    for agent in config["agents"]:
        routing = hook_common.resolve_agent_routing(agent, config)
        if routing["tool"] == "api":
            assert routing["api"]["provider"] == "gemini"
            assert routing["api"]["profile"] != "repo-reader"


def test_shipped_repo_reader_resolves_without_local_overrides() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / "packages/agent-routing/config/cli-tools.yaml").read_text(encoding="utf-8")
    )

    settings = _profiles().resolve_agent_settings(config, "repo-reader")

    assert settings.profile.provider == "gemini"
    assert settings.repo_read is True
    assert settings.limits == _profiles().AgentLimits()
    assert settings.profile.timeout_sec == 120
    assert settings.profile.max_output_tokens == 16384
    assert set(config["api"]["profiles"]["repo-reader"]["limits"]) == {"max_output_tokens"}
