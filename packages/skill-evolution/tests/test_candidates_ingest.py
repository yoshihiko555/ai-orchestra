"""EV-25・29・52〜58: 公開 CLI 契約と byte offset からの取り込み。"""

from __future__ import annotations

import json
import sys
import time

import candidates_ingest as ingest_lib
import pytest
from candidates_ingest import ingest
from candidates_safety import (
    CandidatesError,
    FailLogsUnavailable,
    MaskingUnavailable,
    ValidationError,
)


def write_failures(context, records, *, raw=""):
    path = context.root / ".claude/logs/fail-logs/failures.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.write(raw)
    return path


def failure(number=1, **fields):
    record = {
        "v": 1,
        "eid": f"{number:012x}",
        "type": "failure",
        "sid": "session-1234",
        "ts": "2026-11-01T10:01:00Z",
        "data": {
            "failure_type": "test_failure",
            "error_excerpt": "collection failed password=private-value",
            "tool_use_id": "bash-call-is-not-skill",
        },
    }
    record.update(fields)
    return record


def lessons_payload(context, lessons, *, details=None):
    path = context.orchestra / "packages/fail-logs/scripts/lessons-fixture.json"
    payload = {"lessons": lessons}
    if details is not None:
        payload["details"] = details
    path.write_text(json.dumps(payload))


def lesson():
    return {
        "lid": "L-00000001",
        "title": "検証の順序",
        "event": "テスト失敗",
        "cause": "手順不足",
        "avoidance": "確認する",
        "created_at": "2026-11-01T10:01:00Z",
        "evidence": [
            {
                "kind": "session",
                "ref": "session-1234",
                "ts": "2026-11-01T10:01:00Z",
                "failures": ["000000000001"],
            }
        ],
    }


def test_ev52_ingest_uses_scope_not_failure_tool_id(candidate_context, scope_event, write_runs):
    write_runs([scope_event(), scope_event("turn.stopped", "2026-11-01T10:02:00Z")])
    write_failures(candidate_context, [failure()])
    assert ingest(candidate_context)["ingested"] == 1
    observation = next(iter(candidate_context.store.read()["observations"].values()))
    assert observation["attributed_by"] == "auto"
    assert observation["run_id"] == "R-000000000001"
    assert "private-value" not in observation["excerpt"]
    assert "tool_use_id" not in observation


def test_ev54_lessons_public_summary_is_enriched_but_never_auto_attributed(
    candidate_context, scope_event, write_runs
):
    write_runs([scope_event(), scope_event("turn.stopped", "2026-11-01T10:02:00Z")])
    lessons_payload(candidate_context, [{"lid": "L-00000001", "title": "概要"}], details=[lesson()])
    assert ingest(candidate_context)["ingested"] == 1
    observation = next(iter(candidate_context.store.read()["observations"].values()))
    assert observation["attribution"] == "unknown"
    assert observation["failure_eids"] == ["000000000001"]


def test_ev56_repeated_ingest_offsets_truncation_and_partial_lines(candidate_context):
    path = write_failures(candidate_context, [failure()], raw="invalid\n[]\n")
    assert ingest(candidate_context)["ingested"] == 1
    first_offset = path.stat().st_size
    assert candidate_context.store.ingest_state()["failure_offset"] == first_offset
    assert ingest(candidate_context)["ingested"] == 0
    write_failures(candidate_context, [], raw=json.dumps(failure(2), ensure_ascii=False))
    assert ingest(candidate_context)["ingested"] == 0
    assert candidate_context.store.ingest_state()["failure_offset"] == first_offset
    write_failures(candidate_context, [], raw="\n")
    assert ingest(candidate_context)["ingested"] == 1
    path.write_text(json.dumps(failure(3)) + "\n")
    assert ingest(candidate_context)["ingested"] == 1
    assert len(candidate_context.store.read()["observations"]) == 3
    assert ingest(candidate_context)["ingested"] == 0


