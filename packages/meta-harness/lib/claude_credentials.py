#!/usr/bin/env python3
"""Compatibility exports for the shared docker-runtime OAuth credential loader."""

from __future__ import annotations

import sys
from pathlib import Path

_DOCKER_RUNTIME_LIB = Path(__file__).resolve().parents[2] / "docker-runtime" / "lib"
if str(_DOCKER_RUNTIME_LIB) not in sys.path:
    sys.path.insert(0, str(_DOCKER_RUNTIME_LIB))

import docker_runtime_credentials as _credentials  # noqa: E402

ClaudeCredentialError = _credentials.ClaudeCredentialError
ClaudeOAuthCredential = _credentials.ClaudeOAuthCredential
KEYCHAIN_SERVICE = _credentials.KEYCHAIN_SERVICE
SubprocessRunner = _credentials.SubprocessRunner
TOKEN_TTL_MARGIN_SECONDS = _credentials.TOKEN_TTL_MARGIN_SECONDS
load_claude_oauth_credential = _credentials.load_claude_oauth_credential
minimum_broker_token_ttl_seconds = _credentials.minimum_broker_token_ttl_seconds

# Historically exercised by meta-harness tests and retained for compatibility.
_expiry_epoch = _credentials._expiry_epoch
_parse_keychain_payload = _credentials._parse_keychain_payload
platform = _credentials.platform
