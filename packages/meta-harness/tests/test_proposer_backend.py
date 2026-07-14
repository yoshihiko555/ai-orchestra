"""Phase 2 M4: proposer backend lifecycle / subprocess helpers の直接テスト。"""

from __future__ import annotations

import base64
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
        usage_event = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 250,
                    "output_tokens": 234,
                },
            }
        )
        return subprocess.CompletedProcess(args, returncode, f"{usage_event}\n", "boom\n")

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
                ephemeral_home=tmp_path / "codex-home",
                runner=_runner_writes(None, returncode=7),
            )

    def test_codex_backend_redacts_credentials_from_nonzero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_required_tools(tmp_path / "bin", "srt", "codex")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        canary = backend.generate_auth_canary()
        jwt = _fake_jwt(int(time.time()) + 86400)

        def leaking_runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args,
                7,
                f"stdout leaked {canary}\n",
                f"stderr leaked {jwt}\n",
            )

        with pytest.raises(backend.ProposerRuntimeError) as exc_info:
            backend.launch_proposer_backend(
                view_dir=tmp_path,
                prompt="prompt",
                schema_dir=SCHEMA_DIR,
                config={"proposer": {"tool": "codex"}},
                isolation_launch=_isolation_launch(tmp_path),
                ephemeral_home=tmp_path / "codex-home",
                auth_canary=canary,
                runner=leaking_runner,
            )

        message = str(exc_info.value)
        assert canary not in message
        assert jwt not in message
        assert "[REDACTED:auth canary" in message
        assert "[REDACTED:JWT (3-segment)]" in message

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
                ephemeral_home=tmp_path / "codex-home",
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
                ephemeral_home=tmp_path / "codex-home",
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
            ephemeral_home=tmp_path / "codex-home",
            runner=_runner_writes(json.dumps(_valid_proposal())),
        )

        assert result.proposal["theme"] == "tighten example"
        assert result.tokens_used == 1234

    def test_codex_backend_stages_output_schema_under_ephemeral_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_required_tools(tmp_path / "bin", "srt", "codex")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        view_dir = tmp_path / "view"
        view_dir.mkdir()
        codex_home = tmp_path / "codex-home"
        captured_args: list[str] = []

        def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            captured_args.extend(args)
            output_schema = Path(args[args.index("--output-schema") + 1])
            assert output_schema.is_file()
            Path(args[args.index("-o") + 1]).write_text(
                json.dumps(_valid_proposal()),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, 0, "tokens used: 1\n", "")

        backend.launch_proposer_backend(
            view_dir=view_dir,
            prompt="prompt",
            schema_dir=SCHEMA_DIR,
            config={"proposer": {"tool": "codex"}},
            isolation_launch=_isolation_launch(tmp_path),
            ephemeral_home=codex_home,
            runner=runner,
        )

        output_schema = Path(captured_args[captured_args.index("--output-schema") + 1]).resolve()
        assert output_schema == (codex_home / backend.PROPOSAL_SCHEMA_NAME).resolve()
        assert SCHEMA_DIR.resolve() not in output_schema.parents
        assert output_schema.read_text(encoding="utf-8") == (
            SCHEMA_DIR / backend.PROPOSAL_SCHEMA_NAME
        ).read_text(encoding="utf-8")
        assert output_schema.stat().st_mode & 0o777 == 0o644

    def test_codex_backend_stages_output_schema_with_allowed_based_on_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_required_tools(tmp_path / "bin", "srt", "codex")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        view_dir = tmp_path / "view"
        view_dir.mkdir()
        captured_schema: dict = {}

        def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            output_schema = Path(args[args.index("--output-schema") + 1])
            captured_schema.update(json.loads(output_schema.read_text(encoding="utf-8")))
            Path(args[args.index("-o") + 1]).write_text(
                json.dumps(_valid_proposal()),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, 0, "tokens used: 1\n", "")

        backend.launch_proposer_backend(
            view_dir=view_dir,
            prompt="prompt",
            schema_dir=SCHEMA_DIR,
            config={"proposer": {"tool": "codex"}},
            isolation_launch=_isolation_launch(tmp_path),
            ephemeral_home=tmp_path / "codex-home",
            allowed_based_on_runs=(
                "run-20260708-010000-base-scn-a2-beef",
                "run-20260708-010000-base-scn-a1-abcd",
            ),
            runner=runner,
        )

        assert captured_schema["properties"]["based_on_runs"]["items"]["enum"] == [
            "run-20260708-010000-base-scn-a1-abcd",
            "run-20260708-010000-base-scn-a2-beef",
        ]
        assert captured_schema["properties"]["based_on_runs"]["items"]["pattern"] == "^run-"

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
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t-1"}),
                "not-json",
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 250,
                            "output_tokens": 234,
                        },
                    }
                ),
            ]
        )

        assert backend.parse_tokens_used(stdout) == 1234
        assert backend.parse_tokens_used("tokens used: 42\n") == 42
        assert backend.parse_tokens_used("Tokens Used: 1,234\n") == 1234
        assert backend.parse_tokens_used("no usage here") is None
        assert backend.parse_tokens_used("") is None

    def test_temporary_codex_home_populates_modes_and_cleans_on_exit(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")
        (source / "models_cache.json").write_text('{"models":[]}\n', encoding="utf-8")
        (source / "version.json").write_text('{"version":"0.143.0"}\n', encoding="utf-8")

        with backend.temporary_codex_home(source_home=source) as home:
            assert home.stat().st_mode & 0o777 == 0o700
            assert json.loads((home / "auth.json").read_text(encoding="utf-8")) == {"token": "test"}
            assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
            assert (home / "models_cache.json").read_text(encoding="utf-8") == '{"models":[]}\n'
            assert (home / "models_cache.json").stat().st_mode & 0o777 == 0o644
            assert (home / "version.json").read_text(encoding="utf-8") == (
                '{"version":"0.143.0"}\n'
            )
            assert (home / "version.json").stat().st_mode & 0o777 == 0o644
            assert (home / "config.toml").stat().st_mode & 0o777 == 0o600
            assert (home / "AGENTS.md").read_text(encoding="utf-8") == ""
            home_path = home

        assert not home_path.exists()

    def test_temporary_codex_home_only_copies_allowlisted_state(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")
        (source / "history.jsonl").write_text('{"prompt":"secret"}\n', encoding="utf-8")
        (source / "memories.json").write_text('{"repo":"memory"}\n', encoding="utf-8")
        (source / "sessions").mkdir()
        (source / "sessions" / "session.jsonl").write_text("session\n", encoding="utf-8")
        (source / "rules").mkdir()
        (source / "rules" / "rule.md").write_text("rule\n", encoding="utf-8")

        with backend.temporary_codex_home(source_home=source) as home:
            assert (home / "auth.json").is_file()
            assert not (home / "history.jsonl").exists()
            assert not (home / "memories.json").exists()
            assert not (home / "sessions").exists()
            assert not (home / "rules").exists()
            assert not (home / "models_cache.json").exists()
            assert not (home / "version.json").exists()

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

    def test_run_process_tree_kills_lingering_child_on_success(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "child.pid"
        child_code = (
            "import sys, time, pathlib\n"
            "pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()))\n"
            "time.sleep(60)\n"
        )
        parent_code = (
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen(\n"
            "    [sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            ")\n"
            "pid = pathlib.Path(sys.argv[2])\n"
            "while not pid.exists():\n"
            "    time.sleep(0.01)\n"
        )

        completed = backend._run_process_tree(
            [sys.executable, "-c", parent_code, child_code, str(pid_file)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
        )

        assert completed.returncode == 0
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


class TestProposalOutputHardening:
    def test_read_rejects_symlink_output(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret.json"
        secret.write_text('{"schema_version": "1.0"}\n', encoding="utf-8")
        output_path = tmp_path / "proposal-output.json"
        output_path.symlink_to(secret)

        with pytest.raises(backend.ProposalValidationError, match="regular file"):
            backend._read_proposal_output_bytes(output_path)

    def test_read_rejects_oversized_output(self, tmp_path: Path, monkeypatch) -> None:
        output_path = tmp_path / "proposal-output.json"
        output_path.write_text("x" * 64, encoding="utf-8")
        monkeypatch.setattr(backend, "MAX_PROPOSAL_OUTPUT_BYTES", 16)

        with pytest.raises(backend.ProposalValidationError, match="exceeds max bytes"):
            backend._read_proposal_output_bytes(output_path)

    def test_read_missing_file_reports_missing(self, tmp_path: Path) -> None:
        with pytest.raises(backend.ProposalValidationError, match="missing or empty"):
            backend._read_proposal_output_bytes(tmp_path / "absent.json")

    def test_write_replaces_planted_symlink_without_following(self, tmp_path: Path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("original\n", encoding="utf-8")
        output_path = tmp_path / "proposal-output.json"
        output_path.symlink_to(victim)

        backend._write_proposal_output(output_path, {"schema_version": "1.0"})

        assert victim.read_text(encoding="utf-8") == "original\n"
        assert not output_path.is_symlink()
        assert json.loads(output_path.read_text(encoding="utf-8")) == {"schema_version": "1.0"}


def _fake_jwt(exp_epoch: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp_epoch}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature"


def _codex_source_home(tmp_path: Path, *, exp_epoch: int) -> Path:
    source = tmp_path / "source-home"
    source.mkdir()
    auth = {
        "auth_mode": "chatgpt",
        "last_refresh": "2026-07-07T00:33:33Z",
        "OPENAI_API_KEY": "sk-real-api-key-do-not-stage",
        "tokens": {
            "access_token": _fake_jwt(exp_epoch),
            "id_token": "id.token.value",
            "refresh_token": "real-refresh-token-value",
            "account_id": "account-1234",
        },
    }
    (source / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    return source


class TestCodexAuthMinimization:
    def test_staged_auth_strips_long_lived_credentials(self, tmp_path: Path) -> None:
        exp_epoch = int(time.time()) + 86400
        source = _codex_source_home(tmp_path, exp_epoch=exp_epoch)

        with backend.temporary_codex_home(source_home=source, min_token_ttl_seconds=600) as home:
            staged = json.loads((home / "auth.json").read_text(encoding="utf-8"))

        assert "OPENAI_API_KEY" not in staged
        refresh = staged["tokens"]["refresh_token"]
        assert refresh.startswith(backend.CODEX_AUTH_CANARY_PREFIX)
        assert refresh != "real-refresh-token-value"
        assert staged["tokens"]["access_token"] == _fake_jwt(exp_epoch)
        assert staged["tokens"]["account_id"] == "account-1234"

    def test_expiring_access_token_fails_closed(self, tmp_path: Path) -> None:
        source = _codex_source_home(tmp_path, exp_epoch=int(time.time()) + 60)

        with pytest.raises(backend.ProposalValidationError, match="expires too soon"):
            with backend.temporary_codex_home(source_home=source, min_token_ttl_seconds=600):
                pytest.fail("must not launch with an expiring token")

    def test_non_jwt_access_token_fails_closed(self, tmp_path: Path) -> None:
        source = tmp_path / "source-home"
        source.mkdir()
        auth = {"tokens": {"access_token": "opaque-token", "refresh_token": "r"}}
        (source / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

        with pytest.raises(backend.ProposalValidationError, match="not a JWT"):
            with backend.temporary_codex_home(source_home=source, min_token_ttl_seconds=600):
                pytest.fail("must not launch with an unverifiable token")

    def test_minimization_applies_even_without_ttl_requirement(self, tmp_path: Path) -> None:
        source = _codex_source_home(tmp_path, exp_epoch=int(time.time()) - 100)

        with backend.temporary_codex_home(source_home=source) as home:
            staged = json.loads((home / "auth.json").read_text(encoding="utf-8"))

        assert "OPENAI_API_KEY" not in staged
        assert staged["tokens"]["refresh_token"].startswith(backend.CODEX_AUTH_CANARY_PREFIX)

    def test_explicit_canary_is_staged_verbatim(self, tmp_path: Path) -> None:
        # L2 検知が使う canary と staged refresh_token が同一であることを保証する。
        source = _codex_source_home(tmp_path, exp_epoch=int(time.time()) + 86400)
        canary = backend.generate_auth_canary()

        with backend.temporary_codex_home(source_home=source, auth_canary=canary) as home:
            staged = json.loads((home / "auth.json").read_text(encoding="utf-8"))

        assert staged["tokens"]["refresh_token"] == canary
