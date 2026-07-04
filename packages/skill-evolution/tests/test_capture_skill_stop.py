"""capture-skill-telemetry.py の縮退挙動と capture-skill-stop.py（Stop hook）のユニットテスト。

主旨: メインループ Skill 実行は PostToolUse(Skill) が発火直後（自己申告確定前）に走るため、
PostToolUse 側は自己申告が無ければ pending に触れず保留し、Stop hook（本ファイルの対象）が
transcript との突合で完了記録を補完する（設計 3.8 節の保険）。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from tests.module_loader import load_module

se = load_module("skill_evolution_common", "packages/skill-evolution/lib/skill_evolution_common.py")
telemetry = load_module(
    "capture_skill_telemetry_test", "packages/skill-evolution/hooks/capture-skill-telemetry.py"
)
stop_hook = load_module(
    "capture_skill_stop_test", "packages/skill-evolution/hooks/capture-skill-stop.py"
)


class _FakeStdin:
    """`sys.stdin.buffer.read(...)` を使う hook 用の最小 stdin スタブ。"""

    def __init__(self, payload: dict) -> None:
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))


def _set_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))


def _self_report_text(run_id: str, skill: str, *, critical_ok: bool = True) -> str:
    body = {
        "run_id": run_id,
        "skill": skill,
        "tool_uses": 5,
        "ambiguities": 0,
        "discretion_fills": 0,
        "retries": 0,
        "critical": {"done": critical_ok},
    }
    return f"[skill-self-report]{json.dumps(body)}[/skill-self-report]"


def _write_transcript(tmp_path: Path, lines: list[dict]) -> str:
    path = tmp_path / "transcript.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return str(path)


def _assistant_line(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"text": text}]}}


def _backdate_pending(project_dir: str, run_id: str, seconds_ago: float) -> None:
    """pending の start_epoch を過去にずらす（stale 判定テスト用）。"""
    path = se.pending_path(project_dir, run_id)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["start_epoch"] -= seconds_ago
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# capture-skill-telemetry.py（PostToolUse）: 自己申告なし → 保留
# ---------------------------------------------------------------------------


def test_post_tool_use_defers_when_no_self_report(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = str(tmp_path)
    run_id = "issue-fix-20260101T000000-aaaa"
    se.write_pending(p, run_id, skill="issue-fix")

    _set_stdin(
        monkeypatch,
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "issue-fix"},
            "cwd": p,
            "tool_response": {"content": [{"text": "Launching skill: issue-fix"}]},
        },
    )
    telemetry.main()

    # pending は消費されず、metric も追記されない。
    assert len(se.list_pending(p)) == 1
    assert se.read_metrics(p, "issue-fix") == []


def test_post_tool_use_still_records_when_self_report_present(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = str(tmp_path)
    run_id = "issue-fix-20260101T000000-bbbb"
    se.write_pending(p, run_id, skill="issue-fix")

    _set_stdin(
        monkeypatch,
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "issue-fix"},
            "cwd": p,
            "tool_response": {
                "content": [{"text": "Done. " + _self_report_text(run_id, "issue-fix")}]
            },
        },
    )
    telemetry.main()

    assert se.list_pending(p) == []
    records = se.read_metrics(p, "issue-fix")
    assert len(records) == 1
    assert records[0]["self_report"] is not None
    assert records[0]["success"] is True


# ---------------------------------------------------------------------------
# capture-skill-stop.py（Stop）: transcript と突合して完了記録する
# ---------------------------------------------------------------------------


def test_stop_hook_matches_self_report_in_transcript(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = str(tmp_path)
    run_id = "issue-fix-20260101T000000-cccc"
    se.write_pending(p, run_id, skill="issue-fix")
    transcript_path = _write_transcript(
        tmp_path, [_assistant_line(_self_report_text(run_id, "issue-fix"))]
    )

    monkeypatch.setattr(stop_hook, "_default_transcript_root", lambda: p)
    _set_stdin(monkeypatch, {"cwd": p, "transcript_path": transcript_path})
    stop_hook.main()

    assert se.list_pending(p) == []
    records = se.read_metrics(p, "issue-fix")
    assert len(records) == 1
    assert records[0]["run_id"] == run_id
    assert records[0]["self_report"] is not None
    assert records[0]["machine"]["duration_ms"] is not None


def test_stop_hook_records_machine_only_after_stale_threshold(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = str(tmp_path)
    run_id = "issue-fix-20260101T000000-dddd"
    se.write_pending(p, run_id, skill="issue-fix")
    _backdate_pending(p, run_id, seconds_ago=700)  # 既定 600 秒の猶予を超過させる

    _set_stdin(monkeypatch, {"cwd": p, "transcript_path": str(tmp_path / "missing.jsonl")})
    stop_hook.main()

    assert se.list_pending(p) == []
    records = se.read_metrics(p, "issue-fix")
    assert len(records) == 1
    assert records[0]["self_report"] is None
    assert records[0]["success"] is False


def test_stop_hook_leaves_fresh_pending_untouched(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = str(tmp_path)
    run_id = "issue-fix-20260101T000000-eeee"
    se.write_pending(p, run_id, skill="issue-fix")  # まだ新しい（猶予内）

    _set_stdin(monkeypatch, {"cwd": p, "transcript_path": str(tmp_path / "missing.jsonl")})
    stop_hook.main()

    assert len(se.list_pending(p)) == 1
    assert se.read_metrics(p, "issue-fix") == []


def test_stop_hook_skips_when_already_recorded_by_subagent_stop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SubagentStop が既に記録済みの run_id は Stop hook で二重記録しない。"""
    p = str(tmp_path)
    run_id = "issue-fix-20260101T000000-ffff"
    se.write_pending(p, run_id, skill="issue-fix")
    # capture-subagent-skill.py が先に記録済みという想定（pending は未消費のまま残り得る）。
    se.append_metric(
        p,
        "issue-fix",
        se.build_metric_record("issue-fix", run_id, {"critical": {"done": True}}, duration_ms=1000),
    )
    transcript_path = _write_transcript(
        tmp_path, [_assistant_line(_self_report_text(run_id, "issue-fix"))]
    )

    monkeypatch.setattr(stop_hook, "_default_transcript_root", lambda: p)
    _set_stdin(monkeypatch, {"cwd": p, "transcript_path": transcript_path})
    stop_hook.main()

    # pending は破棄されるが、metric は増えない（1 件のまま）。
    assert se.list_pending(p) == []
    assert len(se.read_metrics(p, "issue-fix")) == 1


