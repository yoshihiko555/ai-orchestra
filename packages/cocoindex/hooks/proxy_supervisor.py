#!/usr/bin/env python3
"""外部固定ポートで待ち受け、内部の mcp-proxy へ TCP 転送する supervisor。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import sys

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if _hooks_dir not in sys.path:
    sys.path.insert(0, _hooks_dir)

from proxy_manager import (  # noqa: E402
    _SUPERVISOR_CONFIG_ENV,
    _build_proxy_command,
    get_proxy_config,
    update_proxy_state,
)

_INNER_HOST = "127.0.0.1"
_PORT_POLL_INTERVAL = 0.2
_CHILD_START_RETRIES = 3
_CHILD_STOP_TIMEOUT = 5


def _load_config_from_env() -> dict:
    raw = os.environ.get(_SUPERVISOR_CONFIG_ENV, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def _allocate_ephemeral_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


async def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if _is_port_in_use(host, port):
            return True
        await asyncio.sleep(_PORT_POLL_INTERVAL)
    return False


class ProxySupervisor:
    def __init__(self, project_dir: str, config: dict) -> None:
        self.project_dir = project_dir
        self.config = config
        self.proxy_cfg = get_proxy_config(config, project_dir)
        self.outer_host = str(self.proxy_cfg["host"])
        self.outer_port = int(self.proxy_cfg["port"])
        self.inner_port: int | None = None
        self.child: asyncio.subprocess.Process | None = None
        self.server: asyncio.AbstractServer | None = None
        self.shutdown_event = asyncio.Event()
        self._client_tasks: set[asyncio.Task] = set()
        self._active_clients = 0
        self._idle_task: asyncio.Task | None = None
        self._idle_timeout = float(self.proxy_cfg.get("idle_timeout", 0) or 0)
        self._exit_state = "stopped"

    async def run(self) -> int:
        try:
            await self._start_child_with_retry()
            self.server = await asyncio.start_server(
                self._handle_client,
                host=self.outer_host,
                port=self.outer_port,
            )
            update_proxy_state(
                self.project_dir,
                self.config,
                proxy_state="ready",
                pid=os.getpid(),
                child_pid=self._child_pid,
                inner_port=self.inner_port,
                active_clients=self._active_clients,
                last_disconnect_at="",
                last_error="",
            )
            await self._apply_runtime_state()
            if self._active_clients == 0:
                self._schedule_idle_shutdown()
            await self.shutdown_event.wait()
            return 0
        except Exception as exc:
            self._exit_state = "failed"
            update_proxy_state(
                self.project_dir,
                self.config,
                proxy_state="failed",
                pid=os.getpid(),
                child_pid=self._child_pid,
                inner_port=self.inner_port,
                active_clients=self._active_clients,
                last_error=f"supervisor failed: {exc}",
            )
            return 1
        finally:
            await self._shutdown()
            update_proxy_state(
                self.project_dir,
                self.config,
                proxy_state=self._exit_state,
                pid=None if self._exit_state == "stopped" else os.getpid(),
                child_pid=None,
                inner_port=None,
                active_clients=0,
                last_disconnect_at="",
                last_error="" if self._exit_state == "stopped" else "supervisor exited with failure",
            )

    async def _start_child_with_retry(self) -> None:
        for _ in range(_CHILD_START_RETRIES):
            inner_port = _allocate_ephemeral_port(_INNER_HOST)
            child_cfg = {**self.proxy_cfg, "host": _INNER_HOST, "port": inner_port}
            cmd = _build_proxy_command(self.config, child_cfg)
            child = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self.child = child
            self.inner_port = inner_port
            update_proxy_state(
                self.project_dir,
                self.config,
                proxy_state="starting",
                pid=os.getpid(),
                child_pid=child.pid,
                inner_port=inner_port,
                active_clients=self._active_clients,
                last_disconnect_at="",
                last_error="",
            )
            if await _wait_for_port(_INNER_HOST, inner_port, float(self.proxy_cfg["startup_timeout"])):
                return
            await self._stop_child()

        raise RuntimeError("unable to start inner mcp-proxy")

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        await self._on_client_connected()

        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            if self.inner_port is None:
                return
            upstream_reader, upstream_writer = await asyncio.open_connection(_INNER_HOST, self.inner_port)
            await asyncio.gather(
                self._relay(client_reader, upstream_writer),
                self._relay(upstream_reader, client_writer),
            )
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with contextlib.suppress(Exception):
                    await upstream_writer.wait_closed()
            client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()
            if task is not None:
                self._client_tasks.discard(task)
            await self._on_client_disconnected()

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.write_eof()
            with contextlib.suppress(Exception):
                await writer.drain()

    async def _shutdown(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()

        tasks = list(self._client_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)

        await self._stop_child()

    async def _stop_child(self) -> None:
        if self.child is None:
            return
        if self.child.returncode is not None:
            return

        self.child.terminate()
        try:
            await asyncio.wait_for(self.child.wait(), timeout=_CHILD_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            self.child.kill()
            with contextlib.suppress(Exception):
                await self.child.wait()

    @property
    def _child_pid(self) -> int | None:
        if self.child is None:
            return None
        return self.child.pid

    async def _on_client_connected(self) -> None:
        self._active_clients += 1
        self._cancel_idle_shutdown()
        await self._apply_runtime_state()

    async def _on_client_disconnected(self) -> None:
        self._active_clients = max(0, self._active_clients - 1)
        await self._apply_runtime_state()
        if self._active_clients == 0:
            self._schedule_idle_shutdown()

    async def _apply_runtime_state(self) -> None:
        if self._active_clients > 0:
            proxy_state = "ready"
            last_disconnect_at = ""
        elif self._idle_timeout > 0:
            proxy_state = "idle"
            last_disconnect_at = _utcnow()
        else:
            proxy_state = "ready"
            last_disconnect_at = ""

        update_proxy_state(
            self.project_dir,
            self.config,
            proxy_state=proxy_state,
            pid=os.getpid(),
            child_pid=self._child_pid,
            inner_port=self.inner_port,
            active_clients=self._active_clients,
            last_disconnect_at=last_disconnect_at,
            last_error="",
        )

    def _schedule_idle_shutdown(self) -> None:
        if self._idle_timeout <= 0:
            return
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.create_task(self._idle_shutdown_after_timeout())

    def _cancel_idle_shutdown(self) -> None:
        if self._idle_task is None:
            return
        if not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_shutdown_after_timeout(self) -> None:
        try:
            await asyncio.sleep(self._idle_timeout)
        except asyncio.CancelledError:
            return
        if self._active_clients > 0:
            return
        update_proxy_state(
            self.project_dir,
            self.config,
            proxy_state="stopping",
            pid=os.getpid(),
            child_pid=self._child_pid,
            inner_port=self.inner_port,
            active_clients=0,
            last_disconnect_at=_utcnow(),
            last_error="",
        )
        self._exit_state = "stopped"
        self.shutdown_event.set()


def _utcnow() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")


async def _run(project_dir: str, config: dict) -> int:
    supervisor = ProxySupervisor(project_dir, config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, supervisor.shutdown_event.set)
    return await supervisor.run()


def main() -> int:
    project_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    if not project_dir:
        return 1

    config = _load_config_from_env()
    if not config:
        return 1

    return asyncio.run(_run(project_dir, config))


if __name__ == "__main__":
    raise SystemExit(main())
