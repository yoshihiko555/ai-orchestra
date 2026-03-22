"""フック・settings 操作を担当する Mixin。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lib.hook_utils import (
    add_hook_to_settings as _add_hook,
)
from lib.hook_utils import (
    find_hook_in_settings,
    get_hook_command,
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


class HooksMixin:
    """OrchestraManager にフック管理機能を提供する Mixin。

    利用側で以下の属性・メソッドが必要:
    - self.orchestra_dir: Path
    - self.SYNC_HOOK_COMMAND: str
    - self.SYNC_HOOK_TIMEOUT: int
    """

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
        """フックの登録/削除を一括実行する。action は 'add' または 'remove'。"""
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

    def setup_env_var(self, dry_run: bool = False) -> None:
        """~/.claude/settings.json の env.AI_ORCHESTRA_DIR を設定"""
        import json

        global_settings_path = Path.home() / ".claude" / "settings.json"
        global_settings: dict[str, Any] = {}

        if global_settings_path.exists():
            with open(global_settings_path, encoding="utf-8") as f:
                global_settings = json.load(f)

        env = global_settings.get("env", {})
        orchestra_dir_str = str(self.orchestra_dir)

        if env.get("AI_ORCHESTRA_DIR") == orchestra_dir_str:
            print(f"環境変数 AI_ORCHESTRA_DIR は設定済み: {orchestra_dir_str}")
            return

        if dry_run:
            print(f"[DRY-RUN] 環境変数設定: AI_ORCHESTRA_DIR={orchestra_dir_str}")
            return

        env["AI_ORCHESTRA_DIR"] = orchestra_dir_str
        global_settings["env"] = env

        global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(global_settings_path, "w", encoding="utf-8") as f:
            json.dump(global_settings, f, indent=2, ensure_ascii=False)
            f.write("\n")

        print(f"環境変数設定: AI_ORCHESTRA_DIR={orchestra_dir_str}")

    def is_sync_hook_registered(self, settings: dict[str, Any]) -> bool:
        """sync-orchestra の SessionStart hook が登録されているかチェック"""
        hooks = settings.get("hooks", {})
        for entry in hooks.get("SessionStart", []):
            if "matcher" in entry:
                continue
            for hook in entry.get("hooks", []):
                if hook.get("command") == self.SYNC_HOOK_COMMAND:
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
                h for h in entry.get("hooks", []) if h.get("command") != self.SYNC_HOOK_COMMAND
            ]

        settings["hooks"]["SessionStart"] = [
            e for e in settings["hooks"]["SessionStart"] if e.get("hooks")
        ]
