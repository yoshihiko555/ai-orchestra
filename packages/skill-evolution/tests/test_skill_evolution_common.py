"""skill_evolution_common（決定論ロジック）の unit test。"""

from __future__ import annotations

import os

from tests.module_loader import load_module

se = load_module("skill_evolution_common", "packages/skill-evolution/lib/skill_evolution_common.py")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_load_config_merges_defaults(tmp_path) -> None:
    cfg = se.load_config(str(tmp_path))
    assert cfg["offline"]["max_iterations"] == 10
    assert cfg["lessons"]["max_lines"] == 40
    assert cfg["enabled"] is True


# ---------------------------------------------------------------------------
# metrics I/O
# ---------------------------------------------------------------------------


def test_append_and_read_metrics_roundtrip(tmp_path) -> None:
    p = str(tmp_path)
    se.append_metric(p, "issue-fix", {"run_id": "a", "success": True})
    se.append_metric(p, "issue-fix", {"run_id": "b", "success": False})
    records = se.read_metrics(p, "issue-fix")
    assert [r["run_id"] for r in records] == ["a", "b"]


def test_read_metrics_skips_broken_lines(tmp_path) -> None:
    p = str(tmp_path)
    path = se.metrics_path(p, "s")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"run_id": "ok"}\n')
        f.write("not-json\n")
        f.write("\n")
    assert [r["run_id"] for r in se.read_metrics(p, "s")] == ["ok"]


# ---------------------------------------------------------------------------
# lessons ＋ 肥大化管理
# ---------------------------------------------------------------------------


def test_append_lesson_rotates_to_archive(tmp_path) -> None:
    p = str(tmp_path)
    cfg = {"lessons": {"max_lines": 2}}
    se.append_lesson(p, "s", "first", cfg)
    se.append_lesson(p, "s", "second", cfg)
    se.append_lesson(p, "s", "third", cfg)
    assert se.lessons_count(p, "s", cfg) == 2
    text = se.read_lessons(p, "s", cfg)
    assert "third" in text and "second" in text and "first" not in text

    assert os.path.isfile(se.lessons_archive_path(p, "s", cfg))
    with open(se.lessons_archive_path(p, "s", cfg), encoding="utf-8") as f:
        assert "first" in f.read()


# ---------------------------------------------------------------------------
# [critical] ＋ success
# ---------------------------------------------------------------------------


def test_parse_critical_items() -> None:
    text = (
        "# Lessons\n\n## [critical] チェックリスト\n"
        "- [ ] 変更が壊れない\n- [x] テストが通る\n\n## 学び（新しい順）\n- 2026: x\n"
    )
    assert se.parse_critical_items(text) == ["変更が壊れない", "テストが通る"]


def test_compute_success_and_pass_rate() -> None:
    assert se.compute_success({"a": True, "b": True}) is True
    assert se.compute_success({"a": True, "b": False}) is False
    assert se.compute_success({}) is False
    assert se.critical_pass_rate({"a": True, "b": False}) == 0.5
    assert se.critical_pass_rate({}) == 0.0


# ---------------------------------------------------------------------------
# 自己申告パース ＋ レコード組み立て
# ---------------------------------------------------------------------------


def test_parse_self_report_valid_and_last_wins() -> None:
    text = (
        'noise [skill-self-report]{"run_id": "1", "ambiguities": 1}[/skill-self-report] '
        '[skill-self-report]{"run_id": "2", "ambiguities": 0}[/skill-self-report]'
    )
    got = se.parse_self_report(text)
    assert got["run_id"] == "2"


def test_parse_self_report_missing_or_malformed() -> None:
    assert se.parse_self_report("no block here") is None
    assert se.parse_self_report("[skill-self-report]not json[/skill-self-report]") is None
    assert se.parse_self_report("") is None


