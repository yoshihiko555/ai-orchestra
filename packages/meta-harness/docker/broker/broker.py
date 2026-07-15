#!/usr/bin/env python3
"""Compatibility entrypoint for the shared credential broker implementation."""

from __future__ import annotations

from pathlib import Path

_SHARED_BROKER = (
    Path(__file__).resolve().parents[3] / "docker-runtime" / "docker" / "broker" / "broker.py"
)
exec(compile(_SHARED_BROKER.read_bytes(), str(_SHARED_BROKER), "exec"), globals())  # noqa: S102
