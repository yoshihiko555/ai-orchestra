#!/usr/bin/env python3
"""Read a short-lived Claude OAuth access token without persisting it.

The token is returned only to an in-process Docker broker launcher. Callers must
never include it in argv, environment variables, logs, or exception messages.
Non-Darwin hosts fail closed with ClaudeCredentialError; Linux support requires a
separately designed credential source, while CI replaces the loader with a mock.
"""

from __future__ import annotations

import json
import math
import platform
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

KEYCHAIN_SERVICE = "Claude Code-credentials"
TOKEN_TTL_MARGIN_SECONDS = 300


class ClaudeCredentialError(RuntimeError):
    """Claude OAuth credentials are unavailable or unsafe for a refresh-less run."""


@dataclass(frozen=True)
class ClaudeOAuthCredential:
    access_token: str
    expires_at_epoch: float

    def assert_minimum_ttl(self, minimum_seconds: int) -> None:
        remaining = self.expires_at_epoch - time.time()
        if remaining < minimum_seconds:
            raise ClaudeCredentialError(
                "Claude OAuth access token expires too soon for a refresh-less broker run "
                f"(remaining={int(remaining)}s < required={minimum_seconds}s). "
                "Run a normal Claude command to refresh the token, then retry."
            )


def load_claude_oauth_credential(
    *,
    minimum_ttl_seconds: int,
    runner: SubprocessRunner = subprocess.run,
) -> ClaudeOAuthCredential:
    """Load the macOS Keychain credential and fail closed on every ambiguity."""
    if platform.system() != "Darwin":
        raise ClaudeCredentialError(
            "Claude OAuth broker currently requires macOS Keychain credentials"
        )
    try:
        completed = runner(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCredentialError("could not read Claude OAuth credential from Keychain") from exc
    if completed.returncode != 0:
        raise ClaudeCredentialError("Claude OAuth credential is unavailable in Keychain")
    credential = _parse_keychain_payload(completed.stdout)
    credential.assert_minimum_ttl(minimum_ttl_seconds)
    return credential


def _parse_keychain_payload(raw: str) -> ClaudeOAuthCredential:
    try:
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ClaudeCredentialError("Claude Keychain credential is not valid JSON") from exc
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        raise ClaudeCredentialError("Claude Keychain credential has no claudeAiOauth object")
    access_token = oauth.get("accessToken")
    expires_at = _expiry_epoch(oauth.get("expiresAt"))
    if not isinstance(access_token, str) or not access_token:
        raise ClaudeCredentialError("Claude Keychain credential has no access token")
    return ClaudeOAuthCredential(access_token=access_token, expires_at_epoch=expires_at)


def _expiry_epoch(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ClaudeCredentialError("Claude Keychain credential has no numeric expiresAt")
    epoch = float(value)
    if not math.isfinite(epoch):
        raise ClaudeCredentialError("Claude Keychain credential has an invalid expiresAt")
    if epoch > 10_000_000_000:
        epoch /= 1000
    if epoch <= 0:
        raise ClaudeCredentialError("Claude Keychain credential has an invalid expiresAt")
    return epoch


def minimum_broker_token_ttl_seconds(config: dict) -> int:
    evaluate = config.get("evaluate") or {}
    broker = (evaluate.get("isolation") or {}).get("broker") or {}
    try:
        timeout_seconds = int(evaluate.get("timeout_ms_default", 300000)) // 1000
        idle_timeout = int(broker.get("idle_timeout_sec", 300))
    except (TypeError, ValueError) as exc:
        raise ClaudeCredentialError("broker timeout settings must be integers") from exc
    if timeout_seconds <= 0 or idle_timeout <= 0:
        raise ClaudeCredentialError("broker timeout settings must be positive")
    return timeout_seconds + idle_timeout + TOKEN_TTL_MARGIN_SECONDS