def test_build_metric_record_with_self_report() -> None:
    sr = {"run_id": "r", "ambiguities": 1, "critical": {"a": True, "b": False}}
    rec = se.build_metric_record("s", "r", sr, duration_ms=1200)
    assert rec["machine"]["critical_pass_rate"] == 0.5
    assert rec["success"] is False
    assert rec["self_report"]["ambiguities"] == 1
    assert rec["machine"]["duration_ms"] == 1200


def test_build_metric_record_without_self_report() -> None:
    rec = se.build_metric_record("s", "r", None, duration_ms=None)
    assert rec["self_report"] is None
    assert rec["success"] is False
    assert rec["machine"]["critical_pass_rate"] == 0.0


# EV-30: metrics 1 行の既存スキーマと型を後方互換契約として固定する。
def test_build_metric_record_schema_is_backward_compatible() -> None:
    rec = se.build_metric_record(
        "issue-fix",
        "run-1",
        {
            "ambiguities": 1,
            "discretion_fills": 2,
            "retries": 3,
            "critical": {"done": True},
        },
        duration_ms=1200,
        tool_uses=7,
    )

    assert set(rec) == {"ts", "skill", "run_id", "self_report", "machine", "success"}
    assert isinstance(rec["ts"], str)
    assert isinstance(rec["skill"], str)
    assert isinstance(rec["run_id"], str)
    assert isinstance(rec["self_report"], dict)
    assert set(rec["self_report"]) == {"ambiguities", "discretion_fills", "retries"}
    assert all(isinstance(value, int) for value in rec["self_report"].values())
    assert isinstance(rec["machine"], dict)
    assert set(rec["machine"]) == {"tool_uses", "duration_ms", "critical_pass_rate"}
    assert isinstance(rec["machine"]["tool_uses"], int)
    assert isinstance(rec["machine"]["duration_ms"], int)
    assert isinstance(rec["machine"]["critical_pass_rate"], float)
    assert isinstance(rec["success"], bool)


# ---------------------------------------------------------------------------
# スコアリング
# ---------------------------------------------------------------------------


def test_score_run_full_and_penalised() -> None:
    full = {
        "machine": {"critical_pass_rate": 1.0},
        "self_report": {"ambiguities": 0, "discretion_fills": 0, "retries": 0},
    }
    assert se.score_run(full) == 100.0
    pen = {
        "machine": {"critical_pass_rate": 1.0},
        "self_report": {"ambiguities": 1, "discretion_fills": 0, "retries": 0},
    }
    assert se.score_run(pen) == 95.0


def test_score_run_without_self_report_uses_machine_only() -> None:
    rec = {"machine": {"critical_pass_rate": 0.5}, "self_report": None}
    assert se.score_run(rec) == 50.0


def test_summarize() -> None:
    records = [
        {
            "success": True,
            "machine": {"critical_pass_rate": 1.0, "tool_uses": 10, "duration_ms": 1000},
            "self_report": {"ambiguities": 0, "discretion_fills": 0, "retries": 0},
        },
        {
            "success": False,
            "machine": {"critical_pass_rate": 0.0, "tool_uses": 20, "duration_ms": 2000},
            "self_report": None,
        },
    ]
    s = se.summarize(records)
    assert s["count"] == 2
    assert s["success_rate"] == 0.5
    assert s["avg_steps"] == 15.0


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_detect_provenance_with_managed_set() -> None:
    managed = {"issue-fix", "review"}
    assert se.detect_provenance("issue-fix", managed) == se.FACET
    assert se.detect_provenance("my-custom", managed) == se.NON_FACET


def test_detect_provenance_unknown_when_unresolvable(monkeypatch) -> None:
    monkeypatch.setenv("AI_ORCHESTRA_DIR", "")
    assert se.detect_provenance("x", None) == se.UNKNOWN


def test_resolve_reflection_target() -> None:
    assert se.resolve_reflection_target(se.FACET) == "facet"
    assert se.resolve_reflection_target(se.NON_FACET) == "lessons_skill_md"
    assert se.resolve_reflection_target(se.UNKNOWN) == "lessons_only"


