"""run_id 採番のテスト（EV-29, Sec2-4）。"""

from __future__ import annotations

import re
from datetime import datetime

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_run_id",
    "packages/meta-harness/lib/evaluator.py",
)

_RUN_ID_PATTERN = re.compile(r"^run-[0-9]{8}-[0-9]{6}-[a-z0-9-]+-a[0-9]+-[0-9a-f]{8}$")


class TestRunIdFormat:
    def test_matches_result_schema_pattern(self) -> None:
        run_id = ev.generate_run_id(
            "cand-20260707-120000-example-facet-a1b2", "summarize-readme", 1
        )
        assert _RUN_ID_PATTERN.match(run_id)

    def test_embeds_fixed_timestamp(self) -> None:
        moment = datetime(2026, 7, 7, 12, 0, 0)
        run_id = ev.generate_run_id("cand-20260707-120000-slug-a1b2", "scenario-a", 3, now=moment)
        assert run_id.startswith("run-20260707-120000-")
        assert "-a3-" in run_id

    def test_cand_slug_strips_timestamp_prefix(self) -> None:
        assert ev._cand_slug("cand-20260707-120000-example-facet-a1b2") == "example-facet-a1b2"

    def test_cand_slug_falls_back_to_whole_id_when_prefix_unrecognized(self) -> None:
        assert ev._cand_slug("not-a-standard-cand-id") == "not-a-standard-cand-id"


class TestRunIdUniqueness:
    def test_same_second_same_attempt_yields_distinct_ids_via_nonce(self) -> None:
        moment = datetime(2026, 7, 7, 12, 0, 0)
        ids = {
            ev.generate_run_id("cand-20260707-120000-slug-a1b2", "scenario-a", 1, now=moment)
            for _ in range(50)
        }
        assert len(ids) == 50  # 全て一意（nonce による衝突回避）
