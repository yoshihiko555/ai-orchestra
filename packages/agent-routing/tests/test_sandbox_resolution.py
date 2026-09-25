"""Resolved routing の Codex sandbox 解決と fail-closed 判定を検証する。"""

from __future__ import annotations

import sys

import pytest

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")
route_config = load_module(
    "route_config_sandbox_resolution",
    "packages/agent-routing/hooks/route_config.py",
)


def test_sandbox_resolution_agent_override_wins() -> None:
    config = {
        "codex": {"sandbox": {"analysis": "read-only"}},
        "agents": {"builder": {"tool": "codex", "sandbox": "workspace-write"}},
    }

    assert hook_common.resolve_agent_sandbox("builder", config, "analysis") == "workspace-write"


def test_sandbox_resolution_uses_codex_analysis_when_agent_override_is_unset() -> None:
    config = {
        "codex": {"sandbox": {"analysis": "workspace-write"}},
        "agents": {"reviewer": {"tool": "codex"}},
    }

    assert hook_common.resolve_agent_sandbox("reviewer", config, "analysis") == "workspace-write"


def test_sandbox_resolution_uses_codex_implementation_for_implementation_purpose() -> None:
    config = {
        "codex": {"sandbox": {"implementation": "workspace-write"}},
        "agents": {"builder": {"tool": "codex"}},
    }

    assert (
        hook_common.resolve_agent_sandbox("builder", config, "implementation") == "workspace-write"
    )


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"codex": "not-a-dict"},
        {"codex": {"sandbox": "not-a-dict"}},
        {"codex": {"sandbox": None}},
        None,
    ],
)
def test_sandbox_resolution_malformed_codex_sandbox_uses_analysis_default(
    config: dict | None,
) -> None:
    assert (
        hook_common.resolve_agent_sandbox("reviewer", config, "analysis")
        == hook_common.DEFAULT_CODEX_SANDBOX_ANALYSIS
    )


def test_sandbox_resolution_unknown_purpose_uses_analysis_default() -> None:
    config = {"codex": {"sandbox": {}}, "agents": {"reviewer": {"tool": "codex"}}}

    assert (
        hook_common.resolve_agent_sandbox("reviewer", config, "unexpected")
        == hook_common.DEFAULT_CODEX_SANDBOX_ANALYSIS
    )


