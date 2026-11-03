#!/usr/bin/env python3
"""教訓化の判断の手動評価（設計 docs/design/fail-logs.md §7.8）。CI では実行しない。

``tests/fixtures/distill_corpus/*.json`` の合成した評価用の記録から、セッションの記録を組み立て、
前段判定（LLM を使わない）と教訓化セッション（``claude -p``）を実行して、要件の「判定」の受け入れ条件の
合格率と費用を出す。

使い方::

    python scripts/eval_distill.py --model sonnet            # 全カテゴリ（LLM を呼ぶ。費用が発生する）
    python scripts/eval_distill.py --signals-only            # 前段判定だけ（LLM を呼ばない）
    python scripts/eval_distill.py --category correction --out /tmp/result.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

_script_dir = os.path.dirname(os.path.abspath(__file__))
_package_dir = os.path.dirname(_script_dir)
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
for _candidate in (
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    os.path.abspath(os.path.join(_package_dir, "..", "core", "hooks")),
    os.path.join(_package_dir, "lib"),
):
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import distill_prompt as prompt  # noqa: E402
import distill_signals as signals_lib  # noqa: E402
import distill_worker as worker  # noqa: E402
import masking  # noqa: E402
import transcript_reader as reader  # noqa: E402

CORPUS_DIR = Path(_package_dir) / "tests" / "fixtures" / "distill_corpus"
REJECT_TEXT = signals_lib.DEFAULT_REJECT_MARKERS[0] + " The tool use was rejected."
INTERRUPT_TEXT = signals_lib.DEFAULT_INTERRUPT_MARKERS[0]

# 要件の「判定」の受け入れ条件（docs/requirements/fail-logs.md FR-07・FR-08・FR-12）。
THRESHOLDS = {
    "correction": ("lesson_rate", ">=", 0.8),
    "benign": ("lesson_rate", "<=", 0.2),
    "critical": ("critical_rate", ">=", 0.8),
    "noncritical": ("critical_rate", "<=", 0.2),
    "tdd": ("lesson_rate", "<=", 0.2),
    "transient": ("lesson_rate", "<=", 0.2),
    "merge_same": ("merge_rate", ">=", 0.8),
    "merge_diff": ("merge_rate", "<=", 0.1),
}


# --- 記録の組み立て ---------------------------------------------------------------


def _record(kind: str, content: object, **extra: object) -> dict:
    return {
        "type": kind,
        "uuid": uuid.uuid4().hex,
        "sessionId": "eval",
        "isSidechain": False,
        "message": {"role": kind, "content": content},
        **extra,
    }


def _tool_records(name: str, tool_input: dict, result: dict) -> list[dict]:
    tool_use_id = "toolu_" + uuid.uuid4().hex[:20]
    use = _record(
        "assistant", [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}]
    )
    text = result.get("error") if "error" in result else result.get("output", "")
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": str(text)}
    if "error" in result:
        block["is_error"] = True
    return [use, _record("user", [block])]


def build_records(turns: list[list]) -> list[dict]:
    """評価用の記録の簡略な表記から、Claude Code の transcript の形の行を組み立てる（設計 §7.3 の項目名）。"""
    records: list[dict] = []
    for turn in turns:
        kind = turn[0]
        if kind == "user":
            records.append(_record("user", turn[1]))
        elif kind == "assistant":
            records.append(_record("assistant", [{"type": "text", "text": turn[1]}]))
        elif kind == "meta":
            records.append(_record("user", turn[1], isMeta=True))
        elif kind == "bash":
            records.extend(_tool_records("Bash", {"command": turn[1]}, turn[2]))
        elif kind == "tool":
            records.extend(_tool_records(turn[1], turn[2], turn[3]))
        elif kind == "reject":
            records.extend(_tool_records("Bash", {"command": turn[1]}, {"error": REJECT_TEXT}))
        elif kind == "interrupt":
            records.append(_record("user", [{"type": "text", "text": INTERRUPT_TEXT}]))
        else:
            raise ValueError(f"unknown turn kind: {kind}")
    return records


def load_corpus(category: str | None = None) -> list[dict]:
    cases: list[dict] = []
    for path in sorted(CORPUS_DIR.glob("*.json")):
        for case in json.loads(path.read_text(encoding="utf-8")):
            case.setdefault("category", path.stem)
            if category is None or case["category"] == category:
                cases.append(case)
    return cases


def detect_case_signals(case: dict, cfg: dict | None = None) -> list[signals_lib.Signal]:
    """前段判定だけを実行する（LLM を使わない。CI のテストからも使う）。"""
    settings = cfg or worker.resolve_distill_config({})
    messages = reader.classify(
        build_records(case["turns"]), frozenset(settings["interrupt_markers"])
    )
    return signals_lib.detect(messages, [], worker._signal_config(settings, {}))


# --- 評価 -----------------------------------------------------------------------


def _run_llm(case: dict, cfg: dict, masker: masking.Masker) -> dict:
    messages = reader.classify(build_records(case["turns"]), frozenset(cfg["interrupt_markers"]))
    found = signals_lib.detect(messages, [], worker._signal_config(cfg, {}))
    anchors = found or [signals_lib.Signal("forced", index) for index in range(len(messages))]
    excerpt = worker.build_excerpt(messages, anchors, masker, cfg)
    known = [
        {
            "id": item["id"],
            "title": item["title"],
            "scope": item.get("scope", "claude"),
            "state": "candidate",
        }
        for item in case.get("existing", [])
    ]
    stdin_text = prompt.build_input(
        excerpt, known, prompt.new_nonce(), cfg["max_lessons_per_session"]
    )
    executable = worker.claude_executable()
    if executable is None:
        return {"status": "failed", "reason": "no claude executable"}
    result = worker.call_session(worker.build_command(executable, cfg), stdin_text, cfg)
    lessons = (result.output or {}).get("lessons", []) if result.status == "done" else []
    return {
        "status": result.status,
        "reason": result.reason,
        "cost_usd": result.cost_usd,
        "signals": [item.kind for item in found],
        "lessons": lessons,
    }


def _score(category: str, outcomes: list[dict], cases: list[dict]) -> dict:
    ok = [outcome for outcome in outcomes if outcome["status"] == "done"]
    total = len(ok) or 1
    with_lesson = [outcome for outcome in ok if outcome["lessons"]]
    critical = [
        o for o in with_lesson if any(item.get("severity") == "critical" for item in o["lessons"])
    ]
    merged = 0
    for outcome, case in zip(outcomes, cases, strict=True):
        ids = {item["id"] for item in case.get("existing", [])}
        if any(
            item.get("action") == "merge" and item.get("merge_into") in ids
            for item in outcome.get("lessons", [])
        ):
            merged += 1
    rates = {
        "lesson_rate": len(with_lesson) / total,
        "critical_rate": len(critical) / (len(with_lesson) or 1),
        "merge_rate": merged / total,
    }
    metric, op, threshold = THRESHOLDS[category]
    value = rates[metric]
    passed = value >= threshold if op == ">=" else value <= threshold
    return {
        "category": category,
        "cases": len(cases),
        "completed": len(ok),
        metric: round(value, 2),
        "pass": passed,
    }


def evaluate(categories: list[str], cfg: dict, masker: masking.Masker) -> dict:
    report: dict = {
        "model": cfg["model"],
        "prompt_version": prompt.PROMPT_VERSION,
        "categories": [],
        "cost_usd": 0.0,
    }
    details = []
    for category in categories:
        cases = load_corpus(category)
        outcomes = [_run_llm(case, cfg, masker) for case in cases]
        report["categories"].append(_score(category, outcomes, cases))
        report["cost_usd"] += sum(outcome.get("cost_usd") or 0.0 for outcome in outcomes)
        details += [
            {"id": case["id"], **outcome} for case, outcome in zip(cases, outcomes, strict=True)
        ]
    report["cost_usd"] = round(report["cost_usd"], 4)
    report["details"] = details
    return report


def signals_report() -> dict:
    """前段判定の合格率（要件 FR-06 の受け入れ条件 3〜5）。"""
    rows = []
    for case in load_corpus():
        expected = case.get("expect", {}).get("signal")
        if expected is None:
            continue
        found = bool(detect_case_signals(case))
        rows.append(
            {"id": case["id"], "category": case["category"], "expected": expected, "found": found}
        )
    return {"cases": rows, "mismatches": [row for row in rows if row["expected"] != row["found"]]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_distill.py", description="教訓化の判断の手動評価")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--category", choices=sorted(THRESHOLDS))
    parser.add_argument("--signals-only", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    if args.signals_only:
        report = signals_report()
    else:
        masker = masking.load_masker()
        if masker is None:
            print("伏せ字（強化版マスキング）が使えないため実行しない", file=sys.stderr)
            return 2
        cfg = {**worker.resolve_distill_config({}), "model": args.model}
        categories = [args.category] if args.category else list(THRESHOLDS)
        report = evaluate(categories, cfg, masker)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "details"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
