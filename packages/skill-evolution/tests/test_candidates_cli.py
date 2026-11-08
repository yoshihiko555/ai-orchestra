"""EV-29・55・59・61〜63・79: 2 本の CLI の公開インターフェース。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import candidates_model as model
import pytest
from candidates_view import listing, suggestions

from tests.module_loader import load_module

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "packages/skill-evolution/scripts/skill_candidates.py"
ADMIN = REPO / "packages/skill-evolution/scripts/skill_candidates_admin.py"
admin_module = load_module("phase2_candidates_admin", str(ADMIN))


def run_cli(arguments, *, admin=False, json_input=None):
    script = Path(os.environ["CLAUDE_PROJECT_DIR"]) / "packages/skill-evolution/scripts"
    return subprocess.run(
        [sys.executable, str(script / (ADMIN.name if admin else CLI.name)), *arguments],
        input=json.dumps(json_input) if json_input is not None else None,
        capture_output=True,
        text=True,
        cwd=os.environ["CLAUDE_PROJECT_DIR"],
        env=os.environ.copy(),
        timeout=15,
    )


def test_ev55_cli_human_attributes_to_run_after_boundary_or_skill(
    candidate_context, observation_event, write_runs, scope_event
):
    write_runs([scope_event(), scope_event("turn.stopped", "2026-11-01T10:00:30Z")])
    candidate_context.store.write(lambda _: [observation_event(skill=None)])
    result = run_cli(["attribute", "O-00000001", "--run-id", "R-000000000001"], admin=True)
    assert result.returncode == 0, result.stderr
    snapshot = candidate_context.store.read()
    assert snapshot["observations"]["O-00000001"]["attributed_by"] == "human"
    assert model.evidence_count(snapshot, ["O-00000001"]) == 1
    result = run_cli(["attribute", "O-00000001", "--skill", "review"], admin=True)
    assert result.returncode == 0, result.stderr
    assert candidate_context.store.read()["observations"]["O-00000001"]["run_id"] is None
    result = run_cli(["attribute", "O-00000001", "--run-id", "R-999999999999"], admin=True)
    assert result.returncode == 5


def test_ev59_cli_candidate_lifecycle_and_human_hypothesis(candidate_context, observation_event):
    candidate_context.store.write(lambda _: [observation_event()])
    result = run_cli(
        ["promote-observation", "O-00000001", "--json", "-"],
        admin=True,
        json_input={"title": "手順不足", "description": "検証を追加"},
    )
    assert result.returncode == 0, result.stderr
    cid = json.loads(result.stdout)["events"][0]["cid"]
    for args in (
        ["reject", cid, "--json", "-"],
        ["unreject", cid],
        ["archive", cid],
        ["restore", cid],
    ):
        result = run_cli(
            args, admin=True, json_input={"note": "対象外"} if args[0] == "reject" else None
        )
        assert result.returncode == 0, result.stderr
    result = run_cli(["show", cid, "--json"])
    assert result.returncode == 0, result.stderr
    candidate = json.loads(result.stdout)["candidate"]
    assert candidate["state"] == "candidate"
    assert candidate["hypothesis"] == {"title": "手順不足", "description": "検証を追加"}
    assert candidate["handoff_status"] == "受け渡し可能"
    assert run_cli(["restore", cid], admin=True).returncode == 5


def test_ev61_cli_nonfacet_candidate_is_displayed_as_ineligible(
    candidate_context, observation_event
):
    candidate_context.store.write(lambda _: [observation_event(skill="plugin:personal")])
    result = run_cli(
        ["promote-observation", "O-00000001", "--json", "-"],
        admin=True,
        json_input={"title": "不足", "description": "説明"},
    )
    assert result.returncode == 0, result.stderr
    result = run_cli(["list", "--json"])
    candidate = json.loads(result.stdout)["candidates"][0]
    assert candidate["handoff_status"] == "受け渡しの対象外"
    assert candidate["handoff_eligible"] is False


@pytest.mark.parametrize(
    ("args", "admin"),
    [
        (["show", "O-deadbeef", "--json"], False),
        (["show", "../escape"], False),
        (["not-a-command"], False),
        (["attribute", "O-00000001"], True),
        (["reject", "C-00000001"], True),
    ],
)
def test_ev29_unknown_ids_and_arguments_have_no_stacktrace(candidate_context, args, admin):
    result = run_cli(args, admin=admin)
    assert result.returncode == 5
    assert result.stderr
    assert "Traceback" not in result.stderr


def test_ev29_corrupt_ledger_lines_do_not_break_cli(candidate_context, observation_event):
    candidate_context.store.write(lambda _: [observation_event()])
    with (candidate_context.store.directory / "candidates.jsonl").open("a") as stream:
        stream.write('not-json\n{"v": 1}\n')
    result = run_cli(["list", "--json"])
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["observations"]) == 1


def test_ev62_admin_permission_error_returns_exit_three(candidate_context, monkeypatch, capsys):
    from candidates_safety import LedgerUnwritable

    monkeypatch.setattr(
        admin_module,
        "ingest",
        lambda _: (_ for _ in ()).throw(LedgerUnwritable("サンドボックスの外で実行してください")),
    )
    assert admin_module.main(["ingest"]) == 3
    assert "サンドボックスの外" in capsys.readouterr().err


def test_ev63_unknown_show_includes_earlier_scopes_and_audit_context(
    candidate_context, observation_event, scope_event, write_runs
):
    candidate_context.store.write(lambda _: [observation_event(skill=None)])
    write_runs(
        [
            scope_event(),
            scope_event(number=2, at="2026-11-01T09:59:00Z"),
            scope_event(number=3, at="2026-11-01T10:02:00Z"),
        ]
    )
    audit = candidate_context.root / ".claude/logs/audit/sessions/session-1234.jsonl"
    audit.parent.mkdir(parents=True)
    audit.write_text(
        json.dumps(
            {
                "ts": "2026-11-01T10:00:30Z",
                "sid": "session-1234",
                "type": "subagent_start",
                "data": {"agent_type": "<unsafe>\u202e", "task_summary": "must-not-copy"},
            }
        )
        + "\n"
    )
    result = run_cli(["show", "O-00000001", "--json"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [item["run_id"] for item in payload["candidate_scopes"]] == [
        "R-000000000001",
        "R-000000000002",
    ]
    assert payload["audit_context"][0]["agent_type"].startswith("‹unsafe›")
    assert "must-not-copy" not in result.stdout
    assert run_cli(["runs", "--sid", "session-1234", "--json"]).returncode == 0


def test_ev79_cross_session_similar_observations_are_first_and_suggestion_only(
    candidate_context, observation_event
):
    records = [
        observation_event(1),
        observation_event(2, contexts=[{"kind": "session", "ref": "session-5678"}]),
    ]
    snapshot = candidate_context.store.write(lambda _: records)
    proposed = suggestions(snapshot)
    assert len(proposed) == 1
    assert proposed[0]["suggestion_only"] is True
    assert not snapshot["candidates"]
    result = run_cli(["list", "--json"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (
        list(payload).index("suggestions")
        < list(payload).index("pending_confirmation")
        < list(payload).index("candidates")
    )
    assert payload["suggestions"][0]["label"] == "候補にするかの提案"


@pytest.mark.parametrize(
    "variation",
    ["same-session", "different-kind", "different-skill", "no-overlap", "unknown", "dismissed"],
)
def test_ev79_suggestion_requires_same_skill_kind_overlap_and_distinct_origins(
    candidate_context, observation_event, variation
):
    first, second = (
        observation_event(),
        observation_event(2, contexts=[{"kind": "pr", "ref": "owner/repo#2"}]),
    )
    if variation == "same-session":
        second["data"]["contexts"] = first["data"]["contexts"]
    elif variation == "different-kind":
        second["data"]["failure_kind"] = "lint_failure"
    elif variation == "different-skill":
        second["data"]["skill"] = "design"
    elif variation == "no-overlap":
        second["data"]["excerpt"] = "network timeout tcp connection"
    elif variation == "unknown":
        second = observation_event(2, skill=None)
    snapshot = model.apply_event(model.apply_event(model.empty_projection(), first), second)
    if variation == "dismissed":
        snapshot["observations"]["O-00000002"]["dismissed"] = True
    assert suggestions(snapshot) == []


def test_ev79_cjk_bigrams_and_pending_items_precede_state_groups(
    candidate_context, observation_event
):
    records = [
        observation_event(1, excerpt="検証手順の不足を確認"),
        observation_event(
            2, excerpt="検証手順の不足を確認する", contexts=[{"kind": "pr", "ref": "owner/repo#2"}]
        ),
        observation_event(3, skill=None),
    ]
    snapshot = candidate_context.store.write(lambda _: records)
    payload = listing(candidate_context, snapshot)
    assert payload["suggestions"]
    assert payload["pending_confirmation"]["unknown"][0]["oid"] == "O-00000003"


def test_ev59_manifest_declares_scripts_skill_and_library_files():
    manifest = json.loads((REPO / "packages/skill-evolution/manifest.json").read_text())
    paths = {script["path"] for script in manifest["scripts"]}
    assert {"scripts/skill_candidates.py", "scripts/skill_candidates_admin.py"} <= paths
    assert manifest["skills"] == ["skill-candidates"]
    for path in (REPO / "packages/skill-evolution/lib").glob("candidates_*.py"):
        assert f"lib/{path.name}" in manifest["files"]


def test_ev29_ingest_distinct_failure_exit_codes_and_no_tracebacks(candidate_context):
    exit_file = candidate_context.orchestra / "packages/fail-logs/scripts/exit-fixture"
    for code in (1, 2, 4):
        exit_file.write_text(str(code))
        result = run_cli(["ingest"], admin=True)
        assert result.returncode == code
        assert "Traceback" not in result.stderr
        assert result.stderr


def test_ev55_run_attribution_refuses_wrong_session_and_future_scope(
    candidate_context, observation_event, write_runs, scope_event
):
    write_runs(
        [scope_event(sid="different-session"), scope_event(number=2, at="2026-11-01T10:02:00Z")]
    )
    candidate_context.store.write(lambda _: [observation_event(skill=None)])
    for run_id in ("R-000000000001", "R-000000000002"):
        assert run_cli(["attribute", "O-00000001", "--run-id", run_id], admin=True).returncode == 5
    assert model.evidence_count(candidate_context.store.read(), ["O-00000001"]) == 0


def test_ev63_pending_states_appear_before_grouped_candidates(
    candidate_context, observation_event, candidate_event
):
    snapshot = model.apply_event(
        model.apply_event(model.empty_projection(), observation_event()), candidate_event()
    )
    snapshot["candidates"]["C-00000001"]["state"] = "improving"
    assert (
        listing(candidate_context, snapshot)["pending_confirmation"]["candidates"][0]["cid"]
        == "C-00000001"
    )


def test_ev59_admin_free_text_uses_stdin_json_without_shell_expansion(
    candidate_context, observation_event
):
    candidate_context.store.write(lambda _: [observation_event()])
    marker = candidate_context.project / "must-not-create"
    text = '確認済み $(touch must-not-create) `touch must-not-create` "引用"'
    result = run_cli(
        ["promote-observation", "O-00000001", "--json", "-"],
        admin=True,
        json_input={"title": text, "description": text + "\n説明"},
    )
    assert result.returncode == 0, result.stderr
    cid = json.loads(result.stdout)["events"][0]["cid"]
    assert candidate_context.store.read()["candidates"][cid]["hypothesis"]["title"] == text
    for command, object_id in (
        ("reject", cid),
        ("unreject", cid),
        ("archive", cid),
        ("restore", cid),
        ("dismiss", "O-00000001"),
    ):
        result = run_cli([command, object_id, "--json", "-"], admin=True, json_input={"note": text})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["events"][0]["data"]["note"] == text
    assert not marker.exists()


@pytest.mark.parametrize(
    "payload", [[], {"title": []}, {"title": "見出し", "description": {}}, {"note": "未知の項目"}]
)
def test_ev29_admin_invalid_text_json_is_rejected_without_append(
    candidate_context, observation_event, payload
):
    candidate_context.store.write(lambda _: [observation_event()])
    ledger = candidate_context.store.directory / "candidates.jsonl"
    before = ledger.read_bytes()
    result = run_cli(
        ["promote-observation", "O-00000001", "--json", "-"], admin=True, json_input=payload
    )
    assert result.returncode == 5
    assert "Traceback" not in result.stderr
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("projection", ["missing", "stale", "corrupt"])
def test_ev63_readonly_commands_catch_up_without_any_disk_writes(
    candidate_context, observation_event, scope_event, write_runs, projection
):
    import hashlib

    store = candidate_context.store
    write_runs([scope_event()])
    first = observation_event()
    ledger = store.directory / "candidates.jsonl"
    if projection == "stale":
        store.write(lambda _: [first])
    elif projection == "corrupt":
        (store.directory / "candidates-state.json").write_text("broken")
    with ledger.open("a") as stream:
        if projection != "stale":
            stream.write(json.dumps(first) + "\n")
        stream.write(json.dumps(observation_event(2)) + "\n")
    lock = store.directory / "candidates.lock"
    lock.unlink(missing_ok=True)

    def fingerprint():
        return {
            str(path.relative_to(store.directory)): (
                path.stat().st_mode,
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in store.directory.iterdir()
            if path.is_file()
        }

    before = fingerprint()
    for arguments in (["list", "--json"], ["show", "O-00000002", "--json"], ["runs", "--json"]):
        result = run_cli(arguments)
        assert result.returncode == 0, result.stderr
        if arguments[0] == "list":
            assert len(json.loads(result.stdout)["observations"]) == 2
        assert fingerprint() == before
        assert not lock.exists()


def test_ev59_cli_merge_refuses_non_candidate_states_without_append(
    candidate_context, observation_event, candidate_event
):
    store = candidate_context.store
    store.write(
        lambda _: [observation_event(), candidate_event(), observation_event(2), candidate_event(2)]
    )
    result = run_cli(
        ["reject", "C-00000002", "--json", "-"], admin=True, json_input={"note": "確認済み"}
    )
    assert result.returncode == 0, result.stderr
    ledger = store.directory / "candidates.jsonl"
    before = ledger.read_bytes()
    for source, target in (("C-00000001", "C-00000002"), ("C-00000002", "C-00000001")):
        result = run_cli(["merge", source, "--into", target], admin=True)
        assert result.returncode == 5
        assert ledger.read_bytes() == before


def test_ev62_lock_timeout_keeps_exit_one_with_distinct_message(
    candidate_context, monkeypatch, capsys
):
    from candidates_safety import LockTimeout

    monkeypatch.setattr(admin_module, "ingest", lambda _: (_ for _ in ()).throw(LockTimeout()))
    assert admin_module.main(["ingest"]) == 1
    message = capsys.readouterr().err
    assert "ロック" in message and "待ち時間切れ" in message
    assert "Traceback" not in message


def test_ev57_ingest_cli_json_reports_skipped_record_reasons(candidate_context):
    path = candidate_context.root / ".claude/logs/fail-logs/failures.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n")
    result = run_cli(["ingest", "--json"], admin=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ingested"] == 0
    assert payload["skipped"] == 1
    assert payload["skip_reasons"]
    assert payload["failure_offset"] == path.stat().st_size
