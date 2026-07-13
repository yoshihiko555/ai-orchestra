#!/usr/bin/env python3
"""Bounded host-side process runner for Docker-contained scenario commands."""

from __future__ import annotations

import logging
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

MAX_SCENARIO_OUTPUT_BYTES = 10_000_000
_READ_CHUNK_BYTES = 64 * 1024
_LOGGER = logging.getLogger(__name__)


class ScenarioContainmentUnavailable(RuntimeError):
    """The requested container cleanup boundary could not be established."""


class ScenarioOutputLimitError(RuntimeError):
    """A scenario or oracle exceeded its bounded output allowance."""


def run_bounded_process_tree(
    args: list[str],
    *,
    cwd: Path,
    stdin: int,
    stdout: BinaryIO,
    stderr: BinaryIO,
    timeout: int | float,
    env: dict[str, str],
    max_output_bytes: int = MAX_SCENARIO_OUTPUT_BYTES,
    cleanup_args: list[str] | None = None,
    success_callback: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess:
    """Run a Docker CLI process while independently bounding stdout and stderr."""
    return _run_bounded(
        args,
        cwd=cwd,
        stdin=stdin,
        stdout_sink=stdout,
        stderr_sink=stderr,
        timeout=timeout,
        env=env,
        max_output_bytes=max_output_bytes,
        cleanup_args=cleanup_args,
        capture=False,
        success_callback=success_callback,
    )


def run_bounded_capture(
    args: list[str],
    *,
    cwd: Path,
    timeout: int | float,
    env: dict[str, str],
    max_output_bytes: int = 1_000_000,
    cleanup_args: list[str] | None = None,
    success_callback: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess:
    """Run a contained oracle/judge command and capture bounded text output."""
    return _run_bounded(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout_sink=None,
        stderr_sink=None,
        timeout=timeout,
        env=env,
        max_output_bytes=max_output_bytes,
        cleanup_args=cleanup_args,
        capture=True,
        success_callback=success_callback,
    )


def _run_bounded(
    args: list[str],
    *,
    cwd: Path,
    stdin: int,
    stdout_sink: BinaryIO | None,
    stderr_sink: BinaryIO | None,
    timeout: int | float,
    env: dict[str, str],
    max_output_bytes: int,
    cleanup_args: list[str] | None,
    capture: bool,
    success_callback: Callable[[], None] | None,
) -> subprocess.CompletedProcess:
    if not cleanup_args:
        raise ScenarioContainmentUnavailable(
            "container cleanup command is required for descendant containment"
        )
    process: subprocess.Popen[bytes] | None = None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    deadline = time.monotonic() + float(timeout)
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        _drain_bounded_pipes(
            process,
            deadline=deadline,
            max_output_bytes=max_output_bytes,
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
            stdout_buffer=stdout_buffer,
            stderr_buffer=stderr_buffer,
        )
        remaining = max(0.0, deadline - time.monotonic())
        return_code = process.wait(timeout=remaining)
        if return_code == 0 and success_callback is not None:
            success_callback()
        return subprocess.CompletedProcess(
            args=args,
            returncode=return_code,
            stdout=stdout_buffer.decode("utf-8", errors="replace") if capture else None,
            stderr=stderr_buffer.decode("utf-8", errors="replace") if capture else None,
        )
    except subprocess.TimeoutExpired:
        raise
    finally:
        cleanup_succeeded = _force_container_cleanup(cleanup_args)
        if process is not None and process.poll() is None:
            _terminate_host_process_group(process)
        if not cleanup_succeeded:
            message = "docker rm -f failed and container absence could not be verified"
            if sys.exc_info()[0] is not None:
                _LOGGER.error("%s while preserving the in-flight process exception", message)
            else:
                raise ScenarioContainmentUnavailable(message)


def _drain_bounded_pipes(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
    max_output_bytes: int,
    stdout_sink: BinaryIO | None,
    stderr_sink: BinaryIO | None,
    stdout_buffer: bytearray,
    stderr_buffer: bytearray,
) -> None:
    if process.stdout is None or process.stderr is None:
        raise ScenarioContainmentUnavailable("could not capture Docker process output")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, (stdout_sink, stdout_buffer, "stdout"))
    selector.register(process.stderr, selectors.EVENT_READ, (stderr_sink, stderr_buffer, "stderr"))
    sizes = {"stdout": 0, "stderr": 0}
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout=0)
            events = selector.select(timeout=min(0.25, remaining))
            if not events and process.poll() is not None:
                events = selector.select(timeout=0)
                if not events:
                    break
            for key, _ in events:
                sink, buffer, label = key.data
                chunk = os.read(key.fileobj.fileno(), _READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                sizes[label] += len(chunk)
                if sizes[label] > max_output_bytes:
                    raise ScenarioOutputLimitError(
                        f"scenario {label} exceeded {max_output_bytes} byte limit"
                    )
                if sink is not None:
                    sink.write(chunk)
                    sink.flush()
                else:
                    buffer.extend(chunk)
    finally:
        selector.close()


def _force_container_cleanup(cleanup_args: list[str]) -> bool:
    try:
        completed = subprocess.run(
            cleanup_args,
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode == 0:
        return True
    if cleanup_args[:3] != ["docker", "rm", "-f"] or len(cleanup_args) != 4:
        return False
    try:
        inspected = subprocess.run(
            ["docker", "inspect", cleanup_args[3]],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return inspected.returncode != 0


def _terminate_host_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return
