"""SessionEnd の起動役（hooks/distill-launch.py）を検証する。

評価セット docs/evaluation/fail-logs.md の EV-11・EV-12・EV-37〜EV-39 に対応する（設計 §7.1・§7.7）。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time

import lessons_helpers as h
import pytest

from tests.module_loader import REPO_ROOT, load_module

launch = load_module("distill_launch_under_test", "packages/fail-logs/hooks/distill-launch.py")
LAUNCH_SCRIPT = REPO_ROOT / "packages" / "fail-logs" / "hooks" / "distill-launch.py"


class _Stdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    def close(self) -> None:
        return None


class _Popen:
    """subprocess.Popen の代わり。起動の引数と標準入力を記録する。"""

    calls: list = []

    def __init__(self, args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.stdin = _Stdin()
        _Popen.calls.append(self)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _Popen.calls = []
    monkeypatch.setattr(launch, "_popen", _Popen)
    monkeypatch.delenv("FAIL_LOGS_DISTILL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")


def _run(monkeypatch, project, capsys) -> str:
    payload = {
        "cwd": str(project),
        "session_id": "sess-launch-01",
        "transcript_path": "/x/sess.jsonl",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    launch.main()
    return capsys.readouterr().out


ENABLED = "distill:\n  enabled: true\n"
DISABLED = "distill:\n  enabled: false\n"


# EV-37: 既定（教訓化は有効）では、対話のセッションの終了時にワーカーを起動する（前段判定はワーカーが行う）。
def test_enabled_by_default(monkeypatch, tmp_path, capsys) -> None:
    project = h.make_project(tmp_path)
    assert _run(monkeypatch, project, capsys) == ""
    assert len(_Popen.calls) == 1
    assert json.loads(_Popen.calls[0].stdin.data.decode())["mode"] == "distill"


# EV-37: `distill.enabled: false` で spool も無ければ、何も起動しない。
def test_disabled_by_config(monkeypatch, tmp_path, capsys) -> None:
    project = h.make_project(tmp_path, local_config=DISABLED)
    assert _run(monkeypatch, project, capsys) == ""
    assert _Popen.calls == []


# EV-38: 教訓化セッションの中・非対話では起動しない。
@pytest.mark.parametrize(
    ("key", "value"), [("FAIL_LOGS_DISTILL", "1"), ("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")]
)
def test_not_launched_inside_distill_or_noninteractive(
    monkeypatch, tmp_path, capsys, key, value
) -> None:
    project = h.make_project(tmp_path, local_config=ENABLED)
    monkeypatch.setenv(key, value)
    _run(monkeypatch, project, capsys)
    assert _Popen.calls == []


def test_noninteractive_included_when_configured(monkeypatch, tmp_path, capsys) -> None:
    project = h.make_project(tmp_path, local_config=ENABLED + "  include_noninteractive: true\n")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    _run(monkeypatch, project, capsys)
    assert len(_Popen.calls) == 1


# EV-39: 有効なら、hook の標準入出力を引き継がずに切り離して起動し、記録のパスは標準入力で渡す。
def test_launch_detached_with_stdin(monkeypatch, tmp_path, capsys) -> None:
    project = h.make_project(tmp_path, local_config=ENABLED)
    assert _run(monkeypatch, project, capsys) == ""

    call = _Popen.calls[0]
    assert call.kwargs["start_new_session"] is True
    assert call.kwargs["stdout"] is subprocess.DEVNULL
    assert call.kwargs["stderr"] is subprocess.DEVNULL
    assert call.kwargs["close_fds"] is True
    assert "/x/sess.jsonl" not in " ".join(map(str, call.args))
    request = json.loads(call.stdin.data.decode())
    assert request == {
        "mode": "distill",
        "session_id": "sess-launch-01",
        "transcript_path": "/x/sess.jsonl",
        "project_dir": str(project),
    }


# EV-37 の補足: 教訓化が無効でも、spool があれば取り込みだけのために起動する。
def test_spool_only_launches_import(monkeypatch, tmp_path, capsys) -> None:
    project = h.make_project(tmp_path, local_config=DISABLED)
    spool = h.log_dir(project) / "spool"
    spool.mkdir(parents=True)
    (spool / ("a" * 32 + ".json")).write_text("{}")
    _run(monkeypatch, project, capsys)
    assert json.loads(_Popen.calls[0].stdin.data.decode())["mode"] == "import_only"


def test_enabled_false_stops_everything(monkeypatch, tmp_path, capsys) -> None:
    project = h.make_project(tmp_path, local_config="enabled: false\n" + ENABLED)
    _run(monkeypatch, project, capsys)
    assert _Popen.calls == []


# EV-39・EV-11: 実プロセスとして 1 秒以内に、無出力・終了コード 0 で戻る（ワーカーの処理を待たない）。
def test_returns_quickly_as_process(tmp_path) -> None:
    project = h.make_project(tmp_path, local_config=ENABLED)
    payload = {
        "cwd": str(project),
        "session_id": "sess-launch-02",
        "transcript_path": str(tmp_path / "none.jsonl"),
    }
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(LAUNCH_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "AI_ORCHESTRA_DIR": str(REPO_ROOT), "HOME": str(tmp_path)},
        check=False,
    )
    assert time.monotonic() - started < 1.5
    assert result.returncode == 0
    assert result.stdout == ""


# EV-12: 内部の例外でも終了コード 0 で戻る。
def test_internal_exception_exits_zero(monkeypatch, tmp_path) -> None:
    project = h.make_project(tmp_path, local_config=ENABLED)

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(launch, "_launch", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(project)})))
    with pytest.raises(SystemExit) as exc_info:
        launch.main()
    assert exc_info.value.code == 0
