"""Claude OAuth credential preflight tests (EV-47)."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from tests.module_loader import load_module

creds = load_module(
    "meta_harness_claude_credentials_tests",
    "packages/meta-harness/lib/claude_credentials.py",
)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _payload(*, expires_at: float, token: str = "oauth-token") -> str:
    return json.dumps(
        {"claudeAiOauth": {"accessToken": token, "expiresAt": int(expires_at * 1000)}}
    )


def test_keychain_token_passes_only_through_completed_stdout(monkeypatch) -> None:
    monkeypatch.setattr(creds.platform, "system", lambda: "Darwin")
    captured: dict = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _completed(_payload(expires_at=time.time() + 3600, token="real-oauth-token"))

    credential = creds.load_claude_oauth_credential(minimum_ttl_seconds=600, runner=runner)

    assert credential.access_token == "real-oauth-token"
    assert "real-oauth-token" not in " ".join(captured["command"])
    assert "env" not in captured["kwargs"]
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL


def test_expiring_token_fails_closed_without_echoing_token(monkeypatch) -> None:
    monkeypatch.setattr(creds.platform, "system", lambda: "Darwin")
    token = "must-never-appear-in-error"

    with pytest.raises(creds.ClaudeCredentialError, match="expires too soon") as exc_info:
        creds.load_claude_oauth_credential(
            minimum_ttl_seconds=600,
            runner=lambda *_args, **_kwargs: _completed(
                _payload(expires_at=time.time() + 30, token=token)
            ),
        )

    assert token not in str(exc_info.value)


def test_non_macos_fails_closed_before_invoking_runner(monkeypatch) -> None:
    monkeypatch.setattr(creds.platform, "system", lambda: "Linux")

    with pytest.raises(creds.ClaudeCredentialError, match="macOS Keychain"):
        creds.load_claude_oauth_credential(
            minimum_ttl_seconds=1,
            runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
        )


@pytest.mark.parametrize("payload", ["not-json", "{}", '{"claudeAiOauth": {}}'])
def test_malformed_keychain_payload_fails_closed(payload: str) -> None:
    with pytest.raises(creds.ClaudeCredentialError):
        creds._parse_keychain_payload(payload)


def test_invalid_timeout_config_fails_ttl_preflight_closed() -> None:
    with pytest.raises(creds.ClaudeCredentialError, match="must be integers"):
        creds.minimum_broker_token_ttl_seconds({"evaluate": {"timeout_ms_default": "invalid"}})