def test_detect_provenance_via_agents_skills_fallback(tmp_path, monkeypatch) -> None:
    # AI_ORCHESTRA_DIR 無しでも .agents/skills/ 生成物で facet 判別できる
    monkeypatch.setenv("AI_ORCHESTRA_DIR", "")
    (tmp_path / ".agents" / "skills" / "issue-fix").mkdir(parents=True)
    assert se.detect_provenance("issue-fix", project_dir=str(tmp_path)) == se.FACET
    assert se.detect_provenance("my-own", project_dir=str(tmp_path)) == se.NON_FACET


def test_recent_run_ids(tmp_path) -> None:
    p = str(tmp_path)
    for i in range(5):
        se.append_metric(p, "s", {"run_id": f"r{i}", "success": True})
    ids = se.recent_run_ids(p, "s")
    assert ids == {"r0", "r1", "r2", "r3", "r4"}
    assert se.recent_run_ids(p, "missing") == set()


# ---------------------------------------------------------------------------
# レビュー反映（PR #105）
# ---------------------------------------------------------------------------


def test_extract_text_preserves_self_report_block() -> None:
    # dict/list を json.dumps せず文字列葉を取り出すため、埋め込みブロックが読める
    resp = {
        "content": [
            {
                "text": 'done [skill-self-report]{"run_id": "r", "ambiguities": 0}[/skill-self-report]'
            }
        ]
    }
    sr = se.parse_self_report(se.extract_text(resp))
    assert sr is not None and sr["run_id"] == "r"


def test_extract_text_types() -> None:
    assert se.extract_text("hi") == "hi"
    assert se.extract_text(None) == ""
    assert se.extract_text({"a": "x", "b": ["y", 1]}) == "x\ny\n1"


def test_consume_pending_by_run_id(tmp_path) -> None:
    p = str(tmp_path)
    rid = "s-20260101T000000-abcd"
    se.write_pending(p, rid)
    got_id, dur = se.consume_pending(p, rid, "s")
    assert got_id == rid and dur is not None and dur >= 0
    # consume 済み → 2 回目は None
    _id2, dur2 = se.consume_pending(p, rid, "s")
    assert dur2 is None


def test_consume_pending_fallback_by_skill(tmp_path) -> None:
    p = str(tmp_path)
    rid = "issue-fix-20260101T000000-aaaa"
    se.write_pending(p, rid)
    got_id, dur = se.consume_pending(p, "", "issue-fix")  # run_id 不明でも skill で拾う
    assert got_id == rid and dur is not None


# ---------------------------------------------------------------------------
# pending 一覧 / skill 保存 / 破棄（Stop hook フォールバック用）
# ---------------------------------------------------------------------------


def test_write_pending_stores_skill(tmp_path) -> None:
    p = str(tmp_path)
    rid = "issue-fix-20260101T000000-aaaa"
    se.write_pending(p, rid, skill="issue-fix")
    entries = se.list_pending(p)
    assert len(entries) == 1
    assert entries[0]["run_id"] == rid
    assert entries[0]["skill"] == "issue-fix"


def test_list_pending_recovers_skill_from_run_id_when_missing(tmp_path) -> None:
    p = str(tmp_path)
    rid = se.gen_run_id("issue-fix")
    se.write_pending(p, rid)  # skill 未指定（旧形式 pending の想定）
    entries = se.list_pending(p)
    assert entries[0]["skill"] == "issue-fix"  # run_id から復元


def test_list_pending_skips_broken_files(tmp_path) -> None:
    p = str(tmp_path)
    se.write_pending(p, "s-20260101T000000-aaaa", skill="s")
    pending_dir = os.path.join(se.logs_dir(p), "pending")
    with open(os.path.join(pending_dir, "broken.json"), "w", encoding="utf-8") as f:
        f.write("not json")
    entries = se.list_pending(p)
    assert len(entries) == 1


