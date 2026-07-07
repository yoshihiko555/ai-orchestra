"""Pareto 判定・frontier CLI のテスト（EV-16, EV-17, EV-18, EV-20, quality_score, Sec3-5）。"""

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
) -> dict:
    return {
        "event": "run_completed",
        "ts": mh.now_iso(),
        "schema_version": "1.0",
        "run_id": f"run-{cand_id}",
        "cand_id": cand_id,
        "scenario_id": "scenario-1",
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
        "attempt": 1,
        "attempts_total": 1,
        "holdout": holdout,
    }


class TestAggregateRunPoints:
    # EV-18 (via eligible flag)
    def test_non_holdout_fail_makes_candidate_ineligible(self) -> None:
        events = [
            _run_completed("c1", quality_score=90, verdict="fail"),
        ]
        points = mh.aggregate_run_points(events, mh.DEFAULTS)
        assert points[0]["eligible"] is False

    def test_non_holdout_error_makes_candidate_ineligible(self) -> None:
        events = [_run_completed("c1", quality_score=90, verdict="error")]
        points = mh.aggregate_run_points(events, mh.DEFAULTS)
        assert points[0]["eligible"] is False

    def test_holdout_fail_does_not_affect_eligibility(self) -> None:
        events = [
            _run_completed("c1", quality_score=90, verdict="pass"),
            _run_completed("c1", quality_score=10, verdict="fail", holdout=True),
        ]
        points = mh.aggregate_run_points(events, mh.DEFAULTS)
        assert points[0]["eligible"] is True

    def test_holdout_runs_excluded_from_quality_mean_and_cost_mean(self) -> None:
        events = [
            _run_completed("c1", quality_score=80, total_tokens=100),
            _run_completed("c1", quality_score=10, total_tokens=99999, holdout=True),
        ]
        points = mh.aggregate_run_points(events, mh.DEFAULTS)
        assert points[0]["quality_mean"] == 80
        assert points[0]["cost_mean"] == 100
        assert points[0]["runs"] == 1

    def test_all_holdout_runs_makes_candidate_ineligible_not_vacuously_true(self) -> None:
        events = [
            _run_completed("c1", quality_score=10, total_tokens=99999, holdout=True),
        ]
        points = mh.aggregate_run_points(events, mh.DEFAULTS)
        assert points[0]["eligible"] is False
        assert points[0]["quality_mean"] == 0.0
        assert points[0]["cost_mean"] == 0.0
        assert points[0]["runs"] == 0

    def test_all_pass_is_eligible(self) -> None:
        events = [_run_completed("c1", quality_score=90, verdict="pass")]
        points = mh.aggregate_run_points(events, mh.DEFAULTS)
        assert points[0]["eligible"] is True

    def test_only_latest_hash_pair_is_aggregated(self) -> None:
        events = [
            _run_completed("c1", quality_score=10, suite_hash="a" * 64, evaluator_hash="e" * 64),
            _run_completed(
                "c1", quality_score=90, suite_hash="a" * 63 + "f", evaluator_hash="e" * 64
            ),
        ]
        points = mh.aggregate_run_points(events, mh.DEFAULTS)
        # 最後の run_completed（2番目）の hash ペアのみが集計対象になる
        assert len(points) == 1
        assert points[0]["quality_mean"] == 90

    def test_eligible_and_ineligible_split_helper(self) -> None:
        events = [
            _run_completed("c1", quality_score=90, verdict="pass"),
            _run_completed("c2", quality_score=10, verdict="fail"),
        ]
        points = mh.aggregate_run_points(events, mh.DEFAULTS)
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
        frontier_path = git_project / ".claude" / "meta-harness" / "frontier.json"
        cached_before = json.loads(frontier_path.read_text(encoding="utf-8"))

        config = mh.load_config(git_project)
        mh.append_ledger_event(git_project, config, _run_completed("c1", quality_score=90))

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
        mh.append_ledger_event(git_project, config, _run_completed("c1", quality_score=90))
        line_count_before = len(ledger_path.read_text(encoding="utf-8").splitlines())

        run_meta("frontier", "--json", project=git_project, check=True)

        line_count_after = len(ledger_path.read_text(encoding="utf-8").splitlines())
        assert line_count_after == line_count_before

    def test_frontier_rebuild_updates_cache_and_appends_ledger_event(
        self, git_project: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        mh.append_ledger_event(git_project, config, _run_completed("c1", quality_score=90))

        result = run_meta("frontier", "--rebuild", "--json", project=git_project, check=True)
        payload = json.loads(result.stdout)

        assert payload["frontier"] == ["c1"]
        cached_after = json.loads(
            (git_project / ".claude" / "meta-harness" / "frontier.json").read_text(encoding="utf-8")
        )
        assert cached_after["frontier"] == ["c1"]

        events = [
            json.loads(line)
            for line in (git_project / ".claude" / "meta-harness" / "ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert any(e["event"] == "frontier_updated" for e in events)

    def test_frontier_json_matches_frontier_schema(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        mh.append_ledger_event(git_project, config, _run_completed("c1", quality_score=90))
        run_meta("frontier", "--rebuild", project=git_project, check=True)

        schema_dir = Path(__file__).resolve().parents[3] / "packages" / "meta-harness" / "schemas"
        schema = mh.load_schema(schema_dir, "frontier.schema.json")
        doc = json.loads(
            (git_project / ".claude" / "meta-harness" / "frontier.json").read_text(encoding="utf-8")
        )
        assert mh.validate_against_schema(doc, schema, schema_dir) == []
