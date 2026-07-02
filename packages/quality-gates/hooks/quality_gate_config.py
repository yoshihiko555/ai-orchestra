#!/usr/bin/env python3
"""quality-gates パッケージの共有設定・状態管理ヘルパー。

複数の hook が共通して必要とする以下の関心事を1箇所に集約する:

- quality_gate.enabled のデフォルト値判定（config 未同期環境での非対称防止）
- プロジェクトスコープの状態キー生成（test-tampering-detector.py の
  get_project_state_key() と同じアルゴリズム）
- プロジェクトスコープの状態ファイル読み書き（JSON, "state_by_project" にネスト）

test-gate-checker.py と post-test-analysis.py は同じ状態ファイルを共有して
ゲート連携するため、それぞれが独自にこのロジックを複製すると実装がずれて
連携が壊れるリスクがある。そのため duplication ではなく共有モジュールとして
切り出す。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
else:
    _fallback_core_hooks = Path(__file__).resolve().parents[2] / "core" / "hooks"
    if str(_fallback_core_hooks) not in sys.path:
        sys.path.insert(0, str(_fallback_core_hooks))

from hook_common import load_package_config  # noqa: E402

# features.quality_gate.enabled が config に無い場合のデフォルト値。
# audit-flags.json のベース値 (enabled: true) に合わせることで、
# Edit/Write 警告とテスト結果ブロック判定の非対称を防ぐ。
QUALITY_GATE_ENABLED_DEFAULT = True


def resolve_quality_gate_enabled(quality_gate: dict) -> bool:
    """quality_gate 設定 dict から enabled 判定を行う（デフォルト値を一元化）。"""
    return bool(quality_gate.get("enabled", QUALITY_GATE_ENABLED_DEFAULT))


def is_quality_gate_enabled(project_dir: str) -> bool:
    """audit-flags.json を読み込み quality_gate feature の enabled を判定する。"""
    config = load_package_config("audit", "audit-flags.json", project_dir)
    quality_gate = config.get("features", {}).get("quality_gate", {})
    return resolve_quality_gate_enabled(quality_gate)


def run_git_command(project_dir: str, *args: str) -> str:
    """git コマンドを実行し、成功時は stdout を返す（失敗時は空文字）。"""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_dir,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""

    if result.returncode != 0:
        return ""
    return result.stdout


def get_project_state_key(project_dir: str) -> str:
    """現在の git プロジェクトに対応する安定した状態キーを返す。

    test-tampering-detector.py の同名関数と同じロジック（git-common-dir 優先）。
    同一リポジトリの worktree 間では意図的に状態を共有する。
    """
    common_dir = run_git_command(project_dir, "rev-parse", "--git-common-dir").strip()
    if common_dir:
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = (Path(project_dir) / common_path).resolve()
        return str(common_path)

    top_level = run_git_command(project_dir, "rev-parse", "--show-toplevel").strip()
    if top_level:
        return str(Path(top_level).resolve())

    return str(Path(project_dir).resolve())


def _read_state_file(state_file: Path) -> dict:
    """状態ファイル全体（プロジェクト横断の生データ）を読み込む。"""
    try:
        if state_file.exists():
            with open(state_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_state_file(state_file: Path, raw_state: dict) -> None:
    """状態ファイル全体を保存する。"""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(raw_state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_project_scoped_state(state_file: Path, project_key: str, default_state: dict) -> dict:
    """プロジェクトごとにネストされた状態を読み込む。

    複数 worktree/セッションが同じ /tmp 状態ファイルを共有しても、
    プロジェクト（git 共通ディレクトリ）が異なれば相互汚染しないようにする。
    on-disk 形式: {"state_by_project": {<project_key>: {...project state...}}}

    `project_key` は呼び出し側が `get_project_state_key(project_dir)` で
    事前に解決した値を渡す（この関数自体は git を意識しない汎用ヘルパー）。
    """
    raw_state = _read_state_file(state_file)
    state_by_project = raw_state.get("state_by_project", {})
    project_state = state_by_project.get(project_key)

    merged = copy.deepcopy(default_state)
    if isinstance(project_state, dict):
        merged.update(project_state)
    return merged


def save_project_scoped_state(state_file: Path, project_key: str, project_state: dict) -> None:
    """プロジェクトスコープの状態を保存する。

    `project_key` は呼び出し側が `get_project_state_key(project_dir)` で
    事前に解決した値を渡す。
    """
    raw_state = _read_state_file(state_file)
    state_by_project = raw_state.get("state_by_project", {})
    state_by_project[project_key] = project_state
    raw_state["state_by_project"] = state_by_project
    _write_state_file(state_file, raw_state)