def test_discard_pending_removes_file(tmp_path) -> None:
    p = str(tmp_path)
    rid = "s-20260101T000000-aaaa"
    se.write_pending(p, rid, skill="s")
    entries = se.list_pending(p)
    se.discard_pending(entries[0]["path"])
    assert se.list_pending(p) == []


def test_discard_pending_missing_file_does_not_raise(tmp_path) -> None:
    se.discard_pending(os.path.join(str(tmp_path), "does-not-exist.json"))  # クラッシュしない


# ---------------------------------------------------------------------------
# 複数自己申告ブロックのパース（Stop hook の transcript 走査用）
# ---------------------------------------------------------------------------


def test_parse_self_reports_returns_all_in_order() -> None:
    text = (
        '[skill-self-report]{"run_id": "1", "skill": "a"}[/skill-self-report] '
        '[skill-self-report]{"run_id": "2", "skill": "b"}[/skill-self-report]'
    )
    reports = se.parse_self_reports(text)
    assert [r["run_id"] for r in reports] == ["1", "2"]


def test_parse_self_reports_skips_malformed_blocks() -> None:
    text = (
        "[skill-self-report]not json[/skill-self-report] "
        '[skill-self-report]{"run_id": "ok"}[/skill-self-report]'
    )
    reports = se.parse_self_reports(text)
    assert [r["run_id"] for r in reports] == ["ok"]


def test_parse_self_reports_empty_when_no_block() -> None:
    assert se.parse_self_reports("no block here") == []
    assert se.parse_self_reports("") == []


def test_parse_self_report_still_returns_last_wins() -> None:
    # 既存の外部挙動（最後のブロック優先）は parse_self_reports へのリファクタ後も変わらない。
    text = (
        '[skill-self-report]{"run_id": "1"}[/skill-self-report] '
        '[skill-self-report]{"run_id": "2"}[/skill-self-report]'
    )
    assert se.parse_self_report(text)["run_id"] == "2"


def test_summarize_skips_null_metrics() -> None:
    recs = [
        {
            "success": True,
            "machine": {"critical_pass_rate": 1.0, "tool_uses": None, "duration_ms": None},
            "self_report": None,
        },
        {
            "success": True,
            "machine": {"critical_pass_rate": 1.0, "tool_uses": 10, "duration_ms": 100},
            "self_report": None,
        },
    ]
    s = se.summarize(recs)
    assert s["avg_steps"] == 10.0  # None を除外して 10 のみで平均
    all_null = se.summarize(
        [{"success": True, "machine": {"critical_pass_rate": 1.0}, "self_report": None}]
    )
    assert all_null["avg_steps"] is None


def test_deep_merge_preserves_unset_keys() -> None:
    base = {"a": 1, "b": {"x": 1, "y": 2}}
    merged = se._deep_merge(base, {"b": {"y": 9}})
    assert merged == {"a": 1, "b": {"x": 1, "y": 9}}
    assert base["b"]["y"] == 2  # 入力は不変


def test_build_metric_record_populates_tool_uses() -> None:
    rec = se.build_metric_record(
        "s", "r", {"run_id": "r", "critical": {"a": True}}, 100, tool_uses=7
    )
    assert rec["machine"]["tool_uses"] == 7


# ---------------------------------------------------------------------------
# 停止条件 ＋ 3 ガード
# ---------------------------------------------------------------------------


def _rec(score, steps=10.0, time_ms=1000.0, holdout=0.0, cost=0.0):
    return se.IterationRecord(
        score=score, steps=steps, time_ms=time_ms, holdout_score=holdout, cost_usd=cost
    )


def test_evaluate_stop_empty() -> None:
    assert se.evaluate_stop([], {}).should_stop is False


def test_evaluate_stop_max_iterations() -> None:
    cfg = {"offline": {"max_iterations": 3}}
    d = se.evaluate_stop([_rec(1), _rec(2), _rec(3)], cfg)
    assert d.should_stop and d.guard == "max_iterations"


