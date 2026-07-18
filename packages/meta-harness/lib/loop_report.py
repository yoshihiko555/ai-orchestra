"""Deterministic human-readable report rendering for meta-harness loops."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import meta_harness_common as mh  # noqa: E402

_CAND_ID_PATTERN = re.compile(r"^cand-[0-9]{8}-[0-9]{6}-[a-z0-9-]+$")


class LoopReportError(RuntimeError):
    pass


def write_report(
    main_root: Path,
    config: dict,
    spec: Any,
    reason: str,
    iterations: list[dict],
) -> Path:
    events = mh.read_ledger_events_strict(main_root, config)
    cutoff = _stop_cutoff(events, spec.loop_id, reason)
    scoped_events = events[: cutoff + 1]
    before = _frontier_before_loop(scoped_events, spec.started_index)
    frontier_events = [event for event in scoped_events if event.get("event") == "frontier_updated"]
    after = _validated_ids(frontier_events[-1].get("frontier", [])) if frontier_events else []
    lines = [
        f"# Meta-harness Loop Report: {spec.loop_id}",
        "",
        f"- Target: `{spec.target}`",
        f"- Stop reason: **{reason}**",
        f"- Baseline best quality: {spec.baseline_best_quality:.2f}",
        "",
        "## Iterations",
        "",
        "| Iteration | Candidate | Best before | Best after | Cost (USD) |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    for event in iterations:
        outcome = event.get("outcome", "candidate")
        candidate = (
            f"`{_validated_ids([event['cand_id']])[0]}`"
            if outcome == "candidate"
            else f"({str(outcome).replace('_', ' ')})"
        )
        lines.append(
            f"| {event['iteration']} | {candidate} | {event['quality_best_before']:.2f} | "
            f"{event['quality_best_after']:.2f} | {event['iteration_cost_usd']:.6f} |"
        )
    if not iterations:
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Frontier change",
            "",
            f"- Before: {_format_ids(before)}",
            f"- After: {_format_ids(after)}",
            "",
            "## Recommended action",
            "",
            _recommended_action(reason),
            "",
        ]
    )
    path = mh.reports_dir(main_root, config) / f"loop-{spec.loop_id}.md"
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(lines).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise LoopReportError(f"could not write loop report: {path}: {exc}") from exc
    return path


def _stop_cutoff(events: list[dict], loop_id: str, reason: str) -> int:
    matches = [
        index
        for index, event in enumerate(events)
        if event.get("event") == "loop_stopped"
        and event.get("loop_id") == loop_id
        and event.get("reason") == reason
    ]
    if not matches:
        raise ValueError(f"loop stop event not found for report: {loop_id}/{reason}")
    return matches[-1]


def _frontier_before_loop(events: list[dict], started_index: int) -> list[str]:
    for event in reversed(events[:started_index]):
        if event.get("event") == "frontier_updated":
            return _validated_ids(event.get("frontier", []))
    return []


def _validated_ids(values: list[Any]) -> list[str]:
    result = [str(value) for value in values]
    invalid = [value for value in result if not _CAND_ID_PATTERN.fullmatch(value)]
    if invalid:
        raise ValueError("frontier/report contains an invalid candidate id")
    return result


def _format_ids(values: list[str]) -> str:
    return ", ".join(f"`{value}`" for value in values) or "(empty)"


def _recommended_action(reason: str) -> str:
    return {
        "budget_exhausted": "Review cost and quality before increasing the loop budget.",
        "max_iterations": "Review the best frontier candidate and decide whether to start a new loop.",
        "divergence": "Inspect recent failed runs and refine the proposer focus before retrying.",
        "converged": "Review the frontier winner and use `orchex meta promote` after approval.",
        "interrupted": "Resume with `orchex meta loop --resume <loop_id>`.",
        "error": "Resolve the reported error, then resume from the ledger.",
    }[reason]
