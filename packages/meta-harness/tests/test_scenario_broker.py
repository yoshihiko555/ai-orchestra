"""Ephemeral broker accounting and header-boundary tests (EV-47)."""

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
from collections.abc import Generator
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from tests.module_loader import load_module

broker = load_module(
    "meta_harness_scenario_broker_tests",
    "packages/meta-harness/docker/broker/broker.py",
)


def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    monkeypatch.setattr(broker, "METRICS_PATH", tmp_path / "metrics.json")
    values = {
        "run_token": "run-token",
        "oauth_token": "real-oauth-token",
        "budget_usd": 3.0,
        "pricing": broker.Pricing(15.0, 75.0, 18.75, 1.5),
        "max_requests": 2,
        "max_total_tokens": 100,
        "max_upstream_bytes": 100000,
    }
    values.update(overrides)
    return broker.BrokerState(**values)


@pytest.fixture
def http_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[Any, Any], None, None]:
    state = _state(tmp_path, monkeypatch, max_requests=10)
    server = broker.BrokerServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(
    server: Any,
    *,
    path: str = "/v1/messages",
    token: str = "run-token",
    body: Any = b"{}",
    headers: dict[str, str] | None = None,
    encode_chunked: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    request_headers = {
        "content-type": "application/json",
        "x-api-key": token,
        **(headers or {}),
    }
    connection.request(
        "POST",
        path,
        body=body,
        headers=request_headers,
        encode_chunked=encode_chunked,
    )
    response = connection.getresponse()
    payload = response.read()
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, payload


def _complete_proxy(handler: Any, _body: bytes) -> None:
    payload = b'{"ok":true}\n'
    handler.state.finish_request(broker.Usage(input_tokens=1), usage_observed=True)
    handler.send_response(200)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(payload)))
    handler.send_header("connection", "close")
    handler.end_headers()
    handler.wfile.write(payload)


def test_http_handler_happy_path_closes_connection(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker
    monkeypatch.setattr(broker.BrokerHandler, "_proxy", _complete_proxy)

    status, headers, payload = _post(server)

    assert status == 200
    assert headers["connection"] == "close"
    assert payload == b'{"ok":true}\n'
    assert state.metrics.request_count == 1


def test_health_probe_refreshes_broker_activity(http_broker: tuple[Any, Any]) -> None:
    server, state = http_broker
    state.last_activity -= 60
    previous = state.last_activity
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)

    connection.request("GET", "/healthz")
    response = connection.getresponse()
    response.read()
    connection.close()

    assert response.status == 200
    assert state.last_activity > previous


