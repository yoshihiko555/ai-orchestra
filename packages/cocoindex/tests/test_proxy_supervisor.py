"""proxy_supervisor.py のユニットテスト。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "cocoindex" / "hooks"))

proxy_supervisor = load_module(
    "proxy_supervisor",
    "packages/cocoindex/hooks/proxy_supervisor.py",
)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestProxySupervisorForwarding:
    def test_forwards_basic_http_response(self, tmp_path: Path) -> None:
        async def _scenario() -> None:
            async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            upstream_server = await asyncio.start_server(_upstream, "127.0.0.1", 0)
            inner_port = upstream_server.sockets[0].getsockname()[1]
            outer_port = _free_port()
            config = {
                "proxy": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": outer_port,
                    "port_range": 0,
                }
            }
            supervisor = proxy_supervisor.ProxySupervisor(str(tmp_path), config)
            supervisor.inner_port = int(inner_port)
            supervisor.server = await asyncio.start_server(
                supervisor._handle_client,
                host=supervisor.outer_host,
                port=supervisor.outer_port,
            )

            reader, writer = await asyncio.open_connection(
                supervisor.outer_host, supervisor.outer_port
            )
            writer.write(b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()

            supervisor.server.close()
            await supervisor.server.wait_closed()
            upstream_server.close()
            await upstream_server.wait_closed()

            assert b"HTTP/1.1 200 OK" in response
            assert response.endswith(b"hello")

        try:
            asyncio.run(_scenario())
        except PermissionError as exc:
            pytest.skip(f"loopback bind not permitted in this environment: {exc}")

    def test_forwards_streaming_response(self, tmp_path: Path) -> None:
        async def _scenario() -> None:
            async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )
                await writer.drain()
                writer.write(b"data: one\n\n")
                await writer.drain()
                await asyncio.sleep(0.05)
                writer.write(b"data: two\n\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            upstream_server = await asyncio.start_server(_upstream, "127.0.0.1", 0)
            inner_port = upstream_server.sockets[0].getsockname()[1]
            outer_port = _free_port()
            config = {
                "proxy": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": outer_port,
                    "port_range": 0,
                }
            }
            supervisor = proxy_supervisor.ProxySupervisor(str(tmp_path), config)
            supervisor.inner_port = int(inner_port)
            supervisor.server = await asyncio.start_server(
                supervisor._handle_client,
                host=supervisor.outer_host,
                port=supervisor.outer_port,
            )

            reader, writer = await asyncio.open_connection(
                supervisor.outer_host, supervisor.outer_port
            )
            writer.write(b"GET /sse HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()

            supervisor.server.close()
            await supervisor.server.wait_closed()
            upstream_server.close()
            await upstream_server.wait_closed()

            assert b"Content-Type: text/event-stream" in response
            assert b"data: one\n\n" in response
            assert b"data: two\n\n" in response

        try:
            asyncio.run(_scenario())
        except PermissionError as exc:
            pytest.skip(f"loopback bind not permitted in this environment: {exc}")


class TestProxySupervisorIdleTimeout:
    def test_idle_timeout_sets_shutdown_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[dict] = []

        def _capture(_project_dir: str, _config: dict, **updates):
            events.append(updates)
            return updates

        monkeypatch.setattr(proxy_supervisor, "update_proxy_state", _capture)

        async def _scenario() -> None:
            supervisor = proxy_supervisor.ProxySupervisor(
                str(tmp_path),
                {
                    "proxy": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 8792,
                        "port_range": 0,
                        "idle_timeout": 0.05,
                    }
                },
            )
            await supervisor._apply_runtime_state()
            supervisor._schedule_idle_shutdown()
            await asyncio.wait_for(supervisor.shutdown_event.wait(), timeout=0.2)

        asyncio.run(_scenario())

        assert any(event.get("proxy_state") == "idle" for event in events)
        assert any(event.get("proxy_state") == "stopping" for event in events)

    def test_active_client_cancels_idle_timer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[dict] = []

        def _capture(_project_dir: str, _config: dict, **updates):
            events.append(updates)
            return updates

        monkeypatch.setattr(proxy_supervisor, "update_proxy_state", _capture)

        async def _scenario() -> None:
            supervisor = proxy_supervisor.ProxySupervisor(
                str(tmp_path),
                {
                    "proxy": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 8792,
                        "port_range": 0,
                        "idle_timeout": 0.1,
                    }
                },
            )
            await supervisor._apply_runtime_state()
            supervisor._schedule_idle_shutdown()
            await supervisor._on_client_connected()
            await asyncio.sleep(0.15)
            assert not supervisor.shutdown_event.is_set()
            await supervisor._on_client_disconnected()
            await asyncio.wait_for(supervisor.shutdown_event.wait(), timeout=0.3)

        asyncio.run(_scenario())

        assert any(
            event.get("proxy_state") == "ready" and event.get("active_clients") == 1
            for event in events
        )
        assert any(event.get("proxy_state") == "stopping" for event in events)
