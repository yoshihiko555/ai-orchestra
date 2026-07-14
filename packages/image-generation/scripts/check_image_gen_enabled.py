#!/usr/bin/env python3
"""codex.enabled kill-switch を判定する CLI（EV-16, Issue #133）。

image-generator エージェント / image-gen スキルが画像生成の Step 0 で呼び出し、
cli-tools.yaml の codex.enabled が明示的に無効化されている場合は画像生成を
中止させる。image-generation パッケージは codex exec を直接呼ぶが、これまで
グローバル無効化スイッチ codex.enabled を参照していなかった（実装ギャップ）。

CLI:
    python3 check_image_gen_enabled.py [--project <dir>]

出力契約（呼び出し側の LLM エージェントが grep する）:
    - 有効時: stdout に "ENABLED" を1行出力、exit 0
    - 明示的に無効時: stdout に "DISABLED" を1行出力、
      stderr に理由（codex.enabled: false のため画像生成は利用不可）、exit 3
    - 予期しないエラー（hook_common が import できない、config 読み込み例外等）:
      後方互換優先で fail-open。stdout に "ENABLED"、stderr に警告、exit 0

Note: codex.enabled はセキュリティ境界ではなく運用上の kill-switch（コスト・
レート制御目的）であり、fail-open は hook_common.is_cli_enabled と同一の
後方互換セマンティクスに合わせた意図的な設計判断である。
"""

from __future__ import annotations

import argparse
import os
import sys

EXIT_ENABLED = 0
EXIT_DISABLED = 3

STATUS_ENABLED = "ENABLED"
STATUS_DISABLED = "DISABLED"

DISABLED_REASON = "codex.enabled: false のため画像生成は利用不可"


def _import_hook_common():
    """hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む。

    AI_ORCHESTRA_DIR 未設定時は、このスクリプト自身の位置（ai-orchestra
    チェックアウト内）から packages/core/hooks を解決する。
    """
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if not orchestra_dir:
        orchestra_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
    core_hooks = os.path.join(orchestra_dir, "packages", "core", "hooks")
    if core_hooks not in sys.path:
        sys.path.insert(0, core_hooks)
    import hook_common

    return hook_common


def check_enabled(project_dir: str) -> bool:
    """codex.enabled を判定する。

    cli-tools.yaml（base + .local）を読み込み、is_cli_enabled で判定する。
    セクション欠落・config ファイル不在時は hook_common 側の後方互換フォールバック
    により True（有効）が返る。
    """
    hook_common = _import_hook_common()
    config = hook_common.load_package_config("agent-routing", "cli-tools.yaml", project_dir)
    return hook_common.is_cli_enabled("codex", config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the codex.enabled kill-switch before running image generation.",
    )
    parser.add_argument(
        "--project",
        default=".",
        help="Project directory to resolve cli-tools.yaml from (default: current directory).",
    )
    args = parser.parse_args(argv)

    try:
        enabled = check_enabled(args.project)
    except Exception as e:
        print(STATUS_ENABLED)
        print(f"Warning: failed to evaluate codex.enabled ({e}); failing open.", file=sys.stderr)
        return EXIT_ENABLED

    if not enabled:
        print(STATUS_DISABLED)
        print(DISABLED_REASON, file=sys.stderr)
        return EXIT_DISABLED

    print(STATUS_ENABLED)
    return EXIT_ENABLED


if __name__ == "__main__":
    sys.exit(main())
