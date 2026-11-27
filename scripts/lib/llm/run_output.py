"""一回の実行が所有する出力先と、途中経過・完了ファイル。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .tool_types import TokenUsage

_PROGRESS = "progress.json"
_RESULT = "result.json"
_EXIT = "exit"


class OutputDirInUse(ValueError):
    """既存の出力を持つディレクトリは再利用しない。"""


@dataclass(frozen=True)
class RunOutput:
    path: Path

    def _write(self, name: str, content: str) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, self.path / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def write_progress(self, turns: int, tokens: TokenUsage) -> None:
        self._write(_PROGRESS, json.dumps({"turns": turns, "tokens": tokens.to_dict()}) + "\n")

    def write_result(self, result: dict[str, object]) -> None:
        self._write(_RESULT, json.dumps(result, ensure_ascii=False) + "\n")

    def write_exit(self, code: int) -> None:
        self._write(_EXIT, f"{code}\n")


def claim_output_dir(path: Path) -> RunOutput:
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise OutputDirInUse("空でない出力先は使用できません。")
    try:
        descriptor = os.open(path / _PROGRESS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise OutputDirInUse("出力先は使用中です。") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"turns": 0, "tokens": TokenUsage(0, 0).to_dict()}) + "\n")
    return RunOutput(path)