def test_http_handler_rejects_invalid_run_token(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker
    monkeypatch.setattr(broker.BrokerHandler, "_proxy", _complete_proxy)

    status, _headers, _payload = _post(server, token="other-run-token")

    assert status == 401
    assert state.metrics.request_count == 0
    assert state.metrics.budget_exceeded is False
    assert state.metrics.anomaly is True


def test_http_handler_rejects_oversized_forwarded_header(
    http_broker: tuple[Any, Any],
) -> None:
    server, state = http_broker

    status, _headers, _payload = _post(
        server,
        headers={"anthropic-version": "2" * 129},
    )

    assert status == 431
    assert state.metrics.request_count == 0
    assert state.metrics.budget_exceeded is False
    assert state.metrics.anomaly is True


def test_http_handler_does_not_append_second_response_after_stream_failure(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker

    class FailingResponse:
        status = 200

        def __init__(self) -> None:
            self.sent_chunk = False

        def getheaders(self) -> list[tuple[str, str]]:
            return [("content-type", "application/json")]

        def read1(self, _size: int) -> bytes:
            if not self.sent_chunk:
                self.sent_chunk = True
                return b"partial-upstream-body"
            raise RuntimeError("stream failed after response headers")

    class FailingConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.response = FailingResponse()

        def request(self, *_args: Any, **_kwargs: Any) -> None:
            return

        def getresponse(self) -> FailingResponse:
            return self.response

        def close(self) -> None:
            return

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", FailingConnection)
    body = b'{"max_tokens":1}'
    request = (
        b"POST /v1/messages HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"x-api-key: run-token\r\n"
        b"content-type: application/json\r\n"
        + f"content-length: {len(body)}\r\n".encode()
        + b"connection: close\r\n\r\n"
        + body
    )
    raw_chunks: list[bytes] = []
    with socket.create_connection(server.server_address, timeout=2) as client:
        client.sendall(request)
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            raw_chunks.append(chunk)
    raw_response = b"".join(raw_chunks)

    assert raw_response.count(b"HTTP/1.1") == 1
    assert b"partial-upstream-body" in raw_response
    assert b'"type": "error"' not in raw_response
    assert state.metrics.budget_exceeded is True

    with state.lock:
        state.metrics.budget_exceeded = False
        state.persist_metrics_locked()
    monkeypatch.setattr(broker.BrokerHandler, "_proxy", _complete_proxy)

    follow_up_status, _headers, _payload = _post(server)

    assert follow_up_status == 200
    assert follow_up_status != 429


def test_http_handler_releases_slot_after_unexpected_proxy_exception(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker
    attempts = 0

    def flaky_proxy(handler: Any, body: bytes) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("unexpected proxy failure")
        _complete_proxy(handler, body)

    monkeypatch.setattr(broker.BrokerHandler, "_proxy", flaky_proxy)

    first_status, _headers, _payload = _post(server)
    assert first_status == 502
    assert state.metrics.budget_exceeded is True
    with state.lock:
        state.metrics.budget_exceeded = False
        state.persist_metrics_locked()

    second_status, _headers, _payload = _post(server)

    assert second_status == 200
    assert second_status != 429


def test_http_handler_rejects_transfer_encoding_without_admission(
    http_broker: tuple[Any, Any],
) -> None:
    server, state = http_broker

    status, headers, _payload = _post(
        server,
        body=[b"{}"],
        headers={"transfer-encoding": "chunked"},
        encode_chunked=True,
    )

    assert status == 400
    assert headers["connection"] == "close"
    assert state.metrics.request_count == 0


def test_http_handler_rejects_query_string(http_broker: tuple[Any, Any]) -> None:
    server, state = http_broker

    status, _headers, _payload = _post(server, path="/v1/messages?redirect=1")

    assert status == 400
    assert state.metrics.request_count == 0


def test_header_allowlist_strips_candidate_auth_and_forwarding_headers() -> None:
    incoming = Message()
    incoming["content-type"] = "application/json"
    incoming["x-api-key"] = "candidate-dummy"
    incoming["authorization"] = "Bearer candidate-dummy"
    incoming["x-forwarded-host"] = "attacker.example"
    incoming["x-stainless-lang"] = "js"

    result = broker._upstream_headers(incoming, "real-oauth-token")

    assert result["authorization"] == "Bearer real-oauth-token"
    assert result["anthropic-beta"] == "oauth-2025-04-20"
    assert result["host"] == "api.anthropic.com"
    assert "x-api-key" not in result
    assert "x-forwarded-host" not in result
    assert "x-stainless-lang" not in result
    assert result["user-agent"] == "ai-orchestra-meta-harness-broker/0.1"


def test_header_values_and_upstream_bytes_are_bounded(tmp_path: Path, monkeypatch) -> None:
    incoming = Message()
    incoming["anthropic-version"] = "x" * 129
    with pytest.raises(ValueError, match="header value"):
        broker._upstream_headers(incoming, "oauth")

    state = _state(tmp_path, monkeypatch, max_upstream_bytes=10)
    assert state.begin_request()[0] is True
    assert state.reserve_upstream_bytes(11) is False
    assert state.metrics.budget_exceeded is True


def test_per_run_token_rejects_cross_run_key(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    valid = Message()
    valid["x-api-key"] = "run-token"
    wrong = Message()
    wrong["x-api-key"] = "another-run-token"

    assert state.authorized(valid) is True
    assert state.authorized(wrong) is False


def test_usage_budget_hard_cap_rejects_following_request(tmp_path: Path, monkeypatch) -> None:
    state = _state(
        tmp_path,
        monkeypatch,
        budget_usd=2.0,
        pricing=broker.Pricing(1_000_000.0, 1_000_000.0, 0.0, 0.0),
    )
    started, _ = state.begin_request()
    assert started is True

    state.finish_request(broker.Usage(input_tokens=1, output_tokens=1))

    started, reason = state.begin_request()
    assert started is False
    assert reason == "run budget exhausted"
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["estimated_cost_usd"] == 2.0
    assert metrics["budget_exceeded"] is True


def test_direct_request_budget_is_rejected_before_upstream(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("over-budget request must not reach the upstream connection")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)
    body = json.dumps({"model": "claude-test", "max_tokens": 500_000, "messages": []}).encode()

    status, _headers, payload = _post(server, body=body)

    assert status == 429
    assert b"budget" in payload
    assert state.metrics.budget_exceeded is True
    assert state.metrics.rejected_count == 1
    assert state.metrics.upstream_request_bytes == 0


def test_request_envelope_records_anomaly(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch, max_requests=1, max_total_tokens=100)
    assert state.begin_request()[0] is True
    state.finish_request(broker.Usage(input_tokens=1))
    assert state.begin_request()[0] is False

    metrics = state.metrics.as_dict()
    assert metrics["anomaly"] is True
    assert "request envelope exceeded" in metrics["anomaly_reasons"]


def test_token_envelope_latches_budget_and_rejects_following_request(
    tmp_path: Path, monkeypatch
) -> None:
    state = _state(tmp_path, monkeypatch, max_requests=10, max_total_tokens=1)
    assert state.begin_request()[0] is True

    state.finish_request(broker.Usage(input_tokens=2))

    assert state.metrics.budget_exceeded is True
    started, reason = state.begin_request()
    assert started is False
    assert reason == "run budget exhausted"


def test_missing_usage_fails_budget_closed(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    assert state.begin_request()[0] is True

    state.finish_request(broker.Usage(), usage_observed=False)

    assert state.metrics.budget_exceeded is True
    assert "omitted usage" in state.metrics.anomaly_reasons[0]


def test_count_tokens_root_usage_is_accounted() -> None:
    parser = broker.UsageParser()
    parser.feed(b'{"input_tokens":42}\n')

    usage = parser.finish()

    assert parser.usage_observed is True
    assert usage.input_tokens == 42


def test_usage_parser_fails_closed_instead_of_buffering_unbounded_response() -> None:
    parser = broker.UsageParser()

    parser.feed(b"x" * (broker.MAX_USAGE_PARSE_BUFFER_BYTES + 1))

    assert parser.invalid is True
    assert parser.usage_observed is False


def test_interrupted_response_fails_budget_closed(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    assert state.begin_request()[0] is True

    state.abort_request()

    assert state.metrics.budget_exceeded is True
    assert state.metrics.anomaly is True
    assert state.begin_request()[0] is False


def test_sse_usage_is_accumulated_without_buffering_full_response() -> None:
    parser = broker.UsageParser()
    parser.feed(
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":10,'
        b'"cache_read_input_tokens":3}}}\n\n'
    )
    parser.feed(b'data: {"type":"message_delta","usage":{"output_tokens":4}}\n\n')

    usage = parser.finish()

    assert usage.input_tokens == 10
    assert usage.output_tokens == 4
    assert usage.cache_read_input_tokens == 3
    assert usage.total_tokens == 17


def test_token_file_is_unlinked_immediately_after_read(tmp_path: Path, monkeypatch) -> None:
    token_path = tmp_path / "oauth-token"
    token_path.write_text("real-oauth-token\n")
    monkeypatch.setattr(broker, "TOKEN_PATH", token_path)

    token = broker._read_and_unlink_token(1)

    assert token == "real-oauth-token"
    assert not token_path.exists()


def test_token_writer_publishes_only_after_complete_write(tmp_path: Path, monkeypatch) -> None:
    token_path = tmp_path / "oauth-token"
    token_input = type("TokenInput", (), {"buffer": io.BytesIO(b"real-oauth-token\n")})()
    original_write = broker.os.write
    final_path_seen_during_write: list[bool] = []

    def checked_write(descriptor: int, payload: bytes) -> int:
        final_path_seen_during_write.append(token_path.exists())
        return original_write(descriptor, payload)

    monkeypatch.setattr(broker, "TOKEN_PATH", token_path)
    monkeypatch.setattr(broker.sys, "stdin", token_input)
    monkeypatch.setattr(broker.os, "write", checked_write)

    assert broker._write_token_from_stdin() == 0
    assert final_path_seen_during_write
    assert not any(final_path_seen_during_write)
    assert token_path.read_text() == "real-oauth-token"


def test_upstream_destination_is_compile_time_constant() -> None:
    assert broker.API_HOST == "api.anthropic.com"
    assert broker.ALLOWED_PATHS == {"/v1/messages", "/v1/messages/count_tokens"}


def test_idle_watchdog_self_terminates_orphaned_broker(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    state.last_activity -= 60
    exits: list[int] = []
    monkeypatch.setattr(broker.time, "sleep", lambda _seconds: None)

    broker._idle_watchdog(state, 30, exit_func=exits.append)

    assert exits == [0]


def test_idle_watchdog_does_not_exit_during_active_request(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    assert state.begin_request()[0] is True
    state.last_activity -= 60
    exits: list[int] = []
    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            state.started_at -= 120

    monkeypatch.setattr(broker.time, "sleep", advance)

    broker._idle_watchdog(
        state,
        30,
        max_lifetime_seconds=60,
        exit_func=exits.append,
    )

    assert sleeps == 2
    assert exits == [0]


def test_watchdog_has_absolute_lifetime_bound(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    state.started_at -= 120
    exits: list[int] = []
    monkeypatch.setattr(broker.time, "sleep", lambda _seconds: None)

    broker._idle_watchdog(state, 300, max_lifetime_seconds=60, exit_func=exits.append)

    assert exits == [0]
