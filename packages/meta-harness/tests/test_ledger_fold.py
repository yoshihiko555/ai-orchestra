"""ledger 追記・状態畳み込みのテスト（EV-06, EV-07, EV-08, Sec1-2）。"""

from __future__ import annotations

from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_ledger",
    "packages/meta-harness/lib/meta_harness_common.py",
)


def _event(**kwargs) -> dict:
    base = {"ts": mh.now_iso(), "schema_version": "1.0"}
    base.update(kwargs)
    return base


class TestAppendLedgerEventIsAppendOnly:
    # EV-06
    def test_two_appends_produce_two_lines(self, tmp_path: Path) -> None:
        main_root = tmp_path
        config = {"storage": {"dir": ".claude/meta-harness"}}
        mh.append_ledger_event(
            main_root, config, _event(event="candidate_registered", cand_id="c1")
        )
        mh.append_ledger_event(
            main_root, config, _event(event="candidate_registered", cand_id="c2")
        )

        lines = mh.ledger_path(main_root, config).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    # EV-06
    def test_first_line_bytes_unchanged_after_second_append(self, tmp_path: Path) -> None:
        main_root = tmp_path
        config = {"storage": {"dir": ".claude/meta-harness"}}
        mh.append_ledger_event(
            main_root, config, _event(event="candidate_registered", cand_id="c1")
        )
        path = mh.ledger_path(main_root, config)
        first_line_before = path.read_bytes().splitlines()[0]

        mh.append_ledger_event(
            main_root, config, _event(event="candidate_registered", cand_id="c2")
        )
        first_line_after = path.read_bytes().splitlines()[0]

        assert first_line_before == first_line_after


class TestFoldCandidateStates:
    # EV-07
    def test_registered_only_yields_candidate_status(self) -> None:
        events = [_event(event="candidate_registered", cand_id="c1")]
        states = mh.fold_candidate_states(events)
        assert states["c1"]["status"] == "candidate"

    # EV-07
    def test_registered_then_run_completed_yields_evaluated(self) -> None:
        events = [
            _event(event="candidate_registered", cand_id="c1"),
            _event(event="run_completed", cand_id="c1", verdict="pass"),
        ]
        states = mh.fold_candidate_states(events)
        assert states["c1"]["status"] == "evaluated"

    # EV-07
    def test_status_changed_to_promoted_is_terminal(self) -> None:
        events = [
            _event(event="candidate_registered", cand_id="c1"),
            _event(event="run_completed", cand_id="c1", verdict="pass"),
            _event(event="status_changed", cand_id="c1", **{"from": "evaluated", "to": "promoted"}),
        ]
        states = mh.fold_candidate_states(events)
        assert states["c1"]["status"] == "promoted"

    # EV-08
    def test_run_completed_after_terminal_status_does_not_change_status_but_warns(self) -> None:
        events = [
            _event(event="candidate_registered", cand_id="c1"),
            _event(event="run_completed", cand_id="c1", verdict="pass"),
            _event(event="status_changed", cand_id="c1", **{"from": "evaluated", "to": "promoted"}),
            _event(event="run_completed", cand_id="c1", verdict="pass"),
        ]
        states = mh.fold_candidate_states(events)
        assert states["c1"]["status"] == "promoted"
        assert len(states["c1"]["warnings"]) == 1
        assert (
            "unexpected" in states["c1"]["warnings"][0] or "terminal" in states["c1"]["warnings"][0]
        )

    # EV-08 (retired 側も同様)
    def test_run_completed_after_retired_warns_without_changing_status(self) -> None:
        events = [
            _event(event="candidate_registered", cand_id="c1"),
            _event(event="run_completed", cand_id="c1", verdict="pass"),
            _event(event="status_changed", cand_id="c1", **{"from": "evaluated", "to": "retired"}),
            _event(event="run_completed", cand_id="c1", verdict="fail"),
        ]
        states = mh.fold_candidate_states(events)
        assert states["c1"]["status"] == "retired"
        assert len(states["c1"]["warnings"]) == 1

    def test_fold_walks_ledger_in_appended_order_per_candidate(self) -> None:
        events = [
            _event(event="candidate_registered", cand_id="c1"),
            _event(event="candidate_registered", cand_id="c2"),
            _event(event="run_completed", cand_id="c1", verdict="pass"),
        ]
        states = mh.fold_candidate_states(events)
        assert states["c1"]["status"] == "evaluated"
        assert states["c2"]["status"] == "candidate"

    def test_promotion_reserved_sets_active_hold_and_released_clears_it(self) -> None:
        events = [
            _event(event="candidate_registered", cand_id="c1"),
            _event(event="run_completed", cand_id="c1", verdict="pass"),
            _event(event="promotion_reserved", cand_id="c1"),
        ]
        states = mh.fold_candidate_states(events)
        assert states["c1"]["has_active_promotion_hold"] is True

        events.append(_event(event="promotion_released", cand_id="c1", reason="aborted"))
        states = mh.fold_candidate_states(events)
        assert states["c1"]["has_active_promotion_hold"] is False

    def test_promotion_opened_also_sets_active_hold(self) -> None:
        events = [
            _event(event="candidate_registered", cand_id="c1"),
            _event(
                event="promotion_opened", cand_id="c1", pr_url="https://example/pr/1", branch="b"
            ),
        ]
        states = mh.fold_candidate_states(events)
        assert states["c1"]["has_active_promotion_hold"] is True
        # promotion_opened 自体は状態遷移イベントではない（Sec1-2）ため、直前の
        # candidate_registered による "candidate" のまま変化しない
        assert states["c1"]["status"] == "candidate"

    def test_events_without_cand_id_are_ignored(self) -> None:
        events = [_event(event="frontier_updated", frontier=[], dominated=[])]
        states = mh.fold_candidate_states(events)
        assert states == {}
