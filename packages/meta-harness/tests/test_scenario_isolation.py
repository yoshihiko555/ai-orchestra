"""Scenario runner OS-isolation tests (ADR-20260711-033)."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_scenario_isolation_tests",
    "packages/meta-harness/lib/meta_harness_common.py",
)
siso = load_module(
    "meta_harness_scenario_isolation_tests",
    "packages/meta-harness/lib/scenario_isolation.py",
)


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _config() -> dict:
    config = copy.deepcopy(mh.DEFAULTS)
    config["evaluate"]["isolation"] = {
        "backend": "srt",
        "srt_version_pin": None,
        "execution_backend": "none",
    }
    return config


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    worktree = tmp_path / "worktree"
    runtime_state = tmp_path / "runtime-state"
    instruction = tmp_path / "instruction.md"
    worktree.mkdir()
    runtime_state.mkdir()
    instruction.write_text("report instructions\n", encoding="utf-8")
    return worktree, runtime_state, instruction


def _fake_canary_runner(git_project: Path):
    def runner(cmd, **_kwargs):
        if cmd == ["/usr/bin/srt", "--version"]:
            return _completed(0, stdout="1.0.0")
        if cmd[:3] == ["git", "worktree", "list"]:
            return _completed(0, stdout=f"worktree {git_project}\n")
        if cmd and Path(cmd[0]).name == "git":
            return _completed(0)
        if cmd[:1] == ["/bin/cat"] or (cmd and Path(cmd[0]).name == "curl"):
            return _completed(0, stdout="reachable")
        if cmd[0] == "/usr/bin/srt":
            if "/bin/cat" in cmd:
                return _completed(1, stderr="cat: Operation not permitted")
            return _completed(56, stderr="curl: (56) connection reset by proxy")
        raise AssertionError(f"unexpected command: {cmd}")

    return runner


def test_scenario_settings_allow_only_runtime_paths(git_project: Path, tmp_path: Path) -> None:
    worktree, runtime_state, instruction = _paths(tmp_path)
    run_tmp = tmp_path / "run-tmp"
    run_tmp.mkdir()
    settings = siso.build_scenario_srt_settings(
        worktree_dir=worktree,
        main_root=git_project,
        config=_config(),
        runtime_state_dir=runtime_state,
        instruction_path=instruction,
        run_tmp_dir=run_tmp,
    )
    read_key = "allow" + "Read"
    write_key = "allow" + "Write"
    deny_key = "deny" + "Read"
    assert settings["network"]["allowedDomains"] == ["api.anthropic.com"]
    assert settings["network"]["strictAllowlist"] is True
    assert {
        str(worktree.resolve()),
        str(runtime_state.resolve()),
        str(instruction.resolve()),
        str(run_tmp.resolve()),
    }.issubset(set(settings["filesystem"][read_key]))
    assert set(settings["filesystem"][write_key]) == {
        str(worktree.resolve()),
        str(runtime_state.resolve() / ("." + "claude")),
        str(run_tmp.resolve()),
    }
    assert settings["filesystem"][deny_key] == ["/"]


def test_runtime_tool_discovery_never_reexposes_real_home(monkeypatch) -> None:
    real_home_tool = Path.home() / ".local" / "share" / "mise" / "bin" / "claude"

    def fake_which(name: str, *, path: str | None = None) -> str | None:
        assert path == siso._SYSTEM_TOOL_SEARCH_PATH
        return str(real_home_tool) if name == "claude" else None

    monkeypatch.setattr(siso.shutil, "which", fake_which)

    roots = siso._scenario_runtime_read_roots()

    assert all(not siso._under_real_home(root) for root in roots)


def test_public_resolver_rejects_until_complete_execution_boundary_exists(
    git_project: Path, tmp_path: Path
) -> None:
    worktree, runtime_state, instruction = _paths(tmp_path)
    with pytest.raises(siso.ScenarioIsolationError, match="execution boundary unavailable"):
        siso.resolve_scenario_isolation(
            worktree_dir=worktree,
            main_root=git_project,
            config=_config(),
            instruction_path=instruction,
            source_commit="a" * 40,
            runtime_state_dir=runtime_state,
        )


def test_resolve_scenario_isolation_builds_launch(
    git_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, runtime_state, instruction = _paths(tmp_path)
    monkeypatch.setattr(siso.iso.shutil, "which", lambda name, **_kwargs: f"/usr/bin/{name}")
    launch = siso._resolve_scenario_isolation_profile(
        worktree_dir=worktree,
        main_root=git_project,
        config=_config(),
        instruction_path=instruction,
        source_commit="a" * 40,
        runtime_state_dir=runtime_state,
        settings_dir=tmp_path / "settings",
        runner=_fake_canary_runner(git_project),
    )
    try:
        root_key = "HO" + "ME"
        config_key = "CLAUDE_" + "CONFIG_DIR"
        assert launch.executable == "/usr/bin/srt"
        assert launch.env[root_key] == str(runtime_state.resolve())
        assert launch.env[config_key] == str(runtime_state.resolve() / ("." + "claude"))
        assert launch.env["AI_ORCHESTRA_DIR"] == str(worktree.resolve())
        assert launch.metadata["backend"] == "srt"
        assert launch.metadata["srt_version"] == "1.0.0"
        assert launch.settings_path.is_file()
    finally:
        siso.cleanup_scenario_isolation(launch)


def test_scenario_isolation_rejects_unknown_backend(git_project: Path, tmp_path: Path) -> None:
    worktree, runtime_state, instruction = _paths(tmp_path)
    config = _config()
    config["evaluate"]["isolation"]["backend"] = "none"
    with pytest.raises(siso.ScenarioIsolationError, match="unsupported evaluate.isolation"):
        siso._resolve_scenario_isolation_profile(
            worktree_dir=worktree,
            main_root=git_project,
            config=config,
            instruction_path=instruction,
            source_commit="a" * 40,
            runtime_state_dir=runtime_state,
        )


def test_scenario_isolation_rejects_version_mismatch(
    git_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, runtime_state, instruction = _paths(tmp_path)
    config = _config()
    config["evaluate"]["isolation"]["srt_version_pin"] = "0.0.64"
    monkeypatch.setattr(siso.iso.shutil, "which", lambda name, **_kwargs: f"/usr/bin/{name}")
    with pytest.raises(siso.ScenarioIsolationError, match="srt_version_pin mismatch"):
        siso._resolve_scenario_isolation_profile(
            worktree_dir=worktree,
            main_root=git_project,
            config=config,
            instruction_path=instruction,
            source_commit="a" * 40,
            runtime_state_dir=runtime_state,
            settings_dir=tmp_path / "settings",
            runner=_fake_canary_runner(git_project),
        )


def test_scenario_isolation_rejects_symlink_instruction(git_project: Path, tmp_path: Path) -> None:
    worktree, runtime_state, instruction = _paths(tmp_path)
    linked = tmp_path / "linked-instruction.md"
    linked.symlink_to(instruction)
    with pytest.raises(siso.ScenarioIsolationError, match="regular non-symlink"):
        siso.build_scenario_srt_settings(
            worktree_dir=worktree,
            main_root=git_project,
            config=_config(),
            runtime_state_dir=runtime_state,
            instruction_path=linked,
        )


def test_container_git_wrapper_uses_fixed_image_git_not_itself(
    git_project: Path, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    wrapper_dir = siso._prepare_isolated_git(
        worktree_dir=git_project,
        runtime_state_dir=runtime,
        source_commit=source_commit,
        runner=subprocess.run,
        container_paths=True,
    )

    wrapper = (wrapper_dir / "git").read_text()
    assert "exec /usr/bin/git --git-dir=/runtime/git-snapshot" in wrapper
    assert "--work-tree=/workspace" in wrapper
    assert wrapper_dir.stat().st_mode & 0o777 == 0o711
    # Issue #350 PR follow-up (bot review): the oracle container bind-mounts this file
    # read-only and reads it as a non-root `--user {uid}:{gid}`, so `container_paths=True`
    # must pin a group/other-readable mode regardless of the host umask.
    assert (runtime / "ignored-baseline.json").stat().st_mode & 0o777 == 0o644


def test_prepare_isolated_git_snapshot_readable_by_container_user_under_restrictive_umask(
    git_project: Path, tmp_path: Path
) -> None:
    """Issue #357 follow-up (bot review): `git init --bare` and the `git status` invoked by
    `_collect_ignored_baseline_paths` create the snapshot's directories/files/index at
    umask-masked permissions. A host running as root with e.g. `umask 077` would leave the
    snapshot at 0700/0600, which the oracle/preparation/scenario containers (non-root
    `--user {uid}:{gid}`) could not read at all -- unlike `wrapper_dir` and
    `ignored-baseline.json`, which already pin a container-readable mode regardless of umask.
    `container_paths=True` must do the same for every artifact under the snapshot repo."""
    runtime = tmp_path / "runtime-umask"
    runtime.mkdir()
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    previous_umask = os.umask(0o077)
    try:
        siso._prepare_isolated_git(
            worktree_dir=git_project,
            runtime_state_dir=runtime,
            source_commit=source_commit,
            runner=subprocess.run,
            container_paths=True,
        )
    finally:
        os.umask(previous_umask)

    snapshot_dir = runtime / "git-snapshot"
    assert snapshot_dir.stat().st_mode & 0o755 == 0o755
    for root, dirs, files in os.walk(snapshot_dir):
        for name in dirs:
            path = Path(root) / name
            assert path.stat().st_mode & 0o755 == 0o755, f"{path} not container-readable"
        for name in files:
            path = Path(root) / name
            assert path.stat().st_mode & 0o644 == 0o644, f"{path} not container-readable"


def test_git_wrapper_head_is_stable_across_invocation_forms(
    git_project: Path, tmp_path: Path
) -> None:
    """Issue #357 regression: before the fix, the wrapper only faked HEAD for the exact
    3-arg (`rev-parse --short HEAD`) and 2-arg (`rev-parse HEAD`) forms and fell through to
    the real snapshot repo for any other equivalent invocation (e.g. `-C <workspace>`). An
    agent and an oracle phrasing the same question differently could therefore observe two
    different HEAD values for the same scenario run. The wrapper must now resolve every
    invocation form identically, without requiring Docker or srt."""
    runtime = tmp_path / "runtime-wrapper-consistency"
    runtime.mkdir()
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    wrapper_dir = siso._prepare_isolated_git(
        worktree_dir=git_project,
        runtime_state_dir=runtime,
        source_commit=source_commit,
        runner=subprocess.run,
    )
    wrapper = str(wrapper_dir / "git")

    bare = subprocess.run(
        [wrapper, "rev-parse", "--short", "HEAD"],
        cwd=git_project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dash_c = subprocess.run(
        [wrapper, "-C", str(git_project), "rev-parse", "--short", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert bare and all(c in "0123456789abcdef" for c in bare)
    assert dash_c == bare
    # The wrapper must reflect the real, freshly created snapshot commit for every invocation
    # form, not silently keep faking one particular form to `source_commit`.
    assert bare != source_commit[:7]


def test_prepare_isolated_git_records_tracked_but_ignored_file_in_baseline(
    git_project: Path, tmp_path: Path
) -> None:
    gitignore = git_project / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + ".claude/docs/\n",
        encoding="utf-8",
    )
    plan = git_project / ".claude" / "docs" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("tracked despite gitignore\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=git_project, check=True)
    subprocess.run(["git", "add", "-f", ".claude/docs/plan.md"], cwd=git_project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=meta-harness-test",
            "-c",
            "user.email=meta-harness-test@invalid",
            "commit",
            "-q",
            "-m",
            "add tracked ignored file",
        ],
        cwd=git_project,
        check=True,
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    runtime = tmp_path / "ignored-runtime"
    runtime.mkdir()

    siso._prepare_isolated_git(
        worktree_dir=git_project,
        runtime_state_dir=runtime,
        source_commit=source_commit,
        runner=subprocess.run,
    )

    baseline_path = runtime / "ignored-baseline.json"
    assert baseline_path.is_file()
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert ".claude/docs/plan.md" in baseline["ignored_paths"]
    # Default (non-container) invocation must pin owner-only 0o600, matching this file's own
    # runtime-state confidentiality posture, regardless of host umask.
    assert baseline_path.stat().st_mode & 0o777 == 0o600


def test_prepare_isolated_git_records_symlinked_ignored_directory_without_descending_into_it(
    git_project: Path, tmp_path: Path
) -> None:
    """A symlink whose *target* is a directory is reported by `os.walk` in `dirnames`, not
    `filenames`. The baseline collector must record the symlink's own path (so the
    collateral-scope oracle's unconditional symlink rejection can catch a candidate that plants
    `ln -s <dir> .claude/docs/evil-link` inside an already-tracked-but-ignored directory) without
    following it into the linked-to directory's contents."""
    gitignore = git_project / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + ".claude/docs/\n",
        encoding="utf-8",
    )
    plan = git_project / ".claude" / "docs" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("tracked despite gitignore\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=git_project, check=True)
    subprocess.run(["git", "add", "-f", ".claude/docs/plan.md"], cwd=git_project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=meta-harness-test",
            "-c",
            "user.email=meta-harness-test@invalid",
            "commit",
            "-q",
            "-m",
            "add tracked ignored file",
        ],
        cwd=git_project,
        check=True,
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # A marker file inside the symlink target: if the walk ever followed the symlink, this
    # would leak into the baseline as `.claude/docs/evil-link/leak-marker.txt`.
    link_target = tmp_path / "outside-target"
    link_target.mkdir()
    (link_target / "leak-marker.txt").write_text("should never be walked into\n", encoding="utf-8")
    (git_project / ".claude" / "docs" / "evil-link").symlink_to(
        link_target, target_is_directory=True
    )
    runtime = tmp_path / "ignored-symlink-runtime"
    runtime.mkdir()

    siso._prepare_isolated_git(
        worktree_dir=git_project,
        runtime_state_dir=runtime,
        source_commit=source_commit,
        runner=subprocess.run,
    )

    baseline = json.loads((runtime / "ignored-baseline.json").read_text(encoding="utf-8"))
    ignored_paths = baseline["ignored_paths"]
    assert ".claude/docs/evil-link" in ignored_paths
    assert not any(path.startswith(".claude/docs/evil-link/") for path in ignored_paths)


def test_prepare_isolated_git_expands_wholly_collapsed_ignored_directory_symlink(
    git_project: Path, tmp_path: Path
) -> None:
    """PR #351 bot review follow-up: the sibling test above (`..._without_descending_into_it`)
    force-tracks a file (`plan.md`) inside the same ignored directory as the symlink, which
    makes Git report the directory's contents as individual `!!` entries rather than a single
    collapsed `<dir>/` entry -- so `evil-link` there is recorded via the flat, non-directory
    branch in `_collect_ignored_baseline_paths`, never actually exercising
    `_walk_ignored_directory_files`'s dirnames-symlink handling. This test instead keeps the
    ignored directory *entirely* untracked (no file placed inside it at all -- the fixture's
    own tracked `README.md` lives under a different, unrelated prefix), so Git collapses it to
    a single `.claude/meta-harness/` `!!` entry and the walk-based expansion path is the only
    way `evil-link` can be discovered and recorded at all."""
    gitignore = git_project / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + ".claude/meta-harness/\n",
        encoding="utf-8",
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    link_target = tmp_path / "outside-target-wholly-collapsed"
    link_target.mkdir()
    (link_target / "leak-marker.txt").write_text("should never be walked into\n", encoding="utf-8")
    ignored_dir = git_project / ".claude" / "meta-harness"
    ignored_dir.mkdir(parents=True)
    (ignored_dir / "evil-link").symlink_to(link_target, target_is_directory=True)
    runtime = tmp_path / "ignored-collapsed-symlink-runtime"
    runtime.mkdir()

    siso._prepare_isolated_git(
        worktree_dir=git_project,
        runtime_state_dir=runtime,
        source_commit=source_commit,
        runner=subprocess.run,
    )

    baseline = json.loads((runtime / "ignored-baseline.json").read_text(encoding="utf-8"))
    ignored_paths = baseline["ignored_paths"]
    assert ".claude/meta-harness/evil-link" in ignored_paths
    assert not any(path.startswith(".claude/meta-harness/evil-link/") for path in ignored_paths)
    # The collapsed directory string itself must never survive as an opaque, unexpanded entry.
    assert ".claude/meta-harness/" not in ignored_paths


def test_real_scenario_srt_blocks_store_path(git_project: Path, tmp_path: Path) -> None:
    if shutil.which("srt") is None:
        pytest.skip("srt is not installed")
    worktree, runtime_state, instruction = _paths(tmp_path)
    outside = mh.tmp_dir(git_project, _config()) / "scenario-isolation-outside.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("outside\n", encoding="utf-8")
    global_tmp_outside = tmp_path / "global-tmp-outside.txt"
    global_tmp_outside.write_text("outside\n", encoding="utf-8")
    try:
        launch = siso._resolve_scenario_isolation_profile(
            worktree_dir=worktree,
            main_root=git_project,
            config=_config(),
            instruction_path=instruction,
            source_commit="a" * 40,
            runtime_state_dir=runtime_state,
        )
    except siso.ScenarioIsolationError as exc:
        if os.getenv("CI") and os.getenv("META_HARNESS_SKIP_SRT_TESTS") != "1":
            pytest.fail(str(exc))
        pytest.skip(f"srt cannot run in this environment: {exc}")
    try:
        completed = subprocess.run(
            [launch.executable, "--settings", str(launch.settings_path), "/bin/cat", str(outside)],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=20,
            env=launch.env,
        )
        assert completed.returncode != 0
        global_tmp_read = subprocess.run(
            [
                launch.executable,
                "--settings",
                str(launch.settings_path),
                "/bin/cat",
                str(global_tmp_outside),
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=20,
            env=launch.env,
        )
        assert global_tmp_read.returncode != 0
        oracle_settings = siso.write_oracle_srt_settings(launch)
        oracle_write = subprocess.run(
            [
                launch.executable,
                "--settings",
                str(oracle_settings),
                "/usr/bin/touch",
                str(worktree / "oracle-must-not-write"),
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=20,
            env=launch.env,
        )
        assert oracle_write.returncode != 0
        assert not (worktree / "oracle-must-not-write").exists()
    finally:
        siso.cleanup_scenario_isolation(launch)
        outside.unlink(missing_ok=True)
        global_tmp_outside.unlink(missing_ok=True)


def test_real_scenario_srt_supports_git_without_main_metadata_access(
    git_project: Path, add_feature_worktree, tmp_path: Path
) -> None:
    if shutil.which("srt") is None:
        pytest.skip("srt is not installed")
    worktree = add_feature_worktree(git_project, "scenario-git")
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    runtime_state = tmp_path / "runtime-state"
    runtime_state.mkdir()
    instruction = tmp_path / "instruction.md"
    instruction.write_text("report instructions\n", encoding="utf-8")
    try:
        launch = siso._resolve_scenario_isolation_profile(
            worktree_dir=worktree,
            main_root=git_project,
            config=_config(),
            instruction_path=instruction,
            source_commit=source_commit,
            runtime_state_dir=runtime_state,
        )
    except siso.ScenarioIsolationError as exc:
        if os.getenv("CI") and os.getenv("META_HARNESS_SKIP_SRT_TESTS") != "1":
            pytest.fail(str(exc))
        pytest.skip(f"srt cannot run in this environment: {exc}")
    try:
        completed = subprocess.run(
            [
                launch.executable,
                "--settings",
                str(launch.settings_path),
                "git",
                "rev-parse",
                "--short",
                "HEAD",
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=20,
            env=launch.env,
        )
        assert completed.returncode == 0, completed.stderr
        # Issue #357: the wrapper no longer fakes HEAD to `source_commit` for this exact
        # invocation form -- it must exec the real (fresh) snapshot repo HEAD instead, and that
        # must be identical regardless of how the caller phrases the same question (e.g. with a
        # `-C` global option, as an oracle command might).
        bare_head = completed.stdout.strip()
        assert bare_head and all(c in "0123456789abcdef" for c in bare_head)
        dash_c = subprocess.run(
            [
                launch.executable,
                "--settings",
                str(launch.settings_path),
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "--short",
                "HEAD",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=20,
            env=launch.env,
        )
        assert dash_c.returncode == 0, dash_c.stderr
        assert dash_c.stdout.strip() == bare_head
        readme = worktree / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        diff = subprocess.run(
            [
                launch.executable,
                "--settings",
                str(launch.settings_path),
                "git",
                "diff",
                "--quiet",
                "HEAD",
                "--",
                "README.md",
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=20,
            env=launch.env,
        )
        assert diff.returncode == 1, diff.stderr
        snapshot_write = subprocess.run(
            [
                launch.executable,
                "--settings",
                str(launch.settings_path),
                "/usr/bin/touch",
                str(Path(launch.env["GIT_DIR"]) / "config"),
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=20,
            env=launch.env,
        )
        assert snapshot_write.returncode != 0
    finally:
        siso.cleanup_scenario_isolation(launch)
