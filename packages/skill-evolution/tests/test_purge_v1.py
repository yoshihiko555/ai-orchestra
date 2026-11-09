"""EV-82・EV-83: 明示的な v1 削除と通常操作による旧データの保持。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import skill_evolution_common as common

from tests.module_loader import load_module

CLI = Path(__file__).resolve().parents[1] / "scripts/skill_evolution.py"
purge = load_module("phase5_skill_evolution", str(CLI))
V1_PATHS = (
    ".claude/skill-evolution/lessons/review.md",
    ".claude/logs/skill-evolution/metrics/review.jsonl",
    ".claude/logs/skill-evolution/pending/session/review.json",
    ".claude/logs/skill-evolution/locks/review.lock",
)


def write_files(directory: Path, names: tuple[str, ...]) -> dict[Path, bytes]:
    snapshot = {}
    for name in names:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"旧データを保持: {name}\n".encode()
        path.write_bytes(content)
        snapshot[path] = content
    return snapshot


def assert_preserved(snapshot: dict[Path, bytes]) -> None:
    assert {path: path.read_bytes() for path in snapshot} == snapshot


def run_purge(project: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), "--project", str(project), "purge-v1", *arguments],
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.fixture
def worktrees(project: Path, tmp_path: Path) -> tuple[Path, Path]:
    def git(*arguments: str) -> None:
        completed = subprocess.run(
            ["git", "-C", str(project), *arguments],
            env=common._load_hook_common().sanitized_git_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr

    git("init", "--initial-branch=main")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    linked = tmp_path / "linked worktree\n日本語"
    git("worktree", "add", "-b", "fixture-branch", str(linked))
    return project.resolve(), linked.resolve()


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_ev82_dry_run_lists_absolute_paths_without_deleting(worktrees, as_json):
    snapshot = {
        path: content for tree in worktrees for path, content in write_files(tree, V1_PATHS).items()
    }
    completed = run_purge(worktrees[1], *(["--json"] if as_json else []))
    assert completed.returncode == 0, completed.stderr
    expected = {
        str(tree / relative)
        for tree in worktrees
        for relative in (
            ".claude/skill-evolution/lessons",
            ".claude/logs/skill-evolution/metrics",
            ".claude/logs/skill-evolution/pending",
            ".claude/logs/skill-evolution/locks",
        )
    }
    if as_json:
        payload = json.loads(completed.stdout)
        assert payload["apply"] is False
        assert {target["path"] for target in payload["targets"]} == expected
        assert all(target["status"] == "would_delete" for target in payload["targets"])
        assert all(target["files"] == 1 for target in payload["targets"])
        assert all(target["kind"] == "directory" for target in payload["targets"])
    else:
        assert all(path in completed.stdout for path in expected)
        assert "ディレクトリ" in completed.stdout and "ファイル 1 件" in completed.stdout
    assert_preserved(snapshot)


@pytest.mark.parametrize("invocation", [0, 1], ids=["root", "linked"])
def test_ev82_apply_deletes_only_v1_in_all_worktrees_and_preserves_v2(worktrees, invocation):
    legacy, protected = {}, {}
    for tree in worktrees:
        legacy.update(write_files(tree, V1_PATHS))
        protected.update(
            write_files(
                tree,
                (
                    ".claude/skill-evolution/keep.md",
                    ".claude/logs/skill-evolution/runs-202611.jsonl",
                    ".claude/logs/skill-evolution/candidates.jsonl",
                    ".claude/logs/skill-evolution/candidates.lock",
                    ".claude/logs/skill-evolution/candidates-state.json",
                    ".claude/logs/skill-evolution/runs.lock",
                    ".claude/logs/skill-evolution/drafts/C-00000001/train.yaml",
                ),
            )
        )
    completed = run_purge(worktrees[invocation], "--apply", "--json")
    assert completed.returncode == 0, completed.stderr
    targets = json.loads(completed.stdout)["targets"]
    assert len(targets) == 8
    assert all(target["status"] == "deleted" for target in targets)
    assert all(not Path(target["path"]).exists() for target in targets)
    assert all(not path.exists() for path in legacy)
    assert_preserved(protected)


@pytest.mark.parametrize(
    "relative", [".claude/skill-evolution/lessons", ".claude/logs/skill-evolution/metrics"]
)
def test_ev82_symlinked_targets_are_reported_and_preserved(worktrees, tmp_path, relative):
    outside = tmp_path / "outside"
    snapshot = write_files(outside, ("keep.md",))
    link = worktrees[0] / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    for arguments in ((), ("--apply",)):
        completed = run_purge(worktrees[0], *arguments)
        assert completed.returncode == 0, completed.stderr
        assert str(link) in completed.stdout
        assert "symlink のため消さない" in completed.stdout
        assert link.is_symlink() and link.readlink() == outside
        assert_preserved(snapshot)


@pytest.mark.parametrize("key", ["dir", "logs_dir"])
@pytest.mark.parametrize(
    "value",
    ["../outside", "safe/../outside", "absolute"],
    ids=["escape", "internal-dotdot", "absolute"],
)
def test_ev82_unsafe_config_is_refused_per_worktree(worktrees, tmp_path, key, value):
    root, linked = worktrees
    configured = str(root / "absolute") if value == "absolute" else value
    outside = Path(os.path.abspath(root / configured))
    names = (
        ("lessons/review.md",)
        if key == "dir"
        else ("metrics/review.json", "pending/review.json", "locks/review.lock")
    )
    snapshot = write_files(outside, names)
    defaults = write_files(root, V1_PATHS)
    linked_files = write_files(linked, V1_PATHS)
    config = root / ".claude/config/skill-evolution/skill-evolution.local.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(f"storage:\n  {key}: {json.dumps(configured)}\n")
    completed = run_purge(linked, "--apply", "--json")
    assert completed.returncode == 0, completed.stderr
    targets = json.loads(completed.stdout)["targets"]
    assert any("`.claude` の外のため消さない" in target["message"] for target in targets)
    assert_preserved(snapshot)
    assert_preserved(
        {
            path: content
            for path, content in defaults.items()
            if ("lessons" in path.parts) == (key == "dir")
        }
    )
    assert all(not path.exists() for path in linked_files)


@pytest.mark.parametrize("key", ["dir", "logs_dir"])
def test_ev82_symlinked_storage_parent_outside_is_refused(worktrees, tmp_path, key):
    root = worktrees[0]
    outside = tmp_path / "outside"
    snapshot = write_files(outside, ("lessons/keep", "metrics/keep", "pending/keep", "locks/keep"))
    link = root / ".claude/redirect"
    link.symlink_to(outside, target_is_directory=True)
    config = root / ".claude/config/skill-evolution/skill-evolution.local.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(f"storage:\n  {key}: .claude/redirect\n")
    completed = run_purge(root, "--apply")
    assert completed.returncode == 0, completed.stderr
    assert "`.claude` の外のため消さない" in completed.stdout
    assert link.is_symlink()
    assert_preserved(snapshot)


def test_ev82_local_config_targets_custom_storage_without_removing_default_siblings(worktrees):
    root = worktrees[0]
    defaults = write_files(root, V1_PATHS)
    custom = write_files(
        root,
        (
            ".claude/legacy/lessons/review.md",
            ".claude/legacy-logs/metrics/keep",
            ".claude/legacy-logs/pending/keep",
            ".claude/legacy-logs/locks/keep",
        ),
    )
    config = root / ".claude/config/skill-evolution/skill-evolution.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("storage: {dir: unused, logs_dir: unused}\n")
    config.with_name("skill-evolution.local.yaml").write_text(
        "storage: {dir: .claude/legacy, logs_dir: .claude/legacy-logs}\n"
    )
    completed = run_purge(root, "--apply")
    assert completed.returncode == 0, completed.stderr
    assert "削除しました" in completed.stdout
    assert all(not path.exists() for path in custom)
    assert_preserved(defaults)


def test_ev82_inner_symlinks_are_unlinked_without_following(worktrees, tmp_path):
    root = worktrees[0]
    outside = tmp_path / "outside"
    snapshot = write_files(outside, ("keep.md",))
    write_files(root, V1_PATHS)
    lessons = root / ".claude/skill-evolution/lessons"
    (lessons / "directory-link").symlink_to(outside, target_is_directory=True)
    (lessons / "file-link").symlink_to(outside / "keep.md")
    (lessons / "dangling-link").symlink_to(outside / "missing")
    completed = run_purge(root, "--apply", "--json")
    assert completed.returncode == 0, completed.stderr
    target = next(
        target
        for target in json.loads(completed.stdout)["targets"]
        if target["path"] == str(lessons)
    )
    assert target["files"] == 4 and target["directories"] == 1
    assert not lessons.exists()
    assert_preserved(snapshot)


def test_ev82_file_targets_are_reported_and_preserved(worktrees):
    root = worktrees[0]
    snapshot = write_files(root, (".claude/skill-evolution/lessons",))
    completed = run_purge(root, "--apply", "--json")
    assert completed.returncode == 0, completed.stderr
    [target] = json.loads(completed.stdout)["targets"]
    assert target["kind"] == "file" and target["status"] == "skipped"
    assert_preserved(snapshot)


@pytest.mark.parametrize("apply", [False, True])
def test_ev82_missing_targets_succeed(worktrees, apply):
    completed = run_purge(worktrees[0], "--json", *(["--apply"] if apply else []))
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"apply": apply, "targets": []}


def test_ev82_git_failure_prevents_any_deletion(worktrees, monkeypatch, capsys):
    snapshot = {
        path: content for tree in worktrees for path, content in write_files(tree, V1_PATHS).items()
    }
    core = common._load_hook_common()
    monkeypatch.setattr(core, "resolve_root_worktree", lambda _: str(worktrees[0]))

    def fail_git(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(purge.subprocess, "run", fail_git)
    assert purge.main(["--project", str(worktrees[1]), "purge-v1", "--apply"]) == 1
    assert "git" in capsys.readouterr().err
    assert_preserved(snapshot)


def test_ev82_scan_failure_prevents_any_deletion(worktrees, monkeypatch, capsys):
    snapshot = {
        path: content for tree in worktrees for path, content in write_files(tree, V1_PATHS).items()
    }
    count_tree = purge._count_tree

    def fail_linked_scan(path):
        if path.is_relative_to(worktrees[1]):
            raise PermissionError("走査できません")
        return count_tree(path)

    monkeypatch.setattr(purge, "_count_tree", fail_linked_scan)
    assert purge.main(["--project", str(worktrees[0]), "purge-v1", "--apply"]) == 1
    assert "走査できません" in capsys.readouterr().err
    assert_preserved(snapshot)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(
    "storage",
    [
        {"dir": ".", "logs_dir": "src"},
        {"dir": "", "logs_dir": ""},
        {"dir": ".claude", "logs_dir": ".claude"},
        {"dir": ".git/legacy", "logs_dir": ".git/logs"},
        {"dir": ".claude-other/legacy", "logs_dir": ".claude-other/logs"},
    ],
    ids=["source", "empty", "claude-root", "git", "prefix"],
)
def test_ev82_local_config_outside_claude_is_reported_and_preserved(worktrees, storage, as_json):
    root = worktrees[0]
    names = (
        f"{storage['dir']}/lessons/keep",
        *(f"{storage['logs_dir']}/{name}/keep" for name in ("metrics", "pending", "locks")),
    )
    snapshot = write_files(root, tuple(name.lstrip("/") for name in names))
    config = root / ".claude/config/skill-evolution/skill-evolution.local.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"storage": storage}))
    completed = run_purge(root, "--apply", *(["--json"] if as_json else []))
    assert completed.returncode == 0, completed.stderr
    if as_json:
        targets = json.loads(completed.stdout)["targets"]
        assert {target["path"] for target in targets} == {
            str(path.parent.resolve()) for path in snapshot
        }
        assert all(target["status"] == "skipped" for target in targets)
        assert all(target["message"] == "`.claude` の外のため消さない" for target in targets)
    else:
        assert all(str(path.parent.resolve()) in completed.stdout for path in snapshot)
        assert completed.stdout.count("`.claude` の外のため消さない") == 4
    assert_preserved(snapshot)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_ev82_deletion_failure_reports_all_results_and_continues(
    worktrees, monkeypatch, capsys, as_json
):
    snapshot = {
        path: content for tree in worktrees for path, content in write_files(tree, V1_PATHS).items()
    }
    failed = worktrees[0] / ".claude/logs/skill-evolution/metrics"
    delete_tree = purge.shutil.rmtree

    def fail_metrics(path: Path) -> None:
        if path == failed:
            raise PermissionError("削除を拒否しました")
        delete_tree(path)

    monkeypatch.setattr(purge.shutil, "rmtree", fail_metrics)
    arguments = ["--project", str(worktrees[0]), "purge-v1", "--apply"]
    assert purge.main([*arguments, *(["--json"] if as_json else [])]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    if as_json:
        payload = json.loads(output.out)
        assert payload["apply"] is True
        targets = payload["targets"]
        assert len(targets) == 8
        assert (
            next(target for target in targets if target["path"] == str(failed))["status"]
            == "failed"
        )
        assert sum(target["status"] == "deleted" for target in targets) == 7
        assert "削除を拒否しました" in next(
            target["message"] for target in targets if target["status"] == "failed"
        )
    else:
        assert output.out.count("削除しました") == 7
        assert "削除を拒否しました" in output.out
        assert all(
            str(tree / relative) in output.out
            for tree in worktrees
            for relative in (
                ".claude/skill-evolution/lessons",
                ".claude/logs/skill-evolution/metrics",
                ".claude/logs/skill-evolution/pending",
                ".claude/logs/skill-evolution/locks",
            )
        )
    assert_preserved({path: content for path, content in snapshot.items() if path.parent == failed})
    assert all(not path.exists() for path in snapshot if path.parent != failed)


@pytest.mark.parametrize("option", ["--a", "--ap", "--appl"])
def test_ev82_abbreviated_apply_is_rejected_without_deleting(worktrees, option):
    snapshot = write_files(worktrees[0], V1_PATHS)
    completed = run_purge(worktrees[0], option)
    assert completed.returncode == 2
    assert option in completed.stderr
    assert_preserved(snapshot)


def test_ev82_top_level_option_abbreviations_are_rejected(worktrees, capsys):
    with pytest.raises(SystemExit) as exc:
        purge.build_parser().parse_args(["--proj", str(worktrees[0]), "purge-v1"])
    assert exc.value.code == 2
    assert "error" in capsys.readouterr().err


@pytest.mark.parametrize("recreated", [False, True], ids=["missing", "recreated"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_ev82_prunable_worktree_is_reported_and_skipped(worktrees, recreated, as_json):
    root, linked = worktrees
    legacy = write_files(root, V1_PATHS)
    shutil.rmtree(linked)
    protected = write_files(linked, V1_PATHS) if recreated else {}
    completed = run_purge(root, "--apply", *(["--json"] if as_json else []))
    assert completed.returncode == 0, completed.stderr
    if as_json:
        targets = json.loads(completed.stdout)["targets"]
        [skipped] = [target for target in targets if target["worktree"] == str(linked)]
        assert skipped["path"] == str(linked)
        assert skipped["kind"] == "worktree" and skipped["status"] == "skipped"
        assert "prunable" in skipped["message"]
        assert sum(target["status"] == "deleted" for target in targets) == 4
    else:
        assert str(linked) in completed.stdout and "prunable" in completed.stdout
        assert completed.stdout.count("削除しました") == 4
    assert all(not path.exists() for path in legacy)
    assert_preserved(protected)


def test_ev82_missing_worktree_without_prunable_marker_is_skipped(worktrees, monkeypatch):
    root, linked = worktrees
    snapshot = write_files(root, V1_PATHS)
    shutil.rmtree(linked)
    run_git = purge.subprocess.run

    def omit_prunable_marker(arguments, **kwargs):
        completed = run_git(arguments, **kwargs)
        if arguments == ["git", "worktree", "list", "--porcelain", "-z"]:
            completed.stdout = "\0\0".join(
                "\0".join(
                    field
                    for field in record.split("\0")
                    if field != "prunable" and not field.startswith("prunable ")
                )
                for record in completed.stdout.split("\0\0")
            )
        return completed

    monkeypatch.setattr(purge.subprocess, "run", omit_prunable_marker)
    payload = purge.purge_v1(str(root), apply=True)
    [skipped] = [target for target in payload["targets"] if target["worktree"] == str(linked)]
    assert skipped["status"] == "skipped"
    assert "存在しない" in skipped["message"]
    assert all(not path.exists() for path in snapshot)


@pytest.mark.parametrize("destination", ["outside", "inside"])
def test_ev82_target_replaced_by_symlink_before_deletion_is_preserved(
    worktrees, tmp_path, monkeypatch, destination
):
    root = worktrees[0]
    lessons = root / ".claude/skill-evolution/lessons"
    original = write_files(root, V1_PATHS)
    link_target = tmp_path / "outside" if destination == "outside" else root / ".claude/keep"
    protected = write_files(link_target, ("keep.md",))
    inspect_target = purge._inspect_target
    moved = lessons.with_name("saved-lessons")

    def swap_after_inspection(worktree, relative, name, core):
        inspected = inspect_target(worktree, relative, name, core)
        if worktree == root and name == "lessons" and not moved.exists():
            lessons.rename(moved)
            lessons.symlink_to(link_target, target_is_directory=True)
        return inspected

    monkeypatch.setattr(purge, "_inspect_target", swap_after_inspection)
    payload = purge.purge_v1(str(root), apply=True)
    [skipped] = [target for target in payload["targets"] if target["path"] == str(lessons)]
    assert skipped["status"] == "skipped" and skipped["kind"] == "symlink"
    assert skipped["message"] == "symlink のため消さない"
    assert lessons.is_symlink() and lessons.readlink() == link_target
    assert moved.joinpath("review.md").read_bytes() == original[lessons / "review.md"]
    assert_preserved(protected)
    assert all(not path.exists() for path in original if not path.is_relative_to(lessons))


@pytest.mark.parametrize("relative", [".claude/archive", "src", ".claude", ".git/archive"])
def test_ev82_symlinked_parent_inside_worktree_obeys_claude_boundary(worktrees, relative):
    """親の symlink は、実体も `.claude/` の直下より深い場合だけ通す。"""
    root = worktrees[0]
    destination = root / relative
    snapshot = write_files(destination, ("lessons/keep.md",))
    link = root / ".claude/skill-evolution"
    link.symlink_to(destination, target_is_directory=True)
    completed = run_purge(root, "--apply", "--json")
    assert completed.returncode == 0, completed.stderr
    [target] = json.loads(completed.stdout)["targets"]
    assert target["path"] == str(link / "lessons")
    assert link.is_symlink() and link.readlink() == destination
    if relative == ".claude/archive":
        assert target["status"] == "deleted"
        assert not (destination / "lessons").exists()
    else:
        assert target["status"] == "skipped"
        assert target["message"] == "`.claude` の外のため消さない"
        assert_preserved(snapshot)


def test_ev82_lexical_path_outside_claude_pointing_inside_is_refused(worktrees):
    root = worktrees[0]
    destination = root / ".claude/archive"
    snapshot = write_files(destination, ("lessons/keep.md",))
    link = root / "redirect"
    link.symlink_to(destination, target_is_directory=True)
    config = root / ".claude/config/skill-evolution/skill-evolution.local.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("storage: {dir: redirect}\n")
    completed = run_purge(root, "--apply", "--json")
    assert completed.returncode == 0, completed.stderr
    [target] = json.loads(completed.stdout)["targets"]
    assert target["status"] == "skipped"
    assert target["message"] == "`.claude` の外のため消さない"
    assert link.is_symlink()
    assert_preserved(snapshot)


def test_ev83_hooks_and_other_cli_commands_preserve_every_v1_file(
    candidate_context, observation_event, candidate_event, run_hook, read_events, skill_dir
):
    project = candidate_context.project
    candidate_context.store.write(lambda _: [observation_event(), candidate_event()])
    snapshot = write_files(project, V1_PATHS)
    base = {"session_id": "session-1", "prompt_id": "prompt-1", "tool_use_id": "toolu_1"}
    for filename, fields in (
        ("inject-lessons.py", {"tool_name": "Skill", "tool_input": {"skill": "review"}}),
        ("capture-skill-telemetry.py", {"tool_name": "Skill", "tool_input": {"skill": "review"}}),
        ("capture-skill-stop.py", {"hook_event_name": "Stop"}),
        ("capture-subagent-skill.py", {"hook_event_name": "SubagentStop", "agent_id": "agent-1"}),
        (
            "record-skill-scope.py",
            {
                "hook_event_name": "UserPromptExpansion",
                "expansion_type": "slash_command",
                "command_name": "review",
            },
        ),
        ("record-skill-scope.py", {"hook_event_name": "UserPromptSubmit"}),
        (
            "record-skill-scope.py",
            {"hook_event_name": "SubagentStart", "agent_id": "agent-1", "agent_type": "Explore"},
        ),
    ):
        run_hook(filename, {**base, **fields})
        assert_preserved(snapshot)
    assert {event["type"] for event in read_events()} == {
        "run.started",
        "run.confirmed",
        "turn.stopped",
        "agent.stopped",
        "turn.started",
        "agent.started",
    }
    for filename, arguments, code in (
        (CLI.name, ["status", "review"], 0),
        (CLI.name, ["check-trigger", "review"], 1),
        (CLI.name, ["evaluate", "--history", "-"], 0),
        (CLI.name, ["provenance", "review"], 0),
        (CLI.name, ["lock", "acquire", "review"], 0),
        (CLI.name, ["lock", "release", "review"], 0),
        ("skill_candidates.py", ["list"], 0),
        ("skill_candidates.py", ["show", "C-00000001"], 0),
        ("skill_candidates.py", ["show", "O-00000001"], 0),
        ("skill_candidates.py", ["runs"], 0),
        ("skill_candidates.py", ["runs", "--sid", "session-1"], 0),
        ("skill_candidates_admin.py", ["prune-runs", "--before", "202601"], 0),
    ):
        completed = subprocess.run(
            [
                sys.executable,
                str(project / "packages/skill-evolution/scripts" / filename),
                "--project",
                str(project),
                *arguments,
            ],
            input="{}",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode == code, completed.stderr
        assert_preserved(snapshot)
