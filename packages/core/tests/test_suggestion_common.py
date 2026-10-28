"""相談提案の再帰防止・重複抑制・安全な状態保存の契約テスト。"""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier

import pytest

from tests.module_loader import load_module

suggestion_common = load_module("suggestion_common", "packages/core/hooks/suggestion_common.py")


def _marker_path(project_dir, session_id, namespace, target):
    session_hash = sha256(session_id.encode("utf-8")).hexdigest()
    target_hash = sha256(f"{namespace}\0{target}".encode()).hexdigest()
    return project_dir / ".claude" / "state" / "suggestions" / session_hash / target_hash


@pytest.mark.parametrize(
    "agent_context",
    [{"agent_id": "child"}, {"agent_id": "child", "agent_type": "implementation"}],
)
def test_top_level_nonempty_agent_id_is_subagent_regardless_of_agent_type(agent_context) -> None:
    assert suggestion_common.is_subagent_context(agent_context) is True


def test_agent_type_only_is_main_context() -> None:
    """agent_type は --agent で起動したメインセッションにも存在する。"""
    assert suggestion_common.is_subagent_context({"agent_type": "implementation"}) is False


@pytest.mark.parametrize("value", ["", " \t\n", None, False, 0, 1, [], {}, ["child"]])
def test_empty_or_non_string_agent_id_with_agent_type_is_main_context(value) -> None:
    assert (
        suggestion_common.is_subagent_context({"agent_id": value, "agent_type": "implementation"})
        is False
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"subagent_type": "planner"},
        {"tool_input": {"subagent_type": "planner"}},
        {"tool_input": {"agent_id": "child", "agent_type": "implementation"}},
        {"tool_response": {"agent_id": "child", "agent_type": "implementation"}},
    ],
)
def test_nested_fields_and_tool_subagent_type_are_not_execution_context(payload) -> None:
    assert suggestion_common.is_subagent_context(payload) is False


@pytest.mark.parametrize(
    "agent_context",
    [{"agent_id": "child"}, {"agent_id": "child", "agent_type": "implementation"}],
)
@pytest.mark.parametrize("with_session", [False, True])
def test_subagent_cannot_claim_and_creates_no_state(tmp_path, agent_context, with_session) -> None:
    payload = dict(agent_context)
    if with_session:
        payload["session_id"] = "subagent-session"
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is False
    assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("with_session", [False, True])
def test_agent_type_only_can_claim_as_main_context(tmp_path, with_session) -> None:
    payload = {"agent_type": "implementation"}
    if with_session:
        payload["session_id"] = "main-agent-session"

    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is True
    expected_repeat_claim = not with_session
    assert (
        suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md")
        is expected_repeat_claim
    )
    if with_session:
        assert _marker_path(tmp_path, "main-agent-session", "codex", "DESIGN.md").is_file()
    else:
        assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("payload", [{}, {"session_id": ""}, {"session_id": None}])
def test_missing_session_allows_each_claim_without_persisting_state(tmp_path, payload) -> None:
    for _ in range(3):
        assert (
            suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is True
        )
    assert not (tmp_path / ".claude").exists()


def test_tool_input_subagent_type_does_not_prevent_claim(tmp_path) -> None:
    payload = {"session_id": "parent-session", "tool_input": {"subagent_type": "planner"}}
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is True
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is False


def test_same_project_session_namespace_target_claims_only_once(tmp_path) -> None:
    payload = {"session_id": "same-session"}
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is True
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is False


def test_existing_marker_is_respected_after_module_reload(tmp_path) -> None:
    """プロセス内キャッシュではなく marker によって重複を抑制する。"""
    payload = {"session_id": "persisted-session"}
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is True
    reloaded = load_module("suggestion_common_reloaded", "packages/core/hooks/suggestion_common.py")
    assert reloaded.claim_suggestion(payload, str(tmp_path), "codex", "DESIGN.md") is False


def test_existing_marker_is_never_truncated_or_overwritten(tmp_path) -> None:
    marker = _marker_path(tmp_path, "existing-marker", "codex", "DESIGN.md")
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"existing marker")

    assert (
        suggestion_common.claim_suggestion(
            {"session_id": "existing-marker"}, str(tmp_path), "codex", "DESIGN.md"
        )
        is False
    )
    assert marker.read_bytes() == b"existing marker"


