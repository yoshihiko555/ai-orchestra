#!/usr/bin/env python3
"""Shared utilities for codex_run.py / codex_review.py.

Implements the fail-closed building blocks described in
docs/design/codex-cli-harness.md:

- ``verify_hooks_trust``: SHA-256 comparison against the distribution
  ledger recorded in ``.claude/orchestra.json`` (``codex_file_hashes``).
  Tracks ``.codex/hooks.json``, ``.codex/hooks/*.py``, ``.codex/rules/*.rules``,
  and ``.codex/validation.json``. Only when every tracked file matches its
  recorded hash is the run considered trusted; anything else (missing
  ledger, modified file, symlink, missing file) is untrusted. The ledger
  itself is only as trustworthy as its git history / human review — see
  docs/design/codex-cli-harness.md §0 for this caveat.
- ``redact_secrets``: best-effort secret redaction for artifacts that are
  small enough to post-process (final.json / review.json / report.md /
  metadata.json / diff.patch / validation.log / input.diff).
- ``write_atomic``: tmp-file + os.replace to avoid partially written
  artifacts.
- ``check_codex_version``: a soft version gate against ``codex --version``.
- ``find_repo_root`` / ``resolve_trust_flags`` / required-file checks:
  preflight helpers shared by both run and review scripts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- Trust verification ---------------------------------------------------

_HOOK_LEDGER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.codex/hooks\.json$"),
    re.compile(r"^\.codex/hooks/[^/]+\.py$"),
    re.compile(r"^\.codex/rules/[^/]+\.rules$"),
    re.compile(r"^\.codex/validation\.json$"),
)


@dataclass
class TrustResult:
    """Result of verify_hooks_trust()."""

    trusted: bool
    reasons: list[str] = field(default_factory=list)


def _is_hook_ledger_target(target: str) -> bool:
    return any(pattern.match(target) for pattern in _HOOK_LEDGER_PATTERNS)


def _load_orchestra_json(project_root: Path) -> dict | None:
    """Load .claude/orchestra.json, returning None if missing/unreadable."""
    path = project_root / ".claude" / "orchestra.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _check_single_hook_file(
    target_path: Path, resolved_root: Path, recorded_hash: str
) -> str | None:
    """Check one ledger-tracked file. Returns a failure reason, or None if OK."""
    if target_path.is_symlink():
        return f"{target_path}: symlink rejected"
    if not target_path.is_file():
        return f"{target_path}: file missing"

    resolved = target_path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        return f"{target_path}: resolved path escapes project root"

    current_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
    if current_hash != recorded_hash:
        return f"{target_path}: hash mismatch (modified after distribution)"
    return None


def verify_hooks_trust(project_root: Path) -> TrustResult:
    """Verify .codex/hooks.json and .codex/hooks/*.py against the sync ledger.

    Fail-closed: any missing ledger, missing/modified file, or symlink
    results in ``trusted=False``.
    """
    orch = _load_orchestra_json(project_root)
    if orch is None:
        return TrustResult(trusted=False, reasons=["orchestra.json not found or unreadable"])

    hashes: dict[str, str] = orch.get("codex_file_hashes", {})
    hook_entries = {k: v for k, v in hashes.items() if _is_hook_ledger_target(k)}
    if not hook_entries:
        return TrustResult(
            trusted=False, reasons=["no hook entries recorded in codex_file_hashes ledger"]
        )

    resolved_root = project_root.resolve()
    reasons: list[str] = []
    for target, recorded_hash in sorted(hook_entries.items()):
        reason = _check_single_hook_file(project_root / target, resolved_root, recorded_hash)
        if reason is not None:
            reasons.append(reason)

    return TrustResult(trusted=not reasons, reasons=reasons)


def resolve_trust_flags(project_root: Path, allow_untrusted: bool, label: str) -> list[str] | None:
    """Resolve the `codex exec` flags implied by hook trust verification.

    Returns the extra CLI flags to pass to `codex exec` (possibly empty),
    or None if the run must be aborted (fail-closed, message already
    printed to stderr).
    """
    trust = verify_hooks_trust(project_root)
    if trust.trusted:
        return ["--dangerously-bypass-hook-trust"]

    joined_reasons = "; ".join(trust.reasons)
    if allow_untrusted:
        print(
            f"[{label}] warning: hooks trust verification failed, continuing without"
            f" bypass flag ({joined_reasons})",
            file=sys.stderr,
        )
        return []

    print(
        f"[{label}] error: hooks trust verification failed, aborting (fail-closed)."
        f" Reasons: {joined_reasons}. Use --allow-untrusted-hooks to override.",
        file=sys.stderr,
    )
    return None


# --- Secret redaction -------------------------------------------------------

# Deliberately mirrors packages/codex-harness/codex/hooks/user_prompt_secret_scan.py
# SECRET_PATTERNS. Kept as a separate constant (not a shared import) so the
# distributed hook file has no dependency on scripts/ at runtime.
_MIN_TOKEN_LENGTH = 20
_MIN_GENERIC_KEY_LENGTH = 10

REDACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OPENAI_API_KEY assignment", re.compile(r"OPENAI_API_KEY\s*=\s*\S+")),
    ("AWS_ACCESS_KEY_ID value", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS_SECRET_ACCESS_KEY assignment", re.compile(r"AWS_SECRET_ACCESS_KEY\s*=\s*\S+")),
    ("GITHUB_TOKEN assignment", re.compile(r"GITHUB_TOKEN\s*=\s*\S+")),
    ("GitHub PAT (ghp_)", re.compile(rf"\bghp_[A-Za-z0-9]{{{_MIN_TOKEN_LENGTH},}}")),
    (
        "GitHub fine-grained PAT (github_pat_)",
        re.compile(rf"\bgithub_pat_[A-Za-z0-9_]{{{_MIN_TOKEN_LENGTH},}}"),
    ),
    ("API key (sk- prefix)", re.compile(rf"\bsk-[A-Za-z0-9]{{{_MIN_GENERIC_KEY_LENGTH},}}")),
    (
        "PEM private key block",
        re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----"),
    ),
]


def redact_secrets(text: str) -> str:
    """Replace secret-like substrings with `[REDACTED:<pattern name>]`."""
    result = text
    for name, pattern in REDACTION_PATTERNS:
        result = pattern.sub(f"[REDACTED:{name}]", result)
    return result


# --- Atomic write ------------------------------------------------------------


def write_atomic(path: Path, content: str) -> None:
    """Write `content` to `path` atomically (tmp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


# --- Version gate ------------------------------------------------------------

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass
class VersionCheck:
    """Result of check_codex_version()."""

    ok: bool
    message: str
    detected: tuple[int, int] | None = None


def check_codex_version(minimum: tuple[int, int]) -> VersionCheck:
    """Run `codex --version` and compare against `minimum` (major, minor).

    An unparseable version or missing binary is treated as an error
    (``detected`` is None); a parseable version below the minimum is a
    warning, not a hard failure.
    """
    try:
        completed = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return VersionCheck(ok=False, message=f"codex CLI not found or unresponsive: {exc}")

    match = _VERSION_RE.search(completed.stdout + completed.stderr)
    if match is None:
        return VersionCheck(ok=False, message="could not parse `codex --version` output")

    major, minor = int(match.group(1)), int(match.group(2))
    detected = (major, minor)
    if detected < minimum:
        return VersionCheck(
            ok=False,
            message=(
                f"codex CLI version {major}.{minor} is below the recommended minimum"
                f" {minimum[0]}.{minimum[1]}"
            ),
            detected=detected,
        )
    return VersionCheck(ok=True, message=f"codex CLI version {major}.{minor} OK", detected=detected)


# --- Preflight helpers -------------------------------------------------------

MINIMUM_CODEX_VERSION = (0, 142)


def find_repo_root(start: Path) -> Path | None:
    """Find the git repo root by walking up from `start` looking for `.git`."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def check_required_codex_files(repo_root: Path, required: list[str]) -> list[str]:
    """Return the subset of `required` (repo-relative paths) that are missing."""
    return [rel for rel in required if not (repo_root / rel).is_file()]


def run_version_gate(label: str) -> bool:
    """Run the codex version gate, printing a warning/error. Returns False on hard failure."""
    version_check = check_codex_version(MINIMUM_CODEX_VERSION)
    if not version_check.ok and version_check.detected is None:
        print(f"[{label}] error: {version_check.message}", file=sys.stderr)
        return False
    if not version_check.ok:
        print(f"[{label}] warning: {version_check.message}", file=sys.stderr)
    return True


# --- events.jsonl parsing ----------------------------------------------------
#
# Shared by codex_run.py / codex_review.py: Codex's JSONL event schema is
# treated as versioned/unstable input (docs/design/codex-cli-harness.md
# §16.3), so extraction here is deliberately best-effort and never raises.

_EVENT_NESTED_KEYS = ("result", "output", "data", "content")


def extract_final_payload(event: dict[str, Any], required_keys: set[str]) -> dict[str, Any] | None:
    """Best-effort extraction of a schema-shaped payload from one event.

    Matches either the event itself, or a nested dict under one of the
    well-known wrapper keys, against `required_keys`.
    """
    if required_keys.issubset(event.keys()):
        return event
    for key in _EVENT_NESTED_KEYS:
        nested = event.get(key)
        if isinstance(nested, dict) and required_keys.issubset(nested.keys()):
            return nested
    return None


def extract_agent_text(event: dict[str, Any]) -> str | None:
    """Best-effort extraction of plain agent message text from one event.

    Codex 0.142.x emits `{"type": "item.completed", "item": {"type":
    "agent_message", "text": ...}}` (verified via E2E); older/other shapes
    are kept as fallbacks. `item.type == "error"` is deliberately excluded
    so warnings never masquerade as the final answer.
    """
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "agent_message":
        text = item.get("text")
        if isinstance(text, str) and text:
            return text
    msg = event.get("msg")
    if isinstance(msg, dict):
        text = msg.get("message") or msg.get("text")
        if isinstance(text, str) and text:
            return text
    for key in ("message", "text"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def parse_schema_text(text: str, required_keys: set[str]) -> dict[str, Any] | None:
    """Parse agent message text as a schema-shaped JSON payload, or None.

    With `--output-schema`, the final agent message body IS the structured
    JSON (as a string inside the agent_message event).
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and required_keys.issubset(payload.keys()):
        return payload
    return None


def parse_event_line(line: str) -> dict[str, Any] | None:
    """Parse one events.jsonl line into a dict, or None if not a JSON object."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def parse_events(
    events_path: Path,
    codex_returncode: int,
    required_keys: set[str],
    build_fallback: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Extract the final structured result from an events.jsonl file.

    Falls back to ``build_fallback(status, summary)`` when no schema-shaped
    payload is found (built from the last agent message text, or a generic
    notice). Parse failures never raise; artifacts are preserved regardless.
    """
    schema_result: dict[str, Any] | None = None
    last_agent_text: str | None = None

    if events_path.is_file():
        try:
            with open(events_path, encoding="utf-8") as f:
                for line in f:
                    event = parse_event_line(line)
                    if event is None:
                        continue
                    candidate = extract_final_payload(event, required_keys)
                    if candidate is not None:
                        schema_result = candidate
                    text = extract_agent_text(event)
                    if text is not None:
                        last_agent_text = text
        except OSError:
            pass

    if schema_result is None and last_agent_text is not None:
        schema_result = parse_schema_text(last_agent_text, required_keys)

    if schema_result is not None:
        return schema_result

    fallback_status = "success" if codex_returncode == 0 else "failed"
    summary = last_agent_text or "(no structured output could be parsed from events.jsonl)"
    return build_fallback(fallback_status, summary)