def test_stop_hook_disabled_via_config_does_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = str(tmp_path)
    run_id = "issue-fix-20260101T000000-gggg"
    se.write_pending(p, run_id, skill="issue-fix")
    monkeypatch.setattr(stop_hook.se, "load_config", lambda _project_dir: {"enabled": False})

    _set_stdin(monkeypatch, {"cwd": p, "transcript_path": str(tmp_path / "missing.jsonl")})
    stop_hook.main()

    assert len(se.list_pending(p)) == 1  # 無効時は一切手を出さない


# ---------------------------------------------------------------------------
# _read_transcript_tail: パストラバーサル防御（CWE-22）
# ---------------------------------------------------------------------------


def test_read_transcript_tail_rejects_path_outside_allowed_root(tmp_path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside" / "secret.jsonl"
    outside.parent.mkdir()
    outside.write_text("top secret\n", encoding="utf-8")

    result = stop_hook._read_transcript_tail(str(outside), str(allowed_root))

    assert result == ""


def test_read_transcript_tail_rejects_symlink_escaping_allowed_root(tmp_path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside" / "secret.jsonl"
    outside.parent.mkdir()
    outside.write_text("top secret\n", encoding="utf-8")
    symlink_path = allowed_root / "transcript.jsonl"
    symlink_path.symlink_to(outside)

    result = stop_hook._read_transcript_tail(str(symlink_path), str(allowed_root))

    assert result == ""


def test_read_transcript_tail_allows_path_within_allowed_root(tmp_path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    transcript_path = allowed_root / "transcript.jsonl"
    transcript_path.write_text("hello\n", encoding="utf-8")

    result = stop_hook._read_transcript_tail(str(transcript_path), str(allowed_root))

    assert result == "hello\n"