def test_ev56_retry_after_ingest_state_failure_keeps_one_observation(
    candidate_context, monkeypatch
):
    write_failures(candidate_context, [failure()])
    with monkeypatch.context() as patch:
        patch.setattr(
            candidate_context.store,
            "save_ingest_state",
            lambda _: (_ for _ in ()).throw(OSError("interrupted")),
        )
        with pytest.raises(OSError):
            ingest(candidate_context)
    assert ingest(candidate_context)["ingested"] == 0
    assert len(candidate_context.store.read()["observations"]) == 1


def test_ev25_fail_logs_local_override_and_custom_root_log_dir(candidate_context):
    config = candidate_context.project / ".claude/config/fail-logs"
    config.mkdir(parents=True)
    (config / "fail-logs.yaml").write_text("enabled: true\nlogs_dir: .claude/logs/base\n")
    (config / "fail-logs.local.yaml").write_text("logs_dir: .claude/logs/custom\n")
    path = candidate_context.root / ".claude/logs/custom/failures.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(failure()) + "\n")
    assert ingest(candidate_context)["ingested"] == 1
    assert candidate_context.store.ingest_state()["failure_source"] == str(path)
    (config / "fail-logs.local.yaml").write_text("enabled: false\n")
    with pytest.raises(FailLogsUnavailable):
        ingest(candidate_context)


def test_ev57_absent_disabled_and_cli_exit_four_stop_without_writes(candidate_context):
    manifest = candidate_context.project / ".claude/orchestra.json"
    manifest.write_text('{"installed_packages": []}')
    with pytest.raises(FailLogsUnavailable) as caught:
        ingest(candidate_context)
    assert caught.value.code == 4
    manifest.write_text('{"installed_packages": ["fail-logs"]}')
    (candidate_context.orchestra / "packages/fail-logs/scripts/exit-fixture").write_text("4")
    with pytest.raises(FailLogsUnavailable):
        ingest(candidate_context)
    assert not candidate_context.store.directory.exists()


def test_ev57_masking_unavailable_does_not_advance_ingest(candidate_context):
    write_failures(candidate_context, [failure()])
    masker = candidate_context.orchestra / "packages/fail-logs/lib/masking.py"
    masker.write_text("def load_masker(): return None\n")
    with pytest.raises(MaskingUnavailable) as caught:
        ingest(candidate_context)
    assert caught.value.code == 2
    assert not (candidate_context.store.directory / "candidates.jsonl").exists()
    assert not (candidate_context.store.directory / "ingest-state.json").exists()


def test_ev57_missing_fail_logs_does_not_stop_hooks(project, run_hook, read_events):
    run_hook(
        "record-skill-scope.py",
        {"hook_event_name": "UserPromptSubmit", "session_id": "session-1234"},
    )
    assert read_events()[0]["type"] == "turn.started"


