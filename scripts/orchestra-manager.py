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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


@dataclass
class HookEntry:
    """フックエントリ（manifest.json の hooks 値）"""
    file: str
    matcher: Optional[str] = None

    @classmethod
    def from_json(cls, value: Union[str, Dict[str, str]]) -> "HookEntry":
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
    depends: List[str]
    hooks: Dict[str, List[HookEntry]]
    files: List[str]
    scripts: List[str]
    config: List[str]
    skills: List[str]
    agents: List[str]
    rules: List[str]
    path: Path

    @classmethod
    def load(cls, manifest_path: Path) -> "Package":
        """manifest.json からパッケージ情報をロード"""
        with open(manifest_path, "r", encoding="utf-8") as f:
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

    def load_packages(self) -> Dict[str, Package]:
        """全パッケージをロード"""
        packages = {}
        for manifest_path in self.packages_dir.glob("*/manifest.json"):
            pkg = Package.load(manifest_path)
            packages[pkg.name] = pkg
        return packages

    def list_packages(self) -> None:
        """パッケージ一覧を表示"""
        packages = self.load_packages()
        for name in sorted(packages.keys()):
            pkg = packages[name]
            print(f"{name:20} {pkg.version:10} {pkg.description}")

    def get_project_dir(self, project_arg: Optional[str]) -> Path:
        """プロジェクトディレクトリを取得"""
        if project_arg:
            return Path(project_arg).resolve()
        if "CLAUDE_PROJECT_DIR" in os.environ:
            return Path(os.environ["CLAUDE_PROJECT_DIR"]).resolve()
        return Path.cwd()

    def load_settings(self, project_dir: Path) -> Dict[str, Any]:
        """settings.local.json をロード"""
        settings_path = project_dir / ".claude" / "settings.local.json"
        if not settings_path.exists():
            return {"hooks": {}}
        with open(settings_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_settings(self, project_dir: Path, settings: Dict[str, Any]) -> None:
        """settings.local.json を保存"""
        settings_path = project_dir / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def load_orchestra_json(self, project_dir: Path) -> Dict[str, Any]:
        """orchestra.json をロード"""
        path = project_dir / ".claude" / "orchestra.json"
        if not path.exists():
            return {"installed_packages": [], "orchestra_dir": "", "last_sync": ""}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_orchestra_json(self, project_dir: Path, data: Dict[str, Any]) -> None:
        """orchestra.json を保存"""
        path = project_dir / ".claude" / "orchestra.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def is_hook_registered(
        self, settings: Dict[str, Any], event: str, filename: str,
        pkg_name: str, matcher: Optional[str] = None,
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
                    if self.is_hook_registered(settings, event, entry.file, pkg.name, entry.matcher):
                        registered += 1

            if registered == total:
                return ("installed", registered, total)
            elif registered > 0:
                return ("partial", registered, total)
            else:
                return ("partial", 0, total)

        # orchestra.json にないが、settings にフックがあるかもしれない (旧形式互換)
        if not pkg.hooks:
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

    def status(self, project: Optional[str]) -> None:
        """プロジェクトでのパッケージ導入状況を表示"""
        project_dir = self.get_project_dir(project)
        packages = self.load_packages()

        print(f"{'PACKAGE':<20} {'STATUS':<15} HOOKS")
        print("-" * 60)

        for name in sorted(packages.keys()):
            pkg = packages[name]
            status, registered, total = self.get_package_status(pkg, project_dir)

            if not pkg.hooks:
                hooks_info = "(library only)"
            elif status == "installed":
                hooks_info = f"{registered}/{total} hooks registered"
            elif status == "partial":
                settings = self.load_settings(project_dir)
                missing = []
                for event, entries in pkg.hooks.items():
                    for entry in entries:
                        if not self.is_hook_registered(settings, event, entry.file, pkg.name, entry.matcher):
                            missing.append(entry.file)
                hooks_info = f"{registered}/{total} hooks registered (missing: {', '.join(missing)})"
            else:
                hooks_info = f"{registered}/{total} hooks registered"

            print(f"{name:<20} {status:<15} {hooks_info}")

    def check_dependencies(self, pkg: Package, installed_packages: set[str]) -> List[str]:
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
        settings: Dict[str, Any],
        event: str,
        filename: str,
        pkg_name: str,
        matcher: Optional[str] = None,
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
        settings: Dict[str, Any],
        event: str,
        filename: str,
        pkg_name: str,
        matcher: Optional[str] = None,
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
        global_settings: Dict[str, Any] = {}

        if global_settings_path.exists():
            with open(global_settings_path, "r", encoding="utf-8") as f:
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

    def is_sync_hook_registered(self, settings: Dict[str, Any]) -> bool:
        """sync-orchestra の SessionStart hook が登録されているかチェック"""
        hooks = settings.get("hooks", {})
        for entry in hooks.get("SessionStart", []):
            if "matcher" in entry:
                continue
            for hook in entry.get("hooks", []):
                if hook.get("command") == self.SYNC_HOOK_COMMAND:
                    return True
        return False

    def register_sync_hook(self, settings: Dict[str, Any], dry_run: bool = False) -> None:
        """sync-orchestra の SessionStart hook を登録"""
        if self.is_sync_hook_registered(settings):
            print("sync-orchestra hook は登録済み")
            return

        if dry_run:
            print(f"[DRY-RUN] sync-orchestra hook 登録: SessionStart")
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

        target_entry["hooks"].append({
            "type": "command",
            "command": self.SYNC_HOOK_COMMAND,
            "timeout": self.SYNC_HOOK_TIMEOUT,
        })

        print("sync-orchestra hook 登録: SessionStart")

    def remove_sync_hook(self, settings: Dict[str, Any]) -> None:
        """sync-orchestra の SessionStart hook を削除"""
        if "hooks" not in settings or "SessionStart" not in settings["hooks"]:
            return

        for entry in settings["hooks"]["SessionStart"]:
            if "matcher" in entry:
                continue
            entry["hooks"] = [
                h for h in entry.get("hooks", [])
                if h.get("command") != self.SYNC_HOOK_COMMAND
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

            for category in ("skills", "agents", "rules"):
                file_list = getattr(pkg, category, [])
                for rel_path in file_list:
                    src = orchestra_path / "packages" / pkg_name / category / rel_path
                    dst = claude_dir / category / rel_path

                    if not src.exists():
                        continue

                    if dry_run:
                        print(f"[DRY-RUN] 同期: {category}/{rel_path}")
                        continue

                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    synced_count += 1

        # トップレベル同期
        if orch.get("sync_top_level", False):
            for category in ("agents", "skills", "rules"):
                src_dir = orchestra_path / category
                if not src_dir.is_dir():
                    continue

                for src_file in src_dir.rglob("*"):
                    if not src_file.is_file():
                        continue

                    rel_path = src_file.relative_to(src_dir)
                    dst = claude_dir / category / rel_path

                    if dry_run:
                        print(f"[DRY-RUN] 同期: {category}/{rel_path}")
                        continue

                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst)
                    synced_count += 1

        if synced_count > 0:
            print(f"{synced_count} ファイルを同期しました")

    def install(self, package_name: str, project: Optional[str], dry_run: bool = False) -> None:
        """パッケージをインストール"""
        packages = self.load_packages()
        if package_name not in packages:
            print(f"エラー: パッケージ '{package_name}' が見つかりません", file=sys.stderr)
            sys.exit(1)

        pkg = packages[package_name]
        project_dir = self.get_project_dir(project)

        # 依存チェック
        orch = self.load_orchestra_json(project_dir)
        installed_packages = set(orch.get("installed_packages", []))
        missing_deps = self.check_dependencies(pkg, installed_packages)
        if missing_deps:
            print(f"警告: 依存パッケージが未インストール: {', '.join(missing_deps)}", file=sys.stderr)

        # 1. 環境変数の設定
        self.setup_env_var(dry_run)

        # 2. config ファイルのコピー
        for file_path in pkg.config:
            if file_path.startswith("config/"):
                filename = Path(file_path).name
                source = pkg.path / file_path
                target = project_dir / ".claude" / "config" / filename
                target.parent.mkdir(parents=True, exist_ok=True)

                if dry_run:
                    print(f"[DRY-RUN] ファイルコピー: {target} <- {source}")
                else:
                    shutil.copy2(source, target)
                    print(f"ファイルコピー: {target.name}")

        # 3. settings.local.json にフック登録
        settings = self.load_settings(project_dir)
        for event, entries in pkg.hooks.items():
            for entry in entries:
                if dry_run:
                    print(f"[DRY-RUN] フック登録: {event} / {entry.file}" +
                          (f" (matcher: {entry.matcher})" if entry.matcher else ""))
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
            orch["last_sync"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.save_orchestra_json(project_dir, orch)

        # 6. 初回同期を実行（skills/agents/rules をコピー）
        self.run_initial_sync(project_dir, dry_run)

        if dry_run:
            print(f"\n[DRY-RUN] orchestra.json 記録: installed_packages に '{package_name}' を追加")
        else:
            print(f"\n✓ パッケージ '{package_name}' をインストールしました")

    def uninstall(self, package_name: str, project: Optional[str], dry_run: bool = False) -> None:
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
                    print(f"[DRY-RUN] フック削除: {event} / {entry.file}" +
                          (f" (matcher: {entry.matcher})" if entry.matcher else ""))
                else:
                    self.remove_hook_from_settings(settings, event, entry.file, pkg.name, entry.matcher)

        if not dry_run:
            self.save_settings(project_dir, settings)

        # 2. config ファイル削除
        for file_path in pkg.config:
            if file_path.startswith("config/"):
                filename = Path(file_path).name
                target = project_dir / ".claude" / "config" / filename

                if dry_run:
                    if target.exists():
                        print(f"[DRY-RUN] ファイル削除: {target}")
                else:
                    if target.exists():
                        target.unlink()
                        print(f"ファイル削除: {target.name}")

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

    def init(self, project: Optional[str], dry_run: bool = False) -> None:
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
            templates_dir / "project" / "docs" / "DESIGN.md":
                project_dir / ".claude" / "docs" / "DESIGN.md",
            templates_dir / "project" / "docs" / "libraries" / "_TEMPLATE.md":
                project_dir / ".claude" / "docs" / "libraries" / "_TEMPLATE.md",
            templates_dir / "project" / "docs" / "research" / ".gitkeep":
                project_dir / ".claude" / "docs" / "research" / ".gitkeep",
            templates_dir / "project" / "logs" / "orchestration" / ".gitkeep":
                project_dir / ".claude" / "logs" / "orchestration" / ".gitkeep",
            templates_dir / "project" / "state" / ".gitkeep":
                project_dir / ".claude" / "state" / ".gitkeep",
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
                print(f"スキップ（既存）: CLAUDE.md")
            elif dry_run:
                print(f"[DRY-RUN] テンプレート配置: CLAUDE.md")
            else:
                shutil.copy2(claude_md_src, claude_md_dst)
                print(f"テンプレート配置: CLAUDE.md")

        # 5. .codex/ テンプレート（既存はスキップ）
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

        # 6. .gemini/ テンプレート（既存はスキップ）
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

        # 7. orchestra.json の初期化（sync_top_level 有効）
        orch = self.load_orchestra_json(project_dir)
        if not orch.get("orchestra_dir"):
            orch["orchestra_dir"] = str(self.orchestra_dir)
        orch.setdefault("installed_packages", [])
        orch["sync_top_level"] = True
        if dry_run:
            print(f"[DRY-RUN] orchestra.json 初期化（sync_top_level: true）")
        else:
            self.save_orchestra_json(project_dir, orch)
            print(f"orchestra.json 初期化（sync_top_level: true）")

        # 8. sync-orchestra の SessionStart hook を登録
        settings = self.load_settings(project_dir)
        self.register_sync_hook(settings, dry_run)
        if not dry_run:
            self.save_settings(project_dir, settings)

        # 9. 初回同期（skills/agents/rules をコピー）
        self.run_initial_sync(project_dir, dry_run)

        if not dry_run:
            print(f"\n✓ プロジェクトを初期化しました: {project_dir}")

    def enable(self, package_name: str, project: Optional[str], dry_run: bool = False) -> None:
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
                    print(f"[DRY-RUN] フック登録: {event} / {entry.file}" +
                          (f" (matcher: {entry.matcher})" if entry.matcher else ""))
                else:
                    self.add_hook_to_settings(settings, event, entry.file, pkg.name, entry.matcher)

        if not dry_run:
            self.save_settings(project_dir, settings)
            print(f"\n✓ パッケージ '{package_name}' を有効化しました")

    def disable(self, package_name: str, project: Optional[str], dry_run: bool = False) -> None:
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
                    print(f"[DRY-RUN] フック削除: {event} / {entry.file}" +
                          (f" (matcher: {entry.matcher})" if entry.matcher else ""))
                else:
                    self.remove_hook_from_settings(settings, event, entry.file, pkg.name, entry.matcher)

        if not dry_run:
            self.save_settings(project_dir, settings)
            print(f"\n✓ パッケージ '{package_name}' を無効化しました")


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

    args = parser.parse_args()

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
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
