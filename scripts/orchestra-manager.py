#!/usr/bin/env python3
"""
ai-orchestra パッケージ管理 CLI ツール

パッケージ単位でフック・スクリプトをプロジェクトに導入/削除する。
v2: $AI_ORCHESTRA_DIR + SessionStart 自動同期方式
"""

import argparse
import bisect
import datetime
import json
import os
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any

# scripts/ ディレクトリをモジュール検索パスに追加（テストの load_module 互換）
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import lib.gitignore_sync as gitignore_sync  # noqa: E402
from lib.facet_builder import FacetBuilder  # noqa: E402
from lib.orchestra_context import ContextMixin  # noqa: E402
from lib.orchestra_hooks import HooksMixin  # noqa: E402
from lib.orchestra_models import Package  # noqa: E402
from lib.sync_engine import collect_manifest_compositions  # noqa: E402


class OrchestraManager(ContextMixin, HooksMixin):
    """パッケージ管理マネージャー"""

    SYNC_HOOK_COMMAND = 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"'
    SYNC_HOOK_TIMEOUT = 15
    # gitignore エントリは gitignore_sync モジュールで一元管理
    CONTEXT_SPECS: tuple[tuple[str, str, str, str, str | None], ...] = (
        ("claude", "claude.md", "templates/project/CLAUDE.md", "CLAUDE.md", None),
        ("codex", "codex.md", "templates/codex/AGENTS.md", "AGENTS.md", "codex-suggestions"),
        (
            "gemini",
            "gemini.md",
            "templates/gemini/GEMINI.md",
            ".gemini/GEMINI.md",
            "gemini-suggestions",
        ),
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

        queue: deque[str] = deque(sorted(n for n in package_names if in_degree[n] == 0))
        result: list[str] = []

        while queue:
            node = queue.popleft()
            result.append(node)
            for dependent in dependents[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    bisect.insort(queue, dependent)

        if len(result) != len(package_names):
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

    @staticmethod
    def build_gitignore_block() -> str:
        """AI Orchestra 管理下の .gitignore ブロックを返す。"""
        return gitignore_sync.build_block()

    @staticmethod
    def merge_gitignore_content(existing: str) -> str:
        """既存 .gitignore 文字列に AI Orchestra ブロックをマージする。"""
        return gitignore_sync.merge_content(existing)

    def has_installed_dependents(
        self, pkg_name: str, installed: list[str], packages: dict[str, Package]
    ) -> bool:
        """指定パッケージに依存するインストール済みパッケージがあるか"""
        for inst_name in installed:
            inst_pkg = packages.get(inst_name)
            if inst_pkg and pkg_name in inst_pkg.depends:
                return True
        return False

    def get_package_status(
        self,
        pkg: Package,
        project_dir: Path,
        orch: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
        all_packages: dict[str, Package] | None = None,
    ) -> tuple[str, int, int]:
        """パッケージの導入状況を判定"""
        if orch is None:
            orch = self.load_orchestra_json(project_dir)
        installed = orch.get("installed_packages", [])

        if pkg.name in installed:
            if not pkg.hooks:
                return ("installed", 0, 0)
            if settings is None:
                settings = self.load_settings(project_dir)
            registered, total = self._count_registered_hooks(pkg, settings)
            if registered == total:
                return ("installed", registered, total)
            return ("partial", registered, total)

        if not pkg.hooks:
            if all_packages is None:
                all_packages = self.load_packages()
            if self.has_installed_dependents(pkg.name, installed, all_packages):
                return ("active", 0, 0)
            return ("not found", 0, 0)

        if settings is None:
            settings = self.load_settings(project_dir)
        registered, total = self._count_registered_hooks(pkg, settings)
        if registered == 0:
            return ("not found", registered, total)
        if registered == total:
            return ("installed", registered, total)
        return ("partial", registered, total)

    def status(self, project: str | None) -> None:
        """プロジェクトでのパッケージ導入状況を表示"""
        project_dir = self.get_project_dir(project)
        packages = self.load_packages()
        orch = self.load_orchestra_json(project_dir)
        settings = self.load_settings(project_dir)

        print(f"{'TAG':<6} {'PACKAGE':<20} {'STATUS':<15} HOOKS")
        print("-" * 70)

        installed_packages: list[str] = []

        for name in sorted(packages.keys()):
            pkg = packages[name]
            status, registered, total = self.get_package_status(
                pkg, project_dir, orch=orch, settings=settings, all_packages=packages
            )

            if not pkg.hooks:
                hooks_info = "(dependency)" if status == "active" else "(library only)"
            elif status == "partial":
                missing = [
                    entry.file
                    for event, entries in pkg.hooks.items()
                    for entry in entries
                    if not self.is_hook_registered(
                        settings, event, entry.file, pkg.name, entry.matcher
                    )
                ]
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
        return [dep for dep in pkg.depends if dep not in installed_packages]

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

        for pkg_name in installed:
            if pkg_name not in packages:
                continue
            pkg = packages[pkg_name]
            pkg_dir = orchestra_path / "packages" / pkg_name

            for category in ("agents", "config"):
                file_list = getattr(pkg, category, [])
                for rel_path in file_list:
                    src = pkg_dir / rel_path
                    if not src.exists():
                        continue

                    if src.is_dir():
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

    def _copy_template_if_missing(
        self, src: Path, dst: Path, label: str, dry_run: bool = False
    ) -> bool:
        """テンプレートファイルが存在しなければコピーする。コピーした場合 True を返す。"""
        if not src.exists():
            return False
        if dst.exists():
            print(f"スキップ（既存）: {label}")
            return False
        if dry_run:
            print(f"[DRY-RUN] テンプレート配置: {label}")
            return True
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"テンプレート配置: {label}")
        return True

    def _is_initialized(self, project_dir: Path) -> bool:
        """プロジェクトが初期化済みかどうかを判定"""
        return (project_dir / ".claude" / "orchestra.json").exists()

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

        if not self._is_initialized(project_dir):
            print("プロジェクト未初期化のため自動初期化します...\n")
            self.init(project, dry_run)
            print()

        orch = self.load_orchestra_json(project_dir)
        installed_packages = set(orch.get("installed_packages", []))
        if not _skip_dep_check:
            missing_deps = self.check_dependencies(pkg, installed_packages)
            if missing_deps:
                print(
                    f"警告: 依存パッケージが未インストール: {', '.join(missing_deps)}",
                    file=sys.stderr,
                )

        self.setup_env_var(dry_run)

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

        settings = self.load_settings(project_dir)
        self._apply_hooks(pkg, settings, "add", dry_run)
        self.register_sync_hook(settings, dry_run)

        if not dry_run:
            self.save_settings(project_dir, settings)

        if not dry_run:
            if pkg.name not in installed_packages:
                installed_packages.add(pkg.name)
            orch["installed_packages"] = sorted(installed_packages)
            orch["orchestra_dir"] = str(self.orchestra_dir)
            orch["last_sync"] = datetime.datetime.now(datetime.UTC).isoformat()
            self.save_orchestra_json(project_dir, orch)

        self.context_sync(project, dry_run)
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

        settings = self.load_settings(project_dir)
        self._apply_hooks(pkg, settings, "remove", dry_run)

        if not dry_run:
            self.save_settings(project_dir, settings)

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

        claude_dir = project_dir / ".claude"
        for agent_path in pkg.agents:
            target = claude_dir / agent_path
            if dry_run:
                if target.exists():
                    print(f"[DRY-RUN] 同期ファイル削除: {target}")
            else:
                if target.exists():
                    target.unlink()
                    print(f"同期ファイル削除: {agent_path}")

        orch = self.load_orchestra_json(project_dir)
        installed = set(orch.get("installed_packages", []))
        if pkg.name in installed:
            installed.discard(pkg.name)
            orch["installed_packages"] = sorted(installed)

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

        self.setup_env_var(dry_run)

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
            self._copy_template_if_missing(src, dst, str(dst.relative_to(project_dir)), dry_run)

        self._copy_template_if_missing(
            templates_dir / "project" / "CLAUDE.md",
            project_dir / "CLAUDE.md",
            "CLAUDE.md",
            dry_run,
        )
        self._copy_template_if_missing(
            templates_dir / "project" / ".claudeignore",
            project_dir / ".claudeignore",
            ".claudeignore",
            dry_run,
        )

        self.sync_gitignore(project_dir, dry_run)

        orch = self.load_orchestra_json(project_dir)
        installed = set(orch.get("installed_packages", []))

        # AGENTS.md はプロジェクトルートに配置（Codex は .codex/ 内ではなくルートを読む）
        if "codex-suggestions" in installed:
            codex_src = templates_dir / "codex"
            codex_root_files = {"AGENTS.md"}
            if codex_src.is_dir():
                codex_dst = project_dir / ".codex"
                for src_file in codex_src.rglob("*"):
                    if not src_file.is_file():
                        continue
                    rel = src_file.relative_to(codex_src)
                    if rel.name in codex_root_files:
                        dst_file = project_dir / rel.name
                        label = rel.name
                    else:
                        dst_file = codex_dst / rel
                        label = f".codex/{rel}"
                    self._copy_template_if_missing(src_file, dst_file, label, dry_run)

        if "gemini-suggestions" in installed:
            gemini_src = templates_dir / "gemini"
            if gemini_src.is_dir():
                for src_file in gemini_src.rglob("*"):
                    if not src_file.is_file():
                        continue
                    rel = src_file.relative_to(gemini_src)
                    dst_file = project_dir / ".gemini" / rel
                    self._copy_template_if_missing(src_file, dst_file, f".gemini/{rel}", dry_run)

        if not orch.get("orchestra_dir"):
            orch["orchestra_dir"] = str(self.orchestra_dir)
        orch.setdefault("installed_packages", [])
        if dry_run:
            print("[DRY-RUN] orchestra.json 初期化")
        else:
            self.save_orchestra_json(project_dir, orch)
            print("orchestra.json 初期化")

        settings = self.load_settings(project_dir)
        self.register_sync_hook(settings, dry_run)
        if not dry_run:
            self.save_settings(project_dir, settings)

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
        self._apply_hooks(pkg, settings, "add", dry_run)

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
        self._apply_hooks(pkg, settings, "remove", dry_run)

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
            stem = entry_path.stem

            if script_name in (entry, entry_path.name, stem):
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

        project_dir = self.get_project_dir(project)
        orch = self.load_orchestra_json(project_dir)
        already_installed = set(orch.get("installed_packages", []))

        total_steps = 1 + len(ordered)

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

        step = 1
        print(f"[{step}/{total_steps}] プロジェクト初期化...")
        self.init(project, dry_run)
        print()

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

        # パッケージインストール後に context ファイルを配布
        self.context_sync(project, dry_run)

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
        pid = proxy_manager._read_pid(pid_path)

        print(f"状態:   {'稼働中' if running else '停止'}")
        print(f"PID:    {pid or '-'}")
        print(f"ポート: {proxy_cfg['host']}:{proxy_cfg['port']}")
        print(f"PIDファイル: {pid_path}")


def main():
    """メインエントリポイント"""
    parser = argparse.ArgumentParser(
        description="AI-ORCHESTRA パッケージ管理 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--orchestra-dir",
        type=Path,
        help="ai-orchestra ディレクトリ（デフォルト: スクリプトの親の親）",
    )

    subparsers = parser.add_subparsers(dest="command", help="サブコマンド")

    subparsers.add_parser("list", help="パッケージ一覧を表示")

    status_parser = subparsers.add_parser("status", help="パッケージ導入状況を表示")
    status_parser.add_argument("--project", help="プロジェクトパス")

    install_parser = subparsers.add_parser("install", help="パッケージをインストール")
    install_parser.add_argument("package", nargs="+", help="パッケージ名（複数指定可）")
    install_parser.add_argument("--project", help="プロジェクトパス")
    install_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    uninstall_parser = subparsers.add_parser("uninstall", help="パッケージをアンインストール")
    uninstall_parser.add_argument("package", help="パッケージ名")
    uninstall_parser.add_argument("--project", help="プロジェクトパス")
    uninstall_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    enable_parser = subparsers.add_parser("enable", help="パッケージを有効化")
    enable_parser.add_argument("package", help="パッケージ名")
    enable_parser.add_argument("--project", help="プロジェクトパス")
    enable_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    disable_parser = subparsers.add_parser("disable", help="パッケージを無効化")
    disable_parser.add_argument("package", help="パッケージ名")
    disable_parser.add_argument("--project", help="プロジェクトパス")
    disable_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    run_parser = subparsers.add_parser(
        "run",
        help="パッケージのスクリプトを実行",
        description="パッケージに含まれるスクリプトを実行する。"
        " -- 以降の引数はスクリプトにパススルーされる。",
    )
    run_parser.add_argument("package", help="パッケージ名")
    run_parser.add_argument("script", help="スクリプト名（短縮名 or フルパス）")
    run_parser.add_argument("--project", help="プロジェクトパス")

    scripts_parser = subparsers.add_parser("scripts", help="スクリプト一覧を表示")
    scripts_parser.add_argument("--package", help="特定パッケージのみ表示")

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

    proxy_parser = subparsers.add_parser("proxy", help="mcp-proxy の管理")
    proxy_sub = proxy_parser.add_subparsers(dest="proxy_command", help="proxy サブコマンド")
    proxy_stop_parser = proxy_sub.add_parser("stop", help="mcp-proxy を停止")
    proxy_stop_parser.add_argument("--project", help="プロジェクトパス")
    proxy_status_parser = proxy_sub.add_parser("status", help="mcp-proxy の状態を表示")
    proxy_status_parser.add_argument("--project", help="プロジェクトパス")

    facet_parser = subparsers.add_parser("facet", help="facet composition から SKILL.md を生成")
    facet_sub = facet_parser.add_subparsers(dest="facet_command", help="facet サブコマンド")
    facet_build_parser = facet_sub.add_parser(
        "build",
        help="facet composition をビルドして SKILL.md を生成",
    )
    facet_build_parser.add_argument("--name", help="composition 名（省略時は全件ビルド）")
    facet_build_parser.add_argument(
        "--target",
        choices=["claude", "codex"],
        default="claude",
        help="出力先（デフォルト: claude）",
    )
    facet_build_parser.add_argument("--project", help="プロジェクトパス")
    facet_extract_parser = facet_sub.add_parser(
        "extract",
        help="生成済みファイルから instruction を抽出してソースに書き戻す",
    )
    facet_extract_parser.add_argument("--name", help="composition 名（省略時は全件）")
    facet_extract_parser.add_argument(
        "--target",
        choices=["claude", "codex"],
        default="claude",
        help="抽出元（デフォルト: claude）",
    )
    facet_extract_parser.add_argument("--project", help="プロジェクトパス")

    setup_parser = subparsers.add_parser("setup", help="プリセットで一括セットアップ")
    setup_parser.add_argument(
        "preset", nargs="?", default=None, help="プリセット名（省略時は一覧表示）"
    )
    setup_parser.add_argument("--project", help="プロジェクトパス")
    setup_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    argv = sys.argv[1:]
    script_args: list[str] = []
    if "--" in argv:
        sep_idx = argv.index("--")
        script_args = argv[sep_idx + 1 :]
        argv = argv[:sep_idx]

    args = parser.parse_args(argv)

    if args.orchestra_dir:
        orchestra_dir = args.orchestra_dir.resolve()
    else:
        orchestra_dir = Path(__file__).parent.parent.resolve()

    manager = OrchestraManager(orchestra_dir)

    if args.command == "list":
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
    elif args.command == "facet":
        project_dir = manager.get_project_dir(args.project)
        project_facets_dir = project_dir / ".claude" / "facets"
        installed_packages = (
            manager.load_orchestra_json(project_dir).get("installed_packages") or []
        )
        manifest_compositions = collect_manifest_compositions(orchestra_dir)

        facet_builder = FacetBuilder(
            orchestra_dir=orchestra_dir,
            project_facets_dir=project_facets_dir if project_facets_dir.is_dir() else None,
            manifest_compositions=manifest_compositions,
            installed_packages=installed_packages,
        )
        if args.facet_command == "build":
            if args.name:
                facet_builder.build_one(args.name, args.target, project_dir)
            else:
                facet_builder.build_all(args.target, project_dir)
        elif args.facet_command == "extract":
            if args.name:
                facet_builder.extract_one(args.name, args.target, project_dir)
            else:
                facet_builder.extract_all(args.target, project_dir)
        else:
            facet_parser.print_help()
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
