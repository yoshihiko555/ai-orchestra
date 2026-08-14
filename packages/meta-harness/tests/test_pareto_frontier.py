"""Pareto 判定・frontier CLI のテスト（EV-16〜18, EV-20, EV-82〜83, Sec3-5）。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_pareto",
    "packages/meta-harness/lib/meta_harness_common.py",
)
mh_cli = load_module(
    "meta_harness_cli_pareto",
    "packages/meta-harness/scripts/meta_harness.py",
)


def _point(
    cand_id: str, quality_mean: float, cost_mean: float, quality_min: float | None = None
) -> dict:
    return {
        "cand_id": cand_id,
        "quality_mean": quality_mean,
        "quality_var": 0.0,
        "quality_min": quality_min if quality_min is not None else quality_mean,
        "cost_mean": cost_mean,
        "runs": 1,
        "eligible": True,
    }


class TestComputeParetoFrontier:
    # EV-16
    def test_a_dominates_b_when_strictly_better_on_both_axes(self) -> None:
        a = _point("a", quality_mean=90, cost_mean=100)
        b = _point("b", quality_mean=80, cost_mean=200)

        frontier, dominated = mh.compute_pareto_frontier([a, b])

        assert frontier == ["a"]
        assert dominated == ["b"]

    # EV-16
    def test_non_dominated_pair_both_on_frontier(self) -> None:
        a = _point("a", quality_mean=90, cost_mean=200)  # 高品質・高コスト
        b = _point("b", quality_mean=80, cost_mean=100)  # 低品質・低コスト

        frontier, dominated = mh.compute_pareto_frontier([a, b])

        assert set(frontier) == {"a", "b"}
        assert dominated == []

    # EV-17
    def test_exact_tie_breaks_on_quality_min(self) -> None:
        a = _point("a", quality_mean=80, cost_mean=100, quality_min=70)
        b = _point("b", quality_mean=80, cost_mean=100, quality_min=60)

        frontier, dominated = mh.compute_pareto_frontier([a, b])

        assert frontier == ["a"]
        assert dominated == ["b"]

    def test_equal_on_all_three_axes_are_mutually_non_dominated(self) -> None:
        a = _point("a", quality_mean=80, cost_mean=100, quality_min=70)
        b = _point("b", quality_mean=80, cost_mean=100, quality_min=70)

        frontier, dominated = mh.compute_pareto_frontier([a, b])

        assert set(frontier) == {"a", "b"}
        assert dominated == []

    # EV-82
    def test_routing_config_equal_quality_lower_cost_does_not_dominate(self) -> None:
        cheaper = _point("cheaper", quality_mean=80, cost_mean=50)
        baseline = _point("baseline", quality_mean=80, cost_mean=100)

        frontier, dominated = mh.compute_pareto_frontier(
            [cheaper, baseline], target="routing-config"
        )

        assert set(frontier) == {"cheaper", "baseline"}
        assert dominated == []

    # EV-82
    def test_routing_config_higher_quality_non_increasing_cost_dominates(self) -> None:
        improved = _point("improved", quality_mean=81, cost_mean=100)
        baseline = _point("baseline", quality_mean=80, cost_mean=100)

        frontier, dominated = mh.compute_pareto_frontier(
            [improved, baseline], target="routing-config"
        )

        assert frontier == ["improved"]
        assert dominated == ["baseline"]

    # EV-105
    def test_routing_config_quality_diff_within_margin_does_not_dominate(self) -> None:
        a = _point("a", quality_mean=80.0 + 1e-7, cost_mean=100)
        b = _point("b", quality_mean=80.0, cost_mean=100)

        frontier, dominated = mh.compute_pareto_frontier([a, b], target="routing-config")

        assert set(frontier) == {"a", "b"}
        assert dominated == []

    # EV-105
    def test_routing_config_quality_diff_exactly_at_margin_does_not_dominate(self) -> None:
        # 80.0 + 1e-6 は丸めで差が margin と厳密一致しないため、0.0 基準で
        # 差 == QUALITY_STRICT_MARGIN を正確に表現する（> を >= に変えると fail する境界）
        a = _point("a", quality_mean=mh.QUALITY_STRICT_MARGIN, cost_mean=100)
        b = _point("b", quality_mean=0.0, cost_mean=100)

        frontier, dominated = mh.compute_pareto_frontier([a, b], target="routing-config")

        assert set(frontier) == {"a", "b"}
        assert dominated == []

    # EV-105
    def test_routing_config_quality_diff_above_margin_dominates(self) -> None:
        a = _point("a", quality_mean=80.0 + 2e-6, cost_mean=100)
        b = _point("b", quality_mean=80.0, cost_mean=100)

        frontier, dominated = mh.compute_pareto_frontier([a, b], target="routing-config")

        assert frontier == ["a"]
        assert dominated == ["b"]

    # EV-16, EV-82 regression
    def test_non_routing_target_keeps_cost_only_dominance(self) -> None:
        cheaper = _point("cheaper", quality_mean=80, cost_mean=50)
        baseline = _point("baseline", quality_mean=80, cost_mean=100)

        frontier, dominated = mh.compute_pareto_frontier(
            [cheaper, baseline], target="skill:handoff"
        )

        assert frontier == ["cheaper"]
        assert dominated == ["baseline"]


class TestQualityScoreNotGamedByMissingReport:
    def test_missing_report_penalty_cannot_beat_genuine_high_quality_result(self) -> None:
        config = mh.DEFAULTS
        penalty_missing_report = config["scoring"]["penalty_missing_report"]

        # 欠落 self-report によるペナルティを負った低い critical_pass_rate の結果
        gamed_score = mh.quality_score(
            critical_pass_rate=0.5, penalty=penalty_missing_report, config=config
        )
        # 完全な self-report を伴う高い critical_pass_rate・ペナルティ0の結果
        genuine_score = mh.quality_score(critical_pass_rate=1.0, penalty=0, config=config)

        assert gamed_score < genuine_score

    def test_zero_penalty_is_additive_on_top_of_critical_pass_rate(self) -> None:
        config = mh.DEFAULTS
        score = mh.quality_score(critical_pass_rate=1.0, penalty=0, config=config)
        assert score == 100.0  # 1.0 * 70 + max(0, 30 - 0) = 100

    def test_large_penalty_floors_at_zero_bonus(self) -> None:
        config = mh.DEFAULTS
        score = mh.quality_score(critical_pass_rate=1.0, penalty=100, config=config)
        assert score == 70.0  # penalty 項は 0 未満にならない


def _run_completed(
    cand_id: str,
    *,
    quality_score: float,
    verdict: str = "pass",
    holdout: bool = False,
    suite_hash: str = "a" * 64,
    evaluator_hash: str = "e" * 64,
    total_tokens: int = 1000,
    scenario_id: str = "scenario-1",
    attempt: int = 1,
    attempts_total: int = 1,
) -> dict:
    return {
        "event": "run_completed",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "run_id": (
            f"run-{cand_id}-{scenario_id}-{'holdout' if holdout else 'train'}-"
            f"{attempt}-{quality_score}"
        ),
        "cand_id": cand_id,
        "scenario_id": scenario_id,
        "target": "claude-harness",
        "suite_id": "claude-harness",
        "suite_hash": suite_hash,
        "scenario_hash": "b" * 64,
        "evaluator_hash": evaluator_hash,
        "verdict": verdict,
        "quality_score": quality_score,
        "critical_pass_rate": 1.0 if verdict == "pass" else 0.0,
        "cost": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": total_tokens,
            "tool_uses": 0,
            "duration_ms": 1,
            "total_cost_usd": 0.01,
            "num_turns": 1,
        },
        "attempt": attempt,
        "attempts_total": attempts_total,
        "holdout": holdout,
    }


def _evaluation_summaries(
    events: list[dict], *, cand_ids: set[str] | None = None, start_index: int = 0
) -> list[dict]:
    latest = mh.latest_non_holdout_run_completed(events)
    if latest is None:
        return []
    matching = [
        event
        for event in events
        if event.get("event") == "run_completed"
        and not event.get("holdout")
        and event.get("suite_hash") == latest["suite_hash"]
        and event.get("evaluator_hash") == latest["evaluator_hash"]
        and (cand_ids is None or event.get("cand_id") in cand_ids)
    ]
    summaries: list[dict] = []
    for offset, cand_id in enumerate(sorted({str(event["cand_id"]) for event in matching}), 1):
        candidate_runs = [event for event in matching if event["cand_id"] == cand_id]
        latest_runs = mh._latest_attempt_groups_per_scenario(candidate_runs)
        verdict = (
            "error"
            if any(event["verdict"] == "error" for event in latest_runs)
            else "pass"
            if all(event["verdict"] == "pass" for event in latest_runs)
            else "fail"
        )
        summaries.append(
            {
                "event": "evaluation_completed",
                "ts": mh.now_iso(),
                "schema_version": "1.0",
                "evaluation_id": f"eval-20260711-120000-{start_index + offset:08x}",
                "cand_id": cand_id,
                "target": "claude-harness",
                "holdout": False,
                "own_run_ids": [str(event["run_id"]) for event in latest_runs],
                "own_suite_hash": latest["suite_hash"],
                "evaluator_hash": latest["evaluator_hash"],
                "own_critical_pass": verdict == "pass",
                "regression_results": [],
                "verdict": verdict,
                "unverified_impacts": [],
                "evaluation_base_commit": "a" * 40,
                "impacted_targets": [],
                "impact_input_hash": "c" * 64,
                "regression_cost_usd": 0.0,
            }
        )
    return summaries


def _evaluated(events: list[dict]) -> list[dict]:
    return [*events, *_evaluation_summaries(events)]


def _append_evaluated_run(project: Path, config: dict, event: dict) -> None:
    mh.append_ledger_event(project, config, event)
    if event.get("holdout"):
        return
    events = mh.read_ledger_events(project, config)
    start_index = sum(item.get("event") == "evaluation_completed" for item in events)
    for summary in _evaluation_summaries(
        events, cand_ids={str(event["cand_id"])}, start_index=start_index
    ):
        mh.append_ledger_event(project, config, summary)


class TestAggregateRunPoints:
    # EV-18 (via eligible flag)
    def test_non_holdout_fail_makes_candidate_ineligible(self) -> None:
        events = [
            _run_completed("c1", quality_score=90, verdict="fail"),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert points[0]["eligible"] is False

    def test_non_holdout_error_makes_candidate_ineligible(self) -> None:
        events = [_run_completed("c1", quality_score=90, verdict="error")]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert points[0]["eligible"] is False

    def test_holdout_fail_does_not_affect_eligibility(self) -> None:
        events = [
            _run_completed("c1", quality_score=90, verdict="pass"),
            _run_completed("c1", quality_score=10, verdict="fail", holdout=True),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert points[0]["eligible"] is True

    def test_holdout_runs_excluded_from_quality_mean_and_cost_mean(self) -> None:
        events = [
            _run_completed("c1", quality_score=80, total_tokens=100),
            _run_completed("c1", quality_score=10, total_tokens=99999, holdout=True),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert points[0]["quality_mean"] == 80
        assert points[0]["cost_mean"] == 0.01
        assert points[0]["runs"] == 1

    def test_all_holdout_runs_yields_empty_points_not_a_zero_runs_point(self) -> None:
        # PR #162 レビュー指摘: non-holdout run が 1 件も無い場合にスコープ選定不能な
        # `runs: 0` の point を作ると frontier.schema.json の `minimum: 1` に違反する。
        # 修正後は「non-holdout run が皆無」＝比較スコープを選定できないため、
        # 空の points リストを返す（従来どおりの「no run_events」ケースと同じ扱い）。
        events = [
            _run_completed("c1", quality_score=10, total_tokens=99999, holdout=True),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert points == []

    def test_all_pass_is_eligible(self) -> None:
        events = [_run_completed("c1", quality_score=90, verdict="pass")]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert points[0]["eligible"] is True

    def test_only_latest_hash_pair_is_aggregated(self) -> None:
        events = [
            _run_completed("c1", quality_score=10, suite_hash="a" * 64, evaluator_hash="e" * 64),
            _run_completed(
                "c1", quality_score=90, suite_hash="a" * 63 + "f", evaluator_hash="e" * 64
            ),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        # 最後の run_completed（2番目）の hash ペアのみが集計対象になる
        assert len(points) == 1
        assert points[0]["quality_mean"] == 90

    # PR #162 レビュー指摘 (FIX A): 末尾が holdout run かつ別 hash ペアでも、
    # non-holdout run のスコープを維持すること
    def test_trailing_holdout_with_different_hash_does_not_empty_the_scope(self) -> None:
        events = [
            _run_completed("c1", quality_score=70, suite_hash="a" * 64, evaluator_hash="e" * 64),
            _run_completed("c1", quality_score=90, suite_hash="a" * 64, evaluator_hash="e" * 64),
            # 末尾に別 suite_hash の holdout run を追記（EVALUATOR が変わった holdout 再評価等）
            _run_completed(
                "c1",
                quality_score=5,
                suite_hash="f" * 64,
                evaluator_hash="e" * 64,
                holdout=True,
            ),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert len(points) == 1
        assert points[0]["runs"] >= 1
        assert points[0]["eligible"] is True
        assert (
            points[0]["quality_mean"] == 90
        )  # 最新の non-holdout run のみ集計（別 scenario なし）

    # PR #162 レビュー指摘 (FIX B): 同一 cand×scenario の再評価（fail→pass）は
    # 最新 attempt 群のみが集計対象になる
    def test_reevaluation_after_failure_uses_only_latest_attempt_group(self) -> None:
        events = [
            _run_completed("c1", quality_score=10, verdict="fail"),  # attempt=1（旧・失敗）
            _run_completed("c1", quality_score=90, verdict="pass"),  # attempt=1（再評価・成功）
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert len(points) == 1
        assert points[0]["eligible"] is True
        assert points[0]["quality_mean"] == 90
        assert points[0]["runs"] == 1

    # PR #162 レビュー指摘 (FIX B): repeat 評価（attempt=1,2,3）は単一グループとして
    # 全件集計されること
    def test_repeat_attempts_within_a_single_group_are_all_aggregated(self) -> None:
        events = [
            _run_completed("c1", quality_score=80, attempt=1, attempts_total=3),
            _run_completed("c1", quality_score=90, attempt=2, attempts_total=3),
            _run_completed("c1", quality_score=100, attempt=3, attempts_total=3),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert len(points) == 1
        assert points[0]["runs"] == 3
        assert points[0]["quality_mean"] == 90  # (80+90+100)/3

    def test_eligible_and_ineligible_split_helper(self) -> None:
        events = [
            _run_completed("c1", quality_score=90, verdict="pass"),
            _run_completed("c2", quality_score=10, verdict="fail"),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        eligible, ineligible_ids = mh_cli._eligible_and_ineligible(points)
        assert [p["cand_id"] for p in eligible] == ["c1"]
        assert ineligible_ids == ["c2"]


class TestFrontierCliRebuildVsCache:
    # EV-20: 「再生成可能キャッシュ」の性質 — `--rebuild` なしでは frontier.json
    # ファイル自体（永続キャッシュ）を書き換えない。表示用の算出結果自体は
    # Sec6 CLI 表の記述（「ledger.jsonl から Pareto frontier を算出する」を
    # rebuild の有無に関わらない基本動作とし、「--rebuild 指定時は frontier.json を
    # 再生成する」を追加の永続化動作と読む）どおり、常に ledger から最新計算される。
    # 差分は「ディスク上の frontier.json を書き換えるかどうか」にあるため、
    # そこを検証する（Sec1-5「自動再生成はしない」は status の警告文脈であり、
    # frontier サブコマンド自体の再計算・非永続表示までは禁じていないと解釈する）。
    def test_frontier_without_rebuild_does_not_persist_frontier_json(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        frontier_path = git_project / ".claude" / "meta-harness" / "frontier-claude-harness.json"
        cached_before = json.loads(frontier_path.read_text(encoding="utf-8"))

        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=90))

        result = run_meta("frontier", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        # 表示結果は最新 ledger を反映する
        assert payload["frontier"] == ["c1"]
        # だがディスク上の frontier.json（永続キャッシュ）は書き換わらない
        cached_after = json.loads(frontier_path.read_text(encoding="utf-8"))
        assert cached_after == cached_before

    def test_frontier_without_rebuild_does_not_append_ledger_event(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        ledger_path = git_project / ".claude" / "meta-harness" / "ledger.jsonl"
        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=90))
        line_count_before = len(ledger_path.read_text(encoding="utf-8").splitlines())

        run_meta("frontier", "--json", project=git_project, check=True)

        line_count_after = len(ledger_path.read_text(encoding="utf-8").splitlines())
        assert line_count_after == line_count_before

    def test_frontier_rebuild_updates_cache_and_appends_ledger_event(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=90))

        result = run_meta("frontier", "--rebuild", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        assert payload["frontier"] == ["c1"]
        cached_after = json.loads(
            (git_project / ".claude" / "meta-harness" / "frontier-claude-harness.json").read_text(
                encoding="utf-8"
            )
        )
        assert cached_after["frontier"] == ["c1"]

        events = [
            json.loads(line)
            for line in (git_project / ".claude" / "meta-harness" / "ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        frontier_events = [e for e in events if e["event"] == "frontier_updated"]
        assert len(frontier_events) == 1
        assert frontier_events[0]["target"] == "claude-harness"

    def test_frontier_json_matches_frontier_schema(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=90))
        run_meta("frontier", "--rebuild", project=git_project, check=True)

        schema_dir = Path(__file__).resolve().parents[3] / "packages" / "meta-harness" / "schemas"
        schema = mh.load_schema(schema_dir, "frontier.schema.json")
        doc = json.loads(
            (git_project / ".claude" / "meta-harness" / "frontier-claude-harness.json").read_text(
                encoding="utf-8"
            )
        )
        assert mh.validate_against_schema(doc, schema, schema_dir) == []


class TestFrontierHashReflectsLatestRunCompleted:
    # frontier のトップレベル suite_hash/evaluator_hash は、points の比較スコープと
    # 同じ「最新の run_completed」のペアであるべき（ledger 末尾イベントとは限らない）
    def test_hash_metadata_uses_latest_run_completed_even_if_ledger_tail_is_not_run_completed(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        suite_hash = "a" * 64
        evaluator_hash = "e" * 64
        _append_evaluated_run(
            git_project,
            config,
            _run_completed(
                "c1", quality_score=90, suite_hash=suite_hash, evaluator_hash=evaluator_hash
            ),
        )
        # ledger 末尾を run_completed 以外のイベントにする（register 後に評価するのは通常運用）
        mh.append_ledger_event(
            git_project,
            config,
            {
                "event": "candidate_registered",
                "ts": mh.now_iso(),
                "schema_version": "1.0",
                "cand_id": "c2",
                "parent_id": None,
                "generation": 0,
                "target": "claude-harness",
                "created_by": "human",
            },
        )

        result = run_meta("frontier", "--rebuild", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        assert payload["suite_hash"] == suite_hash
        assert payload["evaluator_hash"] == evaluator_hash
        cached = json.loads(
            (git_project / ".claude" / "meta-harness" / "frontier-claude-harness.json").read_text(
                encoding="utf-8"
            )
        )
        assert cached["suite_hash"] == suite_hash
        assert cached["evaluator_hash"] == evaluator_hash

    # PR #162 レビュー指摘 (FIX A): 末尾が holdout run（別 hash ペア）でも、hash メタデータは
    # non-holdout のスコープを反映し、points も空にならないこと
    def test_hash_metadata_uses_latest_non_holdout_run_even_if_ledger_tail_is_holdout(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        suite_hash = "a" * 64
        evaluator_hash = "e" * 64
        _append_evaluated_run(
            git_project,
            config,
            _run_completed(
                "c1", quality_score=90, suite_hash=suite_hash, evaluator_hash=evaluator_hash
            ),
        )
        # ledger 末尾に別 suite_hash の holdout run を追記する
        mh.append_ledger_event(
            git_project,
            config,
            _run_completed(
                "c1",
                quality_score=5,
                suite_hash="f" * 64,
                evaluator_hash=evaluator_hash,
                holdout=True,
            ),
        )

        result = run_meta("frontier", "--rebuild", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        assert payload["suite_hash"] == suite_hash
        assert payload["evaluator_hash"] == evaluator_hash
        assert payload["points"] != []
        assert all(p["runs"] >= 1 for p in payload["points"])
        cached = json.loads(
            (git_project / ".claude" / "meta-harness" / "frontier-claude-harness.json").read_text(
                encoding="utf-8"
            )
        )
        assert cached["suite_hash"] == suite_hash
        assert cached["evaluator_hash"] == evaluator_hash
        assert cached["points"] != []

    def test_hash_metadata_falls_back_to_zero_hash_when_no_run_completed_exists(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        mh.append_ledger_event(
            git_project,
            config,
            {
                "event": "candidate_registered",
                "ts": mh.now_iso(),
                "schema_version": "1.0",
                "cand_id": "c1",
                "parent_id": None,
                "generation": 0,
                "target": "claude-harness",
                "created_by": "human",
            },
        )

        result = run_meta("frontier", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        zero_hash = "0" * 64
        assert payload["suite_hash"] == zero_hash
        assert payload["evaluator_hash"] == zero_hash


class TestFrontierRebuildComputesInsideLock:
    # PR #162 レビュー指摘 (FIX P2 / Fix 2): frontier 計算（ledger 読み込み含む）は
    # store_lock 取得後に行われなければならない（lock 待ち中の追記との競合を防ぐため）
    def test_compute_frontier_happens_while_store_lock_is_held(
        self, git_project: Path, run_meta, monkeypatch
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=90))

        lock_held = {"value": False}
        original_read = mh_cli.mh.read_ledger_events

        def tracking_read_ledger_events(main_root, cfg):
            assert lock_held["value"] is True, (
                "frontier computation (read_ledger_events) must happen while store.lock is held"
            )
            return original_read(main_root, cfg)

        original_store_lock = mh_cli.mh.store_lock

        from contextlib import contextmanager

        @contextmanager
        def tracking_store_lock(main_root, cfg):
            with original_store_lock(main_root, cfg):
                lock_held["value"] = True
                try:
                    yield
                finally:
                    lock_held["value"] = False

        monkeypatch.setattr(mh_cli.mh, "read_ledger_events", tracking_read_ledger_events)
        monkeypatch.setattr(mh_cli.mh, "store_lock", tracking_store_lock)

        exit_code = mh_cli.cmd_frontier(str(git_project), True, False)
        assert exit_code == 0

    def test_frontier_rebuild_ledger_line_count_matches_actual_file_after_append(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=90))

        run_meta("frontier", "--rebuild", project=git_project, check=True)

        ledger_path = git_project / ".claude" / "meta-harness" / "ledger.jsonl"
        actual_line_count = len(
            [ln for ln in ledger_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        )
        cached = json.loads(
            (git_project / ".claude" / "meta-harness" / "frontier-claude-harness.json").read_text(
                encoding="utf-8"
            )
        )
        assert cached["ledger_line_count"] == actual_line_count


class TestFrontierExcludesTerminalStates:
    # PR #162 レビュー指摘 (FIX P2 / Fix 3): 畳み込み状態が retired/promoted の候補は
    # points/frontier/dominated のいずれからも除外されること（Sec3-5「evaluated 候補」）
    def test_retired_candidate_excluded_from_frontier_and_points_after_rebuild(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=95))
        mh.append_ledger_event(
            git_project,
            config,
            {
                "event": "status_changed",
                "ts": mh.now_iso(),
                "schema_version": "1.0",
                "cand_id": "c1",
                "from": "evaluated",
                "to": "retired",
                "reason": "superseded",
            },
        )

        result = run_meta("frontier", "--rebuild", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        assert "c1" not in payload["frontier"]
        assert "c1" not in payload["dominated"]
        assert all(p["cand_id"] != "c1" for p in payload["points"])

    def test_fold_candidate_states_terminal_is_excluded_at_lib_level(self) -> None:
        events = [
            _run_completed("c1", quality_score=95),
            {
                "event": "status_changed",
                "ts": mh.now_iso(),
                "schema_version": "1.0",
                "cand_id": "c1",
                "from": "evaluated",
                "to": "promoted",
                "reason": "merged",
            },
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        assert points == []


class TestFrontierScenarioCoverageEligibility:
    # PR #162 レビュー指摘 (FIX P2 / Fix 4): 部分評価（一部シナリオのみ pass）の候補は
    # 要求シナリオ集合（同一スコープで観測された scenario_id の和集合）を満たさない限り
    # ineligible とする
    def test_candidate_missing_scenario_observed_by_others_is_ineligible(self) -> None:
        events = [
            _run_completed("a", quality_score=90, scenario_id="s1"),
            _run_completed("a", quality_score=85, scenario_id="s2"),
            _run_completed("b", quality_score=95, scenario_id="s1"),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        by_id = {p["cand_id"]: p for p in points}
        assert by_id["a"]["eligible"] is True
        assert by_id["b"]["eligible"] is False

    def test_candidate_covering_all_observed_scenarios_is_eligible(self) -> None:
        events = [
            _run_completed("a", quality_score=90, scenario_id="s1"),
            _run_completed("a", quality_score=85, scenario_id="s2"),
            _run_completed("b", quality_score=95, scenario_id="s1"),
            _run_completed("b", quality_score=88, scenario_id="s2"),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        by_id = {p["cand_id"]: p for p in points}
        assert by_id["a"]["eligible"] is True
        assert by_id["b"]["eligible"] is True


class TestHoldoutOnlyCandidateExcludedFromPoints:
    # PR #162 レビュー指摘 (FIX P2 / Fix 5): 通常 frontier 集計のグルーピング前に
    # holdout run を除外し、holdout-only 候補は points にも dominated にも現れないこと
    def test_holdout_only_candidate_in_shared_scope_produces_no_point(self) -> None:
        events = [
            _run_completed("c1", quality_score=90),
            _run_completed("c2", quality_score=5, holdout=True),
        ]
        points = mh.aggregate_run_points(_evaluated(events), mh.DEFAULTS)
        cand_ids = [p["cand_id"] for p in points]
        assert "c2" not in cand_ids
        assert cand_ids == ["c1"]

    def test_holdout_only_candidate_absent_from_frontier_json_and_schema_valid(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=90))
        mh.append_ledger_event(
            git_project, config, _run_completed("c2", quality_score=5, holdout=True)
        )

        result = run_meta("frontier", "--rebuild", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        assert all(p["cand_id"] != "c2" for p in payload["points"])
        assert "c2" not in payload["frontier"]
        assert "c2" not in payload["dominated"]

        schema_dir = Path(__file__).resolve().parents[3] / "packages" / "meta-harness" / "schemas"
        schema = mh.load_schema(schema_dir, "frontier.schema.json")
        cached = json.loads(
            (git_project / ".claude" / "meta-harness" / "frontier-claude-harness.json").read_text(
                encoding="utf-8"
            )
        )
        assert mh.validate_against_schema(cached, schema, schema_dir) == []


class TestCostAxisValidation:
    # PR #162 レビュー指摘 (FIX P2 / Fix 7): cost_axis の typo / cost キー欠落を
    # 黙って 0 にせず fail-closed する
    def test_invalid_cost_axis_raises(self) -> None:
        config = {**mh.DEFAULTS, "frontier": {"cost_axis": "totl_tokens"}}
        events = [_run_completed("c1", quality_score=90)]
        try:
            mh.aggregate_run_points(_evaluated(events), config)
        except mh.MetaHarnessRootError:
            pass
        else:
            raise AssertionError("unknown cost_axis should raise MetaHarnessRootError")

    # EV-83
    def test_default_cost_axis_is_total_cost_usd(self) -> None:
        assert mh.DEFAULTS["frontier"]["cost_axis"] == "total_cost_usd"

    # EV-83
    def test_missing_default_cost_key_in_run_raises_with_run_id(self) -> None:
        event = _run_completed("c1", quality_score=90)
        del event["cost"]["total_cost_usd"]
        try:
            mh.aggregate_run_points(_evaluated([event]), mh.DEFAULTS)
        except mh.MetaHarnessRootError as exc:
            assert event["run_id"] in str(exc)
            assert "total_cost_usd" in str(exc)
        else:
            raise AssertionError("missing cost field should raise MetaHarnessRootError")

    def test_cli_frontier_exits_2_for_invalid_cost_axis(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        _append_evaluated_run(git_project, config, _run_completed("c1", quality_score=90))
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "frontier:\n  cost_axis: totl_tokens\n", encoding="utf-8"
        )

        result = run_meta("frontier", project=git_project, check=False)

        assert result.returncode == 2
