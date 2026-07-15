#!/usr/bin/env python3
"""Compatibility entrypoint for the shared credential broker implementation."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType

_SHARED_BROKER = (
    Path(__file__).resolve().parents[3] / "docker-runtime" / "docker" / "broker" / "broker.py"
)
_SHARED_NAMESPACE = runpy.run_path(str(_SHARED_BROKER), run_name=__name__)
_SHARED_GLOBALS = _SHARED_NAMESPACE["BrokerState"].__init__.__globals__
globals().update(_SHARED_NAMESPACE)


class _BrokerModule(ModuleType):
    """Keep shim attribute overrides visible to the shared implementation."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        _SHARED_GLOBALS[name] = value


sys.modules[__name__].__class__ = _BrokerModule
