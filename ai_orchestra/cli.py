#!/usr/bin/env python3
"""AI Orchestra CLI entry point.

uv tool install / pipx install 経由でインストールされた場合の
`ai-orchestra` コマンドのエントリポイント。
内部で scripts/orchestra-manager.py の main() に委譲する。
"""

import importlib.util
import sys
from pathlib import Path


def get_orchestra_dir() -> Path:
    """AI Orchestra のルートディレクトリを解決する。

    解決順序:
    1. インストール済みパッケージ（force-include で packages/ が同梱されている場合）
    2. リポジトリ直下（開発モード / editable install）
    """
    pkg_dir = Path(__file__).resolve().parent  # ai_orchestra/

    # インストール済みパッケージ: ai_orchestra/packages/ が存在する
    if (pkg_dir / "packages").is_dir():
        return pkg_dir

    # 開発モード: ai_orchestra/../packages/ （リポジトリルート）
    repo_root = pkg_dir.parent
    if (repo_root / "packages").is_dir():
        return repo_root

    # フォールバック: 環境変数
    import os

    env_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir)

    print("エラー: AI Orchestra ディレクトリが見つかりません", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    """orchestra-manager.py の main() を呼び出す。"""
    if len(sys.argv) >= 2 and sys.argv[1] in ("-v", "--version"):
        from ai_orchestra import __version__

        print(f"orchex {__version__}")
        return

    orchestra_dir = get_orchestra_dir()
    script_path = orchestra_dir / "scripts" / "orchestra-manager.py"

    if not script_path.exists():
        print(f"エラー: {script_path} が見つかりません", file=sys.stderr)
        sys.exit(1)

    # orchestra-manager.py を動的にロードして main() を呼び出す
    spec = importlib.util.spec_from_file_location("orchestra_manager", script_path)
    if spec is None or spec.loader is None:
        print(f"エラー: {script_path} をロードできません", file=sys.stderr)
        sys.exit(1)

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()


if __name__ == "__main__":
    main()
