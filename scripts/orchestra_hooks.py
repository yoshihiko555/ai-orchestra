"""フック・settings 操作を担当する Mixin。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from orchestra_models import Package  # noqa: E402


class HooksMixin:
    """OrchestraManager にフック管理機能を提供する Mixin。

    利用側で以下の属性・メソッドが必要:
    - self.orchestra_dir: Path
    - self.SYNC_HOOK_COMMAND: str
    - self.SYNC_HOOK_TIMEOUT: int
    """

    def load_settings(self, project_dir: Path) -> dict[str, Any]:
        """settings.local.json をロード"""
        settings_path = project_dir / ".claude" / "settings.local.json"
        if not settings_path.exists():
            return {"hooks": {}}
        with open(settings_path, encoding="utf-8") as f:
            return json.load(f)

    def save_settings(self, project_dir: Path, settings: dict[str, Any]) -> None:
        """settings.local.json を保存"""
        settings_path = project_dir / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def load_orchestra_json(self, project_dir: Path) -> dict[str, Any]:
        """orchestra.json をロード"""
        path = project_dir / ".claude" / "orchestra.json"
        if not path.exists():
            return {"installed_packages": [], "orchestra_dir": "", "last_sync": ""}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save_orchestra_json(self, project_dir: Path, data: dict[str, Any]) -> None:
        """orchestra.json を保存"""
        path = project_dir / ".claude" / "orchestra.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def get_hook_command(self, pkg_name: str, filename: str) -> str:
        """フックコマンドを生成（$AI_ORCHESTRA_DIR 参照）"""
        return f'python3 "$AI_ORCHESTRA_DIR/packages/{pkg_name}/hooks/{filename}"'

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
        if event not in hooks:
            return False

        command = self.get_hook_command(pkg_name, filename)

        for entry in hooks[event]:
            if matcher:
                if entry.get("matcher") != matcher:
                    continue
            else:
                if "matcher" in entry:
                    continue

            for hook in entry.get("hooks", []):
                if hook.get("command") == command:
                    return True

        return False

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

    def add_hook_to_settings(
        self,
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
        if event not in settings["hooks"]:
            settings["hooks"][event] = []

        command = self.get_hook_command(pkg_name, filename)
        hook_obj = {"type": "command", "command": command, "timeout": timeout}

        target_entry = None
        for entry in settings["hooks"][event]:
            if matcher:
                if entry.get("matcher") == matcher:
                    target_entry = entry
                    break
            else:
                if "matcher" not in entry:
                    target_entry = entry
                    break

        if target_entry is None:
            target_entry = {"hooks": []}
            if matcher:
                target_entry["matcher"] = matcher
            settings["hooks"][event].append(target_entry)

        for hook in target_entry["hooks"]:
            if hook.get("command") == command:
                return

        target_entry["hooks"].append(hook_obj)

    def remove_hook_from_settings(
        self,
        settings: dict[str, Any],
        event: str,
        filename: str,
        pkg_name: str,
        matcher: str | None = None,
    ) -> None:
        """settings.local.json からフックを削除"""
        if "hooks" not in settings or event not in settings["hooks"]:
            return

        command = self.get_hook_command(pkg_name, filename)

        for entry in settings["hooks"][event]:
            if matcher:
                if entry.get("matcher") != matcher:
                    continue
            else:
                if "matcher" in entry:
                    continue

            entry["hooks"] = [h for h in entry.get("hooks", []) if h.get("command") != command]

        settings["hooks"][event] = [e for e in settings["hooks"][event] if e.get("hooks")]

    def setup_env_var(self, dry_run: bool = False) -> None:
        """~/.claude/settings.json の env.AI_ORCHESTRA_DIR を設定"""
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