def test_evaluate_stop_cost() -> None:
    cfg = {"offline": {"max_iterations": 10, "max_cost_usd": 5.0}}
    d = se.evaluate_stop([_rec(1, cost=0), _rec(2, cost=6)], cfg)
    assert d.should_stop and d.guard == "cost"


def test_evaluate_stop_overfit() -> None:
    cfg = {
        "offline": {"max_iterations": 10, "max_cost_usd": 100, "guards": {"overfit_drop_pt": 15}}
    }
    d = se.evaluate_stop([_rec(80, holdout=80), _rec(70, holdout=60)], cfg)
    assert d.should_stop and d.guard == "overfit"


def test_evaluate_stop_convergence() -> None:
    cfg = {
        "offline": {
            "max_iterations": 10,
            "max_cost_usd": 100,
            "stop": {"consecutive": 2, "accuracy_delta_pt": 3, "steps_pct": 10, "time_pct": 15},
            "guards": {"overfit_drop_pt": 15, "divergence_rounds": 3},
        }
    }
    hist = [_rec(80, 10, 1000, 80), _rec(81, 10.5, 1050, 81), _rec(82, 10.2, 1020, 82)]
    d = se.evaluate_stop(hist, cfg)
    assert d.should_stop and d.guard == ""


def test_evaluate_stop_divergence() -> None:
    cfg = {
        "offline": {
            "max_iterations": 10,
            "max_cost_usd": 100,
            "stop": {"consecutive": 2, "accuracy_delta_pt": 3, "steps_pct": 10, "time_pct": 15},
            "guards": {"overfit_drop_pt": 15, "divergence_rounds": 2},
        }
    }
    hist = [_rec(60, 10, 1000, 50), _rec(55, 20, 2000, 50), _rec(50, 5, 500, 50)]
    d = se.evaluate_stop(hist, cfg)
    assert d.should_stop and d.guard == "divergence"


def test_evaluate_stop_no_stop_when_improving() -> None:
    cfg = {"offline": {"max_iterations": 10, "max_cost_usd": 100}}
    d = se.evaluate_stop([_rec(50, holdout=50), _rec(60, holdout=60)], cfg)
    assert d.should_stop is False


# ---------------------------------------------------------------------------
# ロック
# ---------------------------------------------------------------------------


def test_lock_is_exclusive(tmp_path) -> None:
    p = str(tmp_path)
    assert se.acquire_lock(p, "s") is True
    assert se.acquire_lock(p, "s") is False
    se.release_lock(p, "s")
    assert se.acquire_lock(p, "s") is True


def test_acquire_lock_reclaims_stale(tmp_path) -> None:
    import json

    p = str(tmp_path)
    assert se.acquire_lock(p, "s") is True
    # ロックを stale 化（TTL 超過 epoch + 存在しない PID）
    with open(se.lock_path(p, "s"), "w", encoding="utf-8") as f:
        json.dump({"pid": 2_000_000_000, "epoch": 0.0, "ts": "old"}, f)
    assert se.acquire_lock(p, "s") is True  # stale を奪取


def test_acquire_lock_reclaims_unreadable(tmp_path) -> None:
    p = str(tmp_path)
    se.acquire_lock(p, "s")
    with open(se.lock_path(p, "s"), "w", encoding="utf-8") as f:
        f.write("not-json")
    assert se.acquire_lock(p, "s") is True


# ---------------------------------------------------------------------------
# ハードニング（レビュー反映）
# ---------------------------------------------------------------------------


def test_slug_hardening() -> None:
    assert se._slug("..") == "__"
    assert not se._slug(".env").startswith(".")
    assert "/" not in se._slug("a/b/c")
    assert len(se._slug("x" * 500)) <= 120


def test_data_dir_rejects_traversal(tmp_path) -> None:
    p = str(tmp_path)
    d = se.data_dir(p, {"storage": {"dir": "../../etc"}})
    assert d.startswith(os.path.abspath(p))