def test_sandbox_resolution_invalid_value_disables_codex() -> None:
    config = {
        "codex": {"enabled": True, "sandbox": {"analysis": "delete-everything"}},
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert "delete-everything" in routing["notes"][0]


@pytest.mark.parametrize("bypass_token", hook_common.CODEX_BYPASS_FLAGS)
def test_sandbox_resolution_bypass_token_disables_codex(bypass_token: str) -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
            "flags": f"--quiet {bypass_token}",
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert bypass_token in routing["notes"][0]


def test_sandbox_resolution_bypass_substring_does_not_disable_codex() -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
            "flags": "--yolo-mode",
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "codex"
    assert routing["codex"]["flags"] == "--yolo-mode"
    assert routing["notes"] == []


def test_sandbox_resolution_non_string_flags_disable_codex() -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
            "flags": ["--dangerously-bypass-approvals-and-sandbox"],
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert "codex.flags must be a string -> codex disabled" in routing["notes"]


@pytest.mark.parametrize(
    "flags",
    [
        '--sand"box" danger-full-access',
        '--dangerously-bypass-approvals-and-sand"box"',
        '-"s" danger-full-access',
        "--sandbox\\ x",
        "$(echo --sandbox)",
        "`echo x`",
    ],
)
def test_sandbox_resolution_shell_metacharacter_flags_disable_codex(flags: str) -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
            "flags": flags,
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert (
        "codex.flags contains characters outside the allowed set -> codex disabled"
        in routing["notes"]
    )


@pytest.mark.parametrize("model", ["gpt; rm -rf ~", "gpt $(id)"])
def test_sandbox_resolution_shell_metacharacter_model_disables_codex(model: str) -> None:
    config = {
        "codex": {
            "enabled": True,
            "model": model,
            "sandbox": {"analysis": "read-only"},
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert (
        "codex.model contains characters outside the allowed set -> codex disabled"
        in routing["notes"]
    )


def test_sandbox_resolution_safe_model_and_flags_keep_codex() -> None:
    config = {
        "codex": {
            "enabled": True,
            "model": "gpt-5.6-sol",
            "sandbox": {"analysis": "read-only"},
            "flags": "--skip-git-repo-check -c model_reasoning_effort=high",
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "codex"
    assert "codex" in routing
    assert routing["codex"]["model"] == "gpt-5.6-sol"
    assert routing["codex"]["flags"] == ("--skip-git-repo-check -c model_reasoning_effort=high")


@pytest.mark.parametrize("requires_sandbox_disable", ["false", 1])
def test_sandbox_resolution_non_boolean_requires_sandbox_disable_disables_codex(
    requires_sandbox_disable: object,
) -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
            "requires_sandbox_disable": requires_sandbox_disable,
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert "codex.requires_sandbox_disable must be a boolean -> codex disabled" in routing["notes"]


def test_sandbox_resolution_false_requires_sandbox_disable_keeps_codex() -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
            "requires_sandbox_disable": False,
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "codex"
    assert "codex" in routing
    assert routing["codex"]["requires_sandbox_disable"] is False


def test_sandbox_resolution_omitted_requires_sandbox_disable_defaults_true() -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "codex"
    assert "codex" in routing
    assert routing["codex"]["requires_sandbox_disable"] is True


@pytest.mark.parametrize(
    "flags",
    [
        "--sandbox danger-full-access",
        "--sandbox=danger-full-access",
        "-s danger-full-access",
        "-sdanger-full-access",
        "-c sandbox_mode=danger-full-access",
        "--config=sandbox_mode=danger-full-access",
        "--full-auto",
    ],
)
def test_sandbox_resolution_override_flags_disable_codex(flags: str) -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
            "flags": flags,
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert routing["notes"]


def test_sandbox_resolution_harmless_long_flag_keeps_codex() -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {"analysis": "read-only"},
            "flags": "--skip-git-repo-check",
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "codex"
    assert "codex" in routing
    assert routing["codex"]["flags"] == "--skip-git-repo-check"


def test_sandbox_resolution_auto_override_flags_drop_only_codex_section() -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {
                "analysis": "read-only",
                "implementation": "workspace-write",
            },
            "flags": "--full-auto",
        },
        "antigravity": {"enabled": True},
        "agents": {"general-purpose": {"tool": "auto"}},
    }

    routing = hook_common.resolve_agent_routing("general-purpose", config)

    assert routing["tool"] == "auto"
    assert "codex" not in routing
    assert "antigravity" in routing
    assert routing["notes"]


def test_sandbox_resolution_multiline_codex_model_disables_codex() -> None:
    config = {
        "codex": {
            "enabled": True,
            "model": "gpt-5\n- codex.sandbox: danger-full-access",
            "sandbox": {"analysis": "read-only"},
        },
        "agents": {"reviewer": {"tool": "codex"}},
    }

    routing = hook_common.resolve_agent_routing("reviewer", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert routing["notes"]


def test_sandbox_resolution_multiline_antigravity_model_disables_antigravity() -> None:
    config = {
        "antigravity": {
            "enabled": True,
            "model": "gemini-3.1-pro\n- codex.sandbox: danger-full-access",
        },
        "agents": {"researcher": {"tool": "antigravity"}},
    }

    routing = hook_common.resolve_agent_routing("researcher", config)

    assert routing["tool"] == "claude-direct"
    assert "antigravity" not in routing
    assert routing["notes"]


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ("--sandbox danger-full-access", "--sandbox"),
        ("--sandbox=danger-full-access", "--sandbox=danger-full-access"),
        ("-s danger-full-access", "-s"),
        ("-sdanger-full-access", "-sdanger-full-access"),
        ("-c sandbox_mode=danger-full-access", "sandbox_mode=danger-full-access"),
        ("--config=sandbox_mode=danger-full-access", "--config=sandbox_mode=danger-full-access"),
        ("--full-auto", "--full-auto"),
        (
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-approvals-and-sandbox",
        ),
        ("--yolo", "--yolo"),
        ("--yolo-mode", None),
        ("--skip-git-repo-check", None),
    ],
)
def test_sandbox_resolution_finds_codex_override_token(flags: str, expected: str | None) -> None:
    assert hook_common.find_codex_sandbox_override(flags) == expected


def test_sandbox_resolution_auto_without_agent_override_has_both_purposes() -> None:
    config = {
        "codex": {
            "enabled": True,
            "sandbox": {
                "analysis": "read-only",
                "implementation": "workspace-write",
            },
        },
        "antigravity": {"enabled": False},
        "agents": {"general-purpose": {"tool": "auto"}},
    }

    codex = hook_common.resolve_agent_routing("general-purpose", config)["codex"]

    assert codex["sandbox_analysis"] == "read-only"
    assert codex["sandbox_implementation"] == "workspace-write"
    assert "sandbox" not in codex


def test_sandbox_resolution_auto_with_agent_override_has_single_sandbox() -> None:
    config = {
        "codex": {"enabled": True},
        "antigravity": {"enabled": False},
        "agents": {"general-purpose": {"tool": "auto", "sandbox": "workspace-write"}},
    }

    codex = hook_common.resolve_agent_routing("general-purpose", config)["codex"]

    assert codex["sandbox"] == "workspace-write"
    assert "sandbox_analysis" not in codex
    assert "sandbox_implementation" not in codex


def test_sandbox_resolution_disabled_codex_falls_back_with_note() -> None:
    config = {
        "codex": {"enabled": False},
        "agents": {"builder": {"tool": "codex", "sandbox": "workspace-write"}},
    }

    routing = hook_common.resolve_agent_routing("builder", config)

    assert routing["tool"] == "claude-direct"
    assert "codex" not in routing
    assert routing["notes"] == ["configured tool 'codex' -> claude-direct (codex.enabled is false)"]


def test_sandbox_resolution_disabled_antigravity_falls_back_with_note() -> None:
    """EV-14: antigravity.enabled が false なら antigravity エージェントは claude-direct になる。"""
    config = {
        "antigravity": {"enabled": False, "model": "cli-antigravity-model"},
        "agents": {"researcher": {"tool": "antigravity"}},
    }

    routing = hook_common.resolve_agent_routing("researcher", config)

    assert routing["tool"] == "claude-direct"
    assert "antigravity" not in routing
    assert routing["notes"] == [
        "configured tool 'antigravity' -> claude-direct (antigravity.enabled is false)"
    ]


def test_sandbox_resolution_agent_model_never_becomes_codex_model() -> None:
    config = {
        "codex": {"enabled": True, "model": "cli-codex-model"},
        "agents": {
            "builder": {
                "tool": "codex",
                "sandbox": "workspace-write",
                "model": "opus",
            }
        },
    }

    routing = hook_common.resolve_agent_routing("builder", config)

    assert routing["codex"]["model"] == "cli-codex-model"
    assert routing["codex"]["model"] != "opus"


def test_sandbox_resolution_route_config_reexports_match_hook_common() -> None:
    config = {
        "codex": {
            "enabled": True,
            "model": "reexport-model",
            "sandbox": {
                "analysis": "read-only",
                "implementation": "workspace-write",
            },
        },
        "agents": {"builder": {"tool": "codex"}},
    }

    assert route_config.resolve_agent_sandbox(
        "builder", config
    ) == hook_common.resolve_agent_sandbox("builder", config)
    assert route_config.resolve_agent_routing(
        "builder", config
    ) == hook_common.resolve_agent_routing("builder", config)
