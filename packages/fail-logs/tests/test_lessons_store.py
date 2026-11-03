"""教訓候補の台帳（lib/lessons_model.py・lib/lessons_store.py）を検証する。

評価セット docs/evaluation/fail-logs.md の EV-57〜EV-69・EV-72・EV-73 に対応する（設計 §8）。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import lessons_helpers as h
import pytest
from sanitize import ValidationError

model = h.model
store_lib = h.store_lib


def _ledger_lines(project) -> list[dict]:
    path = h.log_dir(project) / store_lib.LEDGER_FILE
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# EV-57: 候補は事象・原因・回避策・根拠（日時）・再発回数・重大度・対象範囲・状態（日時）を持つ。
def test_lesson_has_required_fields(tmp_path) -> None:
    project = h.make_project(tmp_path)
    event = h.created_event()
    snapshot = h.append(h.new_store(project), event)

    lesson = snapshot["lessons"][event["lid"]]
    for key in (
        "title",
        "event",
        "cause",
        "avoidance",
        "recurrence",
        "severity",
        "scope",
        "state",
        "state_changed_at",
    ):
        assert key in lesson
    assert lesson["evidence"][0]["ts"]
    assert lesson["state"] == "candidate"


# EV-58・EV-59: 台帳・写像・一覧はテキストで、ディレクトリ 0700・ファイル 0600 で作られる。
def test_files_are_text_and_owner_only(tmp_path) -> None:
    project = h.make_project(tmp_path)
    h.append(h.new_store(project), h.created_event())

    directory = h.log_dir(project)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in (store_lib.LEDGER_FILE, store_lib.STATE_FILE, store_lib.MARKDOWN_FILE):
        path = directory / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.read_text(encoding="utf-8")


# EV-59: 台帳が symlink なら、たどらずに失敗する（書き込み先を差し替えられない）。
def test_symlinked_ledger_is_not_followed(tmp_path) -> None:
    project = h.make_project(tmp_path)
    directory = h.log_dir(project)
    directory.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("")
    os.symlink(outside, directory / store_lib.LEDGER_FILE)

    with pytest.raises(OSError):
        h.append(h.new_store(project), h.created_event())
    assert outside.read_text() == ""


# EV-60: 台帳に入れる文字列は伏せてから入れ、台帳・写像・一覧に機密情報の原文が残らない。
def test_secrets_never_reach_ledger_state_or_markdown(tmp_path) -> None:
    project = h.make_project(tmp_path)
    h.append(h.new_store(project), h.created_event(avoidance=f"use {h.API_KEY_SAMPLE} carefully"))

    for name in (store_lib.LEDGER_FILE, store_lib.STATE_FILE, store_lib.MARKDOWN_FILE):
        assert h.API_KEY_SAMPLE not in (h.log_dir(project) / name).read_text()


# EV-61: 異なるセッションから 2 回合流すると、候補は 1 件・再発回数 2・根拠 2 件。
def test_merge_from_two_sessions(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event(session="sess-aaaa-0001")
    h.append(store, created)
    snapshot = h.append(store, h.evidence_event(created["lid"], ref="sess-bbbb-0002"))

    assert len(snapshot["lessons"]) == 1
    lesson = snapshot["lessons"][created["lid"]]
    assert lesson["recurrence"] == 2
    assert len(lesson["evidence"]) == 2


# EV-62: 同じセッション・同じ PR から何度合流しても再発回数は増えない。
def test_same_evidence_does_not_increase_recurrence(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event(session="sess-aaaa-0001")
    h.append(store, created)
    h.append(store, h.evidence_event(created["lid"], ref="sess-aaaa-0001"))
    h.append(
        store, h.evidence_event(created["lid"], kind="pr", ref="owner/repo#12", actor="review")
    )
    snapshot = h.append(
        store, h.evidence_event(created["lid"], kind="pr", ref="owner/repo#12", actor="review")
    )

    assert snapshot["lessons"][created["lid"]]["recurrence"] == 2


# EV-63: 昇格中・昇格済み・却下の候補にも合流し、状態はそのまま。
@pytest.mark.parametrize("path", [["rejected"], ["promoting"], ["promoted"]])
def test_merge_into_non_candidate_states(tmp_path, path: list[str]) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    snapshot = h.append(store, created)
    for state in path:
        snapshot = h.append(store, h.state_event(snapshot, created["lid"], state))
    snapshot = h.append(store, h.evidence_event(created["lid"], ref="sess-cccc-0003"))

    lesson = snapshot["lessons"][created["lid"]]
    assert lesson["state"] == path[-1]
    assert lesson["recurrence"] == 2


# EV-64: kind: agent の根拠と、確かめられない spool の根拠は数えない。確かめられた spool の根拠は数える。
def test_self_reported_evidence_is_not_counted(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    h.append(store, h.evidence_event(created["lid"], kind="agent", ref="agent-1234", actor="agent"))
    unverified = h.evidence_event(created["lid"], ref="sess-dddd-0004", actor="spool")
    snapshot = h.append(store, unverified)
    assert snapshot["lessons"][created["lid"]]["recurrence"] == 1

    verified = h.evidence_event(created["lid"], ref="sess-eeee-0005", actor="spool")
    verified["data"]["evidence"]["verified"] = True
    snapshot = h.append(store, verified)
    assert snapshot["lessons"][created["lid"]]["recurrence"] == 2


# EV-65: 重大度は高い方へ・対象範囲は広い方へだけ変わり、人が確定した値は変わらず確認の印が立つ。
def test_severity_and_scope_only_escalate(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event(severity="critical", scope="all")
    h.append(store, created)
    snapshot = h.append(store, h.evidence_event(created["lid"], severity="normal", scope="claude"))
    lesson = snapshot["lessons"][created["lid"]]
    assert (lesson["severity"], lesson["scope"]) == ("critical", "all")

    revised = model.make_event(
        model.EV_REVISED,
        created["lid"],
        "skill",
        "",
        model.build_revised_data({"severity": "normal"}, h.strong_masker()),
    )
    h.append(store, revised)
    snapshot = h.append(
        store, h.evidence_event(created["lid"], ref="sess-ffff-0006", severity="critical")
    )
    lesson = snapshot["lessons"][created["lid"]]
    assert lesson["severity"] == "normal"
    assert lesson["severity_alert"] is True


# EV-65: spool から取り込んだ重大度・対象範囲は写像に反映せず、提案として残す。
def test_spool_severity_is_only_proposed(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event(actor="spool", severity="critical", scope="all")
    snapshot = h.append(store, created)

    lesson = snapshot["lessons"][created["lid"]]
    assert lesson["severity"] == "normal"
    assert lesson["scope"] == "claude"
    assert lesson["proposed_severity"] == "critical"


# EV-66: 複数のプロセスが同時に追加しても、全候補が読めて再発回数が追加した数と一致する。
def test_concurrent_processes_do_not_lose_updates(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    script = (
        "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2], sys.argv[3]];"
        "import lessons_helpers as h;"
        "store = h.new_store(__import__('pathlib').Path(sys.argv[4]));"
        "h.append(store, h.evidence_event(sys.argv[5], ref=sys.argv[6]))"
    )
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(h.REPO_ROOT),
                tests_dir,
                str(h.LIB_DIR),
                str(project),
                created["lid"],
                f"sess-proc-{index:04d}",
            ],
            cwd=str(h.REPO_ROOT),
        )
        for index in range(8)
    ]
    assert all(process.wait(timeout=60) == 0 for process in processes)

    snapshot = store.read()
    assert snapshot["lessons"][created["lid"]]["recurrence"] == 9
    assert len(_ledger_lines(project)) == 9
    assert store.rebuild()["lessons"][created["lid"]]["recurrence"] == 9


# EV-66: 追記の後・写像の置換の前に落ちても、次の書き手が追いつく。
def test_next_writer_catches_up_after_crash(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    orphan = h.evidence_event(created["lid"], ref="sess-orph-0001")
    with open(h.log_dir(project) / store_lib.LEDGER_FILE, "a") as handle:
        handle.write(json.dumps(orphan) + "\n")

    snapshot = h.append(store, h.evidence_event(created["lid"], ref="sess-next-0002"))

    assert snapshot["lessons"][created["lid"]]["recurrence"] == 3


# EV-67: 写像が無い・壊れている・位置が台帳より先なら、台帳の全体から作り直す。
@pytest.mark.parametrize("damage", ["missing", "corrupt", "offset_ahead"])
def test_snapshot_is_rebuilt_when_damaged(tmp_path, damage: str) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    state_path = h.log_dir(project) / store_lib.STATE_FILE
    if damage == "missing":
        state_path.unlink()
    elif damage == "corrupt":
        state_path.write_text("{not json")
    else:
        data = json.loads(state_path.read_text())
        data["ledger_offset"] = 10**9
        data["lessons"] = {}
        state_path.write_text(json.dumps(data))

    snapshot = store.read()

    assert created["lid"] in snapshot["lessons"]


# EV-67: 台帳の末尾が改行で終わっていなければ、改行を足してから追記する。
def test_partial_trailing_line_is_isolated(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    with open(h.log_dir(project) / store_lib.LEDGER_FILE, "a") as handle:
        handle.write('{"broken": tru')

    snapshot = h.append(store, h.evidence_event(created["lid"], ref="sess-tail-0001"))

    assert snapshot["lessons"][created["lid"]]["recurrence"] == 2
    lines = (h.log_dir(project) / store_lib.LEDGER_FILE).read_text().splitlines()
    assert lines[1] == '{"broken": tru'
    assert store.rebuild()["lessons"][created["lid"]]["recurrence"] == 2


# EV-68: §10.3 の表に無い遷移を拒否し、表にある遷移は受け付ける。
def test_state_transitions(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    snapshot = h.append(store, created)
    with pytest.raises(model.InvalidTransition):
        h.append(store, h.state_event(snapshot, created["lid"], "candidate"))
    for to in ("promoting", "candidate", "rejected", "candidate", "promoted", "candidate"):
        snapshot = h.append(store, h.state_event(snapshot, created["lid"], to))
        assert snapshot["lessons"][created["lid"]]["state"] == to
    snapshot = h.append(store, h.state_event(snapshot, created["lid"], "rejected"))
    with pytest.raises(model.InvalidTransition):
        h.append(store, h.state_event(snapshot, created["lid"], "promoted"))


# EV-68: 遷移の検査に失敗したとき、台帳には何も足さない。
def test_rejected_transition_leaves_ledger_untouched(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    snapshot = h.append(store, created)
    bad = model.make_event(
        model.EV_STATE,
        created["lid"],
        "skill",
        "",
        {"from": "candidate", "to": "promoted", "target": None},
    )
    bad["data"]["from"] = "promoting"
    with pytest.raises(ValidationError):
        h.append(store, bad)
    assert len(_ledger_lines(project)) == 1
    assert snapshot["lessons"][created["lid"]]["state"] == "candidate"


# EV-69: 文字種（ゼロ幅・双方向制御・タグ文字・制御文字）を含む入力を拒否する。
@pytest.mark.parametrize("bad", ["a​b", "a‮b", "a\U000e0041b", "a\x07b", "a b"])
def test_rejected_characters(bad: str) -> None:
    with pytest.raises(ValidationError):
        model.build_lesson_data(
            h.lesson_payload(title=bad),
            h.strong_masker(),
            source="agent",
            evidence={"kind": "agent", "ref": "agent-1"},
        )


# EV-69: 根拠の参照の形を kind ごとに検査する。
@pytest.mark.parametrize(
    ("kind", "ref"),
    [
        ("session", "short"),
        ("session", "../../etc"),
        ("pr", "owner/repo"),
        ("pr", "owner/repo#0"),
        ("other", "x"),
    ],
)
def test_invalid_evidence_refs(kind: str, ref: str) -> None:
    with pytest.raises(ValidationError):
        model.normalize_evidence({"kind": kind, "ref": ref})


# EV-72: spool の取り込みは種類を限り、actor・source を固定し、同じ eid は 1 回だけ適用する。
def test_spool_import_rules(tmp_path, monkeypatch) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    spool = tmp_path / "wt" / "spool"
    evidence = h.evidence_event(created["lid"], ref="sess-spool-001", actor="agent")
    evidence["data"]["source"] = "review"
    store_lib.write_spool(str(spool), {**evidence, "eid": "a" * 32})
    forbidden = model.make_event(
        model.EV_STATE,
        created["lid"],
        "skill",
        "",
        {"from": "candidate", "to": "rejected", "target": None},
    )
    store_lib.write_spool(str(spool), {**forbidden, "eid": "b" * 32})
    monkeypatch.setattr(store_lib, "transcript_roots", lambda: [])

    imported = store_lib.import_spool(store, [str(spool)], h.strong_masker())
    assert imported == 1
    assert list(spool.iterdir()) == []

    store_lib.write_spool(str(spool), {**evidence, "eid": "a" * 32})
    store_lib.import_spool(store, [str(spool)], h.strong_masker())
    snapshot = store.read()
    lesson = snapshot["lessons"][created["lid"]]
    assert lesson["state"] == "candidate"
    spooled = [item for item in lesson["evidence"] if item["eid"] == "a" * 32]
    assert len(spooled) == 1
    assert spooled[0]["counted"] is False
    entries = [line for line in _ledger_lines(project) if line["eid"] == "a" * 32]
    assert all(
        entry["actor"] == "spool" and entry["data"]["source"] == "agent" for entry in entries
    )


# EV-72・EV-64: spool のセッションの根拠は、記録が許可するルートにあるときだけ数える。
def test_spool_session_evidence_verified_by_transcript(tmp_path, monkeypatch) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    roots = tmp_path / "projects"
    (roots / "proj").mkdir(parents=True)
    (roots / "proj" / "sess-real-0001.jsonl").write_text("{}\n")
    monkeypatch.setattr(store_lib, "transcript_roots", lambda: [str(roots)])
    spool = tmp_path / "spool"
    event = h.evidence_event(created["lid"], ref="sess-real-0001", actor="agent")
    store_lib.write_spool(str(spool), {**event, "eid": "c" * 32})

    store_lib.import_spool(store, [str(spool)], h.strong_masker())

    assert store.read()["lessons"][created["lid"]]["recurrence"] == 2


# EV-73: 通常ファイルでない・大きすぎる spool のファイルは読まずに消す。
def test_spool_rejects_fifo_symlink_and_large_files(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    spool = tmp_path / "spool"
    spool.mkdir()
    os.mkfifo(spool / ("d" * 32 + ".json"))
    target = tmp_path / "target.json"
    target.write_text("{}")
    os.symlink(target, spool / ("e" * 32 + ".json"))
    (spool / ("f" * 32 + ".json")).write_text("x" * (store_lib.SPOOL_MAX_FILE_BYTES + 1))

    assert store_lib.import_spool(store, [str(spool)], h.strong_masker()) == 0
    assert list(spool.iterdir()) == []
    assert target.exists()


# EV-67（レビュー指摘）: 壊れた出来事（日時が不正・根拠の形が不正）が 1 行あっても、読み取りと作り直しは止まらない。
@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update({"ts": "x"}),
        lambda event: event["data"].update({"evidence": {"kind": "session"}}),
    ],
)
def test_malformed_event_is_skipped(tmp_path, mutate) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    broken = h.evidence_event(created["lid"], ref="sess-bad-00001")
    mutate(broken)
    with open(h.log_dir(project) / store_lib.LEDGER_FILE, "a") as handle:
        handle.write(json.dumps(broken) + "\n")

    snapshot = store.read()
    assert snapshot["lessons"][created["lid"]]["recurrence"] == 1
    assert store.rebuild()["lessons"][created["lid"]]["recurrence"] == 1
    later = h.append(store, h.evidence_event(created["lid"], ref="sess-ok-000002"))
    assert later["lessons"][created["lid"]]["recurrence"] == 2


# EV-66・EV-67（レビュー指摘）: 改行だけ欠けた完全な行があっても、生きている写像と作り直しの結果が一致する。
def test_complete_line_without_newline_matches_rebuild(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    pending = h.evidence_event(created["lid"], ref="sess-nonl-0001")
    with open(h.log_dir(project) / store_lib.LEDGER_FILE, "a") as handle:
        handle.write(json.dumps(pending))

    live = h.append(store, h.evidence_event(created["lid"], ref="sess-nonl-0002"))

    assert live["lessons"][created["lid"]]["recurrence"] == 3
    assert store.rebuild()["lessons"][created["lid"]]["recurrence"] == 3


# EV-72（レビュー指摘）: 同じ eid の再取り込みは、根拠だけでなく変更の記録・日ごとの件数も増やさない。
def test_reapplying_same_eid_is_idempotent(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    event = h.evidence_event(created["lid"], ref="sess-idem-0001", actor="agent")
    event["eid"] = "1" * 32
    first = h.append(store, event)
    changes = len(first["lessons"][created["lid"]]["changes"])
    second = h.append(store, dict(event))

    assert len(second["lessons"][created["lid"]]["changes"]) == changes
    assert second["daily_adds"] == first["daily_adds"]


# EV-73（レビュー指摘）: spool のディレクトリ自体が symlink なら、たどらず、その先のファイルを消さない。
def test_symlinked_spool_dir_is_not_followed(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / ("a" * 32 + ".json")).write_text("{}")
    (victim / "settings.json").write_text("{}")
    os.symlink(victim, tmp_path / "spool")

    assert store_lib.import_spool(store, [str(tmp_path / "spool")], h.strong_masker()) == 0
    assert sorted(p.name for p in victim.iterdir()) == ["a" * 32 + ".json", "settings.json"]


# EV-73（レビュー指摘）: 決まった形でない名前のファイルは消さない。
def test_spool_ignores_unexpected_names(tmp_path) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "settings.json").write_text("{}")

    store_lib.import_spool(store, [str(spool)], h.strong_masker())

    assert (spool / "settings.json").exists()


# EV-72（レビュー指摘）: 不正な spool のファイル（日時のあふれ等）は消して、ほかのファイルの取り込みを止めない。
def test_bad_spool_file_does_not_block_others(tmp_path, monkeypatch) -> None:
    project = h.make_project(tmp_path)
    store = h.new_store(project)
    created = h.created_event()
    h.append(store, created)
    spool = tmp_path / "spool"
    bad = h.evidence_event(created["lid"], ref="sess-bad-00001", actor="agent")
    bad["data"]["evidence"]["ts"] = "0001-01-01T00:00:00+05:00"
    bad["data"]["title"] = 12345
    store_lib.write_spool(str(spool), {**bad, "eid": "0" * 32})
    good = h.evidence_event(created["lid"], ref="sess-good-0001", actor="agent")
    store_lib.write_spool(str(spool), {**good, "eid": "f" * 32})
    monkeypatch.setattr(store_lib, "transcript_roots", lambda: [])

    assert store_lib.import_spool(store, [str(spool)], h.strong_masker()) == 1
    assert list(spool.iterdir()) == []
