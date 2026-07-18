#!/usr/bin/env python3
"""Run-scoped Anthropic OAuth broker for isolated harness containers."""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

API_HOST = "api.anthropic.com"
API_PORT = 443
OAUTH_BETA = "oauth-2025-04-20"
ALLOWED_CLIENT_BETAS = frozenset(
    {
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
    }
)
TOKEN_PATH = Path("/run/secrets/oauth-token")
METRICS_PATH = Path("/run/state/metrics.json")
MAX_REQUEST_BODY_BYTES = 10_000_000
MAX_TOKEN_BYTES = 16_384
MAX_USAGE_PARSE_BUFFER_BYTES = 1_000_000
MAX_BETA_HEADER_BYTES = 1024
UPSTREAM_SLOT_WAIT_SECONDS = 120
ALLOWED_PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
ALLOWED_QUERIES = frozenset({"beta=true"})
ALLOWED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "anthropic-version",
        "anthropic-beta",
    }
)
_ANTHROPIC_VERSION_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ANTHROPIC_BETA_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "request-id",
        "retry-after",
        "x-should-retry",
    }
)
DEFAULT_SERVER_VERSION = "meta-harness-broker"
DEFAULT_USER_AGENT = "ai-orchestra-meta-harness-broker/0.1"
# 63 matches the DNS label length limit (RFC 1035 section 2.3.4), which broker
# namespaces are derived from (container/network aliases, hostnames).
MAX_BROKER_NAMESPACE_LENGTH = 63
_BROKER_NAMESPACE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def merge_max(self, other: Usage) -> None:
        self.input_tokens = max(self.input_tokens, other.input_tokens)
        self.output_tokens = max(self.output_tokens, other.output_tokens)
        self.cache_creation_input_tokens = max(
            self.cache_creation_input_tokens, other.cache_creation_input_tokens
        )
        self.cache_read_input_tokens = max(
            self.cache_read_input_tokens, other.cache_read_input_tokens
        )

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens


@dataclass(frozen=True)
class Pricing:
    input: float
    output: float
    cache_creation: float
    cache_read: float

    def cost(self, usage: Usage) -> float:
        per_million = (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_creation_input_tokens * self.cache_creation
            + usage.cache_read_input_tokens * self.cache_read
        )
        return per_million / 1_000_000


@dataclass(frozen=True)
class BrokerIdentity:
    server_version: str
    user_agent: str


@dataclass(frozen=True)
class BrokerSettings:
    port: int
    startup_timeout_seconds: int
    run_token: str
    budget_usd: float
    pricing: Pricing
    max_requests: int
    max_total_tokens: int
    max_upstream_bytes: int
    idle_timeout_seconds: int
    max_lifetime_seconds: int
    identity: BrokerIdentity
    model_allowlist: frozenset[str] | None = None


def _broker_identity(namespace: str | None) -> BrokerIdentity:
    if namespace is None:
        return BrokerIdentity(DEFAULT_SERVER_VERSION, DEFAULT_USER_AGENT)
    if (
        not namespace
        or len(namespace) > MAX_BROKER_NAMESPACE_LENGTH
        or _BROKER_NAMESPACE_RE.fullmatch(namespace) is None
    ):
        raise ValueError("broker namespace must be a lowercase hyphen-separated identifier")
    return BrokerIdentity(
        server_version=f"{namespace}-broker",
        user_agent=f"ai-orchestra-{namespace}-broker/0.1",
    )


@dataclass
class BrokerMetrics:
    request_count: int = 0
    rejected_count: int = 0
    upstream_request_bytes: int = 0
    usage: Usage = field(default_factory=Usage)
    estimated_cost_usd: float = 0.0
    budget_exceeded: bool = False
    anomaly: bool = False
    anomaly_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["usage"]["total_tokens"] = self.usage.total_tokens
        return value


