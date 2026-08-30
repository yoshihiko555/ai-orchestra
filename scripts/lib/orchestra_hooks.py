"""フック・settings 操作を担当する Mixin。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from lib.hook_utils import (
    HOOK_PYTHON_ENV_VAR,
    find_hook_in_settings,
    get_hook_command,
    is_sync_hook_command,
    migrate_hook_interpreters,
)
from lib.hook_utils import (
    add_hook_to_settings as _add_hook,
)
from lib.hook_utils import (
    remove_hook_from_settings as _remove_hook,
)
from lib.orchestra_models import Package
from lib.settings_io import (
    load_orchestra_json,
    load_settings,
    save_orchestra_json,
    save_settings,
)

GIT_METADATA_TIMEOUT_SECONDS = 5

# hook スクリプトが要求する Python の最小バージョン。pyproject.toml の requires-python と
# 揃える（ドリフトはテストで検出する）。
MIN_HOOK_PYTHON_VERSION = (3, 12)

# インタプリタ検証プローブの上限秒。応答しない shim で init を止めないためのガード。
PYTHON_PROBE_TIMEOUT_SECONDS = 5

_VERSION_PROBE_SCRIPT = (
    f"import sys; sys.exit(0 if sys.version_info >= {MIN_HOOK_PYTHON_VERSION} else 1)"
)


def _can_launch_hooks(interpreter: str) -> bool:
    """指定インタプリタで hook を起動できるか、実際に実行して確かめる（Issue #343）。

    PATH 解決（shutil.which）では「その名前のファイルが在る」ことしか分からず、本 Issue が
    対象とする失敗をそのまま見逃す。バージョンマネージャ未適用のログインシェルが解決する
    system python3（macOS CommandLineTools では 3.9 系）や、実体を失った pyenv shim は
    which では解決できてしまうが、hook スクリプトを起動できない。

    判定は「起動でき、かつ requires-python を満たす」ことに限定し、依存モジュール
    （pyyaml）の有無は見ない。hook 側は遅延 import + フォールバックで欠損を吸収する設計で
    あり、pyyaml を持たない 3.12+ は hook を停止させないため。
    """
    try:
        result = subprocess.run(
            [interpreter, "-c", _VERSION_PROBE_SCRIPT],
            capture_output=True,
            timeout=PYTHON_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _git_directories(repo_dir: Path) -> tuple[Path, Path] | None:
    """Git directory と common directory を絶対パスで返す。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir", "--git-common-dir"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=GIT_METADATA_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return None

    def resolve_git_path(raw_path: str) -> Path:
        path = Path(raw_path)
        return path.resolve() if path.is_absolute() else (repo_dir / path).resolve()

    return resolve_git_path(lines[0]), resolve_git_path(lines[1])


def _is_linked_worktree_of(candidate_dir: Path, main_dir: Path) -> bool:
    """candidate が main_dir と同じリポジトリの linked worktree か判定する。"""
    candidate_git = _git_directories(candidate_dir)
    main_git = _git_directories(main_dir)
    if candidate_git is None or main_git is None:
        return False

    candidate_git_dir, candidate_common_dir = candidate_git
    main_git_dir, main_common_dir = main_git
    return (
        candidate_git_dir != candidate_common_dir
        and main_git_dir == main_common_dir
        and candidate_common_dir == main_common_dir
    )


