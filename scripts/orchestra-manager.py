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

    @classmethod
    def from_json(cls, value: str | dict[str, str]) -> "HookEntry":
        """JSON 値から HookEntry を生成"""
        if isinstance(value, str):
            return cls(file=value)
        return cls(file=value["file"], matcher=value.get("matcher"))


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

    def __init__(self, orchestra_dir: Path):
        self.orchestra_dir = orchestra_dir
        self.packages_dir = orchestra_dir / "packages"

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

        print(f"{'PACKAGE':<20} {'STATUS':<15} HOOKS")
        print("-" * 60)

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

            print(f"{name:<20} {status:<15} {hooks_info}")

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
                    self.add_hook_to_settings(settings, event, entry.file, pkg.name, entry.matcher)

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

        # 6. .codex/ テンプレート（既存はスキップ）
        codex_src = templates_dir / "codex"
        if codex_src.is_dir():
            codex_dst = project_dir / ".codex"
            for src_file in codex_src.rglob("*"):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(codex_src)
                dst_file = codex_dst / rel
                if dst_file.exists():
                    print(f"スキップ（既存）: .codex/{rel}")
                    continue
                if dry_run:
                    print(f"[DRY-RUN] テンプレート配置: .codex/{rel}")
                else:
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                    print(f"テンプレート配置: .codex/{rel}")

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
                    self.add_hook_to_settings(settings, event, entry.file, pkg.name, entry.matcher)

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
    install_parser.add_argument("package", help="パッケージ名")
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

    # setup コマンド
    setup_parser = subparsers.add_parser("setup", help="プリセットで一括セットアップ")
    setup_parser.add_argument("preset", help="プリセット名（essential / all）")
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
        manager.install(args.package, args.project, args.dry_run)
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
    elif args.command == "setup":
        manager.setup(args.preset, args.project, args.dry_run)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
