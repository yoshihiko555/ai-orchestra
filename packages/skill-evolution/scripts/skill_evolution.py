#!/usr/bin/env python3
"""skill-evolution CLI（オフライン反復の決定論部分）。

`orchex run skill-evolution skill_evolution -- <subcommand>` から、または直接実行する。

サブコマンド:
- status <skill>        metrics のサマリ（件数・成功率・平均スコア/ステップ/時間）
- check-trigger <skill> lessons 蓄積がしきい値を超えたか（オフライン起動判定）
- evaluate --history F   反復履歴に停止条件＋3ガードを適用し StopDecision を出力
- provenance <skill>     facet 製/非 facet 製の判別と改善反映先
- lock <acquire|release> <skill>  スキル単位ロック

シナリオ実行・改善案生成といった LLM を要する処理は本 CLI の責務外（skill が担う）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import skill_evolution_common as se  # noqa: E402


def cmd_status(project: str, skill: str) -> int:
    """metrics サマリを出力する。"""
    config = se.load_config(project)
    records = se.read_metrics(project, skill, config)
    summary = se.summarize(records)
    summary["skill"] = skill
    summary["lessons_count"] = se.lessons_count(project, skill, config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_check_trigger(project: str, skill: str) -> int:
    """lessons 蓄積がしきい値超なら triggered=true（exit 0）、未満なら exit 1。"""
    config = se.load_config(project)
    threshold = int((config.get("trigger") or {}).get("lessons_threshold") or 20)
    count = se.lessons_count(project, skill, config)
    triggered = count >= threshold
    print(
        json.dumps(
            {
                "skill": skill,
                "lessons_count": count,
                "threshold": threshold,
                "triggered": triggered,
            },
            ensure_ascii=False,
        )
    )
    return 0 if triggered else 1


def cmd_evaluate(project: str, history_path: str) -> int:
    """反復履歴 JSON に停止条件＋3ガードを適用し StopDecision を出力する。"""
    config = se.load_config(project)
    raw = (
        sys.stdin.read() if history_path == "-" else Path(history_path).read_text(encoding="utf-8")
    )
    data = json.loads(raw)
    if not isinstance(data, list):
        print("history must be a JSON array", file=sys.stderr)
        return 2
    if not all(isinstance(d, dict) for d in data):
        print("history elements must be JSON objects", file=sys.stderr)
        return 2
    history = [
        se.IterationRecord(
            score=float(d.get("score") or 0.0),
            steps=float(d.get("steps") or 0.0),
            time_ms=float(d.get("time_ms") or 0.0),
            holdout_score=float(d.get("holdout_score") or 0.0),
            cost_usd=float(d.get("cost_usd") or 0.0),
        )
        for d in data
    ]
    decision = se.evaluate_stop(history, config)
    print(
        json.dumps(
            {
                "should_stop": decision.should_stop,
                "reason": decision.reason,
                "guard": decision.guard,
                "detail": decision.detail,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_provenance(skill: str) -> int:
    """スキルの provenance と改善反映先を出力する。"""
    prov = se.detect_provenance(skill)
    target = se.resolve_reflection_target(prov)
    print(
        json.dumps(
            {"skill": skill, "provenance": prov, "reflection_target": target}, ensure_ascii=False
        )
    )
    return 0


def cmd_lock(project: str, action: str, skill: str) -> int:
    """スキル単位ロックを取得/解放する。"""
    config = se.load_config(project)
    if action == "acquire":
        ok = se.acquire_lock(project, skill, config)
        print(json.dumps({"skill": skill, "acquired": ok}, ensure_ascii=False))
        return 0 if ok else 1
    se.release_lock(project, skill, config)
    print(json.dumps({"skill": skill, "released": True}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサを構築する。"""
    parser = argparse.ArgumentParser(prog="skill_evolution", description="スキル自己改善 CLI")
    parser.add_argument("--project", default=os.getcwd(), help="プロジェクトルート（既定: cwd）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="metrics サマリ")
    p_status.add_argument("skill")

    p_trigger = sub.add_parser("check-trigger", help="オフライン起動判定")
    p_trigger.add_argument("skill")

    p_eval = sub.add_parser("evaluate", help="停止条件＋3ガード評価")
    p_eval.add_argument("--history", required=True, help="反復履歴 JSON パス（- で stdin）")

    p_prov = sub.add_parser("provenance", help="facet/非facet 判別と反映先")
    p_prov.add_argument("skill")

    p_lock = sub.add_parser("lock", help="スキル単位ロック")
    p_lock.add_argument("action", choices=["acquire", "release"])
    p_lock.add_argument("skill")

    return parser


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。"""
    args = build_parser().parse_args(argv)
    project = os.path.abspath(args.project)
    if args.command == "status":
        return cmd_status(project, args.skill)
    if args.command == "check-trigger":
        return cmd_check_trigger(project, args.skill)
    if args.command == "evaluate":
        return cmd_evaluate(project, args.history)
    if args.command == "provenance":
        return cmd_provenance(args.skill)
    if args.command == "lock":
        return cmd_lock(project, args.action, args.skill)
    return 2


if __name__ == "__main__":
    sys.exit(main())
