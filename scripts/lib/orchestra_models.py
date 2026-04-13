"""パッケージとフックエントリのデータモデル。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HookEntry:
    """フックエントリ（manifest.json の hooks 値）"""

    file: str
    matcher: str | None = None
    timeout: int = 5

    @classmethod
    def from_json(cls, value: str | dict[str, Any]) -> HookEntry:
        """JSON 値から HookEntry を生成"""
        if isinstance(value, str):
            return cls(file=value)
        return cls(
            file=value["file"],
            matcher=value.get("matcher"),
            timeout=value.get("timeout", 5),
        )


@dataclass
class ScriptEntry:
    """スクリプトエントリ（manifest.json の scripts 値）"""

    path: str
    description: str = ""

    @classmethod
    def from_json(cls, value: str | dict[str, Any]) -> ScriptEntry:
        """JSON 値から ScriptEntry を生成。文字列または辞書に対応。"""
        if isinstance(value, str):
            return cls(path=value)
        if "path" not in value:
            msg = f"ScriptEntry requires 'path' key, got: {value!r}"
            raise ValueError(msg)
        return cls(
            path=value["path"],
            description=value.get("description", ""),
        )


@dataclass
class Package:
    """パッケージ情報"""

    name: str
    version: str
    description: str
    depends: list[str]
    hooks: dict[str, list[HookEntry]]
    files: list[str]
    scripts: list[ScriptEntry]
    config: list[str]
    skills: list[str]
    agents: list[str]
    rules: list[str]
    path: Path
    context_files: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, manifest_path: Path) -> Package:
        """manifest.json からパッケージ情報をロード"""
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)

        hooks = {}
        for event, entries in data.get("hooks", {}).items():
            hooks[event] = [HookEntry.from_json(e) for e in entries]

        return cls(
            name=data["name"],
            version=data["version"],
            description=data.get("description", ""),
            depends=data.get("depends", []),
            hooks=hooks,
            files=data.get("files", []),
            scripts=[ScriptEntry.from_json(s) for s in data.get("scripts", [])],
            config=data.get("config", []),
            skills=data.get("skills", []),
            agents=data.get("agents", []),
            rules=data.get("rules", []),
            path=manifest_path.parent,
            context_files=data.get("context_files", {}),
        )
