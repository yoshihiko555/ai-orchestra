"""jev_router のテスト（EV-35〜EV-39）。

送信は差し替えた transport で行い、実際のネットワークには出ない。期待値は
docs/evaluation/agent-routing.md と ADR-20261029-062 から書き起こしている。
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agent-routing" / "hooks"))

jev_router = load_module("jev_router", "packages/agent-routing/hooks/jev_router.py")

# ADR-20261029-062 / jev-v3-spec.json の sha256 先頭 16 桁（手書き。関数の出力を写さない）
PINNED_REQUEST_HASH = "5ae693c78f47091d"
TEST_KEY = "ts-test-0123456789abcdef"
JEV_CONFIG = {"router": {"engine": "jev"}}


def _response(choice: str, p_none: float, *, tokens: int = 2900) -> tuple[int, bytes]:
    body = {
        "model": "jev-1.14.0",
        "answers": {
            "flat": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.9,
                "probabilities": {"none": p_none, "tester": 1 - p_none},
            }
        },
        "usage": {"input_tokens": tokens, "output_tokens": 1},
    }
    return 200, json.dumps(body).encode()


class _Recorder:
    """送信内容を記録して、決めた応答を返す transport。"""

    def __init__(self, response: tuple[int, bytes] | BaseException) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict, float]] = []

    def __call__(self, url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, json.loads(body), headers, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _never_called(*_args: object) -> tuple[int, bytes]:
    raise AssertionError("transport must not be called")


@pytest.fixture(autouse=True)
def _isolated_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """既定ではキーを持たない HOME にし、手元のキーを読まないようにする。"""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", TEST_KEY)


# ---------------------------------------------------------------------------
# 送信内容の固定（EV-36）
# ---------------------------------------------------------------------------


def test_jev_request_hash_matches_pinned_spec() -> None:
    assert jev_router.request_hash() == PINNED_REQUEST_HASH
    assert jev_router.CONFIG_ID == "r3-flat-B-v3"
    assert jev_router.DEFAULT_MODEL == "jev-1.14.0"


def test_jev_request_hash_changes_with_model() -> None:
    assert jev_router.request_hash("jev-9.9.9") != PINNED_REQUEST_HASH


def test_jev_criteria_has_31_choices_including_none() -> None:
    assert len(jev_router.CRITERIA) == 31
    assert "none" in jev_router.CRITERIA


def test_jev_payload_sends_only_the_user_message() -> None:
    payload = jev_router.build_payload("テストを追加してください")
    assert payload == {
        "model": "jev-1.14.0",
        "state": {"user_message": "テストを追加してください"},
        "questions": {
            "flat": {
                "type": "choice",
                "instructions": jev_router.INSTRUCTIONS,
                "criteria": jev_router.CRITERIA,
            }
        },
    }


# ---------------------------------------------------------------------------
# 設定（EV-35）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, "keyword"),
        ({"router": {"engine": "keyword"}}, "keyword"),
        ({"router": {"engine": "jev"}}, "jev"),
        ({"router": {"engine": "JEV"}}, "keyword"),
        ({"router": {"engine": True}}, "keyword"),
        ({"router": "jev"}, "keyword"),
        ({"router": None}, "keyword"),
    ],
)
def test_jev_router_engine(config: dict, expected: str) -> None:
    assert jev_router.router_engine(config) == expected


def test_jev_settings_keep_valid_values() -> None:
    settings = jev_router.jev_settings(
        {"router": {"jev": {"model": "jev-1.15.0", "timeout_ms": 800, "max_p_none": 0.3}}}
    )
    assert (settings.model, settings.timeout_ms, settings.max_p_none) == ("jev-1.15.0", 800, 0.3)


@pytest.mark.parametrize(
    "jev",
    [
        {"model": "bad model", "timeout_ms": 0, "max_p_none": 1.5},
        {"model": 1, "timeout_ms": -5, "max_p_none": -0.1},
        {"model": "", "timeout_ms": "500", "max_p_none": "0.5"},
        {"model": None, "timeout_ms": True, "max_p_none": True},
        {"timeout_ms": float("nan"), "max_p_none": float("inf")},
    ],
)
def test_jev_settings_invalid_values_use_defaults(jev: dict) -> None:
    settings = jev_router.jev_settings({"router": {"jev": jev}})
    assert (settings.model, settings.timeout_ms, settings.max_p_none) == ("jev-1.14.0", 500, 0.5)


# ---------------------------------------------------------------------------
# API キー（EV-35 / EV-37）
# ---------------------------------------------------------------------------


def _write_key_file(home: Path, text: str) -> None:
    path = home / ".config" / "typesafe" / "api-key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_jev_key_prefers_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_key_file(tmp_path / "home", "from-file-key\n")
    monkeypatch.setenv("TYPESAFE_API_KEY", " from-env-key ")
    assert jev_router.read_api_key() == "from-env-key"


def test_jev_key_falls_back_to_config_file(tmp_path: Path) -> None:
    _write_key_file(tmp_path / "home", "from-file-key\n")
    assert jev_router.read_api_key() == "from-file-key"


def test_jev_key_missing_returns_empty() -> None:
    assert jev_router.read_api_key() == ""


def test_jev_key_does_not_read_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert jev_router.read_api_key() == ""


def test_jev_key_with_whitespace_inside_is_rejected(tmp_path: Path) -> None:
    _write_key_file(tmp_path / "home", "two words\n")
    assert jev_router.read_api_key() == ""


# ---------------------------------------------------------------------------
# マスキング（EV-39）
# ---------------------------------------------------------------------------


def test_jev_mask_order_and_targets() -> None:
    mask_secrets = jev_router.load_mask_secrets()
    assert mask_secrets is not None
    prompt = (
        "<pasted_content>see https://example.com/path and ask dev@example.com, "
        "file /Users/someone/work/app.py, api_key=abcd1234efgh, "
        "db postgres://admin:hunter2@db.internal/app</pasted_content>"
    )
    masked = jev_router.mask_prompt(prompt, mask_secrets)
    assert "pasted_content" not in masked
    assert "<URL>" in masked and "example.com/path" not in masked
    assert "<EMAIL>" in masked and "dev@example.com" not in masked
    assert "~/work/app.py" in masked and "someone" not in masked
    assert "abcd1234efgh" not in masked
    assert "hunter2" not in masked


def test_jev_mask_runs_secret_masking_before_url_replacement() -> None:
    """接続文字列の認証情報は mask_secrets が先に伏せ、URL 置換の前に消える。"""
    seen: list[str] = []

    def spy(text: str) -> str:
        seen.append(text)
        return text

    jev_router.mask_prompt("<pasted_content>https://example.com</pasted_content>", spy)
    assert seen == ["https://example.com"]


def test_jev_mask_truncates_to_1200_chars() -> None:
    masked = jev_router.mask_prompt("  " + "あ" * 1500 + "  ", lambda text: text)
    assert masked == "あ" * 1200


# ---------------------------------------------------------------------------
# 判定（EV-36）
# ---------------------------------------------------------------------------


def test_jev_judge_suggests_choice(with_key: None) -> None:
    transport = _Recorder(_response("tester", 0.2))
    judgment = jev_router.judge("単体テストを追加してください", JEV_CONFIG, transport=transport)

    assert judgment.status == "ok"
    assert judgment.agent == "tester"
    assert judgment.choice == "tester"
    assert judgment.p_none == pytest.approx(0.2)
    assert judgment.probabilities == {"none": pytest.approx(0.2), "tester": pytest.approx(0.8)}
    assert judgment.input_tokens == 2900
    assert judgment.request_hash == PINNED_REQUEST_HASH
    assert judgment.latency_ms is not None

    url, body, headers, timeout = transport.calls[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert body["state"] == {"user_message": "単体テストを追加してください"}
    assert body["model"] == "jev-1.14.0"
    # 予算は判定の開始から数えるので、送信に渡すのは残りの時間
    assert 0 < timeout <= 0.5


def test_jev_judge_sends_masked_prompt(with_key: None) -> None:
    transport = _Recorder(_response("tester", 0.2))
    jev_router.judge("token=supersecretvalue のテストを書いて", JEV_CONFIG, transport=transport)
    sent = transport.calls[0][1]["state"]["user_message"]
    assert "supersecretvalue" not in sent


@pytest.mark.parametrize(
    ("choice", "p_none", "max_p_none", "expected"),
    [
        ("tester", 0.5, None, "tester"),
        ("tester", 0.51, None, None),
        ("none", 0.1, None, None),
        ("tester", 0.4, 0.3, None),
    ],
)
def test_jev_judge_threshold(
    with_key: None, choice: str, p_none: float, max_p_none: float | None, expected: str | None
) -> None:
    config = {"router": {"engine": "jev", "jev": {}}}
    if max_p_none is not None:
        config["router"]["jev"]["max_p_none"] = max_p_none
    judgment = jev_router.judge("x" * 10, config, transport=_Recorder(_response(choice, p_none)))
    assert judgment.status == "ok"
    assert judgment.agent == expected
    assert judgment.choice == choice


# ---------------------------------------------------------------------------
# 使えないとき（EV-37）
# ---------------------------------------------------------------------------


def test_jev_judge_without_key_does_not_send() -> None:
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=_never_called)
    assert judgment.status == "no_key"
    assert judgment.agent is None


def test_jev_judge_without_masking_does_not_send(
    with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jev_router, "load_mask_secrets", lambda: None)
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=_never_called)
    assert judgment.status == "no_masking"


def test_jev_judge_masking_error_does_not_send(
    with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_text: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(jev_router, "load_mask_secrets", lambda: broken)
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=_never_called)
    assert judgment.status == "no_masking"


def test_jev_judge_wall_clock_timeout(with_key: None) -> None:
    release = threading.Event()

    def slow(*_args: object) -> tuple[int, bytes]:
        release.wait(5)
        return _response("tester", 0.1)

    config = {"router": {"engine": "jev", "jev": {"timeout_ms": 50}}}
    try:
        judgment = jev_router.judge("テストを書いて", config, transport=slow)
    finally:
        release.set()
    assert judgment.status == "timeout"
    assert judgment.agent is None
    assert judgment.latency_ms is not None and judgment.latency_ms < 1000


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (TimeoutError("timed out"), "timeout"),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (urllib.error.URLError("connection refused"), "network_error"),
        (ConnectionResetError("reset"), "network_error"),
        (OSError("unreachable"), "network_error"),
        ((401, b""), "http_4xx"),
        ((429, b""), "http_4xx"),
        ((503, b""), "http_5xx"),
        ((302, b""), "invalid_response"),
        ((200, b"not json"), "invalid_response"),
        ((200, b"\xff\xfe"), "invalid_response"),
        ((200, b"[]"), "invalid_response"),
        ((200, json.dumps({"answers": {}}).encode()), "invalid_response"),
    ],
)
def test_jev_judge_failure_status(
    with_key: None, response: tuple[int, bytes] | BaseException, status: str
) -> None:
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=_Recorder(response))
    assert judgment.status == status
    assert judgment.agent is None


@pytest.mark.parametrize(
    "answer",
    [
        {"choice": "unknown-agent", "probabilities": {"none": 0.1}},
        {"choice": "tester", "probabilities": {"tester": 0.9}},
        {"choice": "tester", "probabilities": {"none": 1.2}},
        {"choice": "tester", "probabilities": {"none": True}},
        {"choice": "tester", "probabilities": {"none": "0.1"}},
        {"choice": "tester", "probabilities": []},
        {"choice": 3, "probabilities": {"none": 0.1}},
    ],
)
def test_jev_judge_rejects_malformed_answer(with_key: None, answer: dict) -> None:
    body = json.dumps({"answers": {"flat": answer}}).encode()
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=_Recorder((200, body)))
    assert judgment.status == "invalid_response"


def test_jev_judge_never_exposes_key(with_key: None) -> None:
    """キーを含む例外が起きても、判定結果のどこにもキーが入らない。"""
    error = urllib.error.URLError(f"failed with Bearer {TEST_KEY}")
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=_Recorder(error))
    assert judgment.status == "network_error"
    assert TEST_KEY not in repr(judgment)


def test_jev_judge_missing_usage_keeps_decision(with_key: None) -> None:
    body = json.dumps(
        {"answers": {"flat": {"choice": "tester", "probabilities": {"none": 0.1}}}}
    ).encode()
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=_Recorder((200, body)))
    assert judgment.status == "ok"
    assert judgment.input_tokens is None


# ---------------------------------------------------------------------------
# レビュー対応（EV-37 / EV-39）
# ---------------------------------------------------------------------------


def test_jev_email_mask_is_linear_on_long_word_runs() -> None:
    """@ の無い長い英数字列でもメールの置換が二乗時間にならない。"""
    started = time.monotonic()
    masked = jev_router.mask_prompt("dev@example.com " + "a" * 100_000, lambda text: text)
    assert time.monotonic() - started < 2.0
    assert masked.startswith("<EMAIL> ")


def test_jev_judge_unencodable_prompt_is_not_sent(with_key: None) -> None:
    """対になっていないサロゲートを含む入力は送らず、例外も出さない。"""
    judgment = jev_router.judge(
        "単体テストを追加してください \ud800", JEV_CONFIG, transport=_never_called
    )
    assert judgment.status == "no_masking"


def test_jev_judge_huge_integer_probability_is_invalid(with_key: None) -> None:
    body = json.dumps(
        {"answers": {"flat": {"choice": "tester", "probabilities": {"none": 10**400}}}}
    ).encode()
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=_Recorder((200, body)))
    assert judgment.status == "invalid_response"


def test_jev_settings_huge_timeout_uses_default() -> None:
    settings = jev_router.jev_settings({"router": {"jev": {"timeout_ms": 10**400}}})
    assert settings.timeout_ms == 500


def test_jev_judge_huge_float_timeout_does_not_raise(with_key: None) -> None:
    config = {"router": {"engine": "jev", "jev": {"timeout_ms": 1e300}}}
    judgment = jev_router.judge(
        "テストを書いて", config, transport=_Recorder(_response("tester", 0.1))
    )
    assert judgment.status == "ok"


def _read_key_in_thread() -> tuple[bool, str]:
    result: list[str] = []
    thread = threading.Thread(target=lambda: result.append(jev_router.read_api_key()), daemon=True)
    thread.start()
    thread.join(2)
    return not thread.is_alive(), result[0] if result else ""


def test_jev_key_fifo_does_not_block(tmp_path: Path) -> None:
    path = tmp_path / "home" / ".config" / "typesafe" / "api-key"
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    finished, key = _read_key_in_thread()
    assert finished
    assert key == ""


def test_jev_key_directory_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "home" / ".config" / "typesafe" / "api-key").mkdir(parents=True)
    assert jev_router.read_api_key() == ""


def test_jev_key_symlink_to_regular_file_is_read(tmp_path: Path) -> None:
    real = tmp_path / "real-key"
    real.write_text("from-link-key\n", encoding="utf-8")
    path = tmp_path / "home" / ".config" / "typesafe" / "api-key"
    path.parent.mkdir(parents=True)
    path.symlink_to(real)
    assert jev_router.read_api_key() == "from-link-key"


def _slow_masker(delay_sec: float):
    def mask(text: str) -> str:
        time.sleep(delay_sec)
        return text

    return mask


def test_jev_budget_includes_preparation(with_key: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """マスキング等の準備で timeout_ms を使い切ったら送らずに timeout にする。"""
    monkeypatch.setattr(jev_router, "load_mask_secrets", lambda: _slow_masker(0.1))
    config = {"router": {"engine": "jev", "jev": {"timeout_ms": 50}}}
    judgment = jev_router.judge("テストを書いて", config, transport=_never_called)
    assert judgment.status == "timeout"
    assert judgment.latency_ms is not None and judgment.latency_ms >= 100


def test_jev_send_gets_remaining_budget(with_key: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jev_router, "load_mask_secrets", lambda: _slow_masker(0.05))
    transport = _Recorder(_response("tester", 0.1))
    judgment = jev_router.judge("テストを書いて", JEV_CONFIG, transport=transport)
    timeout = transport.calls[0][3]
    assert 0 < timeout < 0.46
    assert judgment.latency_ms is not None and judgment.latency_ms >= 50


def test_jev_transport_does_not_follow_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """30x のリダイレクトを追従せず、Authorization ヘッダーを転送先へ送らない（ループバックのみ）。"""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _redirect(self) -> None:
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leak")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = _redirect
        do_GET = _redirect

        def log_message(self, *_args: object) -> None:
            pass

    # 環境のプロキシを使わず、ループバックに直接つなぐ
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "*")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1/systemone"
    try:
        status, payload = jev_router._urllib_transport(
            url, b"{}", {"Authorization": f"Bearer {TEST_KEY}"}, 5.0
        )
    finally:
        server.shutdown()
        server.server_close()

    assert status == 302
    assert payload == b""
    assert hits == ["/v1/systemone"]