class BrokerState:
    """Thread-safe per-run budget and anomaly accounting."""

    def __init__(
        self,
        *,
        run_token: str,
        oauth_token: str,
        budget_usd: float,
        pricing: Pricing,
        max_requests: int,
        max_total_tokens: int,
        max_upstream_bytes: int,
        user_agent: str = DEFAULT_USER_AGENT,
        model_allowlist: frozenset[str] | None = None,
    ) -> None:
        self.run_token = run_token
        self.oauth_token = oauth_token
        self.budget_usd = budget_usd
        self.pricing = pricing
        self.max_requests = max_requests
        self.max_total_tokens = max_total_tokens
        self.max_upstream_bytes = max_upstream_bytes
        self.user_agent = user_agent
        self.model_allowlist = model_allowlist
        self.metrics = BrokerMetrics()
        self.started_at = time.monotonic()
        self.last_activity = time.monotonic()
        self.active_requests = 0
        self.lock = threading.Lock()
        self.upstream_slot = threading.BoundedSemaphore(1)
        self.pending_slot = threading.BoundedSemaphore(1)
        self.persist_metrics()

    def authorized(self, headers: Any) -> bool:
        api_key = headers.get("x-api-key", "")
        bearer = headers.get("authorization", "")
        supplied = api_key or (bearer[7:] if bearer.lower().startswith("bearer ") else "")
        return bool(supplied) and hmac.compare_digest(supplied, self.run_token)

    def begin_request(self) -> tuple[bool, str]:
        if not self.pending_slot.acquire(blocking=False):
            self.reject("parallel request queue exceeded")
            return False, "parallel request queue is full"
        try:
            acquired = self.upstream_slot.acquire(timeout=UPSTREAM_SLOT_WAIT_SECONDS)
        finally:
            self.pending_slot.release()
        if not acquired:
            self.reject("parallel request wait timed out")
            return False, "parallel request wait timed out"
        with self.lock:
            if self.metrics.budget_exceeded:
                self.metrics.rejected_count += 1
                self.persist_metrics_locked()
                self.upstream_slot.release()
                return False, "run budget exhausted"
            if self.metrics.request_count >= self.max_requests:
                self.metrics.rejected_count += 1
                self._mark_anomaly_locked("request envelope exceeded")
                self.persist_metrics_locked()
                self.upstream_slot.release()
                return False, "run request envelope exhausted"
            self.metrics.request_count += 1
            self.active_requests += 1
            self.last_activity = time.monotonic()
            self.persist_metrics_locked()
        return True, ""

    def request_budget_error(self, path: str, body: bytes) -> tuple[int, str] | None:
        try:
            payload = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return 429, "request body must be a JSON object"
        if not isinstance(payload, dict):
            return 429, "request body must be a JSON object"
        output_tokens = 0
        if path == "/v1/messages":
            if self.model_allowlist is not None:
                model = payload.get("model")
                if not isinstance(model, str) or model not in self.model_allowlist:
                    return 400, "request model is not in the broker model allowlist"
            max_tokens = payload.get("max_tokens")
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
                return 429, "messages request must declare a positive max_tokens"
            output_tokens = max_tokens
        input_tokens_upper_bound = len(body)
        with self.lock:
            remaining_tokens = self.max_total_tokens - self.metrics.usage.total_tokens
            requested_tokens = input_tokens_upper_bound + output_tokens
            if requested_tokens > remaining_tokens:
                return 429, "request token upper bound exceeds the remaining run budget"
            input_price = max(
                self.pricing.input,
                self.pricing.cache_creation,
                self.pricing.cache_read,
            )
            request_cost_upper_bound = (
                input_tokens_upper_bound * input_price + output_tokens * self.pricing.output
            ) / 1_000_000
            remaining_cost = self.budget_usd - self.metrics.estimated_cost_usd
            if request_cost_upper_bound > remaining_cost:
                return 429, "request cost upper bound exceeds the remaining run budget"
        return None

    def finish_request(self, usage: Usage, *, usage_observed: bool = True) -> None:
        with self.lock:
            self.metrics.usage.add(usage)
            self.metrics.estimated_cost_usd = self.pricing.cost(self.metrics.usage)
            if not usage_observed:
                self.metrics.budget_exceeded = True
                self._mark_anomaly_locked("upstream response omitted usage accounting")
            if self.metrics.usage.total_tokens > self.max_total_tokens:
                self.metrics.budget_exceeded = True
                self._mark_anomaly_locked("token envelope exceeded")
            if self.metrics.estimated_cost_usd >= self.budget_usd:
                self.metrics.budget_exceeded = True
            self.last_activity = time.monotonic()
            self.active_requests -= 1
            self.persist_metrics_locked()
        self.upstream_slot.release()

    def touch_activity(self) -> None:
        with self.lock:
            self.last_activity = time.monotonic()

    def reserve_upstream_bytes(self, amount: int) -> bool:
        with self.lock:
            if self.metrics.upstream_request_bytes + amount > self.max_upstream_bytes:
                self.metrics.rejected_count += 1
                self.metrics.budget_exceeded = True
                self._mark_anomaly_locked("upstream byte envelope exceeded")
                self.active_requests -= 1
                self.persist_metrics_locked()
                self.upstream_slot.release()
                return False
            self.metrics.upstream_request_bytes += amount
            self.persist_metrics_locked()
            return True

    def abort_request(
        self,
        reason: str = "upstream usage unknown after interrupted response",
        *,
        rejected: bool = False,
        latch_budget: bool = True,
    ) -> None:
        with self.lock:
            if rejected:
                self.metrics.rejected_count += 1
            if latch_budget:
                self.metrics.budget_exceeded = True
            self._mark_anomaly_locked(reason)
            self.last_activity = time.monotonic()
            self.active_requests -= 1
            self.persist_metrics_locked()
        self.upstream_slot.release()

    def reject(self, reason: str) -> None:
        # Pre-admission input noise has no upstream cost, so latching the budget would let
        # one malformed request brick the run. It is recorded as an anomaly; connection
        # close requires a new connection per attempt, and absolute lifetime bounds duration.
        with self.lock:
            self.metrics.rejected_count += 1
            self._mark_anomaly_locked(reason)
            self.persist_metrics_locked()

    def _mark_anomaly_locked(self, reason: str) -> None:
        self.metrics.anomaly = True
        if reason not in self.metrics.anomaly_reasons:
            self.metrics.anomaly_reasons.append(reason)

    def persist_metrics(self) -> None:
        with self.lock:
            self.persist_metrics_locked()

    def persist_metrics_locked(self) -> None:
        METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = METRICS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.metrics.as_dict(), sort_keys=True) + "\n")
        os.replace(temporary, METRICS_PATH)


