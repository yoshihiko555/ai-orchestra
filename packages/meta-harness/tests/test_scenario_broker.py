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
ev = load_module(
    "meta_harness_evaluator_broker_contract_tests",
    "packages/meta-harness/lib/evaluator.py",
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
    assert state.metrics.budget_rejected_count == 0


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
    assert state.metrics.budget_rejected_count == 0
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
    assert state.metrics.budget_rejected_count == 0
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


def test_http_handler_forwards_allowed_query_string(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker
    requested_targets: list[str] = []

    class UpstreamResponse:
        status = 200

        def __init__(self) -> None:
            self.payload = b'{"usage":{"input_tokens":1,"output_tokens":1}}\n'

        def getheaders(self) -> list[tuple[str, str]]:
            return [("content-type", "application/json")]

        def read1(self, _size: int) -> bytes:
            payload, self.payload = self.payload, b""
            return payload

    class RecordingConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.response = UpstreamResponse()

        def request(
            self,
            method: str,
            target: str,
            *,
            body: bytes,
            headers: dict[str, str],
        ) -> None:
            assert method == "POST"
            assert body
            assert headers["authorization"] == "Bearer real-oauth-token"
            requested_targets.append(target)

        def getresponse(self) -> UpstreamResponse:
            return self.response

        def close(self) -> None:
            return

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", RecordingConnection)

    status, _headers, _payload = _post(
        server,
        path="/v1/messages?beta=true",
        body=b'{"model":"claude-test","max_tokens":1,"messages":[]}',
    )

    assert status == 200
    assert requested_targets == ["/v1/messages?beta=true"]
    assert state.metrics.request_count == 1
    assert state.metrics.rejected_count == 0
    assert state.metrics.budget_exceeded is False


@pytest.mark.parametrize(
    "query",
    [
        "beta=false",
        "foo=1",
        "beta=true&x=1",
        "beta=true&beta=true",
    ],
)
def test_http_handler_rejects_unknown_query_string_without_latching_budget(
    http_broker: tuple[Any, Any], query: str
) -> None:
    server, state = http_broker

    status, _headers, _payload = _post(server, path=f"/v1/messages?{query}")

    assert status == 400
    assert state.metrics.request_count == 0
    assert state.metrics.rejected_count == 1
    assert state.metrics.budget_rejected_count == 0
    assert state.metrics.budget_exceeded is False
    assert state.metrics.anomaly is True


def test_allowed_query_uses_path_for_request_budget_validation(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("invalid request must not reach the upstream connection")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)

    status, _headers, payload = _post(
        server,
        path="/v1/messages?beta=true",
        body=b"{}",
    )

    assert status == 429
    assert b"positive max_tokens" in payload
    assert state.metrics.request_count == 1
    assert state.metrics.rejected_count == 1
    assert state.metrics.budget_rejected_count == 0
    assert state.metrics.budget_exceeded is True


def test_http_handler_records_budget_upper_bound_rejection(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("budget-rejected request must not reach upstream")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)

    status, _headers, payload = _post(
        server,
        body=b'{"model":"claude-test","max_tokens":100,"messages":[]}',
    )

    assert status == 429
    assert b"upper bound exceeds the remaining run budget" in payload
    assert state.metrics.rejected_count == 1
    assert state.metrics.budget_rejected_count == 1
    assert state.metrics.budget_exceeded is True
    assert state.metrics.anomaly_reasons == [
        "request token upper bound exceeds the remaining run budget"
    ]


def test_budget_latch_reasons_match_broker_rejections() -> None:
    assert {
        broker.BUDGET_TOKEN_REJECT_REASON,
        broker.BUDGET_COST_REJECT_REASON,
    } == ev._BUDGET_LATCH_ANOMALY_REASONS


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


def test_injected_namespace_derives_server_and_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = broker._broker_identity("loop-harness")
    state = _state(tmp_path, monkeypatch, user_agent=identity.user_agent)
    server = broker.BrokerServer(
        ("127.0.0.1", 0),
        state,
        server_version=identity.server_version,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        response.read()
        server_header = response.getheader("server")
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    result = broker._upstream_headers(
        Message(),
        "real-oauth-token",
        user_agent=state.user_agent,
    )

    assert server_header is not None
    assert server_header.startswith("loop-harness-broker ")
    assert result["user-agent"] == "ai-orchestra-loop-harness-broker/0.1"


def test_header_allowlist_forwards_only_pinned_cli_beta_features() -> None:
    incoming = Message()
    client_betas = ",".join(
        [
            "claude-code-20250219",
            "context-1m-2025-08-07",
            "interleaved-thinking-2025-05-14",
            "thinking-token-count-2026-05-13",
            "context-management-2025-06-27",
            "prompt-caching-scope-2026-01-05",
            "mid-conversation-system-2026-04-07",
            "advisor-tool-2026-03-01",
            "effort-2025-11-24",
            "structured-outputs-2025-12-15",
        ]
    )
    incoming["anthropic-beta"] = client_betas

    result = broker._upstream_headers(incoming, "real-oauth-token")

    assert result["anthropic-beta"] == f"oauth-2025-04-20,{client_betas}"


@pytest.mark.parametrize(
    "value,match",
    [
        ("unknown-feature-2026-07-14", "unsupported"),
        ("context-management-2025-06-27,context-management-2025-06-27", "duplicate"),
        ("context_management-2025-06-27", "invalid"),
    ],
)
def test_header_allowlist_rejects_unapproved_cli_beta_features(value: str, match: str) -> None:
    incoming = Message()
    incoming["anthropic-beta"] = value

    with pytest.raises(ValueError, match=match):
        broker._upstream_headers(incoming, "oauth")


def test_http_handler_records_safe_header_validation_category(
    http_broker: tuple[Any, Any],
) -> None:
    server, state = http_broker

    status, _headers, _payload = _post(
        server,
        headers={"anthropic-beta": "unknown-feature-2026-07-14"},
    )

    assert status == 431
    assert state.metrics.anomaly_reasons == [
        "invalid upstream header: unsupported anthropic-beta feature: unknown-feature-2026-07-14"
    ]


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


def test_model_allowlist_unset_skips_validation(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker
    assert state.model_allowlist is None
    monkeypatch.setattr(broker.BrokerHandler, "_proxy", _complete_proxy)

    status, _headers, _payload = _post(
        server,
        body=b'{"model":"claude-arbitrary-expensive","max_tokens":1,"messages":[]}',
    )

    assert status == 200
    assert state.metrics.rejected_count == 0


def test_model_allowlist_accepts_matching_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(
        tmp_path,
        monkeypatch,
        max_requests=4,
        max_total_tokens=500_000,
        model_allowlist=frozenset({"claude-cheap-model"}),
    )
    body = json.dumps({"model": "claude-cheap-model", "max_tokens": 1, "messages": []}).encode()

    assert state.begin_request()[0] is True
    assert state.request_budget_error("/v1/messages", body) is None


def test_model_allowlist_rejects_mismatched_model_with_non_retryable_400(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker
    state.model_allowlist = frozenset({"claude-cheap-model"})

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("model allowlist rejection must not reach upstream")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)
    body = json.dumps({"model": "claude-expensive-model", "max_tokens": 1, "messages": []}).encode()

    status, _headers, payload = _post(server, body=body)

    assert status == 400
    assert b"model allowlist" in payload
    assert state.metrics.request_count == 1
    assert state.metrics.rejected_count == 1
    assert state.metrics.budget_rejected_count == 0
    assert state.metrics.upstream_request_bytes == 0
    assert state.metrics.anomaly is True
    assert state.metrics.budget_exceeded is False


def test_model_allowlist_rejects_missing_model_field(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker
    state.model_allowlist = frozenset({"claude-cheap-model"})

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("model allowlist rejection must not reach upstream")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)
    body = json.dumps({"max_tokens": 1, "messages": []}).encode()

    status, _headers, payload = _post(server, body=body)

    assert status == 400
    assert b"model allowlist" in payload
    assert state.metrics.rejected_count == 1
    assert state.metrics.upstream_request_bytes == 0
    assert state.metrics.budget_exceeded is False


def test_model_allowlist_rejection_does_not_latch_run_budget_for_next_request(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker
    state.model_allowlist = frozenset({"claude-cheap-model"})

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("model allowlist rejection must not reach upstream")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)
    bad_body = json.dumps(
        {"model": "claude-expensive-model", "max_tokens": 1, "messages": []}
    ).encode()

    first_status, _headers, _payload = _post(server, body=bad_body)

    assert first_status == 400
    assert state.metrics.rejected_count == 1
    assert state.metrics.budget_exceeded is False

    monkeypatch.setattr(broker.BrokerHandler, "_proxy", _complete_proxy)
    good_body = json.dumps(
        {"model": "claude-cheap-model", "max_tokens": 1, "messages": []}
    ).encode()

    second_status, _headers, _payload = _post(server, body=good_body)

    assert second_status == 200
    assert second_status != 429
    assert state.metrics.request_count == 2


def test_model_allowlist_applies_to_count_tokens_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #261 PR2 review round 3: /v1/messages/count_tokens also spends
    input-token accounting and must be gated by model_allowlist the same way
    /v1/messages is -- it was previously left unchecked, which let a candidate
    confirm an out-of-allowlist model is reachable via this endpoint."""
    state = _state(
        tmp_path,
        monkeypatch,
        max_requests=4,
        max_total_tokens=500_000,
        model_allowlist=frozenset({"claude-cheap-model"}),
    )
    allowed_body = json.dumps({"model": "claude-cheap-model", "messages": []}).encode()
    disallowed_body = json.dumps({"model": "claude-expensive-model", "messages": []}).encode()

    assert state.request_budget_error("/v1/messages/count_tokens", allowed_body) is None
    assert state.request_budget_error("/v1/messages/count_tokens", disallowed_body) == (
        400,
        "request model is not in the broker model allowlist",
    )


def test_model_allowlist_rejects_disallowed_model_via_count_tokens_http(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP-level equivalent of `test_model_allowlist_applies_to_count_tokens_path`:
    a POST to /v1/messages/count_tokens with a disallowed model must be rejected
    with 400 before ever reaching upstream, and must not latch the run budget
    (matching the existing /v1/messages contract, PR #263)."""
    server, state = http_broker
    state.model_allowlist = frozenset({"claude-cheap-model"})

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("model allowlist rejection must not reach upstream")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)
    body = json.dumps({"model": "claude-expensive-model", "messages": []}).encode()

    status, _headers, payload = _post(server, path="/v1/messages/count_tokens", body=body)

    assert status == 400
    assert b"model allowlist" in payload
    assert state.metrics.rejected_count == 1
    assert state.metrics.upstream_request_bytes == 0
    assert state.metrics.budget_exceeded is False


def test_request_budget_error_allows_body_without_price_modifier_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #261 PR2 review round 6 (High): a normal request body must pass through
    unaffected by the new pricing-modifier rejection."""
    state = _state(tmp_path, monkeypatch, max_requests=4, max_total_tokens=500_000)
    body = json.dumps({"model": "claude-sonnet-5", "max_tokens": 1, "messages": []}).encode()

    assert state.request_budget_error("/v1/messages", body) is None


def test_request_budget_uses_ceiling_byte_to_token_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps(
        {"model": "claude-sonnet-5", "max_tokens": 1, "messages": [{"content": "x" * 60}]}
    ).encode()
    input_bytes_per_token = 3
    converted_input_tokens = -(-len(body) // input_bytes_per_token)
    state = _state(
        tmp_path,
        monkeypatch,
        max_total_tokens=converted_input_tokens + 1,
        input_bytes_per_token=input_bytes_per_token,
    )

    result = state.request_budget_error("/v1/messages", body)

    assert len(body) + 1 > state.max_total_tokens
    assert result is None


def test_request_budget_rejects_when_converted_tokens_exceed_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps(
        {"model": "claude-sonnet-5", "max_tokens": 1, "messages": [{"content": "x" * 60}]}
    ).encode()
    converted_input_tokens = -(-len(body) // broker.DEFAULT_INPUT_BYTES_PER_TOKEN)
    state = _state(tmp_path, monkeypatch, max_total_tokens=converted_input_tokens)

    result = state.request_budget_error("/v1/messages", body)

    assert result == (429, broker.BUDGET_TOKEN_REJECT_REASON)
    assert state.metrics.budget_rejected_count == 1


def test_request_cost_uses_converted_input_token_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps(
        {"model": "claude-sonnet-5", "max_tokens": 1, "messages": [{"content": "x" * 60}]}
    ).encode()
    input_bytes_per_token = 3
    converted_input_tokens = -(-len(body) // input_bytes_per_token)
    state = _state(
        tmp_path,
        monkeypatch,
        budget_usd=converted_input_tokens + 0.5,
        pricing=broker.Pricing(1_000_000, 0, 1_000_000, 1_000_000),
        max_total_tokens=1_000_000,
        input_bytes_per_token=input_bytes_per_token,
    )

    result = state.request_budget_error("/v1/messages", body)

    assert len(body) > state.budget_usd
    assert result is None


def test_request_cost_rejects_when_converted_estimate_exceeds_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps(
        {"model": "claude-sonnet-5", "max_tokens": 1, "messages": [{"content": "x" * 60}]}
    ).encode()
    converted_input_tokens = -(-len(body) // broker.DEFAULT_INPUT_BYTES_PER_TOKEN)
    state = _state(
        tmp_path,
        monkeypatch,
        budget_usd=converted_input_tokens - 0.5,
        pricing=broker.Pricing(1_000_000, 0, 1_000_000, 1_000_000),
        max_total_tokens=1_000_000,
    )

    result = state.request_budget_error("/v1/messages", body)

    assert result == (429, broker.BUDGET_COST_REJECT_REASON)
    assert state.metrics.budget_rejected_count == 1


def test_input_bytes_per_token_defaults_to_backward_compatible_byte_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)

    assert state.input_bytes_per_token == broker.DEFAULT_INPUT_BYTES_PER_TOKEN == 1


@pytest.mark.parametrize("invalid_value", [0, -1, True, 1.5])
def test_input_bytes_per_token_rejects_non_positive_or_non_integer_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_value: Any
) -> None:
    with pytest.raises(ValueError, match="input_bytes_per_token"):
        _state(tmp_path, monkeypatch, input_bytes_per_token=invalid_value)


@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("inference_geo", "/v1/messages"),
        ("service_tier", "/v1/messages"),
        ("speed", "/v1/messages"),
        ("inference_geo", "/v1/messages/count_tokens"),
        ("service_tier", "/v1/messages/count_tokens"),
        ("speed", "/v1/messages/count_tokens"),
    ],
)
def test_request_budget_error_rejects_price_modifier_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, path: str
) -> None:
    """Issue #261 PR2 review round 6/7 (High): a body carrying a known pricing-modifier
    field (e.g. a non-default inference region, a priority service tier, or a
    premium-priced fast `speed`) can attach a price multiplier the broker's fixed
    pricing_upper_bound_usd_per_million ceiling is not calibrated for, so it is
    rejected fail-closed on both billable paths -- existence of the field alone is
    rejected (fail-closed), not allowlisted, since the evaluation harness CLI never
    sends it."""
    state = _state(tmp_path, monkeypatch, max_requests=4, max_total_tokens=500_000)
    body = json.dumps(
        {"model": "claude-sonnet-5", "max_tokens": 1, "messages": [], field: "us"}
    ).encode()

    result = state.request_budget_error(path, body)

    assert result is not None
    status, message = result
    assert status == 400
    assert field in message


def test_broker_rejects_price_modifier_field_via_http(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP-level equivalent: a POST with a pricing-modifier field must be rejected
    with 400 before ever reaching upstream, and must not latch the run budget."""
    server, state = http_broker

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("price modifier rejection must not reach upstream")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)
    body = json.dumps(
        {"model": "claude-sonnet-5", "max_tokens": 1, "messages": [], "service_tier": "priority"}
    ).encode()

    status, _headers, payload = _post(server, body=body)

    assert status == 400
    assert b"pricing modifier" in payload
    assert state.metrics.rejected_count == 1
    assert state.metrics.upstream_request_bytes == 0
    assert state.metrics.budget_exceeded is False


def test_two_1024_token_requests_fit_three_dollar_run_budget(tmp_path: Path, monkeypatch) -> None:
    state = _state(
        tmp_path,
        monkeypatch,
        max_requests=4,
        max_total_tokens=500_000,
    )
    body = json.dumps(
        {
            "model": "claude-opus-4-8",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "x" * 20_000}],
        }
    ).encode()

    for _ in range(2):
        started, reason = state.begin_request()
        assert started is True, reason
        assert state.request_budget_error("/v1/messages", body) is None
        state.finish_request(broker.Usage(input_tokens=5_000, output_tokens=512))

    assert state.metrics.request_count == 2
    assert state.metrics.estimated_cost_usd < 3.0
    assert state.metrics.budget_exceeded is False


