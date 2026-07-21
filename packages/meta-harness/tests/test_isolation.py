"""proposer isolation backend のテスト（Phase 2 M1, EV-35/EV-40）。

実 `srt` が無い環境では reachability regression は skip し、設定生成・静的検査・
fail-closed 経路は fake runner で常時検証する。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common",
    "packages/meta-harness/lib/meta_harness_common.py",
)
iso = load_module(
    "meta_harness_isolation",
    "packages/meta-harness/lib/isolation.py",
)


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _is_unsandboxed_canary(cmd: list[str]) -> bool:
    return cmd[:1] == ["/bin/cat"] or (bool(cmd) and Path(cmd[0]).name == "curl")


def _config() -> dict:
    return copy.deepcopy(mh.DEFAULTS)


@pytest.fixture(autouse=True)
def _cleanup_per_run_tmp_dirs():
    """resolve 成功時の per-run tmp（/tmp/mh-ptmp-*）ownership は呼び出し側にあるため、
    テストが明示 cleanup しない分をここで回収する。"""
    base = Path("/tmp").resolve()
    before = set(base.glob("mh-ptmp-*"))
    yield
    for path in set(base.glob("mh-ptmp-*")) - before:
        shutil.rmtree(path, ignore_errors=True)


def _make_view_and_home(tmp_path: Path) -> tuple[Path, Path]:
    view_dir = tmp_path / "view"
    ephemeral_home = tmp_path / "codex-home"
    view_dir.mkdir()
    ephemeral_home.mkdir()
    return view_dir, ephemeral_home


class TestSrtSettingsGeneration:
    def test_build_srt_settings_for_codex_uses_full_schema(self, git_project, tmp_path) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)

        settings = iso.build_srt_settings(
            view_dir=view_dir,
            main_root=git_project,
            config=_config(),
            ephemeral_home=ephemeral_home,
            proposer_tool="codex",
        )

        assert set(settings) == {
            "network",
            "filesystem",
            "ignoreViolations",
            "enableWeakerNestedSandbox",
            "enableWeakerNetworkIsolation",
            "allowAppleEvents",
        }
        assert set(settings["network"]) == {
            "allowedDomains",
            "deniedDomains",
            "strictAllowlist",
            "allowUnixSockets",
            "allowLocalBinding",
            "tlsTerminate",
        }
        assert set(settings["filesystem"]) == {
            "denyRead",
            "allowRead",
            "allowWrite",
            "denyWrite",
        }
        assert settings["network"]["allowedDomains"] == iso.CODEX_ALLOWED_DOMAINS
        assert settings["network"]["allowLocalBinding"] is True
        assert settings["network"]["strictAllowlist"] is True
        assert settings["network"]["deniedDomains"] == []
        assert settings["network"]["tlsTerminate"] == {
            "excludeDomains": iso.CODEX_TLS_TERMINATE_EXCLUDE_DOMAINS
        }
        assert settings["filesystem"]["allowRead"] == [str(view_dir.resolve())]
        assert str(ephemeral_home.resolve()) in settings["filesystem"]["allowWrite"]
        assert str(git_project.resolve()) in settings["filesystem"]["denyRead"]
        assert str(Path.home().resolve()) in settings["filesystem"]["denyRead"]

    def test_build_srt_settings_for_claude_bare_uses_anthropic_domain(
        self, git_project, tmp_path
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)

        settings = iso.build_srt_settings(
            view_dir=view_dir,
            main_root=git_project,
            config=_config(),
            ephemeral_home=ephemeral_home,
            proposer_tool="claude-bare",
        )

        assert settings["network"]["allowedDomains"] == iso.CLAUDE_BARE_ALLOWED_DOMAINS
        assert settings["network"]["allowLocalBinding"] is False
        assert "tlsTerminate" not in settings["network"]
        assert str(ephemeral_home.resolve()) not in settings["filesystem"]["allowWrite"]

    def test_write_srt_settings_uses_canonical_json_and_0600(self, tmp_path) -> None:
        settings = {"network": {"deniedDomains": [], "allowedDomains": []}}

        path = iso.write_srt_settings(settings, tmp_path)

        assert json.loads(path.read_text(encoding="utf-8")) == settings
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestDenyReadAndStaticGuards:
    def test_deny_read_derivation_includes_home_main_root_and_worktrees(
        self, git_project, add_feature_worktree
    ) -> None:
        feature_worktree = add_feature_worktree(git_project)

        deny_read = iso.derive_deny_read_paths(git_project)

        assert Path.home().resolve() in deny_read
        assert git_project.resolve() in deny_read
        assert feature_worktree.resolve() in deny_read

    def test_allow_read_extra_intersecting_codex_home_is_rejected(
        self, git_project, tmp_path
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        config = _config()
        config["proposer"]["isolation"]["allow_read_extra"] = [str(Path.home() / ".codex")]

        with pytest.raises(iso.IsolationError, match="forbidden asset"):
            iso.build_srt_settings(
                view_dir=view_dir,
                main_root=git_project,
                config=config,
                ephemeral_home=ephemeral_home,
                proposer_tool="codex",
            )

    def test_view_inside_store_is_rejected_by_static_guard(self, git_project) -> None:
        config = _config()
        view_dir = mh.store_dir(git_project, config) / "tmp" / "view-under-store"
        ephemeral_home = git_project / ".tmp-codex-home"

        with pytest.raises(iso.IsolationError, match="forbidden asset"):
            iso.build_srt_settings(
                view_dir=view_dir,
                main_root=git_project,
                config=config,
                ephemeral_home=ephemeral_home,
                proposer_tool="codex",
            )

    def test_view_equal_to_main_root_is_rejected_by_static_guard(
        self, git_project, tmp_path
    ) -> None:
        _view_dir, ephemeral_home = _make_view_and_home(tmp_path)

        with pytest.raises(iso.IsolationError, match="forbidden asset"):
            iso.build_srt_settings(
                view_dir=git_project,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                proposer_tool="codex",
            )


class TestEnvironmentAndInstructionGuards:
    def test_minimal_env_drops_unlisted_canary(self, monkeypatch) -> None:
        monkeypatch.setenv("META_HARNESS_CANARY_SECRET", "do-not-leak")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        env = iso.build_minimal_env({"CODEX_HOME": "/tmp/codex-home"})

        assert env["PATH"] == "/usr/bin:/bin"
        assert env["CODEX_HOME"] == "/tmp/codex-home"
        assert "META_HARNESS_CANARY_SECRET" not in env

    def test_instruction_files_in_view_are_rejected(self, tmp_path) -> None:
        view_dir = tmp_path / "view"
        view_dir.mkdir()
        (view_dir / "AGENTS.md").write_text("malicious instruction\n", encoding="utf-8")

        with pytest.raises(iso.IsolationError, match="instruction file"):
            iso.verify_no_instruction_files(view_dir)

    def test_empty_agents_file_is_created_in_ephemeral_home(self, tmp_path) -> None:
        agents_path = iso.ensure_empty_agents_file(tmp_path / "codex-home")

        assert agents_path.name == "AGENTS.md"
        assert agents_path.read_text(encoding="utf-8") == ""
        assert stat.S_IMODE(agents_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(agents_path.parent.stat().st_mode) == 0o700


class TestResolveIsolationBackend:
    def test_wrap_srt_command_stops_option_parsing(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "srt-settings.json"
        command = ["bash", "-c", 'exec "$0" "$@"', "codex", "exec"]

        assert iso.wrap_srt_command("/usr/bin/srt", settings_path, command) == [
            "/usr/bin/srt",
            "--settings",
            str(settings_path),
            "--",
            *command,
        ]

    def test_platform_profile_input_hash_changes_with_settings(self) -> None:
        base_settings = {"network": {"allowedDomains": ["a.example"], "deniedDomains": []}}
        changed_settings = {"network": {"allowedDomains": ["b.example"], "deniedDomains": []}}

        base = iso.build_isolation_metadata(
            backend_name="srt", srt_version="1.0.0", settings=base_settings
        )
        changed = iso.build_isolation_metadata(
            backend_name="srt", srt_version="1.0.0", settings=changed_settings
        )

        assert base["platform_profile_input_sha256"] != changed["platform_profile_input_sha256"]

    def test_unknown_isolation_backend_fails_closed(self, git_project, tmp_path) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        config = _config()
        config["proposer"]["isolation"]["backend"] = "none"

        with pytest.raises(iso.IsolationError, match="unsupported proposer.isolation.backend"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=config,
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
            )

    def test_unknown_proposer_tool_fails_closed(self, git_project, tmp_path) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)

        with pytest.raises(iso.IsolationError, match="unsupported proposer.tool"):
            iso.build_srt_settings(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                proposer_tool="unknown",
            )

    def test_claude_bare_requires_api_key(self, git_project, tmp_path, monkeypatch) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(iso.IsolationError, match="claude-bare"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
                proposer_tool="claude-bare",
            )

    def test_claude_bare_env_includes_api_key_only_when_available(
        self, git_project, tmp_path, monkeypatch
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic-key")
        monkeypatch.setenv("META_HARNESS_CANARY_SECRET", "do-not-leak")
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="0.0.64")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                assert kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-test-anthropic-key"
                assert "META_HARNESS_CANARY_SECRET" not in kwargs["env"]
                if "/bin/cat" in cmd:
                    return _completed(1, stderr="cat: Operation not permitted")
                return _completed(56, stderr="curl: (56) connection reset by proxy")
            raise AssertionError(f"unexpected command: {cmd}")

        launch = iso.resolve_isolation_backend(
            view_dir=view_dir,
            main_root=git_project,
            config=_config(),
            ephemeral_home=ephemeral_home,
            settings_dir=tmp_path,
            proposer_tool="claude-bare",
            runner=runner,
        )

        assert launch.env["ANTHROPIC_API_KEY"] == "sk-test-anthropic-key"
        assert launch.settings["network"]["allowedDomains"] == iso.CLAUDE_BARE_ALLOWED_DOMAINS

    def test_missing_srt_fails_closed(self, git_project, tmp_path, monkeypatch) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setattr(iso.shutil, "which", lambda name: None)

        with pytest.raises(iso.IsolationError, match="srt is not available"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
            )

    def test_owned_temp_settings_dir_is_removed_on_failure(
        self, git_project, tmp_path, monkeypatch
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        created_dirs: list[Path] = []

        def fake_mkdtemp(prefix: str, dir: str | None = None):
            path = tmp_path / f"{prefix}owned"
            path.mkdir()
            created_dirs.append(path)
            return str(path)

        monkeypatch.setattr(iso.tempfile, "mkdtemp", fake_mkdtemp)
        monkeypatch.setattr(iso.shutil, "which", lambda name: None)

        with pytest.raises(iso.IsolationError, match="srt is not available"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
            )

        assert created_dirs
        assert not created_dirs[0].exists()

    def test_version_parser_uses_only_first_line(self) -> None:
        def runner(cmd, **kwargs):
            return _completed(0, stdout="not a version\n1.2.3\n")

        with pytest.raises(iso.IsolationError, match="could not determine srt version"):
            iso._get_srt_version("/usr/bin/srt", runner=runner)

    def test_version_parser_accepts_stderr_first_line_when_stdout_is_empty(self) -> None:
        def runner(cmd, **kwargs):
            return _completed(0, stderr="1.2.3\nextra 9.9.9\n")

        assert iso._get_srt_version("/usr/bin/srt", runner=runner) == "1.2.3"

    def test_version_pin_mismatch_fails_closed(self, git_project, tmp_path, monkeypatch) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        config = _config()
        config["proposer"]["isolation"]["srt_version_pin"] = "0.0.65"
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="srt 0.0.64\n")
            raise AssertionError(f"unexpected command: {cmd}")

        with pytest.raises(iso.IsolationError, match="srt_version_pin mismatch"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=config,
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
                runner=runner,
            )

    def test_resolve_runs_canaries_and_returns_metadata(
        self, git_project, tmp_path, monkeypatch
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setenv("META_HARNESS_CANARY_SECRET", "do-not-leak")
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")
        calls: list[tuple[list[str], dict]] = []

        def runner(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="@anthropic-ai/sandbox-runtime 0.0.64")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                assert "META_HARNESS_CANARY_SECRET" not in kwargs["env"]
                if "/bin/cat" in cmd:
                    return _completed(1, stderr="cat: Operation not permitted")
                return _completed(56, stderr="curl: (56) connection reset by proxy")
            raise AssertionError(f"unexpected command: {cmd}")

        launch = iso.resolve_isolation_backend(
            view_dir=view_dir,
            main_root=git_project,
            config=_config(),
            ephemeral_home=ephemeral_home,
            settings_dir=tmp_path,
            runner=runner,
        )

        assert launch.backend_name == "srt"
        assert launch.metadata["srt_version"] == "0.0.64"
        assert launch.owned_settings_dir is None
        assert len(launch.metadata["settings_sha256"]) == 64
        assert len(launch.metadata["platform_profile_input_sha256"]) == 64
        assert sum(1 for cmd, _kwargs in calls if "--settings" in cmd) == 3
        assert sum(1 for cmd, _kwargs in calls if _is_unsandboxed_canary(cmd)) == 3
        assert (ephemeral_home / "AGENTS.md").read_text(encoding="utf-8") == ""

    def test_owned_temp_settings_dir_is_reported_on_success(
        self, git_project, tmp_path, monkeypatch
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        owned_dir = tmp_path / "meta-harness-srt-owned"

        def fake_mkdtemp(prefix: str, dir: str | None = None):
            if prefix == "mh-ptmp-":
                run_tmp = tmp_path / "run-tmp"
                run_tmp.mkdir(exist_ok=True)
                return str(run_tmp)
            return str(owned_dir)

        monkeypatch.setattr(iso.tempfile, "mkdtemp", fake_mkdtemp)
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="0.0.64")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                if "/bin/cat" in cmd:
                    return _completed(1, stderr="cat: Operation not permitted")
                return _completed(56, stderr="curl: (56) connection reset by proxy")
            raise AssertionError(f"unexpected command: {cmd}")

        launch = iso.resolve_isolation_backend(
            view_dir=view_dir,
            main_root=git_project,
            config=_config(),
            ephemeral_home=ephemeral_home,
            runner=runner,
        )

        assert launch.owned_settings_dir == owned_dir
        assert (owned_dir / iso.SRT_SETTINGS_FILENAME).is_file()

    def test_read_canary_success_fails_closed(self, git_project, tmp_path, monkeypatch) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="0.0.64")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                return _completed(0, stdout="leaked")
            raise AssertionError(f"unexpected command: {cmd}")

        with pytest.raises(iso.IsolationError, match="unexpectedly succeeded"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
                runner=runner,
            )

    def test_network_canary_dns_failure_does_not_prove_isolation(
        self, git_project, tmp_path, monkeypatch
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="0.0.64")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                if "/bin/cat" in cmd:
                    return _completed(1, stderr="cat: Operation not permitted")
                # DNS 解決失敗 (curl exit 6) は遮断の証明にならない
                return _completed(6, stderr="curl: (6) Could not resolve host")
            raise AssertionError(f"unexpected command: {cmd}")

        with pytest.raises(iso.IsolationError, match="without a sandbox-denial signal"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
                runner=runner,
            )

    def test_direct_ip_origin_http_error_does_not_prove_isolation(
        self, git_project, tmp_path, monkeypatch
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="0.0.64")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                if iso.NETWORK_DIRECT_IP_CANARY_URL in cmd:
                    return _completed(22, stderr="curl: (22) HTTP response code said error")
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                if "/bin/cat" in cmd:
                    return _completed(1, stderr="cat: Operation not permitted")
                return _completed(56, stderr="curl: (56) connection reset by proxy")
            raise AssertionError(f"unexpected command: {cmd}")

        with pytest.raises(iso.IsolationError, match="unsandboxed control failed"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
                runner=runner,
            )

    def test_read_canary_missing_file_does_not_prove_isolation(
        self, git_project, tmp_path, monkeypatch
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="0.0.64")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                # canary ファイル欠損等の失敗は遮断の証明にならない
                return _completed(1, stderr="cat: No such file or directory")
            raise AssertionError(f"unexpected command: {cmd}")

        with pytest.raises(iso.IsolationError, match="without a sandbox-denial signal"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
                runner=runner,
            )

    def test_srt_startup_failure_does_not_count_as_canary_rejection(
        self, git_project, tmp_path, monkeypatch
    ) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="1.0.0")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                return _completed(
                    1,
                    stderr=(
                        "Error: listen EPERM: operation not permitted "
                        "/var/folders/example/T/srt-mux.sock"
                    ),
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with pytest.raises(iso.IsolationError, match="could not prove isolation"):
            iso.resolve_isolation_backend(
                view_dir=view_dir,
                main_root=git_project,
                config=_config(),
                ephemeral_home=ephemeral_home,
                settings_dir=tmp_path,
                runner=runner,
            )


def _prepare_real_srt_launch(git_project: Path, tmp_path: Path, monkeypatch) -> tuple[object, Path]:
    if iso.shutil.which("srt") is None:
        _skip_or_fail_real_srt("srt is not installed")
    view_dir, ephemeral_home = _make_view_and_home(tmp_path)
    monkeypatch.setenv("META_HARNESS_CANARY_SECRET", "do-not-leak")
    try:
        launch = iso.resolve_isolation_backend(
            view_dir=view_dir,
            main_root=git_project,
            config=_config(),
            ephemeral_home=ephemeral_home,
            settings_dir=tmp_path,
        )
    except iso.IsolationError as exc:
        if "unexpectedly succeeded" in str(exc):
            pytest.fail(str(exc))
        _skip_or_fail_real_srt(f"srt cannot run in this environment: {exc}")
    return launch, view_dir


def _skip_or_fail_real_srt(reason: str) -> None:
    if os.getenv("CI") and os.getenv("META_HARNESS_SKIP_SRT_TESTS") != "1":
        pytest.fail(
            f"{reason}. Set META_HARNESS_SKIP_SRT_TESTS=1 only on CI runners that "
            "explicitly cannot support srt."
        )
    pytest.skip(reason)


def _run_real_srt(launch, view_dir: Path, command: list[str]) -> subprocess.CompletedProcess:
    srt_path = iso.shutil.which("srt")
    assert srt_path is not None
    return subprocess.run(
        iso.wrap_srt_command(srt_path, launch.settings_path, command),
        cwd=view_dir,
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        env=launch.env,
    )


def test_real_srt_blocks_file_escape_vectors(git_project, tmp_path, monkeypatch) -> None:
    launch, view_dir = _prepare_real_srt_launch(git_project, tmp_path, monkeypatch)
    secret = mh.tmp_dir(git_project, _config()) / "real-srt-secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("secret\n", encoding="utf-8")
    symlink_path = view_dir / "escape-link"
    symlink_path.symlink_to(secret)
    traversal_path = os.path.relpath(secret, view_dir)

    try:
        assert _run_real_srt(launch, view_dir, ["/bin/cat", str(secret)]).returncode != 0
        assert _run_real_srt(launch, view_dir, ["/bin/cat", traversal_path]).returncode != 0
        assert _run_real_srt(launch, view_dir, ["/bin/cat", str(symlink_path)]).returncode != 0
    finally:
        secret.unlink(missing_ok=True)


def test_real_srt_blocks_network_and_env_leak(git_project, tmp_path, monkeypatch) -> None:
    launch, view_dir = _prepare_real_srt_launch(git_project, tmp_path, monkeypatch)
    curl = iso.shutil.which("curl")
    if curl is None:
        pytest.skip("curl is not installed")

    domain = _run_real_srt(
        launch, view_dir, [curl, "--fail", "--max-time", "3", "https://example.com"]
    )
    direct_ip = _run_real_srt(
        launch,
        view_dir,
        [curl, "--fail", "--max-time", "3", iso.NETWORK_DIRECT_IP_CANARY_URL],
    )
    env_check = _run_real_srt(
        launch,
        view_dir,
        ["/bin/sh", "-c", 'test -z "$META_HARNESS_CANARY_SECRET"'],
    )

    assert domain.returncode != 0
    assert direct_ip.returncode != 0
    assert env_check.returncode == 0


class TestPerRunTmpDir:
    def test_allow_write_excludes_shared_tmp(self, git_project, tmp_path) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        run_tmp = tmp_path / "run-tmp"
        run_tmp.mkdir(mode=0o700)

        settings = iso.build_srt_settings(
            view_dir=view_dir,
            main_root=git_project,
            config=_config(),
            ephemeral_home=ephemeral_home,
            proposer_tool="codex",
            run_tmp_dir=run_tmp,
        )

        allow_write = settings["filesystem"]["allowWrite"]
        assert "/tmp" not in allow_write
        assert "/private/tmp" not in allow_write
        assert str(run_tmp.resolve()) in allow_write

    def test_resolve_pins_tmp_env_to_per_run_dir(self, git_project, tmp_path, monkeypatch) -> None:
        view_dir, ephemeral_home = _make_view_and_home(tmp_path)
        monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")

        def runner(cmd, **kwargs):
            if cmd == ["/usr/bin/srt", "--version"]:
                return _completed(0, stdout="0.0.64")
            if cmd[:3] == ["git", "worktree", "list"]:
                return _completed(0, stdout=f"worktree {git_project}\n")
            if _is_unsandboxed_canary(cmd):
                return _completed(0, stdout="canary reachable")
            if cmd[0] == "/usr/bin/srt":
                if "/bin/cat" in cmd:
                    return _completed(1, stderr="cat: Operation not permitted")
                return _completed(56, stderr="curl: (56) connection reset by proxy")
            raise AssertionError(f"unexpected command: {cmd}")

        launch = iso.resolve_isolation_backend(
            view_dir=view_dir,
            main_root=git_project,
            config=_config(),
            ephemeral_home=ephemeral_home,
            settings_dir=tmp_path / "settings",
            runner=runner,
        )

        run_tmp = Path(launch.env["TMPDIR"])
        try:
            assert run_tmp.is_dir()
            assert run_tmp.stat().st_mode & 0o777 == 0o700
            # unix socket の sun_path 上限対策で /tmp 直下の短いパスに作られる
            assert run_tmp.parent == Path("/tmp").resolve()
            assert run_tmp.name.startswith("mh-ptmp-")
            assert launch.owned_tmp_dir == run_tmp
            assert launch.env["TMP"] == launch.env["TMPDIR"]
            assert launch.env["TEMP"] == launch.env["TMPDIR"]
            assert str(run_tmp) in launch.settings["filesystem"]["allowWrite"]
        finally:
            shutil.rmtree(run_tmp, ignore_errors=True)
