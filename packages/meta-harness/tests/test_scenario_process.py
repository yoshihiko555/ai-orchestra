"""Bounded Docker CLI process runner tests (EV-46)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.module_loader import load_module

sproc = load_module(
    "meta_harness_scenario_process_tests",
    "packages/meta-harness/lib/scenario_process.py",
)


def test_cleanup_boundary_is_mandatory(tmp_path: Path) -> None:
    with pytest.raises(sproc.ScenarioContainmentUnavailable, match="cleanup command"):
        sproc.run_bounded_capture(
            [sys.executable, "-c", "print('ok')"],
            cwd=tmp_path,
            timeout=1,
            env={"PATH": "/usr/bin:/bin"},
        )


def test_bounded_capture_returns_output_and_always_runs_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    cleaned: list[list[str]] = []

    def cleanup(args):
        cleaned.append(args)
        return True

    monkeypatch.setattr(sproc, "_force_container_cleanup", cleanup)

    completed = sproc.run_bounded_capture(
        [sys.executable, "-c", "print('ok')"],
        cwd=tmp_path,
        timeout=2,
        env={"PATH": "/usr/bin:/bin"},
        cleanup_args=["docker", "rm", "-f", "mh-run-test"],
    )

    assert completed.returncode == 0
    assert completed.stdout == "ok\n"
    assert cleaned == [["docker", "rm", "-f", "mh-run-test"]]


def test_success_callback_exports_before_cleanup(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        sproc,
        "_force_container_cleanup",
        lambda _args: events.append("cleanup") or True,
    )

    completed = sproc.run_bounded_capture(
        [sys.executable, "-c", "print('done')"],
        cwd=tmp_path,
        timeout=2,
        env={"PATH": "/usr/bin:/bin"},
        cleanup_args=["docker", "rm", "-f", "mh-run-export"],
        success_callback=lambda: events.append("export"),
    )

    assert completed.returncode == 0
    assert events == ["export", "cleanup"]


def test_each_output_stream_has_independent_hard_limit(tmp_path: Path, monkeypatch) -> None:
    cleaned: list[list[str]] = []

    def cleanup(args):
        cleaned.append(args)
        return True

    monkeypatch.setattr(sproc, "_force_container_cleanup", cleanup)

    with pytest.raises(sproc.ScenarioOutputLimitError, match="stdout"):
        sproc.run_bounded_capture(
            [sys.executable, "-c", "print('x' * 100)"],
            cwd=tmp_path,
            timeout=2,
            env={"PATH": "/usr/bin:/bin"},
            max_output_bytes=10,
            cleanup_args=["docker", "rm", "-f", "mh-run-limit"],
        )

    assert cleaned == [["docker", "rm", "-f", "mh-run-limit"]]


def test_timeout_always_runs_cleanup(tmp_path: Path, monkeypatch) -> None:
    cleaned: list[list[str]] = []

    def cleanup(args):
        cleaned.append(args)
        return True

    monkeypatch.setattr(sproc, "_force_container_cleanup", cleanup)

    with pytest.raises(subprocess.TimeoutExpired):
        sproc.run_bounded_capture(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            timeout=0.05,
            env={"PATH": "/usr/bin:/bin"},
            cleanup_args=["docker", "rm", "-f", "mh-run-timeout"],
        )

    assert cleaned == [["docker", "rm", "-f", "mh-run-timeout"]]


def test_timeout_is_preserved_when_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sproc, "_force_container_cleanup", lambda _args: False)

    with pytest.raises(subprocess.TimeoutExpired):
        sproc.run_bounded_capture(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            timeout=0.05,
            env={"PATH": "/usr/bin:/bin"},
            cleanup_args=["docker", "rm", "-f", "mh-run-timeout-cleanup-failure"],
        )

    assert "preserving the in-flight process exception" in caplog.text


def test_successful_command_fails_closed_when_container_cleanup_is_unverified(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sproc, "_force_container_cleanup", lambda _args: False)

    with pytest.raises(sproc.ScenarioContainmentUnavailable, match="could not be verified"):
        sproc.run_bounded_capture(
            [sys.executable, "-c", "print('ok')"],
            cwd=tmp_path,
            timeout=2,
            env={"PATH": "/usr/bin:/bin"},
            cleanup_args=["docker", "rm", "-f", "mh-run-cleanup-failure"],
        )


def test_cleanup_inspect_daemon_error_does_not_verify_container_absence(monkeypatch) -> None:
    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="remove failed")
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        )

    monkeypatch.setattr(sproc.subprocess, "run", fake_run)

    assert sproc._force_container_cleanup(["docker", "rm", "-f", "mh-run-test"]) is False


def test_cleanup_inspect_missing_object_verifies_container_absence(monkeypatch) -> None:
    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="remove failed")
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="Error: No such object: mh-run-test",
        )

    monkeypatch.setattr(sproc.subprocess, "run", fake_run)

    assert sproc._force_container_cleanup(["docker", "rm", "-f", "mh-run-test"]) is True
