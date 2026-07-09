"""Phase 2 M4: proposer backend lifecycle / subprocess helpers の直接テスト。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.module_loader import load_module

backend = load_module(
    "meta_harness_proposer_backend_test",
    "packages/meta-harness/lib/proposer_backend.py",
)

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _valid_proposal() -> dict:
    return {
        "schema_version": "1.0",
        "hypothesis": "Tighten example facet.",
        "theme": "tighten example",
        "changes": [{"path": "facets/example/SKILL.md", "new_content": "# Example\n"}],
        "based_on_runs": ["run-20260708-010000-base-scn-a1-abcd"],
        "expected_effect": "The run should pass.",
        "risk_notes": "Fixture only.",
    }


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _install_required_tools(bin_dir: Path, *names: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        _write_executable(bin_dir / name)


def _isolation_launch(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    return backend.iso.IsolationLaunch("srt", settings, {}, {}, {})


def _runner_writes(output: str | None, *, returncode: int = 0):
    def _runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        if output is not None and "-o" in args:
            Path(args[args.index("-o") + 1]).write_text(output, encoding="utf-8")
        return subprocess.CompletedProcess(args, returncode, "tokens used: 1,234\n", "boom\n")

    return _runner


class TestProposerBackendLaunch:
    def test_codex_backend_rejects_nonzero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_required_tools(tmp_path / "bin", "srt", "codex")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

        with pytest.raises(backend.ProposerRuntimeError, match="exited 7"):
            backend.launch_proposer_backend(
                view_dir=tmp_path,
                prompt="prompt",
                schema_dir=SCHEMA_DIR,
                config={"proposer": {"tool": "codex"}},
                isolation_launch=_isolation_launch(tmp_path),
                runner=_runner_writes(None, returncode=7),
            )

    def test_codex_backend_rejects_missing_output_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_required_tools(tmp_path / "bin", "srt", "codex")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

        with pytest.raises(backend.ProposalValidationError, match="missing or empty"):
            backend.launch_proposer_backend(
                view_dir=tmp_path,
                prompt="prompt",
                schema_dir=SCHEMA_DIR,
                config={"proposer": {"tool": "codex"}},
                isolation_launch=_isolation_launch(tmp_path),
                runner=_runner_writes(None),
            )

    def test_codex_backend_rejects_invalid_output_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_required_tools(tmp_path / "bin", "srt", "codex")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

        with pytest.raises(backend.ProposalValidationError, match="not valid JSON"):
            backend.launch_proposer_backend(
                view_dir=tmp_path,
                prompt="prompt",
                schema_dir=SCHEMA_DIR,
                config={"proposer": {"tool": "codex"}},
                isolation_launch=_isolation_launch(tmp_path),
                runner=_runner_writes("{not-json"),
            )

    def test_codex_backend_returns_tokens_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_required_tools(tmp_path / "bin", "srt", "codex")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

        result = backend.launch_proposer_backend(
            view_dir=tmp_path,
            prompt="prompt",
            schema_dir=SCHEMA_DIR,
            config={"proposer": {"tool": "codex"}},
            isolation_launch=_isolation_launch(tmp_path),
            runner=_runner_writes(json.dumps(_valid_proposal())),
        )

        assert result.proposal["theme"] == "tighten example"
        assert result.tokens_used == 1234

    def test_claude_bare_backend_normalizes_nested_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_required_tools(tmp_path / "bin", "srt", "claude")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

        nested = json.dumps(_valid_proposal())

        def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": nested}), "")

        result = backend.launch_proposer_backend(
            view_dir=tmp_path,
            prompt="prompt",
            schema_dir=SCHEMA_DIR,
            config={"proposer": {"tool": "claude-bare"}},
            isolation_launch=_isolation_launch(tmp_path),
            runner=runner,
        )

        assert result.proposal["theme"] == "tighten example"
        assert (
            json.loads(result.output_path.read_text(encoding="utf-8"))["theme"] == "tighten example"
        )


class TestProposerBackendHelpers:
    def test_timeout_seconds_validation(self) -> None:
        with pytest.raises(backend.ProposalValidationError, match="must be > 0"):
            backend._proposer_timeout_seconds({"timeout_seconds": 0})
        with pytest.raises(backend.ProposalValidationError, match="must be an integer"):
            backend._proposer_timeout_seconds({"timeout_seconds": "soon"})

    def test_parse_tokens_used(self) -> None:
        assert backend.parse_tokens_used("tokens used: 42\n") == 42
        assert backend.parse_tokens_used("Tokens Used: 1,234\n") == 1234
        assert backend.parse_tokens_used("no usage here") is None

    def test_temporary_codex_home_populates_modes_and_cleans_on_exit(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")

        with backend.temporary_codex_home(source_home=source) as home:
            assert home.stat().st_mode & 0o777 == 0o700
            assert (home / "auth.json").read_text(encoding="utf-8") == '{"token":"test"}\n'
            assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
            assert (home / "config.toml").stat().st_mode & 0o777 == 0o600
            assert (home / "AGENTS.md").read_text(encoding="utf-8") == ""
            home_path = home

        assert not home_path.exists()

    def test_sigterm_handler_cleans_temporary_codex_home(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")
        home_path: Path | None = None
        previous = signal.getsignal(signal.SIGTERM)

        with pytest.raises(SystemExit):
            with backend.temporary_codex_home(source_home=source) as home:
                home_path = home
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)

        assert home_path is not None
        assert not home_path.exists()
        assert signal.getsignal(signal.SIGTERM) == previous

    def test_sweep_orphan_codex_homes_removes_only_old_dirs(self) -> None:
        tmp_root = Path(tempfile.gettempdir())
        old = Path(tempfile.mkdtemp(prefix=backend._CODEX_HOME_PREFIX, dir=tmp_root))
        fresh = Path(tempfile.mkdtemp(prefix=backend._CODEX_HOME_PREFIX, dir=tmp_root))
        old_time = time.time() - backend._CODEX_ORPHAN_MAX_AGE.total_seconds() - 60
        os.utime(old, (old_time, old_time))

        try:
            backend._sweep_orphan_codex_homes()

            assert not old.exists()
            assert fresh.exists()
        finally:
            if fresh.exists():
                shutil.rmtree(fresh, ignore_errors=True)

    def test_run_process_tree_kills_child_on_timeout(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "child.pid"
        child_code = (
            "import signal, sys, time, pathlib\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()))\n"
            "time.sleep(60)\n"
        )
        parent_code = (
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
            "pid = pathlib.Path(sys.argv[2])\n"
            "while not pid.exists():\n"
            "    time.sleep(0.01)\n"
            "time.sleep(60)\n"
        )

        with pytest.raises(subprocess.TimeoutExpired):
            backend._run_process_tree(
                [sys.executable, "-c", parent_code, child_code, str(pid_file)],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=0.5,
                stdin=subprocess.DEVNULL,
                env=os.environ.copy(),
            )

        child_pid = int(pid_file.read_text(encoding="utf-8"))
        for _ in range(40):
            if not _pid_exists(child_pid):
                break
            time.sleep(0.05)
        assert not _pid_exists(child_pid)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
