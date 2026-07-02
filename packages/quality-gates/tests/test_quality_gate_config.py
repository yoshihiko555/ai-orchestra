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


# ---------------------------------------------------------------------------
# update_project_scoped_state
# ---------------------------------------------------------------------------


def test_update_project_scoped_state_mutates_and_persists(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    default_state = {"count": 0, "items": []}

    def mutate(state: dict) -> dict:
        state["count"] += 1
        state["items"].append("x.py")
        return state

    result = quality_gate_config.update_project_scoped_state(
        state_file, "project-a", mutate, default_state
    )

    assert result == {"count": 1, "items": ["x.py"]}

    reloaded = quality_gate_config.load_project_scoped_state(state_file, "project-a", default_state)
    assert reloaded == {"count": 1, "items": ["x.py"]}


def test_update_project_scoped_state_honors_default_when_missing(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    default_state = {"count": 0, "items": []}
    seen_states = []

    def mutate(state: dict) -> dict:
        seen_states.append(dict(state))
        return state

    quality_gate_config.update_project_scoped_state(
        state_file, "unseen-project", mutate, default_state
    )

    assert seen_states == [{"count": 0, "items": []}]


def test_update_project_scoped_state_does_not_clobber_other_projects(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    default_state = {"count": 0, "items": []}

    quality_gate_config.save_project_scoped_state(
        state_file, "project-a", {"count": 5, "items": ["a.py"]}
    )

    quality_gate_config.update_project_scoped_state(
        state_file, "project-b", lambda state: {**state, "count": 1}, default_state
    )

    loaded_a = quality_gate_config.load_project_scoped_state(state_file, "project-a", default_state)
    assert loaded_a == {"count": 5, "items": ["a.py"]}

    loaded_b = quality_gate_config.load_project_scoped_state(state_file, "project-b", default_state)
    assert loaded_b == {"count": 1, "items": []}


def test_update_project_scoped_state_leaves_no_stray_tmp_files(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    default_state = {"count": 0}

    quality_gate_config.update_project_scoped_state(
        state_file, "project-a", lambda state: {"count": state["count"] + 1}, default_state
    )

    remaining = {p.name for p in tmp_path.iterdir()}
    assert remaining == {"state.json", "state.json.lock"}


def test_update_project_scoped_state_sequential_calls_do_not_lose_updates(tmp_path) -> None:
    """Sequential increments must accumulate without lost updates.

    True multi-process race testing is impractical in a unit test; this
    verifies that repeated update_project_scoped_state calls (as would be
    made by separate processes serialized via the flock) correctly
    accumulate rather than clobbering each other's snapshot.
    """
    state_file = tmp_path / "state.json"
    default_state = {"count": 0}

    for _ in range(20):
        quality_gate_config.update_project_scoped_state(
            state_file, "project-a", lambda state: {"count": state["count"] + 1}, default_state
        )

    final = quality_gate_config.load_project_scoped_state(state_file, "project-a", default_state)
    assert final == {"count": 20}


def test_update_project_scoped_state_concurrent_threads_do_not_lose_updates(tmp_path) -> None:
    """Concurrent callers sharing the same flock must not lose updates."""
    import threading

    state_file = tmp_path / "state.json"
    default_state = {"count": 0}
    increments_per_thread = 25
    thread_count = 4

    def worker() -> None:
        for _ in range(increments_per_thread):
            quality_gate_config.update_project_scoped_state(
                state_file, "project-a", lambda state: {"count": state["count"] + 1}, default_state
            )

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = quality_gate_config.load_project_scoped_state(state_file, "project-a", default_state)
    assert final == {"count": increments_per_thread * thread_count}