class UsageParser:
    """Extract response usage from JSON or SSE without buffering the response."""

    def __init__(self) -> None:
        self.usage = Usage()
        self.buffer = b""
        self.usage_observed = False
        self.invalid = False

    def feed(self, chunk: bytes) -> None:
        if self.invalid:
            return
        if len(self.buffer) + len(chunk) > MAX_USAGE_PARSE_BUFFER_BYTES:
            self.invalid = True
            self.buffer = b""
            return
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self._parse_line(line.strip())

    def finish(self) -> Usage:
        if self.invalid:
            return self.usage
        if self.buffer.strip():
            self._parse_line(self.buffer.strip())
        return self.usage

    def _parse_line(self, line: bytes) -> None:
        if line.startswith(b"data:"):
            line = line[5:].strip()
        if not line or line == b"[DONE]":
            return
        try:
            payload = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        candidates = [payload.get("usage")]
        if "input_tokens" in payload:
            candidates.append(payload)
        message = payload.get("message")
        if isinstance(message, dict):
            candidates.append(message.get("usage"))
        for candidate in candidates:
            if isinstance(candidate, dict):
                self.usage_observed = True
                self.usage.merge_max(_usage_from_mapping(candidate))


def _usage_from_mapping(value: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=_non_negative_int(value.get("input_tokens")),
        output_tokens=_non_negative_int(value.get("output_tokens")),
        cache_creation_input_tokens=_non_negative_int(value.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_non_negative_int(value.get("cache_read_input_tokens")),
    )


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class BrokerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = DEFAULT_SERVER_VERSION

    @property
    def state(self) -> BrokerState:
        return self.server.state  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.close_connection = True
        if self.path != "/healthz":
            self._json_error(404, "not found")
            return
        self.state.touch_activity()
        body = b'{"status":"ok"}\n'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.close_connection = True
        self._headers_sent = False
        path, separator, query = self.path.partition("?")
        if separator and query not in ALLOWED_QUERIES:
            self.state.reject("query string is not allowed")
            self._json_error(400, "query string is not allowed")
            return
        if path not in ALLOWED_PATHS:
            self.state.reject("disallowed upstream path")
            self._json_error(404, "endpoint is not allowed")
            return
        if not self.state.authorized(self.headers):
            self.state.reject("invalid per-run token")
            self._json_error(401, "invalid per-run token")
            return
        if self.headers.get("transfer-encoding") is not None:
            self.state.reject("transfer encoding is not allowed")
            self._json_error(400, "transfer encoding is not allowed")
            return
        try:
            _upstream_headers(
                self.headers,
                self.state.oauth_token,
                user_agent=self.state.user_agent,
            )
        except ValueError as exc:
            # _upstream_headers emits only fixed validation categories and never
            # includes credential or header values. Keep that category in metrics so
            # operators can distinguish a pinned-CLI drift from auth/budget failures.
            self.state.reject(f"invalid upstream header: {exc}")
            self._json_error(431, str(exc))
            return
        started, reason = self.state.begin_request()
        if not started:
            self._json_error(429, reason)
            return
        try:
            body = self._read_request_body()
            self._proxy(body)
        except ValueError as exc:
            # Post-admission validation failures intentionally latch the run budget: once a
            # request owns the upstream slot, ambiguous accounting remains fail-closed.
            self.state.abort_request()
            self._json_error_if_headers_unsent(413, str(exc))
        except (OSError, http.client.HTTPException, TimeoutError):
            self.state.abort_request()
            self._json_error_if_headers_unsent(502, "upstream request failed")
        except Exception:
            self.state.abort_request()
            self._json_error_if_headers_unsent(502, "upstream request failed")

    def _read_request_body(self) -> bytes:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            raise ValueError("request body exceeds broker limit")
        return self.rfile.read(length)

    def _proxy(self, body: bytes) -> None:
        path = self.path.partition("?")[0]
        pre_admission_error = self.state.request_budget_error(path, body)
        if pre_admission_error is not None:
            status, message = pre_admission_error
            # A model allowlist rejection (400) never reached upstream and incurred no
            # cost, so unlike the budget/envelope 429s below it must not latch the run
            # budget shut for subsequent, otherwise-valid requests.
            self.state.abort_request(message, rejected=True, latch_budget=status != 400)
            self._json_error(status, message)
            return
        headers = _upstream_headers(
            self.headers,
            self.state.oauth_token,
            user_agent=self.state.user_agent,
        )
        forwarded_bytes = len(body) + sum(
            len(name) + len(value) + 4 for name, value in headers.items()
        )
        if not self.state.reserve_upstream_bytes(forwarded_bytes):
            self._json_error(429, "run upstream byte envelope exhausted")
            return
        connection = http.client.HTTPSConnection(API_HOST, API_PORT, timeout=120)
        parser = UsageParser()
        try:
            connection.request("POST", self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in ALLOWED_RESPONSE_HEADERS or lower.startswith("anthropic-ratelimit-"):
                    self.send_header(name, value)
            self.send_header("connection", "close")
            self.end_headers()
            self._headers_sent = True
            while True:
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                self.state.touch_activity()
                parser.feed(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            connection.close()
        self.state.finish_request(parser.finish(), usage_observed=parser.usage_observed)

    def _json_error_if_headers_unsent(self, status: int, message: str) -> None:
        if self._headers_sent or self.wfile.closed:
            return
        self._json_error(status, message)

    def _json_error(self, status: int, message: str) -> None:
        body = json.dumps({"type": "error", "error": {"message": message}}).encode() + b"\n"
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: Any) -> None:
        return


def _upstream_headers(
    headers: Any,
    oauth_token: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, str]:
    result: dict[str, str] = {}
    client_betas: list[str] = []
    beta_header_seen = False
    for name, value in headers.items():
        lower = name.lower()
        if lower not in ALLOWED_REQUEST_HEADERS:
            continue
        if lower == "anthropic-beta":
            if beta_header_seen:
                raise ValueError("duplicate anthropic-beta header is not allowed")
            beta_header_seen = True
            client_betas = _validated_client_betas(value)
            continue
        if len(value) > 128:
            raise ValueError("upstream header value exceeds broker limit")
        if lower == "anthropic-version" and not _ANTHROPIC_VERSION_RE.fullmatch(value):
            raise ValueError("invalid anthropic-version header")
        result[lower] = value
    result["accept"] = "application/json"
    result["content-type"] = "application/json"
    result["user-agent"] = user_agent
    result["authorization"] = f"Bearer {oauth_token}"
    result["anthropic-beta"] = ",".join([OAUTH_BETA, *client_betas])
    result["host"] = API_HOST
    return result


def _validated_client_betas(value: str) -> list[str]:
    if len(value) > MAX_BETA_HEADER_BYTES:
        raise ValueError("anthropic-beta header exceeds broker limit")
    betas = [item.strip() for item in value.split(",")]
    if not betas or any(not item for item in betas):
        raise ValueError("invalid anthropic-beta header")
    if len(set(betas)) != len(betas):
        raise ValueError("duplicate anthropic-beta feature is not allowed")
    if any(_ANTHROPIC_BETA_RE.fullmatch(item) is None for item in betas):
        raise ValueError("invalid anthropic-beta feature")
    unknown = sorted(set(betas) - ALLOWED_CLIENT_BETAS)
    if unknown:
        raise ValueError(f"unsupported anthropic-beta feature: {','.join(unknown)}")
    return betas


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        state: BrokerState,
        *,
        server_version: str = DEFAULT_SERVER_VERSION,
    ) -> None:
        handler_class = type(
            "ConfiguredBrokerHandler",
            (BrokerHandler,),
            {"server_version": server_version},
        )
        super().__init__(address, handler_class)
        self.state = state


def _read_and_unlink_token(timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            token = TOKEN_PATH.read_text().strip()
            TOKEN_PATH.unlink(missing_ok=True)
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        if not token:
            raise RuntimeError("OAuth token file was empty")
        return token
    raise RuntimeError("timed out waiting for OAuth token injection")


def _write_token_from_stdin() -> int:
    token = sys.stdin.buffer.read(MAX_TOKEN_BYTES + 1)
    if not token or len(token) > MAX_TOKEN_BYTES:
        return 2
    token = token.rstrip(b"\r\n")
    if not token:
        return 2
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{TOKEN_PATH.name}.", dir=TOKEN_PATH.parent
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            remaining = memoryview(token)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("failed to write OAuth token")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(temporary_path, TOKEN_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)
    return 0


def _print_metrics() -> int:
    try:
        sys.stdout.write(METRICS_PATH.read_text())
    except OSError:
        return 1
    return 0


def _health(port: int) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def _env_value(name: str, legacy_name: str) -> str:
    if name in os.environ:
        return os.environ[name]
    if legacy_name in os.environ:
        return os.environ[legacy_name]
    raise KeyError(f"neither '{name}' nor '{legacy_name}' is set in the environment")


def _float_env(name: str, legacy_name: str) -> float:
    return float(_env_value(name, legacy_name))


def _int_env(name: str, legacy_name: str) -> int:
    return int(_env_value(name, legacy_name))


def _model_allowlist_env(name: str, legacy_name: str) -> frozenset[str] | None:
    if name in os.environ:
        raw = os.environ[name]
    elif legacy_name in os.environ:
        raw = os.environ[legacy_name]
    else:
        return None
    values = frozenset(item.strip() for item in raw.split(",") if item.strip())
    return values or None


def _broker_settings_from_env() -> BrokerSettings:
    run_token = _env_value("DR_BROKER_RUN_TOKEN", "MH_BROKER_RUN_TOKEN")
    if not run_token:
        raise RuntimeError("broker run token must not be empty")
    return BrokerSettings(
        port=_int_env("DR_BROKER_PORT", "MH_BROKER_PORT"),
        startup_timeout_seconds=_int_env(
            "DR_BROKER_STARTUP_TIMEOUT_SEC", "MH_BROKER_STARTUP_TIMEOUT_SEC"
        ),
        run_token=run_token,
        budget_usd=_float_env("DR_BROKER_BUDGET_USD", "MH_BROKER_BUDGET_USD"),
        pricing=Pricing(
            input=_float_env("DR_PRICE_INPUT", "MH_PRICE_INPUT"),
            output=_float_env("DR_PRICE_OUTPUT", "MH_PRICE_OUTPUT"),
            cache_creation=_float_env("DR_PRICE_CACHE_CREATION", "MH_PRICE_CACHE_CREATION"),
            cache_read=_float_env("DR_PRICE_CACHE_READ", "MH_PRICE_CACHE_READ"),
        ),
        max_requests=_int_env("DR_BROKER_MAX_REQUESTS", "MH_BROKER_MAX_REQUESTS"),
        max_total_tokens=_int_env("DR_BROKER_MAX_TOTAL_TOKENS", "MH_BROKER_MAX_TOTAL_TOKENS"),
        max_upstream_bytes=_int_env("DR_BROKER_MAX_UPSTREAM_BYTES", "MH_BROKER_MAX_UPSTREAM_BYTES"),
        idle_timeout_seconds=_int_env("DR_BROKER_IDLE_TIMEOUT_SEC", "MH_BROKER_IDLE_TIMEOUT_SEC"),
        max_lifetime_seconds=_int_env("DR_BROKER_MAX_LIFETIME_SEC", "MH_BROKER_MAX_LIFETIME_SEC"),
        identity=_broker_identity(os.environ.get("DR_BROKER_NAMESPACE")),
        model_allowlist=_model_allowlist_env(
            "DR_BROKER_MODEL_ALLOWLIST", "MH_BROKER_MODEL_ALLOWLIST"
        ),
    )


def _idle_watchdog(
    state: BrokerState,
    idle_timeout_seconds: int,
    *,
    max_lifetime_seconds: int | None = None,
    exit_func: Any = os._exit,
) -> None:
    while True:
        time.sleep(min(5, max(1, idle_timeout_seconds // 4)))
        with state.lock:
            idle = time.monotonic() - state.last_activity
            lifetime = time.monotonic() - state.started_at
            active_request = state.active_requests > 0
        if (not active_request and idle >= idle_timeout_seconds) or (
            max_lifetime_seconds is not None and lifetime >= max_lifetime_seconds
        ):
            exit_func(0)
            return


def _serve() -> int:
    settings = _broker_settings_from_env()
    oauth_token = _read_and_unlink_token(settings.startup_timeout_seconds)
    state = BrokerState(
        run_token=settings.run_token,
        oauth_token=oauth_token,
        budget_usd=settings.budget_usd,
        pricing=settings.pricing,
        max_requests=settings.max_requests,
        max_total_tokens=settings.max_total_tokens,
        max_upstream_bytes=settings.max_upstream_bytes,
        user_agent=settings.identity.user_agent,
        model_allowlist=settings.model_allowlist,
    )
    watchdog = threading.Thread(
        target=_idle_watchdog,
        args=(state, settings.idle_timeout_seconds),
        kwargs={"max_lifetime_seconds": settings.max_lifetime_seconds},
        daemon=True,
    )
    watchdog.start()
    server = BrokerServer(
        ("0.0.0.0", settings.port),
        state,
        server_version=settings.identity.server_version,
    )
    server.serve_forever(poll_interval=0.5)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-token", action="store_true")
    parser.add_argument("--print-metrics", action="store_true")
    parser.add_argument("--health", action="store_true")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DR_BROKER_PORT", os.environ.get("MH_BROKER_PORT", "8787"))),
    )
    args = parser.parse_args(argv)
    if args.write_token:
        return _write_token_from_stdin()
    if args.print_metrics:
        return _print_metrics()
    if args.health:
        return _health(args.port)
    return _serve()


if __name__ == "__main__":
    raise SystemExit(main())
