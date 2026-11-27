"""既存 routing と分離したエージェント設定の検証。"""

from __future__ import annotations

import importlib

import pytest

from tests.module_loader import REPO_ROOT


@pytest.fixture
def profiles(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "packages/core/hooks"))
    return importlib.import_module("api_profiles")


def _config(**fields):
    return {
        "api": {
            "providers": {"gemini": {"model": "test-model"}},
            "profiles": {"p": {"provider": "gemini", **fields}},
        }
    }


def test_agent_limit_defaults(profiles):
    settings = profiles.resolve_agent_settings(_config(), "p")

    assert settings.limits.max_turns == 30
    assert settings.limits.max_total_tokens == 2_000_000
    assert settings.limits.max_duration_sec == 900
    assert settings.limits.rate_limit_attempts == 3
    assert settings.limits.rate_limit_initial_delay_sec == 2.0


def test_agent_limits_accept_explicit_values(profiles):
    values = {
        "max_turns": 7,
        "max_total_tokens": 999,
        "max_duration_sec": 1.5,
        "rate_limit_attempts": 2,
        "rate_limit_initial_delay_sec": 0,
    }

    settings = profiles.resolve_agent_settings(_config(limits=values), "p")

    assert {field: getattr(settings.limits, field) for field in values} == values


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_turns", 0),
        ("max_turns", True),
        ("max_turns", 1.5),
        ("max_total_tokens", -1),
        ("max_total_tokens", "100"),
        ("max_duration_sec", 0),
        ("max_duration_sec", float("inf")),
        ("max_duration_sec", float("nan")),
        ("max_duration_sec", True),
        ("rate_limit_attempts", 0),
        ("rate_limit_attempts", True),
        ("rate_limit_initial_delay_sec", -1),
        ("rate_limit_initial_delay_sec", float("inf")),
        ("rate_limit_initial_delay_sec", "private-invalid-marker"),
    ],
)
def test_agent_rejects_invalid_limits_without_echoing_values(profiles, field, value):
    with pytest.raises(profiles.ProfileError) as caught:
        profiles.resolve_agent_settings(_config(limits={field: value}), "p")

    assert "private-invalid-marker" not in str(caught.value)


@pytest.mark.parametrize(
    "fields",
    [
        {"tools": []},
        {"tools": {"repo_read": "true"}},
        {"tools": {"unknown_tool": True}},
        {"deny": "*.db"},
        {"deny": [123]},
    ],
)
def test_agent_rejects_invalid_tools_and_deny(profiles, fields):
    with pytest.raises(profiles.ProfileError):
        profiles.resolve_agent_settings(_config(**fields), "p")


def test_existing_profile_resolution_keeps_its_routing_fields(profiles):
    from dataclasses import asdict

    config = _config(tools={"repo_read": True}, deny=["*.db"], limits={"max_turns": 3})
    profiles.resolve_agent_settings(config, "p")

    resolved = asdict(profiles.resolve_profile(config, "p"))

    assert set(resolved) == {
        "profile",
        "provider",
        "provider_enabled",
        "model",
        "web_search",
        "attachments",
        "timeout_sec",
        "max_output_tokens",
        "warnings",
    }
