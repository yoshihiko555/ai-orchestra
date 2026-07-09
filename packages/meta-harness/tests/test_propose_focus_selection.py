"""Phase 2 M4: propose focus run selection のテスト。"""

from __future__ import annotations

import pytest

from tests.module_loader import load_module

cli = load_module(
    "meta_harness_script_focus_test",
    "packages/meta-harness/scripts/meta_harness.py",
)


def _run_event(run_id: str, *, verdict: str, target: str = "claude-harness", holdout: bool = False):
    return {
        "event": "run_completed",
        "run_id": run_id,
        "target": target,
        "verdict": verdict,
        "holdout": holdout,
    }


def test_default_focus_run_selection_uses_recent_failures_up_to_limit() -> None:
    events = (
        _run_event("run-old-fail", verdict="fail"),
        _run_event("run-pass", verdict="pass"),
        _run_event("run-other-target", verdict="fail", target="skill:other"),
        _run_event("run-holdout", verdict="fail", holdout=True),
        _run_event("run-new-error", verdict="error"),
        _run_event("run-new-fail", verdict="fail"),
    )

    selected = cli._select_focus_run_ids(
        events,
        target="claude-harness",
        focus_run=None,
        max_focus_runs=2,
    )

    assert selected == ("run-new-fail", "run-new-error")


def test_explicit_focus_run_rejects_holdout_run() -> None:
    events = (_run_event("run-holdout", verdict="fail", holdout=True),)

    with pytest.raises(cli.prop.ProposerError, match="holdout"):
        cli._select_focus_run_ids(
            events,
            target="claude-harness",
            focus_run="run-holdout",
            max_focus_runs=5,
        )
