#!/usr/bin/env python3
"""meta-harness proposer helpers（Phase 2 M2）。

M2 では proposer の実起動はまだ行わず、`proposal.schema.json` の検証入口と
prompt template の描画だけを提供する。filtered view 構築・isolated launch・候補登録は後続 M3/M4。
"""

from __future__ import annotations

import sys
from pathlib import Path
from string import Template
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import meta_harness_common as mh  # noqa: E402

PROPOSAL_SCHEMA_NAME = "proposal.schema.json"
PROPOSER_PROMPT_TEMPLATE_NAME = "proposer-prompt-template.md"
DEFAULT_MAX_OVERLAY_BYTES = 200000
_MISSING_FOCUS = "(none)"


def validate_proposal(proposal: dict[str, Any], schema_dir: Path) -> list[str]:
    """proposal JSON を `proposal.schema.json` に照らして検証する（空 list = valid）。"""
    schema = mh.load_schema(schema_dir, PROPOSAL_SCHEMA_NAME)
    return mh.validate_against_schema(proposal, schema, schema_dir)


def render_proposer_prompt(
    *,
    view_dir: Path,
    frontier_doc: dict[str, Any] | None,
    config: dict[str, Any],
    package_dir: Path,
    target: str,
    focus_run_id: str | None = None,
    focus_candidate_id: str | None = None,
) -> str:
    """package resource の prompt template に実行時コンテキストを埋め込む。"""
    template = _load_prompt_template(package_dir)
    proposer_cfg = config.get("proposer") or {}
    max_overlay_bytes = proposer_cfg.get("max_overlay_bytes", DEFAULT_MAX_OVERLAY_BYTES)
    return Template(template).safe_substitute(
        view_dir=str(view_dir.resolve()),
        target=target,
        focus_run_id=focus_run_id or _MISSING_FOCUS,
        focus_candidate_id=focus_candidate_id or _MISSING_FOCUS,
        max_overlay_bytes=max_overlay_bytes,
        frontier_summary=summarize_frontier(frontier_doc),
    )


def summarize_frontier(frontier_doc: dict[str, Any] | None, *, max_points: int = 5) -> str:
    """proposer prompt に埋め込む Pareto frontier の短い要約を作る。"""
    if not frontier_doc:
        return "- frontier: (none)\n- dominated: (none)\n- points: (none)"

    frontier_ids = _string_list(frontier_doc.get("frontier"))
    dominated_ids = _string_list(frontier_doc.get("dominated"))
    points = [p for p in frontier_doc.get("points", []) if isinstance(p, dict)]
    points_by_id = {str(p.get("cand_id")): p for p in points if p.get("cand_id") is not None}

    lines = [
        f"- frontier: {_join_or_none(frontier_ids)}",
        f"- dominated: {_join_or_none(dominated_ids)}",
        "- points:",
    ]
    selected_ids = frontier_ids[:max_points] or [str(p.get("cand_id")) for p in points[:max_points]]
    if not selected_ids:
        lines[-1] = "- points: (none)"
        return "\n".join(lines)

    for cand_id in selected_ids:
        point = points_by_id.get(cand_id)
        if point is None:
            lines.append(f"  - {cand_id}: point details unavailable")
            continue
        lines.append(
            "  - "
            f"{cand_id}: quality_mean={_format_metric(point.get('quality_mean'))}, "
            f"cost_mean={_format_metric(point.get('cost_mean'))}, "
            f"runs={point.get('runs', 'unknown')}"
        )
    return "\n".join(lines)


def _load_prompt_template(package_dir: Path) -> str:
    path = package_dir / "config" / PROPOSER_PROMPT_TEMPLATE_NAME
    return path.read_text(encoding="utf-8")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _join_or_none(values: list[str]) -> str:
    return ", ".join(values) if values else "(none)"


def _format_metric(value: Any) -> str:
    if isinstance(value, int | float):
        return f"{value:.3f}"
    return "unknown"
