"""出力ファイルの公開時点と、書込失敗時の既存内容保持。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from lib.llm.run_output import claim_output_dir
from lib.llm.tool_types import TokenUsage


def test_claim_initializes_readable_zero_progress(tmp_path):
    output = claim_output_dir(tmp_path / "new/out")

    progress = json.loads((output.path / "progress.json").read_text(encoding="utf-8"))
    assert progress == {"turns": 0, "tokens": {"input": 0, "output": 0, "total": 0}}
    assert not (output.path / "exit").exists()


def test_files_are_complete_before_atomic_publication_and_exit_follows_result(
    tmp_path, monkeypatch
):
    output = claim_output_dir(tmp_path / "out")
    replace = os.replace
    publications = []

    def observe(source, destination):
        destination = Path(destination)
        if destination.name == "progress.json":
            previous = json.loads(destination.read_text(encoding="utf-8"))
            assert previous["turns"] == 0
            assert json.loads(Path(source).read_text(encoding="utf-8"))["turns"] == 1
        elif destination.name == "result.json":
            assert not (output.path / "exit").exists()
            assert json.loads(Path(source).read_text(encoding="utf-8")) == {"answer": "回答"}
        else:
            assert destination.name == "exit"
            assert json.loads((output.path / "result.json").read_text(encoding="utf-8")) == {
                "answer": "回答"
            }
            assert Path(source).read_text(encoding="utf-8") == "0\n"
        publications.append(destination.name)
        replace(source, destination)

    monkeypatch.setattr(os, "replace", observe)
    output.write_progress(1, TokenUsage(2, 3))
    output.write_result({"answer": "回答"})
    output.write_exit(0)

    assert publications == ["progress.json", "result.json", "exit"]
    assert {path.name for path in output.path.iterdir()} == set(publications)


def test_failed_publication_preserves_progress_and_removes_temporary_file(tmp_path, monkeypatch):
    output = claim_output_dir(tmp_path / "out")
    previous = (output.path / "progress.json").read_bytes()

    def reject(source, destination):
        raise OSError("書込失敗")

    monkeypatch.setattr(os, "replace", reject)
    with pytest.raises(OSError):
        output.write_progress(1, TokenUsage(2, 3))

    assert (output.path / "progress.json").read_bytes() == previous
    assert [path.name for path in output.path.iterdir()] == ["progress.json"]
