#!/usr/bin/env python3
"""
ai-orchestra パッケージ管理 CLI ツール

パッケージ単位でフック・スクリプトをプロジェクトに導入/削除する。
v2: $AI_ORCHESTRA_DIR + SessionStart 自動同期方式
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HookEntry:
    """フックエントリ（manifest.json の hooks 値）"""

    file: str
    matcher: str | None = None
    timeout: int = 5

    @classmethod
    def from_json(cls, value: str | dict[str, Any]) -> "HookEntry":
        """JSON 値から HookEntry を生成"""
        if isinstance(value, str):
            return cls(file=value)
        return cls(
            file=value["file"],
            matcher=value.get("matcher"),
            timeout=value.get("timeout", 5),
        )


@dataclass
class Package:
    """パッケージ情報"""

    name: str
    version: str
    description: str
    depends: list[str]
    hooks: dict[str, list[HookEntry]]
    files: list[str]
    scripts: list[str]
    config: list[str]
    skills: list[str]
    agents: list[str]
    rules: list[str]
    path: Path

    @classmethod
    def load(cls, manifest_path: Path) -> "Package":
        """manifest.json からパッケージ情報をロード"""
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)

        hooks = {}
        for event, entries in data.get("hooks", {}).items():
            hooks[event] = [HookEntry.from_json(e) for e in entries]

        return cls(
            name=data["name"],
            version=data["version"],
            description=data.get("description", ""),
            depends=data.get("depends", []),
            hooks=hooks,
            files=data.get("files", []),
            scripts=data.get("scripts", []),
            config=data.get("config", []),
            skills=data.get("skills", []),
            agents=data.get("agents", []),
            rules=data.get("rules", []),
            path=manifest_path.parent,
        )


class OrchestraManager:
    """パッケージ管理マネージャー"""

    SYNC_HOOK_COMMAND = 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"'
    SYNC_HOOK_TIMEOUT = 15
    GITIGNORE_BLOCK_START = "# >>> AI Orchestra (.claude) >>>"
    GITIGNORE_BLOCK_END = "# <<< AI Orchestra (.claude) <<<"
    GITIGNORE_CLAUDE_ENTRIES = [
        ".claude/docs/",
        ".claude/logs/",
        ".claude/state/",
        ".claude/checkpoints/",
        ".claude/context/",
        ".claude/Plans.md",
        ".claude/Plans.archive.md",
        
    ]
    CONTEXT_SPECS: tuple[tuple[str, str, str, str], ...] = (
        ("claude", "claude.md", "templates/project/CLAUDE.md", "CLAUDE.md"),
        ("codex", "codex.md", "templates/codex/AGENTS.md", "AGENTS.md"),
        ("gemini", "gemini.md", "templates/gemini/GEMINI.md", ".gemini/GEMINI.md"),
    )
    CONTEXT_SHARED_REL = "templates/context/shared.md"
    COLOR_RESET = "\033[0m"
    COLOR_GREEN = "\033[32m"
    COLOR_YELLOW = "\033[33m"
    COLOR_RED = "\033[31m"
    COLOR_CYAN = "\033[36m"

    def __init__(self, orchestra_dir: Path):
        self.orchestra_dir = orchestra_dir
        self.packages_dir = orchestra_dir / "packages"
        self.use_color = sys.stdout.isatty() and os.getenv("NO_COLOR") is None

    def colorize(self, text: str, color: str | None) -> str:
        """色付き文字列を返す（非TTY/NO_COLORでは無効）"""
        if not color or not self.use_color:
            return text
        return f"{color}{text}{self.COLOR_RESET}"

    def get_status_color(self, status: str) -> str | None:
        """ステータスに対応する色コードを返す"""
        if status == "installed":
            return self.COLOR_GREEN
        if status == "partial":
            return self.COLOR_YELLOW
        if status == "not found":
            return self.COLOR_RED
        if status == "active":
            return self.COLOR_CYAN
        return None

    def load_packages(self) -> dict[str, Package]:
        """全パッケージをロード"""
        packages = {}
        for manifest_path in self.packages_dir.glob("*/manifest.json"):
            pkg = Package.load(manifest_path)
            packages[pkg.name] = pkg
        return packages

    def load_presets(self) -> dict[str, Any]:
        """presets.json を読み込み、__all__ を全パッケージ名に展開"""
        presets_path = self.orchestra_dir / "presets.json"
        if not presets_path.exists():
            print("エラー: presets.json が見つかりません", file=sys.stderr)
            sys.exit(1)

        with open(presets_path, encoding="utf-8") as f:
            presets = json.load(f)

        all_package_names = sorted(self.load_packages().keys())
        for preset in presets.values():
            if preset.get("packages") == "__all__":
                preset["packages"] = all_package_names

        return presets

    def resolve_install_order(self, package_names: list[str]) -> list[str]:
        """依存関係を考慮したインストール順を返す（トポロジカルソート）"""
        packages = self.load_packages()
        target_set = set(package_names)

        # 隣接リスト（依存先 → 依存元）と入次数を構築
        in_degree: dict[str, int] = {name: 0 for name in package_names}
        dependents: dict[str, list[str]] = {name: [] for name in package_names}

        for name in package_names:
            pkg = packages.get(name)
            if not pkg:
                continue
            for dep in pkg.depends:
                if dep in target_set:
                    in_degree[name] += 1
                    dependents[dep].append(name)

        # Kahn のアルゴリズム
        queue = sorted([n for n in package_names if in_degree[n] == 0])
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            for dependent in sorted(dependents[node]):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
                    queue.sort()

        if len(result) != len(package_names):
            # 循環依存がある場合は元の順序で返す
            print("警告: 循環依存が検出されました。元の順序で実行します", file=sys.stderr)
            return package_names

        return result

    def list_packages(self) -> None:
        """パッケージ一覧を表示"""
        packages = self.load_packages()
        for name in sorted(packages.keys()):
            pkg = packages[name]
            print(f"{name:20} {pkg.version:10} {pkg.description}")

    def get_project_dir(self, project_arg: str | None) -> Path:
        """プロジェクトディレクトリを取得"""
        if project_arg:
            return Path(project_arg).resolve()
        if "CLAUDE_PROJECT_DIR" in os.environ:
            return Path(os.environ["CLAUDE_PROJECT_DIR"]).resolve()
        return Path.cwd()

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

    @classmethod
    def build_gitignore_block(cls) -> str:
        """AI Orchestra 管理下の .gitignore ブロックを返す。"""
        lines = [
            cls.GITIGNORE_BLOCK_START,
            *cls.GITIGNORE_CLAUDE_ENTRIES,
            cls.GITIGNORE_BLOCK_END,
        ]
        return "\n".join(lines) + "\n"

    @classmethod
    def merge_gitignore_content(cls, existing: str) -> str:
        """既存 .gitignore 文字列に AI Orchestra ブロックをマージする。"""
        block = cls.build_gitignore_block()
        start = cls.GITIGNORE_BLOCK_START
        end = cls.GITIGNORE_BLOCK_END

        start_idx = existing.find(start)
        end_idx = existing.find(end)
        if start_idx >= 0 and end_idx >= 0 and start_idx < end_idx:
            end_idx += len(end)
            before = existing[:start_idx].rstrip("\n")
            after = existing[end_idx:].lstrip("\n")
            parts = []
            if before:
                parts.append(before)
            parts.append(block.rstrip("\n"))
            if after:
                parts.append(after.rstrip("\n"))
            return "\n\n".join(parts) + "\n"

        # 既存で同等エントリがすべてある場合は追記しない
        all_entries_exist = True
        for entry in cls.GITIGNORE_CLAUDE_ENTRIES:
            if re.search(rf"(?m)^{re.escape(entry)}$", existing) is None:
                all_entries_exist = False
                break
        if all_entries_exist:
            return existing if existing.endswith("\n") else existing + "\n"

        if not existing.strip():
            return block

        base = existing if existing.endswith("\n") else existing + "\n"
        return base + "\n" + block

    def _load_context_file(self, path: Path) -> str:
        """context テンプレートファイルを読み込む（存在しなければ終了）。"""
        if not path.exists():
            print(f"エラー: context テンプレートが見つかりません: {path}", file=sys.stderr)
            sys.exit(1)
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"エラー: context テンプレートの読み込みに失敗: {path} ({e})", file=sys.stderr)
            sys.exit(1)

    def _render_context_content(self, source_rel: str) -> str:
        """templates/context の断片から1つの文書を生成する。"""
        source_path = self.orchestra_dir / "templates" / "context" / source_rel
        shared_path = self.orchestra_dir / self.CONTEXT_SHARED_REL
        source_content = self._load_context_file(source_path)
        shared_content = self._load_context_file(shared_path)

        sections = [
            "<!-- DO NOT EDIT: generated by `orchex context build` -->",
            f"<!-- Sources: templates/context/{source_rel}, {self.CONTEXT_SHARED_REL} -->",
            "",
            source_content,
        ]
        if shared_content:
            sections.extend(["", "---", "", shared_content])
        return "\n".join(sections).rstrip() + "\n"

    def _update_file_if_needed(
        self,
        path: Path,
        content: str,
        label: str,
        dry_run: bool = False,
        skip_if_exists: bool = False,
    ) -> bool:
        """差分がある場合のみファイルを更新する。"""
        if skip_if_exists and path.exists():
            print(f"スキップ（既存）: {label}")
            return False

        existing = None
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                existing = None

        if existing == content:
            print(f"スキップ（差分なし）: {label}")
            return False

        if dry_run:
            print(f"[DRY-RUN] 更新: {label}")
            return True

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"更新: {label}")
        return True

    def context_build(self, dry_run: bool = False) -> int:
        """templates/context から配布テンプレートを再生成する。"""
        changed = 0
        for _, source_rel, template_rel, _ in self.CONTEXT_SPECS:
            dst = self.orchestra_dir / template_rel
            content = self._render_context_content(source_rel)
            label = str(dst.relative_to(self.orchestra_dir))
            if self._update_file_if_needed(dst, content, label, dry_run=dry_run):
                changed += 1

        if changed == 0:
            print("context build: 差分なし")
        return changed

    def context_check(self) -> bool:
        """templates/context 由来の生成結果とテンプレートの一致を検証する。"""
        mismatches: list[str] = []

        for _, source_rel, template_rel, _ in self.CONTEXT_SPECS:
            expected = self._render_context_content(source_rel)
            target = self.orchestra_dir / template_rel
            label = str(target.relative_to(self.orchestra_dir))

            if not target.exists():
                mismatches.append(f"{label} (missing)")
                continue

            try:
                actual = target.read_text(encoding="utf-8")
            except OSError:
                mismatches.append(f"{label} (unreadable)")
                continue

            if actual != expected:
                mismatches.append(f"{label} (outdated)")

        if not mismatches:
            print("context check: OK")
            return True

        print("context check: NG")
        print("不一致ファイル:")
        for item in mismatches:
            print(f"- {item}")
        print("実行コマンド: orchex context build")
        return False

    def context_sync(self, project: str | None, dry_run: bool = False, force: bool = False) -> int:
        """生成済み context をプロジェクトのトップレベル文書へ同期する。"""
        project_dir = self.get_project_dir(project)
        project_root = project_dir.resolve()
        changed = 0
        skip_existing = not force

        for _, source_rel, _, project_rel in self.CONTEXT_SPECS:
            dst = project_dir / project_rel

            # シンボリックリンク経由の意図しない上書きを防ぐ
            if dst.is_symlink():
                print(f"スキップ（安全性）: {project_rel} (symlink)")
                continue
            try:
                dst_parent_resolved = dst.parent.resolve()
            except OSError:
                print(f"スキップ（安全性）: {project_rel} (parent unreadable)")
                continue
            try:
                dst_parent_resolved.relative_to(project_root)
            except ValueError:
                print(f"スキップ（安全性）: {project_rel} (outside project)")
                continue

            content = self._render_context_content(source_rel)
            if self._update_file_if_needed(
                dst,
                content,
                project_rel,
                dry_run=dry_run,
                skip_if_exists=skip_existing,
            ):
                changed += 1

        if changed == 0:
            print("context sync: 差分なし")
        return changed

    def sync_gitignore(self, project_dir: Path, dry_run: bool = False) -> bool:
        """プロジェクトの .gitignore に AI Orchestra ブロックを追加/更新する。"""
        path = project_dir / ".gitignore"
        existing = ""
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                existing = ""

        merged = self.merge_gitignore_content(existing)
        if merged == existing:
            print("スキップ（既存）: .gitignore (AI Orchestra block)")
            return False

        if dry_run:
            print("[DRY-RUN] .gitignore 更新: AI Orchestra block")
            return True

        path.write_text(merged, encoding="utf-8")
        print(".gitignore 更新: AI Orchestra block")
        return True

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

    def has_installed_dependents(
        self, pkg_name: str, installed: list[str], packages: dict[str, "Package"]
    ) -> bool:
        """指定パッケージに依存するインストール済みパッケージがあるか"""
        for inst_name in installed:
            inst_pkg = packages.get(inst_name)
            if inst_pkg and pkg_name in inst_pkg.depends:
                return True
        return False

    def get_package_status(self, pkg: Package, project_dir: Path) -> tuple[str, int, int]:
        """パッケージの導入状況を判定"""
        # orchestra.json ベースでチェック
        orch = self.load_orchestra_json(project_dir)
        installed = orch.get("installed_packages", [])

        if pkg.name in installed:
            # settings にフックが登録されているかも確認
            if not pkg.hooks:
                return ("installed", 0, 0)

            settings = self.load_settings(project_dir)
            total = sum(len(entries) for entries in pkg.hooks.values())
            registered = 0
            for event, entries in pkg.hooks.items():
                for entry in entries:
                    if self.is_hook_registered(
                        settings, event, entry.file, pkg.name, entry.matcher
                    ):
                        registered += 1

            if registered == total:
                return ("installed", registered, total)
            elif registered > 0:
                return ("partial", registered, total)
            else:
                return ("partial", 0, total)

        # orchestra.json にないが、依存元がインストール済みならライブラリとして使用中
        if not pkg.hooks:
            packages = self.load_packages()
            if self.has_installed_dependents(pkg.name, installed, packages):
                return ("active", 0, 0)
            return ("not found", 0, 0)

        settings = self.load_settings(project_dir)
        total = sum(len(entries) for entries in pkg.hooks.values())
        registered = 0
        for event, entries in pkg.hooks.items():
            for entry in entries:
                if self.is_hook_registered(settings, event, entry.file, pkg.name, entry.matcher):
                    registered += 1

        if registered == 0:
            return ("not found", registered, total)
        elif registered == total:
            return ("installed", registered, total)
        else:
            return ("partial", registered, total)

    def status(self, project: str | None) -> None:
        """プロジェクトでのパッケージ導入状況を表示"""
        project_dir = self.get_project_dir(project)
        packages = self.load_packages()

        print(f"{'TAG':<6} {'PACKAGE':<20} {'STATUS':<15} HOOKS")
        print("-" * 70)

        installed_packages: list[str] = []

        for name in sorted(packages.keys()):
            pkg = packages[name]
            status, registered, total = self.get_package_status(pkg, project_dir)

            if not pkg.hooks:
                hooks_info = "(dependency)" if status == "active" else "(library only)"
            elif status == "installed":
                hooks_info = f"{registered}/{total} hooks registered"
            elif status == "partial":
                settings = self.load_settings(project_dir)
                missing = []
                for event, entries in pkg.hooks.items():
                    for entry in entries:
                        if not self.is_hook_registered(
                            settings, event, entry.file, pkg.name, entry.matcher
                        ):
                            missing.append(entry.file)
                hooks_info = (
                    f"{registered}/{total} hooks registered (missing: {', '.join(missing)})"
                )
            else:
                hooks_info = f"{registered}/{total} hooks registered"

            if status == "installed":
                installed_packages.append(name)

            marker = "INST" if status == "installed" else ""
            marker_cell = f"{marker:<6}"
            status_cell = f"{status:<15}"
            marker_color = self.COLOR_GREEN if status == "installed" else None
            status_color = self.get_status_color(status)
            marker_cell = self.colorize(marker_cell, marker_color)
            status_cell = self.colorize(status_cell, status_color)
            print(f"{marker_cell} {name:<20} {status_cell} {hooks_info}")

        if installed_packages:
            print()
            print("Installed packages summary:")
            for installed_name in installed_packages:
                print(f"  - {installed_name}")

    def check_dependencies(self, pkg: Package, installed_packages: set[str]) -> list[str]:
        """依存パッケージのチェック"""
        missing = []
        for dep in pkg.depends:
            if dep not in installed_packages:
                missing.append(dep)
        return missing

    def get_hook_command(self, pkg_name: str, filename: str) -> str:
        """フックコマンドを生成（$AI_ORCHESTRA_DIR 参照）"""
        return f'python3 "$AI_ORCHESTRA_DIR/packages/{pkg_name}/hooks/{filename}"'

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

        # matcher なしのエントリを探す
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

    def run_initial_sync(self, project_dir: Path, dry_run: bool = False) -> None:
        """初回同期を実行（sync-orchestra.py と同等のロジック）"""
        orch = self.load_orchestra_json(project_dir)
        installed = orch.get("installed_packages", [])
        orchestra_dir = orch.get("orchestra_dir", "")

        if not orchestra_dir:
            return

        orchestra_path = Path(orchestra_dir)
        if not orchestra_path.is_dir():
            return

        packages = self.load_packages()
        claude_dir = project_dir / ".claude"
        synced_count = 0

        # パッケージ単位の同期
        for pkg_name in installed:
            if pkg_name not in packages:
                continue
            pkg = packages[pkg_name]
            pkg_dir = orchestra_path / "packages" / pkg_name

            for category in ("skills", "agents", "rules", "config"):
                file_list = getattr(pkg, category, [])
                for rel_path in file_list:
                    # rel_path はカテゴリプレフィックスを含む (例: "config/flags.json")
                    src = pkg_dir / rel_path
                    if not src.exists():
                        continue

                    if src.is_dir():
                        # ディレクトリの場合: 中身を再帰的に展開して個別コピー
                        for src_file in src.rglob("*"):
                            if not src_file.is_file():
                                continue
                            file_rel = str(src_file.relative_to(pkg_dir))
                            dst = claude_dir / file_rel
                            if dry_run:
                                print(f"[DRY-RUN] 同期: {file_rel}")
                                continue
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src_file, dst)
                            synced_count += 1
                    else:
                        if category == "config":
                            # config はパッケージ名サブディレクトリに配置
                            dst = claude_dir / "config" / pkg_name / Path(rel_path).name
                        else:
                            dst = claude_dir / rel_path

                        if dry_run:
                            print(f"[DRY-RUN] 同期: {category}/{rel_path}")
                            continue

                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        synced_count += 1

        if synced_count > 0:
            print(f"{synced_count} ファイルを同期しました")

    def install(
        self,
        package_name: str,
        project: str | None,
        dry_run: bool = False,
        _skip_dep_check: bool = False,
    ) -> None:
        """パッケージをインストール"""
        packages = self.load_packages()
        if package_name not in packages:
            print(f"エラー: パッケージ '{package_name}' が見つかりません", file=sys.stderr)
            sys.exit(1)

        pkg = packages[package_name]
        project_dir = self.get_project_dir(project)

        # 依存チェック（setup 経由の場合は依存順が保証されるためスキップ）
        orch = self.load_orchestra_json(project_dir)
        installed_packages = set(orch.get("installed_packages", []))
        if not _skip_dep_check:
            missing_deps = self.check_dependencies(pkg, installed_packages)
            if missing_deps:
                print(
                    f"警告: 依存パッケージが未インストール: {', '.join(missing_deps)}",
                    file=sys.stderr,
                )

        # 1. 環境変数の設定
        self.setup_env_var(dry_run)

        # 2. config ファイルのコピー
        for file_path in pkg.config:
            if file_path.startswith("config/"):
                filename = Path(file_path).name
                source = pkg.path / file_path
                target = project_dir / ".claude" / "config" / pkg.name / filename
                target.parent.mkdir(parents=True, exist_ok=True)

                if dry_run:
                    print(f"[DRY-RUN] ファイルコピー: {target} <- {source}")
                else:
                    shutil.copy2(source, target)
                    print(f"ファイルコピー: {pkg.name}/{target.name}")

        # 3. settings.local.json にフック登録
        settings = self.load_settings(project_dir)
        for event, entries in pkg.hooks.items():
            for entry in entries:
                if dry_run:
                    print(
                        f"[DRY-RUN] フック登録: {event} / {entry.file}"
                        + (f" (matcher: {entry.matcher})" if entry.matcher else "")
                    )
                else:
                    self.add_hook_to_settings(
                        settings, event, entry.file, pkg.name, entry.matcher, entry.timeout
                    )

        # 4. sync-orchestra の SessionStart hook を登録（初回のみ）
        self.register_sync_hook(settings, dry_run)

        if not dry_run:
            self.save_settings(project_dir, settings)

        # 5. orchestra.json にパッケージ情報を記録
        if not dry_run:
            if pkg.name not in installed_packages:
                installed_packages.add(pkg.name)
            orch["installed_packages"] = sorted(installed_packages)
            orch["orchestra_dir"] = str(self.orchestra_dir)
            orch["last_sync"] = datetime.datetime.now(datetime.UTC).isoformat()
            self.save_orchestra_json(project_dir, orch)

        # 6. 初回同期を実行（skills/agents/rules/config をコピー）
        self.run_initial_sync(project_dir, dry_run)

        if dry_run:
            print(f"\n[DRY-RUN] orchestra.json 記録: installed_packages に '{package_name}' を追加")
        else:
            print(f"\n✓ パッケージ '{package_name}' をインストールしました")

    def uninstall(self, package_name: str, project: str | None, dry_run: bool = False) -> None:
        """パッケージをアンインストール"""
        packages = self.load_packages()
        if package_name not in packages:
            print(f"エラー: パッケージ '{package_name}' が見つかりません", file=sys.stderr)
            sys.exit(1)

        pkg = packages[package_name]
        project_dir = self.get_project_dir(project)

        # 1. settings.local.json からフック削除
        settings = self.load_settings(project_dir)
        for event, entries in pkg.hooks.items():
            for entry in entries:
                if dry_run:
                    print(
                        f"[DRY-RUN] フック削除: {event} / {entry.file}"
                        + (f" (matcher: {entry.matcher})" if entry.matcher else "")
                    )
                else:
                    self.remove_hook_from_settings(
                        settings, event, entry.file, pkg.name, entry.matcher
                    )

        if not dry_run:
            self.save_settings(project_dir, settings)

        # 2. config ファイル削除
        for file_path in pkg.config:
            if file_path.startswith("config/"):
                filename = Path(file_path).name
                target = project_dir / ".claude" / "config" / pkg.name / filename

                if dry_run:
                    if target.exists():
                        print(f"[DRY-RUN] ファイル削除: {target}")
                else:
                    if target.exists():
                        target.unlink()
                        print(f"ファイル削除: {pkg.name}/{target.name}")

        # 3. 同期済みファイル削除（skills/agents/rules）
        claude_dir = project_dir / ".claude"
        for category in ("skills", "agents", "rules"):
            file_list = getattr(pkg, category, [])
            for rel_path in file_list:
                target = claude_dir / category / rel_path
                if dry_run:
                    if target.exists():
                        print(f"[DRY-RUN] 同期ファイル削除: {target}")
                else:
                    if target.exists():
                        target.unlink()
                        print(f"同期ファイル削除: {category}/{rel_path}")

        # 4. orchestra.json からパッケージを削除
        orch = self.load_orchestra_json(project_dir)
        installed = set(orch.get("installed_packages", []))
        if pkg.name in installed:
            installed.discard(pkg.name)
            orch["installed_packages"] = sorted(installed)

            # 全パッケージ削除時は sync hook も削除
            if not installed:
                self.remove_sync_hook(settings)
                self.save_settings(project_dir, settings)

            if dry_run:
                print(f"[DRY-RUN] orchestra.json: '{package_name}' を削除")
            else:
                self.save_orchestra_json(project_dir, orch)

        if not dry_run:
            print(f"\n✓ パッケージ '{package_name}' をアンインストールしました")

    def init(self, project: str | None, dry_run: bool = False) -> None:
        """プロジェクトを初期化（ディレクトリ構造 + テンプレート配置）"""
        project_dir = self.get_project_dir(project)
        templates_dir = self.orchestra_dir / "templates"

        # 1. 環境変数の設定
        self.setup_env_var(dry_run)

        # 2. .claude/ ディレクトリ構造
        claude_dirs = [
            project_dir / ".claude" / "docs",
            project_dir / ".claude" / "docs" / "research",
            project_dir / ".claude" / "docs" / "libraries",
            project_dir / ".claude" / "logs",
            project_dir / ".claude" / "logs" / "orchestration",
            project_dir / ".claude" / "state",
            project_dir / ".claude" / "checkpoints",
        ]
        for d in claude_dirs:
            if dry_run:
                if not d.exists():
                    print(f"[DRY-RUN] ディレクトリ作成: {d.relative_to(project_dir)}")
            else:
                d.mkdir(parents=True, exist_ok=True)

        # 3. .claude/ テンプレートファイル（既存はスキップ）
        project_templates = {
            templates_dir / "project" / "docs" / "DESIGN.md": project_dir
            / ".claude"
            / "docs"
            / "DESIGN.md",
            templates_dir / "project" / "docs" / "libraries" / "_TEMPLATE.md": project_dir
            / ".claude"
            / "docs"
            / "libraries"
            / "_TEMPLATE.md",
            templates_dir / "project" / "docs" / "research" / ".gitkeep": project_dir
            / ".claude"
            / "docs"
            / "research"
            / ".gitkeep",
            templates_dir / "project" / "logs" / "orchestration" / ".gitkeep": project_dir
            / ".claude"
            / "logs"
            / "orchestration"
            / ".gitkeep",
            templates_dir / "project" / "state" / ".gitkeep": project_dir
            / ".claude"
            / "state"
            / ".gitkeep",
            templates_dir / "project" / "checkpoints" / ".gitkeep": project_dir
            / ".claude"
            / "checkpoints"
            / ".gitkeep",
            templates_dir / "project" / "Plans.md": project_dir / ".claude" / "Plans.md",
        }
        for src, dst in project_templates.items():
            if not src.exists():
                continue
            if dst.exists():
                print(f"スキップ（既存）: {dst.relative_to(project_dir)}")
                continue
            if dry_run:
                print(f"[DRY-RUN] テンプレート配置: {dst.relative_to(project_dir)}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"テンプレート配置: {dst.relative_to(project_dir)}")

        # 4. CLAUDE.md（既存はスキップ）
        claude_md_src = templates_dir / "project" / "CLAUDE.md"
        claude_md_dst = project_dir / "CLAUDE.md"
        if claude_md_src.exists():
            if claude_md_dst.exists():
                print("スキップ（既存）: CLAUDE.md")
            elif dry_run:
                print("[DRY-RUN] テンプレート配置: CLAUDE.md")
            else:
                shutil.copy2(claude_md_src, claude_md_dst)
                print("テンプレート配置: CLAUDE.md")

        # 5. .claudeignore（既存はスキップ）
        claudeignore_src = templates_dir / "project" / ".claudeignore"
        claudeignore_dst = project_dir / ".claudeignore"
        if claudeignore_src.exists():
            if claudeignore_dst.exists():
                print("スキップ（既存）: .claudeignore")
            elif dry_run:
                print("[DRY-RUN] テンプレート配置: .claudeignore")
            else:
                shutil.copy2(claudeignore_src, claudeignore_dst)
                print("テンプレート配置: .claudeignore")

        # 5b. .gitignore（AI Orchestra block を追加/更新）
        self.sync_gitignore(project_dir, dry_run)

        # 6. .codex/ テンプレート（既存はスキップ）
        # AGENTS.md はプロジェクトルートに配置（Codex は .codex/ 内ではなくルートを読む）
        codex_src = templates_dir / "codex"
        root_files = {"AGENTS.md"}
        if codex_src.is_dir():
            codex_dst = project_dir / ".codex"
            for src_file in codex_src.rglob("*"):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(codex_src)
                if rel.name in root_files:
                    dst_file = project_dir / rel.name
                    label = rel.name
                else:
                    dst_file = codex_dst / rel
                    label = f".codex/{rel}"
                if dst_file.exists():
                    print(f"スキップ（既存）: {label}")
                    continue
                if dry_run:
                    print(f"[DRY-RUN] テンプレート配置: {label}")
                else:
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                    print(f"テンプレート配置: {label}")

        # 7. .gemini/ テンプレート（既存はスキップ）
        gemini_src = templates_dir / "gemini"
        if gemini_src.is_dir():
            gemini_dst = project_dir / ".gemini"
            for src_file in gemini_src.rglob("*"):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(gemini_src)
                dst_file = gemini_dst / rel
                if dst_file.exists():
                    print(f"スキップ（既存）: .gemini/{rel}")
                    continue
                if dry_run:
                    print(f"[DRY-RUN] テンプレート配置: .gemini/{rel}")
                else:
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                    print(f"テンプレート配置: .gemini/{rel}")

        # 8. orchestra.json の初期化
        orch = self.load_orchestra_json(project_dir)
        if not orch.get("orchestra_dir"):
            orch["orchestra_dir"] = str(self.orchestra_dir)
        orch.setdefault("installed_packages", [])
        if dry_run:
            print("[DRY-RUN] orchestra.json 初期化")
        else:
            self.save_orchestra_json(project_dir, orch)
            print("orchestra.json 初期化")

        # 9. sync-orchestra の SessionStart hook を登録
        settings = self.load_settings(project_dir)
        self.register_sync_hook(settings, dry_run)
        if not dry_run:
            self.save_settings(project_dir, settings)

        # 10. 初回同期（skills/agents/rules/config をコピー）
        self.run_initial_sync(project_dir, dry_run)

        if not dry_run:
            print(f"\n✓ プロジェクトを初期化しました: {project_dir}")

    def enable(self, package_name: str, project: str | None, dry_run: bool = False) -> None:
        """パッケージを有効化（settings.local.json にフック登録を復元）"""
        packages = self.load_packages()
        if package_name not in packages:
            print(f"エラー: パッケージ '{package_name}' が見つかりません", file=sys.stderr)
            sys.exit(1)

        pkg = packages[package_name]
        project_dir = self.get_project_dir(project)

        settings = self.load_settings(project_dir)
        for event, entries in pkg.hooks.items():
            for entry in entries:
                if dry_run:
                    print(
                        f"[DRY-RUN] フック登録: {event} / {entry.file}"
                        + (f" (matcher: {entry.matcher})" if entry.matcher else "")
                    )
                else:
                    self.add_hook_to_settings(
                        settings, event, entry.file, pkg.name, entry.matcher, entry.timeout
                    )

        if not dry_run:
            self.save_settings(project_dir, settings)
            print(f"\n✓ パッケージ '{package_name}' を有効化しました")

    def disable(self, package_name: str, project: str | None, dry_run: bool = False) -> None:
        """パッケージを無効化（settings.local.json からフック登録を削除）"""
        packages = self.load_packages()
        if package_name not in packages:
            print(f"エラー: パッケージ '{package_name}' が見つかりません", file=sys.stderr)
            sys.exit(1)

        pkg = packages[package_name]
        project_dir = self.get_project_dir(project)

        settings = self.load_settings(project_dir)
        for event, entries in pkg.hooks.items():
            for entry in entries:
                if dry_run:
                    print(
                        f"[DRY-RUN] フック削除: {event} / {entry.file}"
                        + (f" (matcher: {entry.matcher})" if entry.matcher else "")
                    )
                else:
                    self.remove_hook_from_settings(
                        settings, event, entry.file, pkg.name, entry.matcher
                    )

        if not dry_run:
            self.save_settings(project_dir, settings)
            print(f"\n✓ パッケージ '{package_name}' を無効化しました")

    def resolve_script_path(self, pkg: Package, script_name: str) -> Path | None:
        """スクリプト名を解決してファイルパスを返す

        manifest の scripts エントリと照合し、実ファイルのパスを返す。
        短縮名（例: dashboard）、ファイル名（例: dashboard.py）、
        フルパス（例: scripts/dashboard.py）のいずれも受け付ける。
        """
        for entry in pkg.scripts:
            entry_path = Path(entry)
            # エントリのファイル名部分（拡張子なし）
            stem = entry_path.stem

            if script_name in (entry, entry_path.name, stem):
                # 実ファイルパスを構築
                if entry_path.parts[0] == "scripts":
                    return pkg.path / entry
                return pkg.path / "scripts" / entry_path.name
        return None

    def run_script(
        self,
        package_name: str,
        script_name: str,
        project: str | None,
        script_args: list[str],
    ) -> None:
        """パッケージのスクリプトを実行"""
        packages = self.load_packages()
        if package_name not in packages:
            print(f"エラー: パッケージ '{package_name}' が見つかりません", file=sys.stderr)
            sys.exit(1)

        pkg = packages[package_name]
        if not pkg.scripts:
            print(
                f"エラー: パッケージ '{package_name}' にスクリプトは定義されていません",
                file=sys.stderr,
            )
            sys.exit(1)

        script_path = self.resolve_script_path(pkg, script_name)
        if script_path is None:
            available = [Path(s).stem for s in pkg.scripts]
            print(
                f"エラー: スクリプト '{script_name}' が見つかりません\n"
                f"利用可能: {', '.join(available)}",
                file=sys.stderr,
            )
            sys.exit(1)

        if not script_path.exists():
            print(
                f"エラー: スクリプトファイルが存在しません: {script_path}",
                file=sys.stderr,
            )
            sys.exit(1)

        project_dir = self.get_project_dir(project)
        cmd = [sys.executable, str(script_path)] + script_args
        result = subprocess.run(cmd, cwd=project_dir)
        sys.exit(result.returncode)

    def list_scripts(self, package_filter: str | None = None) -> None:
        """パッケージのスクリプト一覧を表示"""
        packages = self.load_packages()

        if package_filter:
            if package_filter not in packages:
                print(
                    f"エラー: パッケージ '{package_filter}' が見つかりません",
                    file=sys.stderr,
                )
                sys.exit(1)
            target_packages = {package_filter: packages[package_filter]}
        else:
            target_packages = packages

        rows: list[tuple[str, str, str]] = []
        for name in sorted(target_packages.keys()):
            pkg = target_packages[name]
            for entry in pkg.scripts:
                entry_path = Path(entry)
                short_name = entry_path.stem
                # 表示用パス: 常に scripts/ プレフィックス付き
                if entry_path.parts[0] == "scripts":
                    display_path = entry
                else:
                    display_path = f"scripts/{entry_path.name}"
                rows.append((name, short_name, display_path))

        if not rows:
            print("スクリプトが見つかりません")
            return

        print(f"{'PACKAGE':<20} {'SCRIPT':<30} {'PATH'}")
        for pkg_name, short_name, display_path in rows:
            print(f"{pkg_name:<20} {short_name:<30} {display_path}")

    def list_presets(self) -> None:
        """プリセット一覧を表示"""
        presets = self.load_presets()
        print(f"{'PRESET':<15} {'PACKAGES':<40} DESCRIPTION")
        print("-" * 80)
        for name in sorted(presets.keys()):
            preset = presets[name]
            description = preset.get("description", "")
            packages = preset["packages"]
            if isinstance(packages, list):
                pkg_str = ", ".join(packages)
            else:
                pkg_str = str(packages)
            print(f"{name:<15} {pkg_str:<40} {description}")

    def setup(self, preset_name: str, project: str | None, dry_run: bool = False) -> None:
        """プリセットを使って一括セットアップ"""
        presets = self.load_presets()
        if preset_name not in presets:
            available = ", ".join(sorted(presets.keys()))
            print(
                f"エラー: プリセット '{preset_name}' が見つかりません\n利用可能: {available}",
                file=sys.stderr,
            )
            sys.exit(1)

        preset = presets[preset_name]
        package_names = preset["packages"]
        description = preset.get("description", "")
        ordered = self.resolve_install_order(package_names)

        # インストール済みパッケージを取得
        project_dir = self.get_project_dir(project)
        orch = self.load_orchestra_json(project_dir)
        already_installed = set(orch.get("installed_packages", []))

        total_steps = 1 + len(ordered)  # init + パッケージ数

        print(f"\n=== AI Orchestra セットアップ: {preset_name} ===")
        if description:
            print(description)
        print()

        if dry_run:
            print("[DRY-RUN] 以下のパッケージをインストールします:")
            for i, name in enumerate(ordered, 1):
                skip = " (スキップ: インストール済み)" if name in already_installed else ""
                print(f"  [{i + 1}/{total_steps}] {name}{skip}")
            print()

        # 1. init
        step = 1
        print(f"[{step}/{total_steps}] プロジェクト初期化...")
        self.init(project, dry_run)
        print()

        # 2. 各パッケージをインストール
        installed_count = 0
        skipped_count = 0
        for i, pkg_name in enumerate(ordered):
            step = i + 2
            if pkg_name in already_installed:
                print(f"[{step}/{total_steps}] {pkg_name} はインストール済み（スキップ）")
                skipped_count += 1
                continue

            print(f"[{step}/{total_steps}] {pkg_name} をインストール中...")
            self.install(pkg_name, project, dry_run, _skip_dep_check=True)
            already_installed.add(pkg_name)
            installed_count += 1
            print()

        # サマリー
        print("=== セットアップ完了 ===")
        all_names = ", ".join(ordered)
        if skipped_count > 0:
            print(
                f"インストール済み: {all_names} ({len(ordered)} パッケージ, "
                f"新規: {installed_count}, スキップ: {skipped_count})"
            )
        else:
            print(f"インストール済み: {all_names} ({len(ordered)} パッケージ)")

    # ------------------------------------------------------------------
    # proxy 管理
    # ------------------------------------------------------------------

    def _load_proxy_modules(self) -> tuple:
        """proxy_manager と hook_common をインポートして返す。"""
        core_hooks = str(self.orchestra_dir / "packages" / "core" / "hooks")
        cocoindex_hooks = str(self.orchestra_dir / "packages" / "cocoindex" / "hooks")
        for p in [core_hooks, cocoindex_hooks]:
            if p not in sys.path:
                sys.path.insert(0, p)

        import hook_common
        import proxy_manager

        return hook_common, proxy_manager

    def proxy_stop(self, project: str | None) -> None:
        """mcp-proxy を停止する"""
        hook_common, proxy_manager = self._load_proxy_modules()
        project_dir = self.get_project_dir(project)

        config = hook_common.load_package_config("cocoindex", "cocoindex.yaml", str(project_dir))
        if not config:
            print("エラー: cocoindex パッケージがインストールされていません", file=sys.stderr)
            sys.exit(1)

        if not proxy_manager.is_proxy_running(config, str(project_dir)):
            print("mcp-proxy は停止しています")
            return

        if proxy_manager.stop_proxy(config, str(project_dir)):
            print("✓ mcp-proxy を停止しました")
        else:
            print("エラー: mcp-proxy の停止に失敗しました", file=sys.stderr)
            sys.exit(1)

    def proxy_status(self, project: str | None) -> None:
        """mcp-proxy の状態を表示する"""
        hook_common, proxy_manager = self._load_proxy_modules()
        project_dir = self.get_project_dir(project)

        config = hook_common.load_package_config("cocoindex", "cocoindex.yaml", str(project_dir))
        if not config:
            print("エラー: cocoindex パッケージがインストールされていません", file=sys.stderr)
            sys.exit(1)

        proxy_cfg = proxy_manager.get_proxy_config(config, str(project_dir))
        pid_path = proxy_manager.resolve_pid_path(config, str(project_dir))
        running = proxy_manager.is_proxy_running(config, str(project_dir))
        # is_proxy_running が stale PID をクリーンアップするため、その後に読む
        pid = proxy_manager._read_pid(pid_path)

        print(f"状態:   {'稼働中' if running else '停止'}")
        print(f"PID:    {pid or '-'}")
        print(f"ポート: {proxy_cfg['host']}:{proxy_cfg['port']}")
        print(f"PIDファイル: {pid_path}")


def main():
    """メインエントリポイント"""
    parser = argparse.ArgumentParser(
        description="ai-orchestra パッケージ管理 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--orchestra-dir",
        type=Path,
        help="ai-orchestra ディレクトリ（デフォルト: スクリプトの親の親）",
    )

    subparsers = parser.add_subparsers(dest="command", help="サブコマンド")

    # init コマンド
    init_parser = subparsers.add_parser("init", help="プロジェクトを初期化")
    init_parser.add_argument("--project", help="プロジェクトパス")
    init_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    # list コマンド
    subparsers.add_parser("list", help="パッケージ一覧を表示")

    # status コマンド
    status_parser = subparsers.add_parser("status", help="パッケージ導入状況を表示")
    status_parser.add_argument("--project", help="プロジェクトパス")

    # install コマンド
    install_parser = subparsers.add_parser("install", help="パッケージをインストール")
    install_parser.add_argument("package", nargs="+", help="パッケージ名（複数指定可）")
    install_parser.add_argument("--project", help="プロジェクトパス")
    install_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    # uninstall コマンド
    uninstall_parser = subparsers.add_parser("uninstall", help="パッケージをアンインストール")
    uninstall_parser.add_argument("package", help="パッケージ名")
    uninstall_parser.add_argument("--project", help="プロジェクトパス")
    uninstall_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    # enable コマンド
    enable_parser = subparsers.add_parser("enable", help="パッケージを有効化")
    enable_parser.add_argument("package", help="パッケージ名")
    enable_parser.add_argument("--project", help="プロジェクトパス")
    enable_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    # disable コマンド
    disable_parser = subparsers.add_parser("disable", help="パッケージを無効化")
    disable_parser.add_argument("package", help="パッケージ名")
    disable_parser.add_argument("--project", help="プロジェクトパス")
    disable_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    # run コマンド
    run_parser = subparsers.add_parser(
        "run",
        help="パッケージのスクリプトを実行",
        description="パッケージに含まれるスクリプトを実行する。"
        " -- 以降の引数はスクリプトにパススルーされる。",
    )
    run_parser.add_argument("package", help="パッケージ名")
    run_parser.add_argument("script", help="スクリプト名（短縮名 or フルパス）")
    run_parser.add_argument("--project", help="プロジェクトパス")

    # scripts コマンド
    scripts_parser = subparsers.add_parser("scripts", help="スクリプト一覧を表示")
    scripts_parser.add_argument("--package", help="特定パッケージのみ表示")

    # context コマンド
    context_parser = subparsers.add_parser(
        "context",
        help="CLAUDE.md / AGENTS.md / GEMINI.md テンプレート管理",
    )
    context_sub = context_parser.add_subparsers(dest="context_command", help="context サブコマンド")

    context_build_parser = context_sub.add_parser(
        "build",
        help="templates/context から配布テンプレートを再生成",
    )
    context_build_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    context_sub.add_parser(
        "check",
        help="templates/context 由来の生成結果と配布テンプレートの一致を検証",
    )

    context_sync_parser = context_sub.add_parser(
        "sync",
        help="生成ルールに基づいてプロジェクトのトップレベル文書へ同期",
    )
    context_sync_parser.add_argument("--project", help="プロジェクトパス")
    context_sync_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")
    context_sync_parser.add_argument(
        "--force",
        action="store_true",
        help="既存ファイルも上書きする（デフォルトは既存ファイルをスキップ）",
    )

    # proxy コマンド
    proxy_parser = subparsers.add_parser("proxy", help="mcp-proxy の管理")
    proxy_sub = proxy_parser.add_subparsers(dest="proxy_command", help="proxy サブコマンド")
    proxy_stop_parser = proxy_sub.add_parser("stop", help="mcp-proxy を停止")
    proxy_stop_parser.add_argument("--project", help="プロジェクトパス")
    proxy_status_parser = proxy_sub.add_parser("status", help="mcp-proxy の状態を表示")
    proxy_status_parser.add_argument("--project", help="プロジェクトパス")

    # setup コマンド
    setup_parser = subparsers.add_parser("setup", help="プリセットで一括セットアップ")
    setup_parser.add_argument(
        "preset", nargs="?", default=None, help="プリセット名（省略時は一覧表示）"
    )
    setup_parser.add_argument("--project", help="プロジェクトパス")
    setup_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    # run コマンドの -- 以降をスクリプト引数として分離
    argv = sys.argv[1:]
    script_args: list[str] = []
    if "--" in argv:
        sep_idx = argv.index("--")
        script_args = argv[sep_idx + 1 :]
        argv = argv[:sep_idx]

    args = parser.parse_args(argv)

    # orchestra_dir の決定
    if args.orchestra_dir:
        orchestra_dir = args.orchestra_dir.resolve()
    else:
        # スクリプトの親の親
        orchestra_dir = Path(__file__).parent.parent.resolve()

    manager = OrchestraManager(orchestra_dir)

    # コマンド実行
    if args.command == "init":
        manager.init(args.project, args.dry_run)
    elif args.command == "list":
        manager.list_packages()
    elif args.command == "status":
        manager.status(args.project)
    elif args.command == "install":
        if len(args.package) == 1:
            manager.install(args.package[0], args.project, args.dry_run)
        else:
            ordered = manager.resolve_install_order(args.package)
            for pkg_name in ordered:
                manager.install(pkg_name, args.project, args.dry_run, _skip_dep_check=True)
    elif args.command == "uninstall":
        manager.uninstall(args.package, args.project, args.dry_run)
    elif args.command == "enable":
        manager.enable(args.package, args.project, args.dry_run)
    elif args.command == "disable":
        manager.disable(args.package, args.project, args.dry_run)
    elif args.command == "run":
        manager.run_script(args.package, args.script, args.project, script_args)
    elif args.command == "scripts":
        manager.list_scripts(args.package)
    elif args.command == "context":
        if args.context_command == "build":
            manager.context_build(args.dry_run)
        elif args.context_command == "check":
            ok = manager.context_check()
            if not ok:
                sys.exit(1)
        elif args.context_command == "sync":
            manager.context_sync(args.project, args.dry_run, args.force)
        else:
            context_parser.print_help()
            sys.exit(1)
    elif args.command == "proxy":
        if args.proxy_command == "stop":
            manager.proxy_stop(args.project)
        elif args.proxy_command == "status":
            manager.proxy_status(args.project)
        else:
            proxy_parser.print_help()
            sys.exit(1)
    elif args.command == "setup":
        if args.preset is None:
            manager.list_presets()
        else:
            manager.setup(args.preset, args.project, args.dry_run)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
