"""settings.local.json / orchestra.json の I/O ユーティリティ。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_settings(project_dir: Path) -> dict[str, Any]:
    """settings.local.json をロードする。"""
    settings_path = project_dir / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return {"hooks": {}}
    try:
        with open(settings_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"hooks": {}}


def save_settings(project_dir: Path, settings: dict[str, Any]) -> None:
    """settings.local.json を保存する。"""
    settings_path = project_dir / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_orchestra_json(project_dir: Path) -> dict[str, Any]:
    """orchestra.json をロードする。"""
    path = project_dir / ".claude" / "orchestra.json"
    if not path.exists():
        return {"installed_packages": [], "orchestra_dir": "", "last_sync": ""}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"installed_packages": [], "orchestra_dir": "", "last_sync": ""}


def save_orchestra_json(project_dir: Path, data: dict[str, Any]) -> None:
    """orchestra.json を保存する。"""
    path = project_dir / ".claude" / "orchestra.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