def test_ev29_invalid_lessons_json_and_timeout_are_explained(candidate_context, monkeypatch):
    command = candidate_context.orchestra / "packages/fail-logs/scripts/lessons_cli.py"
    command.write_text("print('not-json')")
    with pytest.raises(ValidationError):
        ingest(candidate_context)

    command.write_text("import time; time.sleep(2)")
    monkeypatch.setattr(ingest_lib, "LESSONS_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(CandidatesError, match="時間切れ"):
        ingest(candidate_context)


def test_ev57_malformed_records_are_skipped_and_offsets_advance(candidate_context):
    malformed = lesson()
    del malformed["evidence"][0]["ref"]
    lessons_payload(candidate_context, [malformed, lesson()])
    path = write_failures(
        candidate_context,
        [
            failure(
                data={"failure_type": "test_failure", "error_excerpt": "\x1b[31m失敗\x1b[0m\t\r\n"}
            ),
            failure(2, sid="bad sid"),
            failure(3, data={"failure_type": "invalid kind", "error_excerpt": "不正"}),
            failure(4),
        ],
    )
    result = ingest(candidate_context)
    assert result["ingested"] == 3
    assert result["skipped"] == 3
    assert sum(result["skip_reasons"].values()) == 3
    assert any("セッション ID" in reason for reason in result["skip_reasons"])
    assert result["failure_offset"] == path.stat().st_size
    assert candidate_context.store.ingest_state()["failure_offset"] == path.stat().st_size
    observations = candidate_context.store.read()["observations"].values()
    ansi = next(item for item in observations if item["evidence"]["ref"] == "000000000001")
    assert ansi["excerpt"] == " [31m失敗 [0m  \n"
    assert ingest(candidate_context)["ingested"] == 0


@pytest.mark.parametrize(
    "malformation",
    [
        "not-object",
        "bad-id",
        "missing-ref",
        "nonlist-failures",
        "bad-eid",
        "bad-text",
        "missing-text",
        "bad-detail",
    ],
)
def test_ev57_bad_lesson_does_not_block_following_evidence(candidate_context, malformation):
    malformed = lesson()
    if malformation == "not-object":
        malformed = []
    elif malformation == "bad-id":
        malformed["lid"] = []
    elif malformation == "missing-ref":
        del malformed["evidence"][0]["ref"]
    elif malformation == "nonlist-failures":
        malformed["evidence"][0]["failures"] = "000000000001"
    elif malformation == "bad-eid":
        malformed["evidence"][0]["failures"] = [[]]
    elif malformation == "bad-text":
        malformed["title"] = {}
    elif malformation == "missing-text":
        del malformed["title"]
    elif malformation == "bad-detail":
        del malformed["evidence"]
    valid = {**lesson(), "lid": "L-00000002"}
    lessons_payload(candidate_context, [malformed, valid], details=[{"lid": "L-other"}])
    if malformation == "bad-detail":
        script = candidate_context.orchestra / "packages/fail-logs/scripts/lessons_cli.py"
        script.write_text(
            "import sys\n"
            f"print({json.dumps({'lessons': [malformed, valid]})!r} if sys.argv[1] == 'list' else '{{\"lesson\": null}}')\n"
        )
    path = write_failures(candidate_context, [failure()])
    result = ingest(candidate_context)
    assert result["ingested"] == 2
    assert result["skipped"] == 1
    assert result["failure_offset"] == path.stat().st_size
    assert list(result["skip_reasons"])[0].startswith("lesson:")


def test_ev57_overlong_failure_lines_are_bounded_and_skipped(candidate_context, monkeypatch):
    limit = 1024
    monkeypatch.setattr(ingest_lib, "FAILURE_LINE_LIMIT_BYTES", limit)
    path = write_failures(candidate_context, [], raw="x" * (limit * 5) + "\n")
    write_failures(candidate_context, [failure()])
    result = ingest(candidate_context)
    assert result["ingested"] == 1
    assert result["skipped"] == 1
    assert result["failure_offset"] == path.stat().st_size
    assert any("バイト数" in reason for reason in result["skip_reasons"])
    write_failures(candidate_context, [], raw="x" * (limit + 1))
    result = ingest(candidate_context)
    assert result["skipped"] == 1
    assert result["failure_offset"] == path.stat().st_size
    assert ingest(candidate_context)["skipped"] == 0


def test_ev57_lessons_cli_uses_trusted_path_interpreter_and_minimal_env(
    candidate_context, monkeypatch, tmp_path
):
    import candidates_safety

    untrusted = tmp_path / "untrusted"
    untrusted.mkdir()
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(untrusted))
    monkeypatch.setenv("PYTHONPATH", "must-not-inherit")
    monkeypatch.setenv("PYTHONSTARTUP", "must-not-inherit")
    monkeypatch.setenv("PYTHONHOME", "must-not-inherit")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-inherit")
    expected = {"PATH", "HOME", "AI_ORCHESTRA_DIR", "CLAUDE_PROJECT_DIR"}
    original = ingest_lib.subprocess.Popen
    calls = []

    def inspect(arguments, **kwargs):
        calls.append(arguments)
        assert arguments[0] == sys.executable
        assert arguments[1] == str(
            candidate_context.orchestra / "packages/fail-logs/scripts/lessons_cli.py"
        )
        assert set(kwargs["env"]) <= expected
        assert kwargs["env"]["AI_ORCHESTRA_DIR"] == str(candidate_context.orchestra)
        assert "shell" not in kwargs
        return original(arguments, **kwargs)

    monkeypatch.setattr(ingest_lib.subprocess, "Popen", inspect)
    assert candidates_safety.orchestra_directory() == candidate_context.orchestra
    assert candidates_safety.load_masker() is not None
    assert ingest_lib.read_lessons(candidate_context, {}) == []
    assert len(calls) == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_ev29_lessons_cli_output_is_bounded(candidate_context, monkeypatch, stream):
    script = candidate_context.orchestra / "packages/fail-logs/scripts/lessons_cli.py"
    script.write_text(f"import sys; sys.{stream}.write('x' * 100000)")
    monkeypatch.setattr(ingest_lib, "LESSONS_OUTPUT_LIMIT_BYTES", 4096)
    with pytest.raises(CandidatesError, match="出力量"):
        ingest(candidate_context)
    assert not candidate_context.store.directory.exists()