def test_one_parallel_request_waits_and_is_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch, max_requests=4, max_total_tokens=500_000)
    assert state.begin_request()[0] is True
    waiting = threading.Event()
    real_slot = state.upstream_slot

    class NotifyingSlot:
        def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
            if real_slot.acquire(blocking=False):
                return True
            waiting.set()
            return real_slot.acquire(blocking=blocking, timeout=timeout)

        def release(self) -> None:
            real_slot.release()

    state.upstream_slot = NotifyingSlot()
    result: list[tuple[bool, str]] = []
    thread = threading.Thread(target=lambda: result.append(state.begin_request()))
    thread.start()
    assert waiting.wait(timeout=1)
    assert result == []

    state.finish_request(broker.Usage(input_tokens=1))
    thread.join(timeout=1)

    assert result == [(True, "")]
    assert state.metrics.request_count == 2
    assert state.metrics.anomaly is False
    state.finish_request(broker.Usage(input_tokens=1))


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


def test_http_budget_rejection_latches_after_converted_token_overflow(
    http_broker: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state = http_broker

    class UnexpectedConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("over-budget request must not reach the upstream connection")

    monkeypatch.setattr(broker.http.client, "HTTPSConnection", UnexpectedConnection)
    body = json.dumps(
        {"model": "claude-test", "max_tokens": 1, "messages": [{"content": "x" * 90}]}
    ).encode()
    converted_input_tokens = -(-len(body) // state.input_bytes_per_token)
    state.max_total_tokens = converted_input_tokens

    status, _headers, payload = _post(server, body=body)

    assert status == 429
    assert broker.BUDGET_TOKEN_REJECT_REASON.encode() in payload
    assert state.metrics.budget_rejected_count == 1
    assert state.metrics.budget_exceeded is True
    started, reason = state.begin_request()
    assert started is False
    assert reason == "run budget exhausted"


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
