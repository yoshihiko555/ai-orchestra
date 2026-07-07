"""共通フラグ（--project / --json）のサブコマンド前後互換性テスト（build_parser, Sec6）。

`run_meta` フィクスチャは `--project` を常に引数列の末尾に固定で追加するため、
「サブコマンドより前に置いた --project がサブパーサの既定値で上書きされないか」を
検証するには、argv の並び順を完全に制御できる直接 subprocess 呼び出しが必要になる
（`conftest.py` の `git_project` / `_sandbox_safe_base` と同じ回避策を踏襲する）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
META_HARNESS_SCRIPT = REPO_ROOT / "packages" / "meta-harness" / "scripts" / "meta_harness.py"

_SANDBOX_TMP = Path("/private/tmp/claude-501")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test",
    "GIT_COMMITTER_EMAIL": "test@test",
}


def _sandbox_safe_base(tmp_path: Path) -> Path:
    if _SANDBOX_TMP.is_dir():
        return Path(tempfile.mkdtemp(dir=_SANDBOX_TMP))
    return tmp_path


def _init_git_project(base: Path, name: str) -> Path:
    project = base / name
    project.mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (project / ".gitignore").write_text(".claude/meta-harness/\n", encoding="utf-8")
    env = {**os.environ, **_GIT_ENV}
    subprocess.run(
        ["git", "init", "-q", "--template="], cwd=project, check=True, capture_output=True, env=env
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=project, check=True, capture_output=True, env=env
    )
    return project


def _run_argv(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """argv の並び順を一切加工せず `meta_harness.py` をサブプロセス起動する。"""
    cmd = [sys.executable, str(META_HARNESS_SCRIPT), *argv]
    env = {**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)}
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env, timeout=30)


def _store_ledger(project: Path) -> Path:
    return project / ".claude" / "meta-harness" / "ledger.jsonl"


class TestCommonFlagsSurviveAcrossSubcommandPlacement:
    def test_project_before_subcommand_targets_explicit_project(self, tmp_path: Path) -> None:
        base = _sandbox_safe_base(tmp_path)
        store_a = _init_git_project(base, "store-a")
        store_b = _init_git_project(base, "store-b")

        result = _run_argv(["--project", str(store_b), "init"], cwd=store_a)

        assert result.returncode == 0, result.stderr
        assert _store_ledger(store_b).is_file()
        assert not _store_ledger(store_a).is_file()

    def test_project_after_subcommand_targets_explicit_project(self, tmp_path: Path) -> None:
        base = _sandbox_safe_base(tmp_path)
        store_a = _init_git_project(base, "store-a-post")
        store_b = _init_git_project(base, "store-b-post")

        result = _run_argv(["init", "--project", str(store_b)], cwd=store_a)

        assert result.returncode == 0, result.stderr
        assert _store_ledger(store_b).is_file()
        assert not _store_ledger(store_a).is_file()

    def test_json_before_subcommand_still_produces_json_output(self, tmp_path: Path) -> None:
        base = _sandbox_safe_base(tmp_path)
        store_a = _init_git_project(base, "store-a-json")
        store_b = _init_git_project(base, "store-b-json")

        result = _run_argv(["--json", "init", "--project", str(store_b)], cwd=store_a)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"

    def test_no_project_flag_defaults_to_cwd(self, tmp_path: Path) -> None:
        base = _sandbox_safe_base(tmp_path)
        store_a = _init_git_project(base, "store-a-default")

        result = _run_argv(["init"], cwd=store_a)

        assert result.returncode == 0, result.stderr
        assert _store_ledger(store_a).is_file()