def test_ev29_all_lesson_details_share_one_deadline(candidate_context, monkeypatch):
    deadlines = []

    def command(context, arguments, *, deadline):
        deadlines.append(deadline)
        if arguments == ["list"]:
            return {"lessons": [{"lid": "L-00000001"}, {"lid": "L-00000002"}]}
        return {"lesson": {**lesson(), "lid": arguments[1]}}

    monkeypatch.setattr(ingest_lib, "_lessons_command", command)
    assert len(ingest_lib.read_lessons(candidate_context, {})) == 2
    assert len(deadlines) == 3 and len(set(deadlines)) == 1


def test_ev29_lesson_details_cannot_extend_total_runtime(candidate_context, monkeypatch):
    script = candidate_context.orchestra / "packages/fail-logs/scripts/lessons_cli.py"
    script.write_text(
        "import json, sys, time\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'lessons': [{'lid': f'L-{n:08x}'} for n in range(1, 10)]}))\n"
        "else:\n"
        "    time.sleep(0.1)\n"
        f"    print(json.dumps({{'lesson': {{**{lesson()!r}, 'lid': sys.argv[2]}}}}))\n"
    )
    monkeypatch.setattr(ingest_lib, "LESSONS_TIMEOUT_SECONDS", 0.3)
    start = time.monotonic()
    with pytest.raises(CandidatesError, match="時間切れ"):
        ingest(candidate_context)
    assert time.monotonic() - start < 2
    assert not candidate_context.store.directory.exists()


@pytest.mark.parametrize(
    "malformation",
    ["eid", "ts", "sid", "data", "failure_type", "error_excerpt", "bad-excerpt", "bad-kind"],
)
def test_ev57_missing_or_ill_typed_failure_fields_do_not_block_ingest(
    candidate_context, malformation
):
    malformed = failure()
    if malformation in {"eid", "ts", "sid", "data"}:
        del malformed[malformation]
    elif malformation in {"failure_type", "error_excerpt"}:
        del malformed["data"][malformation]
    elif malformation == "bad-excerpt":
        malformed["data"]["error_excerpt"] = []
    else:
        malformed["data"]["failure_type"] = {}
    path = write_failures(candidate_context, [malformed, failure(2)])
    result = ingest(candidate_context)
    assert result["ingested"] == 1
    assert result["skipped"] == 1
    assert sum(result["skip_reasons"].values()) == 1
    assert result["failure_offset"] == path.stat().st_size
