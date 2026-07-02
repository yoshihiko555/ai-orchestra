from __future__ import annotations

from tests.module_loader import load_module

quality_gate_config = load_module(
    "quality_gate_config_standalone", "packages/quality-gates/hooks/quality_gate_config.py"
)


# ---------------------------------------------------------------------------
# resolve_quality_gate_enabled
# ---------------------------------------------------------------------------


def test_resolve_quality_gate_enabled_defaults_to_true_when_key_missing() -> None:
    assert quality_gate_config.resolve_quality_gate_enabled({}) is True


def test_resolve_quality_gate_enabled_respects_false() -> None:
    assert quality_gate_config.resolve_quality_gate_enabled({"enabled": False}) is False


def test_resolve_quality_gate_enabled_respects_true() -> None:
    assert quality_gate_config.resolve_quality_gate_enabled({"enabled": True}) is True


# ---------------------------------------------------------------------------
# get_project_state_key
# ---------------------------------------------------------------------------


def test_get_project_state_key_prefers_git_common_dir(monkeypatch) -> None:
    monkeypatch.setattr(
        quality_gate_config,
        "run_git_command",
        lambda _project_dir, *args: (
            "../../.git\n" if args == ("rev-parse", "--git-common-dir") else ""
        ),
    )

    key = quality_gate_config.get_project_state_key("/repo/.worktrees/feat-4")

    assert key.endswith("/repo/.git")


def test_get_project_state_key_falls_back_to_show_toplevel(monkeypatch) -> None:
    monkeypatch.setattr(
        quality_gate_config,
        "run_git_command",
        lambda _project_dir, *args: "/repo\n" if args == ("rev-parse", "--show-toplevel") else "",
    )

    key = quality_gate_config.get_project_state_key("/repo/subdir")

    assert key == "/repo"


def test_get_project_state_key_falls_back_to_project_dir(monkeypatch) -> None:
    monkeypatch.setattr(quality_gate_config, "run_git_command", lambda *_args: "")

    key = quality_gate_config.get_project_state_key("/not-a-repo")

    assert key == "/not-a-repo"


# ---------------------------------------------------------------------------
# load_project_scoped_state / save_project_scoped_state
# ---------------------------------------------------------------------------


def test_load_project_scoped_state_returns_default_when_missing(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    default_state = {"count": 0, "items": []}

    loaded = quality_gate_config.load_project_scoped_state(state_file, "project-a", default_state)

    assert loaded == default_state
    # Ensure the returned dict is a copy, not the same object as default_state.
    loaded["count"] = 99
    assert default_state["count"] == 0


def test_save_and_load_project_scoped_state_round_trips(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    default_state = {"count": 0, "items": []}

    quality_gate_config.save_project_scoped_state(
        state_file, "project-a", {"count": 3, "items": ["x.py"]}
    )

    loaded = quality_gate_config.load_project_scoped_state(state_file, "project-a", default_state)
    assert loaded == {"count": 3, "items": ["x.py"]}


def test_project_scoped_state_is_isolated_between_projects(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    default_state = {"count": 0, "items": []}

    quality_gate_config.save_project_scoped_state(
        state_file, "project-a", {"count": 5, "items": ["a.py"]}
    )

    loaded_b = quality_gate_config.load_project_scoped_state(state_file, "project-b", default_state)
    assert loaded_b == default_state

    loaded_a = quality_gate_config.load_project_scoped_state(state_file, "project-a", default_state)
    assert loaded_a == {"count": 5, "items": ["a.py"]}