class HooksMixin:
    """OrchestraManager にフック管理機能を提供する Mixin。

    利用側は以下の属性を定義すること。
    """

    orchestra_dir: Path
    SYNC_HOOK_COMMAND: str
    SYNC_HOOK_TIMEOUT: int

    # --- settings / orchestra.json I/O（settings_io に委譲） ---

    @staticmethod
    def load_settings(project_dir: Path) -> dict[str, Any]:
        """settings.local.json をロード"""
        return load_settings(project_dir)

    @staticmethod
    def save_settings(project_dir: Path, settings: dict[str, Any]) -> None:
        """settings.local.json を保存"""
        save_settings(project_dir, settings)

    @staticmethod
    def load_orchestra_json(project_dir: Path) -> dict[str, Any]:
        """orchestra.json をロード"""
        return load_orchestra_json(project_dir)

    @staticmethod
    def save_orchestra_json(project_dir: Path, data: dict[str, Any]) -> None:
        """orchestra.json を保存"""
        save_orchestra_json(project_dir, data)

    # --- hook 操作（hook_utils に委譲） ---

    @staticmethod
    def get_hook_command(pkg_name: str, filename: str) -> str:
        """フックコマンドを生成（$AI_ORCHESTRA_DIR 参照）"""
        return get_hook_command(pkg_name, filename)

    def is_hook_registered(
        self,
        settings: dict[str, Any],
        event: str,
        filename: str,
        pkg_name: str,
        matcher: str | None = None,
    ) -> bool:
        """フックが settings.local.json に登録されているかチェック"""
        hooks = settings.get("hooks", {})
        command = get_hook_command(pkg_name, filename)
        return find_hook_in_settings(hooks, event, command, matcher)

    def _count_registered_hooks(self, pkg: Package, settings: dict[str, Any]) -> tuple[int, int]:
        """パッケージのフック登録状況を集計して (registered, total) を返す"""
        total = sum(len(entries) for entries in pkg.hooks.values())
        registered = sum(
            1
            for event, entries in pkg.hooks.items()
            for entry in entries
            if self.is_hook_registered(settings, event, entry.file, pkg.name, entry.matcher)
        )
        return registered, total

    def _apply_hooks(
        self,
        pkg: Package,
        settings: dict[str, Any],
        action: str,
        dry_run: bool = False,
    ) -> None:
        """フックの登録/削除を一括実行する。action は 'add' または 'remove'。

        登録/削除に先立ち、既存 hook のインタプリタ表記を現行形式へ揃える（Issue #343）。
        旧表記のまま残っていると、新表記の追加が別 hook として並んで二重起動し、削除も
        取りこぼす（どちらもコマンド文字列の完全一致で判定するため）。
        """
        if not dry_run and isinstance(settings.get("hooks"), dict):
            migrate_hook_interpreters(settings["hooks"])

        for event, entries in pkg.hooks.items():
            for entry in entries:
                matcher_info = f" (matcher: {entry.matcher})" if entry.matcher else ""
                if dry_run:
                    verb = "フック登録" if action == "add" else "フック削除"
                    print(f"[DRY-RUN] {verb}: {event} / {entry.file}{matcher_info}")
                elif action == "add":
                    self.add_hook_to_settings(
                        settings, event, entry.file, pkg.name, entry.matcher, entry.timeout
                    )
                else:
                    self.remove_hook_from_settings(
                        settings, event, entry.file, pkg.name, entry.matcher
                    )

    @staticmethod
    def add_hook_to_settings(
        settings: dict[str, Any],
        event: str,
        filename: str,
        pkg_name: str,
        matcher: str | None = None,
        timeout: int = 5,
    ) -> None:
        """settings.local.json にフックを追加"""
        if "hooks" not in settings:
            settings["hooks"] = {}
        command = get_hook_command(pkg_name, filename)
        _add_hook(settings["hooks"], event, command, matcher, timeout)

    @staticmethod
    def remove_hook_from_settings(
        settings: dict[str, Any],
        event: str,
        filename: str,
        pkg_name: str,
        matcher: str | None = None,
    ) -> None:
        """settings.local.json からフックを削除"""
        if "hooks" not in settings or event not in settings["hooks"]:
            return
        command = get_hook_command(pkg_name, filename)
        _remove_hook(settings["hooks"], event, command, matcher)

    @staticmethod
    def _resolve_python_update(env: dict[str, Any]) -> str | None:
        """AI_ORCHESTRA_PYTHON に書き込むべき値を返す。書き込み不要なら None。

        既存値は「その値で hook を起動できる」ときだけ尊重する（Issue #343）。判定は
        PATH 解決ではなく実起動プローブで行う（`_can_launch_hooks` 参照）。which による
        解決可否では、本 Issue が対象とする「解決はできるが hook を動かせないインタプリタ」
        （system python3 = 3.9 系、実体を失った shim 等）を取りこぼすため。

        起動できない値（削除された venv、古すぎる Python 等）を放置すると全 hook が
        起動不能になり、同じ変数で起動する SessionStart 同期による自己修復も失われるため、
        警告のうえ現在のインタプリタで修復する。
        """
        existing = env.get(HOOK_PYTHON_ENV_VAR)
        if not isinstance(existing, str) or not existing:
            return sys.executable

        if _can_launch_hooks(existing):
            print(f"環境変数 {HOOK_PYTHON_ENV_VAR} は設定済み: {existing}")
            return None

        required = f"{MIN_HOOK_PYTHON_VERSION[0]}.{MIN_HOOK_PYTHON_VERSION[1]}"
        print(
            f"警告: 環境変数 {HOOK_PYTHON_ENV_VAR}={existing} では hook を起動できません"
            f"（実行不可、または Python {required} 未満）。{sys.executable} で修復します",
            file=sys.stderr,
        )
        return sys.executable

    def _resolve_env_updates(self, env: dict[str, Any]) -> dict[str, str]:
        """グローバル env に書き込むべき差分を返す。

        AI_ORCHESTRA_DIR（配布元リポジトリの位置）と AI_ORCHESTRA_PYTHON（hook 起動用
        インタプリタ）は独立した関心事として扱う。linked worktree からの実行では
        AI_ORCHESTRA_DIR の更新だけを抑止し、インタプリタの補完は継続する（Issue #343）。
        後者は worktree 依存の値ではないうえ、抑止すると worktree で作業する利用者に
        修正が一切届かなくなるため。

        既に設定済みで有効なキーは差分に含めない。
        """
        updates: dict[str, str] = {}
        orchestra_dir_str = str(self.orchestra_dir)
        existing_orchestra_dir = env.get("AI_ORCHESTRA_DIR")

        if existing_orchestra_dir == orchestra_dir_str:
            print(f"環境変数 AI_ORCHESTRA_DIR は設定済み: {orchestra_dir_str}")
        elif isinstance(existing_orchestra_dir, str) and _is_linked_worktree_of(
            self.orchestra_dir, Path(existing_orchestra_dir)
        ):
            print(
                "警告: linked worktree からの実行を検出したため、"
                f"グローバル AI_ORCHESTRA_DIR={existing_orchestra_dir} を保持します "
                f"(実行元: {orchestra_dir_str})。"
                f"{HOOK_PYTHON_ENV_VAR} の補完は継続します",
                file=sys.stderr,
            )
        else:
            updates["AI_ORCHESTRA_DIR"] = orchestra_dir_str

        python_update = self._resolve_python_update(env)
        if python_update is not None:
            updates[HOOK_PYTHON_ENV_VAR] = python_update

        return updates

    def setup_env_var(self, dry_run: bool = False) -> None:
        """~/.claude/settings.json の env.AI_ORCHESTRA_DIR / AI_ORCHESTRA_PYTHON を設定

        AI_ORCHESTRA_PYTHON は hook コマンドが使うインタプリタ（Issue #343）。
        未設定のときだけ現在の sys.executable を書き込み、PATH 解決に依存しないようにする。
        """
        import json

        global_settings_path = Path.home() / ".claude" / "settings.json"
        global_settings: dict[str, Any] = {}

        if global_settings_path.exists():
            with open(global_settings_path, encoding="utf-8") as f:
                global_settings = json.load(f)

        env = global_settings.get("env", {})
        updates = self._resolve_env_updates(env)
        if not updates:
            return

        if dry_run:
            for key, value in updates.items():
                print(f"[DRY-RUN] 環境変数設定: {key}={value}")
            return

        global_settings["env"] = {**env, **updates}

        global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(global_settings_path, "w", encoding="utf-8") as f:
            json.dump(global_settings, f, indent=2, ensure_ascii=False)
            f.write("\n")

        for key, value in updates.items():
            print(f"環境変数設定: {key}={value}")

    def is_sync_hook_registered(self, settings: dict[str, Any]) -> bool:
        """sync-orchestra の SessionStart hook が登録されているかチェック"""
        hooks = settings.get("hooks", {})
        for entry in hooks.get("SessionStart", []):
            if "matcher" in entry:
                continue
            for hook in entry.get("hooks", []):
                if is_sync_hook_command(hook.get("command", "")):
                    return True
        return False

    def register_sync_hook(self, settings: dict[str, Any], dry_run: bool = False) -> None:
        """sync-orchestra の SessionStart hook を登録"""
        if self.is_sync_hook_registered(settings):
            print("sync-orchestra hook は登録済み")
            return

        if dry_run:
            print("[DRY-RUN] sync-orchestra hook 登録: SessionStart")
            return

        if "hooks" not in settings:
            settings["hooks"] = {}
        if "SessionStart" not in settings["hooks"]:
            settings["hooks"]["SessionStart"] = []

        target_entry = None
        for entry in settings["hooks"]["SessionStart"]:
            if "matcher" not in entry:
                target_entry = entry
                break

        if target_entry is None:
            target_entry = {"hooks": []}
            settings["hooks"]["SessionStart"].append(target_entry)

        target_entry["hooks"].append(
            {
                "type": "command",
                "command": self.SYNC_HOOK_COMMAND,
                "timeout": self.SYNC_HOOK_TIMEOUT,
            }
        )

        print("sync-orchestra hook 登録: SessionStart")

    def remove_sync_hook(self, settings: dict[str, Any]) -> None:
        """sync-orchestra の SessionStart hook を削除"""
        if "hooks" not in settings or "SessionStart" not in settings["hooks"]:
            return

        for entry in settings["hooks"]["SessionStart"]:
            if "matcher" in entry:
                continue
            entry["hooks"] = [
                h for h in entry.get("hooks", []) if not is_sync_hook_command(h.get("command", ""))
            ]

        settings["hooks"]["SessionStart"] = [
            e for e in settings["hooks"]["SessionStart"] if e.get("hooks")
        ]
