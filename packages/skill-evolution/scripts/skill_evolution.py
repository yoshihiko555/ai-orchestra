#!/usr/bin/env python3
"""v1 の引数を受け付け、廃止した処理の代わりに非推奨の案内を返す。"""

from __future__ import annotations

import argparse
import json
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサを構築する。"""
    parser = argparse.ArgumentParser(
        prog="skill_evolution", description="スキル改善 v1 互換 CLI（非推奨: ADR-20261108-065）"
    )
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
    args = build_parser().parse_args(argv)
    payload = {
        "deprecated": True,
        "message": "v1 の処理は非推奨です。ADR-20261108-065 を参照してください。",
    }
    if args.command == "check-trigger":
        payload["triggered"] = False
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if args.command == "check-trigger" else 0


if __name__ == "__main__":
    sys.exit(main())