def test_build_metric_record_sanitizes_nonnumeric() -> None:
    sr = {"run_id": "r", "ambiguities": "abc", "critical": {"a": True}}
    rec = se.build_metric_record("s", "r", sr, None)
    assert rec["self_report"]["ambiguities"] == 0
    assert rec["machine"]["critical_pass_rate"] == 1.0


def test_within_zero_baseline() -> None:
    assert se._within(0.0, 0.0, 10) is True
    assert se._within(0.5, 0.0, 10) is False


def test_append_lesson_collapses_newlines(tmp_path) -> None:
    p = str(tmp_path)
    cfg = {"lessons": {"max_lines": 10}}
    se.append_lesson(p, "s", "line1\nline2", cfg)
    assert se.lessons_count(p, "s", cfg) == 1


def test_score_run_penalty_from_raw_nonnumeric() -> None:
    # score_run が生の非数値 self_report でもクラッシュしない
    rec = {"machine": {"critical_pass_rate": 1.0}, "self_report": {"ambiguities": "x"}}
    assert se.score_run(rec) == 100.0


# ---------------------------------------------------------------------------
# レビュー反映（PR #105 第2ラウンド: Codex）
# ---------------------------------------------------------------------------


def test_coerce_bool_strict() -> None:
    assert se._coerce_bool(True) is True
    assert se._coerce_bool("true") is True
    assert se._coerce_bool("false") is False  # 文字列 "false" を True にしない
    assert se._coerce_bool("maybe") is False  # 未知文字列は安全側 False
    assert se._coerce_bool(0) is False
    assert se._coerce_bool(1) is True


def test_build_metric_record_quoted_false_is_failure() -> None:
    rec = se.build_metric_record("s", "r", {"run_id": "r", "critical": {"a": "false"}}, None)
    assert rec["machine"]["critical_pass_rate"] == 0.0
    assert rec["success"] is False


def test_convergence_blocked_by_new_ambiguities() -> None:
    cfg = {
        "offline": {
            "max_iterations": 10,
            "max_cost_usd": 100,
            "stop": {"consecutive": 2, "accuracy_delta_pt": 3, "steps_pct": 10, "time_pct": 15},
            "guards": {"overfit_drop_pt": 15, "divergence_rounds": 3},
        }
    }
    # score/steps/time は収束条件を満たすが、新規不明瞭点が残るので停止しない
    hist = [
        se.IterationRecord(80, 10, 1000, 80, new_ambiguities=1),
        se.IterationRecord(81, 10.2, 1010, 81, new_ambiguities=1),
        se.IterationRecord(82, 10.1, 1005, 82, new_ambiguities=1),
    ]
    assert se.evaluate_stop(hist, cfg).should_stop is False


def test_lock_not_reclaimed_within_ttl(tmp_path) -> None:
    import json
    import time

    p = str(tmp_path)
    assert se.acquire_lock(p, "s") is True
    # 死んだように見える PID でも epoch が新しければ TTL 内 → stale ではない（奪取しない）
    with open(se.lock_path(p, "s"), "w", encoding="utf-8") as f:
        json.dump({"pid": 2_000_000_000, "epoch": time.time(), "ts": "x"}, f)
    assert se.acquire_lock(p, "s") is False


def test_load_config_reads_project_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AI_ORCHESTRA_DIR", os.getcwd())  # hook_common 解決用
    cfg_dir = tmp_path / ".claude" / "config" / "skill-evolution"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "skill-evolution.yaml").write_text(
        "trigger:\n  lessons_threshold: 3\n", encoding="utf-8"
    )
    cfg = se.load_config(str(tmp_path))
    assert cfg["trigger"]["lessons_threshold"] == 3  # 上書き反映
    assert cfg["offline"]["max_iterations"] == 10  # 未指定は DEFAULTS 保持
