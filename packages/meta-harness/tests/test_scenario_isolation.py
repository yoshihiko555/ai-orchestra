"""Scenario runner OS-isolation tests (ADR-20260711-033)."""

from __future__ import annotations

import copy
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
        assert completed.stdout.strip() == source_commit[:7]
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
