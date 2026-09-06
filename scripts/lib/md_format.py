"""生成 Markdown を prettier で整形するユーティリティ。

facet build の出力は yaml.safe_dump 由来の frontmatter と facets ソースの
連結でできており、prettier を通していない。一方でエディタ（Zed の
format_on_save）や PostToolUse hook（lint-on-save.py）は同じファイルを
prettier で整形するため、整形済み / 未整形の間を往復して大量の差分が出る。
生成時点で prettier を通し、この往復を断つ。

prettier が解決できない環境では警告のみを出して原文をそのまま残す
（SessionStart の facet build を失敗させないため）。
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

# lint-on-save.py の node_tool_commands と同じ解決順。
# scripts/ から packages/ の hook を import しないため意図的に複製している。
#
# `npm exec` / `npx` には未導入時のインストールを禁じるフラグを付けている。
# 生成物を整形するツールが暗黙にダウンロードされると、実行環境ごとに
# prettier のバージョンが変わり、整形結果＝コミット差分がぶれるため。
PRETTIER_COMMAND_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("pnpm", "exec", "prettier"),
    ("npm", "exec", "--no", "--", "prettier"),
    ("yarn", "prettier"),
    ("npx", "--no-install", "prettier"),
    ("prettier",),
)

# sync_engine.py は facet build を timeout=30 で起動する。整形がそれを超えると
# build ごと打ち切られて生成物が中途半端に残るため、候補の選別から整形完了までの
# 合計をこの予算に収める。実測では 63 ファイルの一括整形で約 1.6 秒。
FORMAT_BUDGET_SEC = 20.0

# 1 候補あたりの `--version` の待ち時間。全候補が沈黙しても予算内に収める。
PROBE_TIMEOUT_SEC = 2.0

MAX_WARNING_OUTPUT_CHARS = 200


def format_markdown_files(paths: Sequence[Path], cwd: Path) -> bool:
    """Markdown ファイル群を prettier で一括整形する。

    整形できた場合のみ True を返す。prettier が見つからない場合や実行に
    失敗した場合は警告を出して False を返し、ファイルは原文のまま残す。
    """
    targets = [str(path) for path in paths if path.is_file()]
    if not targets:
        return True

    deadline = time.monotonic() + FORMAT_BUDGET_SEC
    prefix = _resolve_prettier_command(cwd, deadline)
    if prefix is None:
        _warn_format_skipped("")
        return False

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _warn_format_skipped("prettier timed out")
        return False

    try:
        result = subprocess.run(
            [*prefix, "--write", *targets],
            capture_output=True,
            text=True,
            timeout=remaining,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        _warn_format_skipped("prettier timed out")
        return False

    if result.returncode == 0:
        return True

    # ここまで来たら prettier は起動できている。残る失敗はパースエラー等の
    # 実質的な失敗なので、別のランチャーで再試行せずそのまま報告する。
    _warn_format_skipped(((result.stdout or "") + (result.stderr or "")).strip())
    return False


def _resolve_prettier_command(cwd: Path, deadline: float) -> tuple[str, ...] | None:
    """prettier を起動できるコマンド候補を 1 つ選ぶ。

    `--version` で選別してから `--write` する。ランチャーは prettier が
    無いときも独自のエラーで非ゼロ終了する（例: `pnpm exec` の
    ERR_PNPM_RECURSIVE_EXEC_NO_PACKAGE）ため、終了コードやメッセージだけでは
    「起動できなかった」と「整形に失敗した」を区別できない。
    """
    for prefix in PRETTIER_COMMAND_PREFIXES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            probe = subprocess.run(
                [*prefix, "--version"],
                capture_output=True,
                text=True,
                timeout=min(PROBE_TIMEOUT_SEC, remaining),
                cwd=str(cwd),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return prefix
    return None


def _warn_format_skipped(last_output: str) -> None:
    """整形をスキップした理由を警告として出力する。"""
    if not last_output:
        print(
            "[facet] warn: prettier not found; generated Markdown left unformatted",
            file=sys.stderr,
        )
        return

    detail = last_output[:MAX_WARNING_OUTPUT_CHARS]
    print(
        f"[facet] warn: prettier failed; generated Markdown left unformatted: {detail}",
        file=sys.stderr,
    )
