"""2 本の CLI の出力・例外処理。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from candidates_ingest import CandidatesContext, build_context
from candidates_safety import CandidatesError, LockTimeout, ValidationError, display, sanitize


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValidationError(message)


def execute(
    parser: argparse.ArgumentParser,
    handler: Callable[[CandidatesContext, argparse.Namespace], dict],
    argv: list[str] | None = None,
) -> int:
    try:
        args = parser.parse_args(argv)
        context = build_context(getattr(args, "project", None))
        payload = handler(context, args)
        print(display(payload, as_json=getattr(args, "json", True)))
        return 0
    except LockTimeout:
        print("[skill-evolution] 台帳のロックを取得できません（待ち時間切れ）", file=sys.stderr)
        return 1
    except CandidatesError as exc:
        print(f"[skill-evolution] {sanitize(str(exc))}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(
            f"[skill-evolution] ファイルを読み書きできません: {sanitize(exc.strerror or 'OS error')}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "[skill-evolution] 記録または設定の形式が正しくないため処理できません", file=sys.stderr
        )
        return 1
