#!/usr/bin/env python3
"""quality-gates パッケージの共有設定・状態管理ヘルパー。

複数の hook が共通して必要とする以下の関心事を1箇所に集約する:

- quality_gate.enabled のデフォルト値判定（config 未同期環境での非対称防止）
- プロジェクトスコープの状態キー生成（test-tampering-detector.py の
  get_project_state_key() と同じアルゴリズム）
- プロジェクトスコープの状態ファイル読み書き（JSON, "state_by_project" にネスト）
- テストゲート共有状態のデフォルトスキーマ（DEFAULT_TEST_GATE_STATE）
- 状態ファイルの保存先解決（`.claude/state/` + resolve_path_within。
  evaluation-set-checker.py と同じ規約。Issue #154 で /tmp からの移行に統一）

test-gate-checker.py と post-test-analysis.py は同じ状態ファイルを共有して
ゲート連携するため、それぞれが独自にこのロジックを複製すると実装がずれて
連携が壊れるリスクがある。そのため duplication ではなく共有モジュールとして
切り出す。

状態ファイルの読み書きは複数 worktree/セッションから並行に呼ばれうるため、
`update_project_scoped_state()` は fcntl.flock による排他ロックと
tmp ファイル + os.replace によるアトミック書き込みで
read-modify-write 全体を単一のクリティカルセクションにまとめる。
`update_locked_json_state()` はプロジェクトスコープのネストを持たない
呼び出し元（test-tampering-detector.py 等）向けに同じロック/アトミック
書き込み方式を提供する低レベルプリミティブ。
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
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

from hook_common import load_package_config, resolve_path_within  # noqa: E402
from log_common import find_project_root  # noqa: E402

# features.quality_gate.enabled が config に無い場合のデフォルト値。
# audit-flags.json のベース値 (enabled: true) に合わせることで、
# Edit/Write 警告とテスト結果ブロック判定の非対称を防ぐ。
QUALITY_GATE_ENABLED_DEFAULT = True

# test-gate-checker.py と post-test-analysis.py が共有するテストゲート状態の
# デフォルトスキーマ。両ファイルが独自定義すると schema drift のリスクがある
# ため、ここに一元化して import させる。
DEFAULT_TEST_GATE_STATE: dict = {
    "files_modified_since_test": [],
    "lines_modified_since_test": 0,
    "last_test_result": None,
    "warned": False,
}

# .claude/state/ 配下に状態ファイルを解決する既定ディレクトリ。
# evaluation-set-checker.py と同じ規約（audit-flags.json の paths.state_dir で
# 上書き可能）。/tmp のグローバル共有ファイルはプロジェクト外に置かれるため、
# パストラバーサル対策込みで worktree（= project_dir）配下に閉じ込める。
DEFAULT_STATE_DIR = os.path.join(".claude", "state")


def _sanitize_state_filename(filename: str) -> str:
    """`filename` を単一の安全な basename に制限する。

    PR #191 CodeRabbit レビュー指摘: `resolve_state_path` の最終フォールバックが
    `resolve_path_within` に拒否された（project_dir の外を指す）`filename` を
    そのまま `os.path.join` していたため、絶対パスなら project_dir 接頭辞ごと
    捨てられ、`../` ならそのまま外部へ脱出できてしまっていた。`os.path.basename`
    で末尾要素だけを取り出すことでディレクトリ区切りやトラバーサル要素を除去し、
    basename が空文字・`.`・`..` に潰れる異常系は固定の安全な名前へフォールバック
    する（常にディレクトリ区切りを含まない非空文字列を返す）。
    """
    name = os.path.basename(filename) if filename else ""
    if not name or name in (os.curdir, os.pardir):
        return "invalid-state-filename"
    return name


def resolve_state_path(project_dir: str, filename: str, config: dict | None = None) -> str:
    """<project_dir>/.claude/state/<filename> に解決する（paths.state_dir 上書き対応）。

    quality-gates パッケージ内の唯一の実装（evaluation-set-checker.py もこの
    関数を import して使う。Issue #154 のレビュー指摘: 同名関数の重複実装は
    契約のズレを招くため一本化した）。

    `project_dir` には hook payload の `cwd` がそのまま渡ってくることがある。
    Claude Code がリポジトリのサブディレクトリ（例: `packages/core`）から
    起動された場合、payload の cwd もそのサブディレクトリになるため、正規化
    せずに使うと state ファイルが repo root ではなくサブディレクトリ配下の
    `.claude/state/` にアンカーされてしまう（PR #191 レビュー指摘）。そのため
    `find_project_root()`（log_common、`.claude/` を持つ最寄りの親を探す既存
    ユーティリティ）で project_dir をプロジェクトルートへ正規化してから
    config 読み込み・パス解決を行う。`.claude/` が見つからない場合は元の
    project_dir をそのまま使う（既存の呼び出し元・テストとの後方互換）。

    `config` に呼び出し側が事前読み込みした audit-flags.json の dict を渡すと、
    同一 hook 呼び出し内での重複読み込みを避けられる（省略時は内部で読み込む。
    既存呼び出し元との後方互換のためデフォルト None）。

    `filename` は `_sanitize_state_filename` で単一の安全な basename に丸めて
    から使う（PR #191 CodeRabbit レビュー指摘: 拒否パスを未検証の os.path.join
    で復活させない）。resolve_path_within によるパストラバーサル防御込みで、
    project_dir の外に解決される場合は DEFAULT_STATE_DIR 直下へフォールバック
    する。常に非 None の str を返す。
    """
    normalized_project_dir = find_project_root(project_dir) if project_dir else find_project_root()
    resolved_config = (
        config
        if config is not None
        else load_package_config("audit", "audit-flags.json", normalized_project_dir)
    )
    state_dir_value = resolved_config.get("paths", {}).get("state_dir")
    state_dir = (
        state_dir_value
        if isinstance(state_dir_value, str) and state_dir_value
        else DEFAULT_STATE_DIR
    )
    safe_filename = _sanitize_state_filename(filename)
    resolved = resolve_path_within(normalized_project_dir, state_dir, safe_filename)
    if resolved:
        return resolved
    fallback = resolve_path_within(normalized_project_dir, DEFAULT_STATE_DIR, safe_filename)
    return fallback or os.path.join(normalized_project_dir, DEFAULT_STATE_DIR, safe_filename)


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
    """状態ファイル全体（プロジェクト横断の生データ）を読み込む。

    呼び出し側がロックを保持している前提のヘルパー（ロック取得はしない）。
    """
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
    """状態ファイル全体をアトミックに保存する。

    同一ディレクトリに一時ファイルを書き出してから `os.replace` で
    差し替えることで、書き込み中断時にも既存ファイルが破損しないようにする。
    呼び出し側がロックを保持している前提のヘルパー（ロック取得はしない）。
    """
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=state_file.parent,
            prefix=f".{state_file.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(raw_state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, state_file)
        except OSError:
            tmp_path.unlink(missing_ok=True)
    except OSError:
        pass


def _run_locked(
    state_file: Path,
    fallback: Callable[[], dict],
    body: Callable[[], dict],
) -> dict:
    """`state_file` 隣の `.lock` に対する排他ロックを取得し `body()` を実行する。

    PR #191 CodeRabbit レビュー指摘: `.claude/state` が読み取り専用等の場合、
    ロック用ディレクトリ作成（mkdir）・ロックファイル open・flock 取得のいずれ
    からも `OSError` が発生しうるが、これを呼び出し元（フックの `main()`）まで
    伝播させると「hook は失敗しても Claude Code を止めない」という fail-open
    設計原則に反する。そのため、ロック取得系の `OSError` はすべて `fallback()`
    （ディスクへの永続化を諦め、メモリ上のデフォルト状態に対して処理を続行する
    関数）へのフォールバックに変換し、例外を外へ漏らさない。
    """
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(state_file) + ".lock")
        lock_file = open(lock_path, "w", encoding="utf-8")
    except OSError:
        return fallback()

    try:
        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError:
                return fallback()
            try:
                return body()
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError:
        return fallback()


def update_project_scoped_state(
    state_file: Path,
    project_key: str,
    mutate_fn: Callable[[dict], dict],
    default_state: dict,
) -> dict:
    """プロジェクトスコープの状態を単一トランザクションで読み書きする。

    「flock 取得 → read → mutate_fn(state) → write（tmp + os.replace）→ unlock」を
    1 つのクリティカルセクションで行うことで、load→mutate→save を別々に呼ぶ
    構成に付随する TOCTOU（read-modify-write レース）を構造的に排除する。

    `mutate_fn` は現在のプロジェクト状態（`default_state` とのマージ済み dict）
    を受け取り、永続化すべき新しい状態を返す。in-place で変更して同じ dict を
    返しても、新しい dict を返してもよい。

    Args:
        state_file: プロジェクト横断の状態ファイルパス。
        project_key: `get_project_state_key(project_dir)` で解決したキー。
        mutate_fn: 現在の状態を受け取り、新しい状態を返す関数。
        default_state: プロジェクト未登録時に使うデフォルト状態。

    Returns:
        永続化された新しいプロジェクト状態。ロック取得（mkdir/open/flock）が
        `.claude/state` の読み取り専用化等で失敗した場合は、ディスクへの
        永続化を諦め、`default_state` に `mutate_fn` を適用したメモリ上の
        結果を返す（fail-open。PR #191 CodeRabbit レビュー指摘）。
    """

    def _fallback() -> dict:
        return mutate_fn(copy.deepcopy(default_state))

    def _body() -> dict:
        raw_state = _read_state_file(state_file)
        state_by_project = raw_state.get("state_by_project", {})
        current_project_state = state_by_project.get(project_key)

        merged = copy.deepcopy(default_state)
        if isinstance(current_project_state, dict):
            merged.update(current_project_state)

        new_project_state = mutate_fn(merged)

        state_by_project[project_key] = new_project_state
        raw_state["state_by_project"] = state_by_project
        _write_state_file(state_file, raw_state)
        return new_project_state

    return _run_locked(state_file, _fallback, _body)


def update_locked_json_state(
    state_file: Path,
    mutate_fn: Callable[[dict], dict],
    default_state: dict,
) -> dict:
    """プロジェクトスコープのネストを持たない状態ファイル向けの共通ロック済み更新。

    `update_project_scoped_state` と同じ「flock 取得 → read → mutate_fn →
    write（tmp + os.replace）→ unlock」の単一トランザクションを、
    "state_by_project" ラップ無しでそのまま使いたい呼び出し元
    （test-tampering-detector.py のようにファイル全体を1つのフラットな dict
    として扱うケース）向けに提供する。

    `mutate_fn` は現在の状態（`default_state` とのマージ済み dict）を受け取り、
    永続化すべき新しい状態を返す。in-place で変更して同じ dict を返しても、
    新しい dict を返してもよい。

    Args:
        state_file: 状態ファイルパス。
        mutate_fn: 現在の状態を受け取り、新しい状態を返す関数。
        default_state: ファイル未作成時に使うデフォルト状態。

    Returns:
        永続化された新しい状態。ロック取得（mkdir/open/flock）が
        `.claude/state` の読み取り専用化等で失敗した場合は、ディスクへの
        永続化を諦め、`default_state` に `mutate_fn` を適用したメモリ上の
        結果を返す（fail-open。PR #191 CodeRabbit レビュー指摘）。
    """

    def _fallback() -> dict:
        return mutate_fn(copy.deepcopy(default_state))

    def _body() -> dict:
        raw_state = _read_state_file(state_file)
        merged = copy.deepcopy(default_state)
        if isinstance(raw_state, dict):
            merged.update(raw_state)
        new_state = mutate_fn(merged)
        _write_state_file(state_file, new_state)
        return new_state

    return _run_locked(state_file, _fallback, _body)


def load_project_scoped_state(state_file: Path, project_key: str, default_state: dict) -> dict:
    """プロジェクトごとにネストされた状態を読み込む。

    複数 worktree/セッションが同じ /tmp 状態ファイルを共有しても、
    プロジェクト（git 共通ディレクトリ）が異なれば相互汚染しないようにする。
    on-disk 形式: {"state_by_project": {<project_key>: {...project state...}}}

    `project_key` は呼び出し側が `get_project_state_key(project_dir)` で
    事前に解決した値を渡す（この関数自体は git を意識しない汎用ヘルパー）。

    並行する `update_project_scoped_state` 呼び出しと read が interleave
    しないよう、読み込み自体も同じロックで保護する。

    ロック取得（mkdir/open/flock）が `.claude/state` の読み取り専用化等で
    失敗した場合は、読み込みを諦めて `default_state` のコピーを返す
    （fail-open。PR #191 CodeRabbit レビュー指摘）。
    """

    def _fallback() -> dict:
        return copy.deepcopy(default_state)

    def _body() -> dict:
        raw_state = _read_state_file(state_file)
        state_by_project = raw_state.get("state_by_project", {})
        project_state = state_by_project.get(project_key)

        merged = copy.deepcopy(default_state)
        if isinstance(project_state, dict):
            merged.update(project_state)
        return merged

    return _run_locked(state_file, _fallback, _body)


def save_project_scoped_state(state_file: Path, project_key: str, project_state: dict) -> None:
    """プロジェクトスコープの状態を保存する。

    `project_key` は呼び出し側が `get_project_state_key(project_dir)` で
    事前に解決した値を渡す。内部的には `update_project_scoped_state` を
    再利用し、同じロック/アトミック書き込みで保護する。
    """
    update_project_scoped_state(
        state_file,
        project_key,
        lambda _current: project_state,
        default_state=project_state,
    )