def test_parallel_duplicate_claims_have_exactly_one_winner(tmp_path) -> None:
    workers = 16
    ready = Barrier(workers)

    def claim_once(_index):
        ready.wait(timeout=10)
        return suggestion_common.claim_suggestion(
            {"session_id": "parallel-session"}, str(tmp_path), "codex", "DESIGN.md"
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        claims = list(executor.map(claim_once, range(workers)))

    assert claims.count(True) == 1
    assert claims.count(False) == workers - 1
    assert all(isinstance(claim, bool) for claim in claims)
    markers = [path for path in (tmp_path / ".claude").rglob("*") if path.is_file()]
    assert markers == [_marker_path(tmp_path, "parallel-session", "codex", "DESIGN.md")]
    assert markers[0].read_bytes() == b""


def test_parallel_distinct_targets_can_all_claim(tmp_path) -> None:
    workers = 16
    ready = Barrier(workers)

    def claim_target(index):
        ready.wait(timeout=10)
        return suggestion_common.claim_suggestion(
            {"session_id": "parallel-targets"}, str(tmp_path), "codex", f"contracts/{index}.py"
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        claims = list(executor.map(claim_target, range(workers)))

    assert claims == [True] * workers
    markers = [path for path in (tmp_path / ".claude").rglob("*") if path.is_file()]
    assert len(markers) == workers
    assert all(path.read_bytes() == b"" for path in markers)


@pytest.mark.parametrize("changed_key", ["project", "session", "namespace", "target"])
def test_claim_keys_are_independent(tmp_path, changed_key) -> None:
    project_dir = tmp_path / "project-one"
    other_project = tmp_path / "project-two"
    project_dir.mkdir()
    other_project.mkdir()
    payload = {"session_id": "session-one"}
    assert (
        suggestion_common.claim_suggestion(payload, str(project_dir), "codex", "DESIGN.md") is True
    )

    next_project = other_project if changed_key == "project" else project_dir
    next_payload = {"session_id": "session-two"} if changed_key == "session" else payload
    next_namespace = "antigravity" if changed_key == "namespace" else "codex"
    next_target = "ARCHITECTURE.md" if changed_key == "target" else "DESIGN.md"
    assert (
        suggestion_common.claim_suggestion(
            next_payload, str(next_project), next_namespace, next_target
        )
        is True
    )
    assert (
        suggestion_common.claim_suggestion(
            next_payload, str(next_project), next_namespace, next_target
        )
        is False
    )
    assert (
        suggestion_common.claim_suggestion(payload, str(project_dir), "codex", "DESIGN.md") is False
    )


def test_namespace_and_target_are_separated_before_hashing(tmp_path) -> None:
    payload = {"session_id": "namespace-boundary"}
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "ab", "c") is True
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), "a", "bc") is True
    assert _marker_path(tmp_path, "namespace-boundary", "ab", "c").is_file()
    assert _marker_path(tmp_path, "namespace-boundary", "a", "bc").is_file()


def test_state_contains_only_hashed_names_and_an_empty_marker(tmp_path) -> None:
    session_id = "private-session/../../do-not-persist"
    namespace = "antigravity-private-namespace"
    target = "compare secret-question alternatives"
    payload = {
        "session_id": session_id,
        "tool_input": {"query": target, "content": "private-body-must-not-be-saved"},
    }
    assert suggestion_common.claim_suggestion(payload, str(tmp_path), namespace, target) is True

    marker = _marker_path(tmp_path, session_id, namespace, target)
    assert marker.is_file()
    assert marker.read_bytes() == b""
    stored_files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert stored_files == [marker]
    stored_paths = "\n".join(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    for private_value in (session_id, namespace, target, "private-body-must-not-be-saved"):
        assert private_value not in stored_paths


@pytest.mark.parametrize("symlink_level", ["claude", "state", "suggestions", "session", "marker"])
@pytest.mark.parametrize("destination_inside_project", [False, True])
def test_symlink_in_state_path_is_rejected_without_writing_through_it(
    tmp_path, symlink_level, destination_inside_project
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    destination = (project_dir if destination_inside_project else tmp_path) / "destination"
    destination.mkdir()
    sentinel = destination / "sentinel"
    sentinel.write_bytes(b"unchanged")
    marker = _marker_path(project_dir, "symlink-session", "codex", "DESIGN.md")
    link_path = {
        "claude": project_dir / ".claude",
        "state": project_dir / ".claude" / "state",
        "suggestions": marker.parent.parent,
        "session": marker.parent,
        "marker": marker,
    }[symlink_level]
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(sentinel if symlink_level == "marker" else destination)

    assert (
        suggestion_common.claim_suggestion(
            {"session_id": "symlink-session"}, str(project_dir), "codex", "DESIGN.md"
        )
        is False
    )
    assert sentinel.read_bytes() == b"unchanged"
    assert list(destination.rglob("*")) == [sentinel]
    assert link_path.is_symlink()


@pytest.mark.parametrize("symlink_level", ["claude", "state", "suggestions", "session", "marker"])
def test_dangling_symlink_in_state_path_is_not_followed(tmp_path, symlink_level) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    marker = _marker_path(project_dir, "dangling-session", "codex", "DESIGN.md")
    link_path = {
        "claude": project_dir / ".claude",
        "state": project_dir / ".claude" / "state",
        "suggestions": marker.parent.parent,
        "session": marker.parent,
        "marker": marker,
    }[symlink_level]
    destination = tmp_path / "must-not-be-created"
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(destination)

    assert (
        suggestion_common.claim_suggestion(
            {"session_id": "dangling-session"}, str(project_dir), "codex", "DESIGN.md"
        )
        is False
    )
    assert not destination.exists()
    assert link_path.is_symlink()


@pytest.mark.parametrize("blocked_level", ["claude", "state", "suggestions", "session"])
def test_state_directory_creation_failure_returns_false_instead_of_raising(
    tmp_path, blocked_level
) -> None:
    marker = _marker_path(tmp_path, "blocked-session", "codex", "DESIGN.md")
    blocked_path = {
        "claude": tmp_path / ".claude",
        "state": tmp_path / ".claude" / "state",
        "suggestions": marker.parent.parent,
        "session": marker.parent,
    }[blocked_level]
    blocked_path.parent.mkdir(parents=True, exist_ok=True)
    blocked_path.write_bytes(b"not a directory")

    assert (
        suggestion_common.claim_suggestion(
            {"session_id": "blocked-session"}, str(tmp_path), "codex", "DESIGN.md"
        )
        is False
    )
    assert blocked_path.read_bytes() == b"not a directory"
    assert not marker.is_file()
