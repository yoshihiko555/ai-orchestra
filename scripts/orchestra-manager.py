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
import tomllib
from collections import deque
from collections.abc import Callable
from functools import cached_property  # noqa: F401 — used as decorator
from pathlib import Path
from typing import Any, TypedDict


def _resolve_orchex_version() -> str:
    """orchex のバージョン文字列を解決する。

    orchex エントリポイント経由の実行では ai_orchestra が import 済みのため
    その値を使う。`python3 scripts/orchestra-manager.py` の直接実行時は、
    site-packages にインストール済みの ai_orchestra にシャドウイングされない
    よう、import 前にチェックアウトルートを sys.path の先頭へ挿入する。
    """
    if "ai_orchestra" not in sys.modules:
        repo_root = Path(__file__).resolve().parent.parent
        if (repo_root / "ai_orchestra").is_dir() and str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

    try:
        from ai_orchestra import __version__

        return __version__
    except ImportError:
        return "unknown"


ORCHEX_VERSION = _resolve_orchex_version()

# scripts/ ディレクトリをモジュール検索パスに追加（テストの load_module 互換）
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import lib.gitignore_sync as gitignore_sync  # noqa: E402
from lib.facet_builder import FacetBuilder  # noqa: E402
from lib.hook_utils import SYNC_HOOK_COMMAND as _SYNC_HOOK_COMMAND  # noqa: E402
from lib.orchestra_context import ContextMixin  # noqa: E402
from lib.orchestra_hooks import HooksMixin  # noqa: E402
from lib.orchestra_models import Package  # noqa: E402
from lib.sync_engine import (  # noqa: E402
    _is_within_project,
    apply_codex_harness_config,
    collect_manifest_compositions,
    compute_file_hash,
    config_target_relative_path,
    get_recorded_file_hash,
    is_user_modified,
    needs_sync,
    record_file_hash,
    sync_codex_files,
)
from lib.toml_merge import TomlMergeError  # noqa: E402


def _fmt_inner_proxy(inner_port: object) -> str:
    if isinstance(inner_port, int) and inner_port > 0:
        return f"127.0.0.1:{inner_port}"
    return "-"


class OrchestraManager(ContextMixin, HooksMixin):
    """パッケージ管理マネージャー"""

    SYNC_HOOK_COMMAND = _SYNC_HOOK_COMMAND
    SYNC_HOOK_TIMEOUT = 15
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

    def _build_context_specs(
        self,
    ) -> tuple[tuple[str, tuple[str, ...], str, str, str | None], ...]:
        """manifest の context_files から CONTEXT_SPECS を動的に構築する。

        core は常に先頭（CLAUDE.md を最初に処理）、その他はパッケージ名の昇順。
        core の context は required_package=None で常時配布扱い。
        context_files.fragments があれば source に続けて連結する
        （例: AGENTS.md = codex.md + antigravity.md のセクション合成）。
        """
        packages = self.load_packages()
        specs_with_pkg: list[tuple[str, tuple[str, tuple[str, ...], str, str, str | None]]] = []
        for pkg_name, pkg in packages.items():
            cf = pkg.context_files
            if not cf:
                continue
            source = cf.get("source")
            template = cf.get("template")
            sync_list = cf.get("sync") or []
            if not source or not template or not sync_list:
                continue
            fragments = cf.get("fragments") or []
            sources = (source, *fragments)
            project_rel = sync_list[0]
            required: str | None = None if pkg.name == "core" else pkg.name
            name = Path(source).stem
            specs_with_pkg.append((pkg_name, (name, sources, template, project_rel, required)))
        # core first (owns claude.md), then alphabetical by package name
        specs_with_pkg.sort(key=lambda item: (0 if item[0] == "core" else 1, item[0]))
        return tuple(spec for _, spec in specs_with_pkg)

    @cached_property
    def CONTEXT_SPECS(self) -> tuple[tuple[str, tuple[str, ...], str, str, str | None], ...]:
        """manifest.context_files から動的構築された context spec タプル。"""
        return self._build_context_specs()

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
        """presets.json を読み込み、__all__ を全パッケージ名に展開し、exclude 指定を除外して返す"""
        presets_path = self.orchestra_dir / "presets.json"
        if not presets_path.exists():
            print("エラー: presets.json が見つかりません", file=sys.stderr)
            sys.exit(1)

        with open(presets_path, encoding="utf-8") as f:
            presets = json.load(f)

        all_package_names = sorted(self.load_packages().keys())
        for preset in presets.values():
            packages = preset.get("packages")
            if packages == "__all__":
                packages = all_package_names
            excluded_packages = set(preset.get("exclude", []))
            preset["packages"] = [name for name in packages if name not in excluded_packages]

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

    def run_initial_sync(
        self, project_dir: Path, dry_run: bool = False, force: bool = False
    ) -> None:
        """初回同期を実行（sync-orchestra.py と同等のロジック）。

        SessionStart 側と同じ sync_engine.needs_sync() ゲートを適用し、
        ユーザーが編集済みのファイルを黙って上書きしない。実際にコピーした
        ファイルの SHA-256 ハッシュを orchestra.json の file_hashes に記録する。
        force=True の場合、codex_files の配布時ハッシュ不一致でも上書きする。
        """
        orch = self.load_orchestra_json(project_dir)
        installed = orch.get("installed_packages", [])
        orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")

        if not orchestra_dir:
            return

        orchestra_path = Path(orchestra_dir).resolve()
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
                            if not needs_sync(src_file, dst):
                                continue
                            if dry_run:
                                print(f"[DRY-RUN] 同期: {file_rel}")
                                continue
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src_file, dst)
                            record_file_hash(orch, pkg_name, file_rel, compute_file_hash(dst))
                            synced_count += 1
                    else:
                        if category == "config":
                            target_rel = config_target_relative_path(pkg_name, rel_path)
                            dst = claude_dir / target_rel
                            file_key = str(target_rel)
                        else:
                            dst = claude_dir / rel_path
                            file_key = rel_path

                        if category in ("config", "agents") and is_user_modified(
                            orch, pkg_name, file_key, dst
                        ):
                            print(
                                f"警告: {dst} はインストール後に変更されているため"
                                "上書きをスキップしました"
                            )
                            continue

                        if not needs_sync(src, dst):
                            continue

                        if dry_run:
                            print(f"[DRY-RUN] 同期: {category}/{rel_path}")
                            continue

                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        record_file_hash(orch, pkg_name, file_key, compute_file_hash(dst))
                        synced_count += 1

        codex_synced_count = sync_codex_files(
            project_dir, orchestra_path, installed, orch, force=force, dry_run=dry_run
        )
        synced_count += codex_synced_count
        try:
            config_updated = apply_codex_harness_config(
                project_dir, orchestra_path, installed, dry_run=dry_run
            )
        except (TomlMergeError, tomllib.TOMLDecodeError, OSError) as e:
            print(
                f"警告: .codex/config.toml マージに失敗したためスキップしました: {e}",
                file=sys.stderr,
            )
            config_updated = False
        if config_updated and not dry_run:
            print(".codex/config.toml を codex-harness 設定で更新しました")

        if synced_count > 0:
            print(f"{synced_count} ファイルを同期しました")
            if not dry_run:
                self.save_orchestra_json(project_dir, orch)

    def _install_context_init_files(self, pkg: Package, project_dir: Path, dry_run: bool) -> None:
        """manifest.context_files.init に基づきテンプレートを初回配置する。

        init リスト内のエントリを SSOT として扱い、リストに列挙されたパスだけを
        コピーする。rglob ベースの一括コピーは行わない。
        """
        cf = pkg.context_files or {}
        init_entries: list[str] = cf.get("init") or []
        template_rel = cf.get("template")
        if not init_entries or not template_rel:
            return

        template_file = self.orchestra_dir / template_rel
        template_root = template_file.parent
        if not template_root.is_dir():
            print(
                f"警告: テンプレートディレクトリが見つかりません: {template_root}",
                file=sys.stderr,
            )
            return

        for entry in init_entries:
            is_dir = entry.endswith("/")
            entry_clean = entry.rstrip("/")
            if not entry_clean:
                continue

            if "/" in entry_clean:
                # プレフィックス付き: .codex/config.toml → src は templates/codex/config.toml
                _, rest = entry_clean.split("/", 1)
                src = template_root / rest
            else:
                # ルート直下: AGENTS.md や CLAUDE.md
                src = template_root / entry_clean

            dst = project_dir / entry_clean
            label = entry_clean

            if is_dir:
                if not src.is_dir():
                    continue
                for src_file in src.rglob("*"):
                    if not src_file.is_file():
                        continue
                    file_rel = src_file.relative_to(src)
                    self._copy_template_if_missing(
                        src_file, dst / file_rel, f"{label}/{file_rel}", dry_run
                    )
            else:
                if not src.is_file():
                    continue
                self._copy_template_if_missing(src, dst, label, dry_run)

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
        force: bool = False,
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
                source = pkg.path / file_path
                target_rel = config_target_relative_path(pkg.name, file_path)
                target = project_dir / ".claude" / target_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                file_key = str(target_rel)

                self._copy_config_if_safe(
                    orch,
                    pkg.name,
                    file_key,
                    source,
                    target,
                    dry_run,
                    label=f"{pkg.name}/{target.name}",
                )

        settings = self.load_settings(project_dir)
        self._apply_hooks(pkg, settings, "add", dry_run)
        self.register_sync_hook(settings, dry_run)

        if not dry_run:
            self.save_settings(project_dir, settings)

        if not dry_run:
            if pkg.name not in installed_packages:
                installed_packages.add(pkg.name)
            orch["installed_packages"] = sorted(installed_packages)
            orch["last_sync"] = datetime.datetime.now(datetime.UTC).isoformat()
            self.save_orchestra_json(project_dir, orch)

        self.context_sync(project, dry_run)
        self.run_initial_sync(project_dir, dry_run, force=force)

        if dry_run:
            print(f"\n[DRY-RUN] orchestra.json 記録: installed_packages に '{package_name}' を追加")
        else:
            print(f"\n✓ パッケージ '{package_name}' をインストールしました")

    def _copy_config_if_safe(
        self,
        orch: dict[str, Any],
        pkg_name: str,
        file_key: str,
        source: Path,
        target: Path,
        dry_run: bool,
        label: str,
    ) -> None:
        """配布時ハッシュと現在のファイル内容を比較し、ユーザー編集済みなら上書きをスキップする。

        対象が存在しない、またはハッシュが未記録（新規/初回 install）の場合は
        通常どおりコピーする。dry_run 時も判定結果（コピー予定/スキップ予定）は
        同じロジックで表示するが、ファイルおよび orch dict は変更しない。
        """
        if target.exists():
            recorded = get_recorded_file_hash(orch, pkg_name, file_key)
            if recorded is not None and compute_file_hash(target) != recorded:
                print(
                    f"警告: {target} はインストール後に変更されているため上書きをスキップしました"
                )
                return

        if dry_run:
            print(f"[DRY-RUN] ファイルコピー: {target} <- {source}")
            return

        shutil.copy2(source, target)
        record_file_hash(orch, pkg_name, file_key, compute_file_hash(target))
        print(f"ファイルコピー: {label}")

    def _remove_if_unchanged(
        self,
        orch: dict[str, Any],
        pkg_name: str,
        file_key: str,
        target: Path,
        dry_run: bool,
        label: str,
        prefix: str = "ファイル削除",
    ) -> None:
        """配布時ハッシュと現在のファイル内容が一致する場合のみ削除する（安全側スキップ）。

        ハッシュが未記録（旧 install 由来）、またはインストール後に変更が
        検出された場合は削除せず警告を出す。dry_run 時も判定結果（削除予定/
        スキップ予定）は同じロジックで表示する。
        """
        if not target.exists():
            return

        recorded = get_recorded_file_hash(orch, pkg_name, file_key)
        if recorded is None:
            print(f"警告: {target} は配布時ハッシュが記録されていないため削除をスキップしました")
            return

        if compute_file_hash(target) != recorded:
            print(f"警告: {target} はインストール後に変更されているため削除をスキップしました")
            return

        if dry_run:
            print(f"[DRY-RUN] {prefix}: {target}")
            return

        target.unlink()
        print(f"{prefix}: {label}")

    def _remove_codex_file_if_unchanged(
        self,
        orch: dict[str, Any],
        target_key: str,
        target: Path,
        project_dir: Path,
        dry_run: bool,
    ) -> None:
        """codex_files（.codex/ 配下配布物）を配布時ハッシュ台帳と照合して削除する。

        台帳（orch["codex_file_hashes"]）はパッケージ名で名前空間化されない
        フラット構造（sync_engine.sync_codex_files() と同じキー形式）。
        ハッシュが一致する（未改変の）ファイルのみ削除し、台帳エントリも
        併せて削除する。ハッシュ未記録・改変済みのファイルは削除せず警告する
        （安全側スキップ、_remove_if_unchanged と同じ方針）。

        target_key（manifest.json の codex_files.target）が絶対パスや ../ で
        project_dir 外を指す場合は sync_codex_files() の _is_within_project()
        と同じ境界チェックを適用し、削除せず警告する（H5 相当の防御を
        uninstall 側にも適用）。
        """
        hashes = orch.setdefault("codex_file_hashes", {})

        if not _is_within_project(target, project_dir):
            print(
                f"警告: {target_key} はプロジェクト外を指すため削除をスキップしました"
                "（絶対パスまたは ../ による脱出の疑い）"
            )
            return

        if not target.exists():
            if not dry_run:
                hashes.pop(target_key, None)
            return

        recorded = hashes.get(target_key)
        if recorded is None:
            print(f"警告: {target} は配布時ハッシュが記録されていないため削除をスキップしました")
            return

        if compute_file_hash(target) != recorded:
            print(f"警告: {target} はインストール後に変更されているため削除をスキップしました")
            return

        if dry_run:
            print(f"[DRY-RUN] ファイル削除: {target}")
            return

        target.unlink()
        hashes.pop(target_key, None)
        print(f"ファイル削除: {target_key}")

    def uninstall(self, package_name: str, project: str | None, dry_run: bool = False) -> None:
        """パッケージをアンインストール

        削除前に配布時ハッシュ（orchestra.json の file_hashes / codex_file_hashes）
        と現在のファイル内容を比較し、ユーザーが変更済みのファイル（またはハッシュ
        未記録のファイル）は削除せずスキップして警告する（安全側）。
        codex_files（.codex/hooks/*.py 等、manifest.json の codex_files 宣言分）も
        同じ方針で削除し、対応する codex_file_hashes 台帳エントリも削除する。
        """
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

        orch = self.load_orchestra_json(project_dir)

        for file_path in pkg.config:
            if file_path.startswith("config/"):
                target_rel = config_target_relative_path(pkg.name, file_path)
                target = project_dir / ".claude" / target_rel
                file_key = str(target_rel)
                self._remove_if_unchanged(
                    orch, pkg.name, file_key, target, dry_run, label=f"{pkg.name}/{target.name}"
                )

        claude_dir = project_dir / ".claude"
        for agent_path in pkg.agents:
            target = claude_dir / agent_path
            self._remove_if_unchanged(
                orch,
                pkg.name,
                agent_path,
                target,
                dry_run,
                label=agent_path,
                prefix="同期ファイル削除",
            )

        for codex_file in pkg.codex_files:
            target_rel = codex_file.get("target") if isinstance(codex_file, dict) else None
            if not target_rel:
                continue
            target = project_dir / target_rel
            self._remove_codex_file_if_unchanged(orch, target_rel, target, project_dir, dry_run)

        installed = set(orch.get("installed_packages", []))
        if pkg.name in installed:
            installed.discard(pkg.name)
            orch["installed_packages"] = sorted(installed)
            orch.get("file_hashes", {}).pop(pkg.name, None)

            if not installed:
                if dry_run:
                    print("[DRY-RUN] 同期フック解除: settings.local.json (SessionStart)")
                else:
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
            templates_dir / "project" / "Plans.md": project_dir / ".claude" / "Plans.md",
        }
        for src, dst in project_templates.items():
            self._copy_template_if_missing(src, dst, str(dst.relative_to(project_dir)), dry_run)

        self.sync_gitignore(project_dir, dry_run)

        orch = self.load_orchestra_json(project_dir)
        installed = set(orch.get("installed_packages", []))

        packages_map = self.load_packages()
        installed_with_core = set(installed) | {"core"}
        for pkg_name in sorted(installed_with_core):
            pkg = packages_map.get(pkg_name)
            if pkg is None or not pkg.context_files:
                continue
            self._install_context_init_files(pkg, project_dir, dry_run)

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
        """パッケージを有効化（settings.local.json にフック登録を復元）

        hook を登録し直す経路なので、`install` と同じく env.AI_ORCHESTRA_PYTHON も
        補完する（Issue #343）。`install` を経ずに `enable` だけを実行した環境で
        インタプリタ固定が抜け落ちるのを防ぐ。
        """
        packages = self.load_packages()
        if package_name not in packages:
            print(f"エラー: パッケージ '{package_name}' が見つかりません", file=sys.stderr)
            sys.exit(1)

        pkg = packages[package_name]
        project_dir = self.get_project_dir(project)
        orch = self.load_orchestra_json(project_dir)
        installed_packages = set(orch.get("installed_packages", []))
        if package_name not in installed_packages:
            print(
                f"エラー: パッケージ '{package_name}' はインストールされていません"
                "（先に install を実行してください）",
                file=sys.stderr,
            )
            sys.exit(1)

        self.setup_env_var(dry_run)

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
            entry_path = Path(entry.path)
            stem = entry_path.stem

            if script_name in (entry.path, entry_path.name, stem):
                if entry_path.parts[0] == "scripts":
                    return pkg.path / entry.path
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
            available = [Path(s.path).stem for s in pkg.scripts]
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
                entry_path = Path(entry.path)
                short_name = entry_path.stem
                rows.append((name, short_name, entry.description))

        if not rows:
            print("スクリプトが見つかりません")
            return

        print(f"{'PACKAGE':<20} {'SCRIPT':<25} {'DESCRIPTION'}")
        for pkg_name, short_name, desc in rows:
            print(f"{pkg_name:<20} {short_name:<25} {desc}")

        print("\n実行方法: orchex run <package> <script> [-- <args>]")
        print("詳細:     orchex run <package> <script> -- --help")

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

    def _require_cocoindex_installed(self, project_dir: Path) -> None:
        """cocoindex が `.claude/orchestra.json` の installed_packages に無ければエラー終了する。

        config ファイルの発見可否だけに頼ると、AI_ORCHESTRA_DIR が ai-orchestra
        リポジトリ自体を指す通常構成ではベース設定が常に発見されてしまい、
        未導入プロジェクトでもエラー分岐が実質的に発火しない（Issue #236）。
        """
        orch = self.load_orchestra_json(project_dir)
        installed_packages = set(orch.get("installed_packages", []))
        if "cocoindex" not in installed_packages:
            print("エラー: cocoindex パッケージがインストールされていません", file=sys.stderr)
            sys.exit(1)

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

        self._require_cocoindex_installed(project_dir)

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

        self._require_cocoindex_installed(project_dir)

        config = hook_common.load_package_config("cocoindex", "cocoindex.yaml", str(project_dir))
        if not config:
            print("エラー: cocoindex パッケージがインストールされていません", file=sys.stderr)
            sys.exit(1)

        proxy_cfg = proxy_manager.get_proxy_config(config, str(project_dir))
        state = proxy_manager.get_proxy_state(config, str(project_dir))
        pid_path = proxy_manager.resolve_pid_path(config, str(project_dir))
        running = state.get("proxy_state") in {"ready", "idle"}
        pid = state.get("pid") or proxy_manager._read_pid(pid_path)
        child_pid = state.get("child_pid")
        inner_port = state.get("inner_port")

        print(f"状態:   {'稼働中' if running else '停止'} ({state.get('proxy_state', 'unknown')})")
        print(f"PID:    {pid or '-'}")
        print(f"Child:  {child_pid or '-'}")
        print(f"ポート: {proxy_cfg['host']}:{proxy_cfg['port']}")
        print(f"内部:   {_fmt_inner_proxy(inner_port)}")
        print(f"PIDファイル: {pid_path}")


def _first_positional_command(argv: list[str]) -> str | None:
    """argv の先頭にある既知のトップレベルオプション（`--orchestra-dir`）をスキップし、
    最初の位置引数（サブコマンド名）を返す。位置引数が無ければ None。

    `run -- <args>` パススルー分割（`main()` 内）は「argv[0] がサブコマンド run か」
    ではなく「最初の位置引数が run か」で判定する必要がある。`--orchestra-dir <dir> run ...`
    のようにグローバルオプションが前置されるケースでも run パススルーが機能するようにする
    ための判定専用ヘルパー。
    """
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--orchestra-dir":
            i += 2
            continue
        if token.startswith("--orchestra-dir="):
            i += 1
            continue
        return token
    return None


def _split_run_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """`run` サブコマンドの `-- <script_args>` パススルーを argv から切り出す。

    最初の位置引数（`--orchestra-dir` 等のグローバルオプションをスキップした後の
    先頭トークン）が `run` かつ argv に `--` が含まれる場合のみ分割する。`meta`
    サブコマンド（`argparse.REMAINDER` で自前パススルーする）等、`run` 以外の
    コマンドでは `--` を含んでいても一切分割しない。

    戻り値は `(parser_argv, script_args)`。`parser_argv` は argparse にそのまま渡す
    引数列（分割対象外なら入力 argv と同じ）。
    """
    if _first_positional_command(argv) == "run" and "--" in argv:
        sep_idx = argv.index("--")
        return argv[:sep_idx], argv[sep_idx + 1 :]
    return argv, []


class CommandEntry(TypedDict):
    """トップレベルコマンドのレジストリエントリ。"""

    name: str
    group: str
    summary: str
    examples: tuple[str, ...]
    build_parser: Callable[[Any], argparse.ArgumentParser]


GROUP_ORDER: tuple[str, ...] = (
    "getting_started",
    "package_management",
    "generate_sync",
    "run_delegate",
)

GROUP_HEADINGS: dict[str, str] = {
    "getting_started": "はじめに",
    "package_management": "パッケージ管理",
    "generate_sync": "生成・同期",
    "run_delegate": "実行・委譲",
}

COMMAND_NAME_COLUMN_PADDING = 3


def _format_examples_block(examples: tuple[str, ...]) -> str:
    """examples タプルを help description に追記する「例:」ブロックへ整形する。"""
    if not examples:
        return ""
    example_lines = "\n".join(f"  {example}" for example in examples)
    return f"\n\n例:\n{example_lines}"


def _add_command_parser(
    subparsers: Any, command_name: str, *, description: str | None = None, **kwargs: Any
) -> argparse.ArgumentParser:
    """レジストリの name/summary/examples を使ってトップレベル parser を追加する。

    トップレベル `--help` ではデフォルトのフラット一覧を出さない（グループ化一覧を
    `create_parser()` が別途描画するため）よう `help=argparse.SUPPRESS` を指定する。
    各コマンド自身の `--help` には description（未指定時は summary）と、
    registry の examples を「例:」ブロックとして自動追記する。
    """
    entry = COMMAND_REGISTRY[command_name]
    full_description = (description or entry["summary"]) + _format_examples_block(entry["examples"])
    return subparsers.add_parser(
        entry["name"],
        help=argparse.SUPPRESS,
        description=full_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        **kwargs,
    )


def build_init_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "init",
        description="プロジェクトを AI Orchestra 管理下として初期化する"
        "（.claude/orchestra.json 等の雛形を作成する）。",
    )
    parser.add_argument("--project", help="プロジェクトパス")
    parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")
    return parser


def build_setup_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "setup",
        description="プリセット（essential/all）に基づき依存順で一括インストールする。"
        "プリセット省略時は一覧を表示する。",
    )
    parser.add_argument("preset", nargs="?", default=None, help="プリセット名（省略時は一覧表示）")
    parser.add_argument("--project", help="プロジェクトパス")
    parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")
    return parser


def build_list_parser(subparsers: Any) -> argparse.ArgumentParser:
    return _add_command_parser(
        subparsers, "list", description="配布可能なパッケージ一覧を表示する。"
    )


def build_install_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "install",
        description="パッケージをプロジェクトにインストールする"
        "（複数指定時は依存関係順に実行する）。",
    )
    parser.add_argument("package", nargs="+", help="パッケージ名（複数指定可）")
    parser.add_argument("--project", help="プロジェクトパス")
    parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")
    parser.add_argument(
        "--force",
        action="store_true",
        help="ユーザー編集済みの codex_files も配布版で上書きする（デフォルトはスキップ）",
    )
    return parser


def build_uninstall_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "uninstall",
        description="パッケージをアンインストールする"
        "（ユーザー変更済みファイルは安全側で保持し削除しない）。",
    )
    parser.add_argument("package", help="パッケージ名")
    parser.add_argument("--project", help="プロジェクトパス")
    parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")
    return parser


def build_enable_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "enable",
        description="インストール済みパッケージの hooks を再登録する"
        "（installed_packages は変更しない）。",
    )
    parser.add_argument("package", help="パッケージ名")
    parser.add_argument("--project", help="プロジェクトパス")
    parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")
    return parser


def build_disable_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "disable",
        description="インストール済みパッケージの hooks を一時的に解除する"
        "（installed_packages は変更しない）。",
    )
    parser.add_argument("package", help="パッケージ名")
    parser.add_argument("--project", help="プロジェクトパス")
    parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")
    return parser


def build_status_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "status",
        description="プロジェクトの各パッケージの導入状況"
        "（installed/active/partial/not found）を表示する。",
    )
    parser.add_argument("--project", help="プロジェクトパス")
    return parser


def build_context_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "context",
        description="CLAUDE.md / AGENTS.md テンプレートを管理する"
        "（build: 生成、check: 整合性検証、sync: プロジェクトへ反映）。",
    )
    context_sub = parser.add_subparsers(dest="context_command", help="context サブコマンド")

    build_parser = context_sub.add_parser(
        "build",
        help="templates/context から配布テンプレートを再生成",
    )
    build_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")

    context_sub.add_parser(
        "check",
        help="templates/context 由来の生成結果と配布テンプレートの一致を検証",
    )

    sync_parser = context_sub.add_parser(
        "sync",
        help="生成ルールに基づいてプロジェクトのトップレベル文書へ同期",
    )
    sync_parser.add_argument("--project", help="プロジェクトパス")
    sync_parser.add_argument("--dry-run", action="store_true", help="実行内容を表示のみ")
    sync_parser.add_argument(
        "--force",
        action="store_true",
        help="既存ファイルも上書きする（デフォルトは既存ファイルをスキップ）",
    )
    return parser


def build_facet_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "facet",
        description="facet composition から SKILL.md / ルール .md を生成・書き戻しする。",
    )
    facet_sub = parser.add_subparsers(dest="facet_command", help="facet サブコマンド")
    build_parser = facet_sub.add_parser(
        "build",
        help="facet composition をビルドして SKILL.md を生成",
    )
    build_parser.add_argument("--name", help="composition 名（省略時は全件ビルド）")
    build_parser.add_argument(
        "--target",
        choices=["claude", "codex"],
        default="claude",
        help="出力先（デフォルト: claude）",
    )
    build_parser.add_argument("--project", help="プロジェクトパス")
    extract_parser = facet_sub.add_parser(
        "extract",
        help="生成済みファイルから instruction を抽出してソースに書き戻す",
    )
    extract_parser.add_argument("--name", help="composition 名（省略時は全件）")
    extract_parser.add_argument(
        "--target",
        choices=["claude", "codex"],
        default="claude",
        help="抽出元（デフォルト: claude）",
    )
    extract_parser.add_argument("--project", help="プロジェクトパス")
    return parser


def build_run_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "run",
        description="パッケージに含まれるスクリプトを実行する。"
        " -- 以降の引数はスクリプトにパススルーされる。"
        " 利用可能なスクリプトの一覧は `orchex scripts` で確認できる。",
    )
    parser.add_argument("package", help="パッケージ名")
    parser.add_argument("script", help="スクリプト名（短縮名 or フルパス）")
    parser.add_argument("--project", help="プロジェクトパス")
    return parser


def build_scripts_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "scripts",
        description="パッケージに含まれる実行可能スクリプトの一覧を表示する。",
    )
    parser.add_argument("--package", help="特定パッケージのみ表示")
    return parser


def build_meta_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "meta",
        description="packages/meta-harness/scripts/meta_harness.py へ全引数をパススルーする"
        "（Meta-Harness CLI）。",
    )
    parser.add_argument(
        "meta_args", nargs=argparse.REMAINDER, help="meta_harness.py へパススルーする引数"
    )
    return parser


def build_proxy_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = _add_command_parser(
        subparsers,
        "proxy",
        description="cocoindex mcp-proxy の状態確認・停止を行う"
        "（cocoindex 未導入時はエラーで非ゼロ終了する）。",
    )
    proxy_sub = parser.add_subparsers(dest="proxy_command", help="proxy サブコマンド")
    stop_parser = proxy_sub.add_parser("stop", help="mcp-proxy を停止")
    stop_parser.add_argument("--project", help="プロジェクトパス")
    status_parser = proxy_sub.add_parser("status", help="mcp-proxy の状態を表示")
    status_parser.add_argument("--project", help="プロジェクトパス")
    return parser


COMMAND_REGISTRY: dict[str, CommandEntry] = {
    "init": {
        "name": "init",
        "group": "getting_started",
        "summary": "プロジェクトを初期化",
        "examples": (
            "orchex init --project .",
            "orchex init --project /path/to/project --dry-run",
        ),
        "build_parser": build_init_parser,
    },
    "setup": {
        "name": "setup",
        "group": "getting_started",
        "summary": "プリセットで一括セットアップ",
        "examples": (
            "orchex setup essential --project .",
            "orchex setup all --project . --dry-run",
        ),
        "build_parser": build_setup_parser,
    },
    "list": {
        "name": "list",
        "group": "getting_started",
        "summary": "パッケージ一覧を表示",
        "examples": ("orchex list",),
        "build_parser": build_list_parser,
    },
    "install": {
        "name": "install",
        "group": "package_management",
        "summary": "パッケージをインストール",
        "examples": (
            "orchex install core --project .",
            "orchex install core agent-routing --project . --dry-run",
        ),
        "build_parser": build_install_parser,
    },
    "uninstall": {
        "name": "uninstall",
        "group": "package_management",
        "summary": "パッケージをアンインストール",
        "examples": ("orchex uninstall tmux-monitor --project .",),
        "build_parser": build_uninstall_parser,
    },
    "enable": {
        "name": "enable",
        "group": "package_management",
        "summary": "パッケージを有効化",
        "examples": ("orchex enable audit --project .",),
        "build_parser": build_enable_parser,
    },
    "disable": {
        "name": "disable",
        "group": "package_management",
        "summary": "パッケージを無効化",
        "examples": ("orchex disable audit --project .",),
        "build_parser": build_disable_parser,
    },
    "status": {
        "name": "status",
        "group": "package_management",
        "summary": "パッケージ導入状況を表示",
        "examples": ("orchex status --project .",),
        "build_parser": build_status_parser,
    },
    "context": {
        "name": "context",
        "group": "generate_sync",
        "summary": "CLAUDE.md / AGENTS.md / GEMINI.md テンプレート管理",
        "examples": (
            "orchex context build",
            "orchex context check",
            "orchex context sync --project . --force",
        ),
        "build_parser": build_context_parser,
    },
    "facet": {
        "name": "facet",
        "group": "generate_sync",
        "summary": "facet composition から SKILL.md を生成",
        "examples": (
            "orchex facet build --project .",
            "orchex facet extract --name review --project .",
        ),
        "build_parser": build_facet_parser,
    },
    "run": {
        "name": "run",
        "group": "run_delegate",
        "summary": "パッケージのスクリプトを実行",
        "examples": (
            "orchex run audit dashboard",
            "orchex run audit log-viewer --project . -- --last 10",
        ),
        "build_parser": build_run_parser,
    },
    "scripts": {
        "name": "scripts",
        "group": "run_delegate",
        "summary": "スクリプト一覧を表示",
        "examples": ("orchex scripts", "orchex scripts --package audit"),
        "build_parser": build_scripts_parser,
    },
    "meta": {
        "name": "meta",
        "group": "run_delegate",
        "summary": "Meta-Harness（候補評価・進化基盤）CLI へ委譲",
        "examples": (
            "orchex meta status --target skill:issue-fix",
            "orchex meta frontier --target skill:issue-fix",
        ),
        "build_parser": build_meta_parser,
    },
    "proxy": {
        "name": "proxy",
        "group": "run_delegate",
        "summary": "mcp-proxy の管理",
        "examples": (
            "orchex proxy status --project .",
            "orchex proxy stop --project .",
        ),
        "build_parser": build_proxy_parser,
    },
}


def _render_grouped_command_listing() -> str:
    """COMMAND_REGISTRY からライフサイクル順のグループ化コマンド一覧を描画する。

    トップレベル help の description に埋め込む。コマンド列挙のハードコードは
    ドリフトの原因になるため、必ず registry から動的に導出する。
    """
    registry_groups = {entry["group"] for entry in COMMAND_REGISTRY.values()}
    unknown_groups = registry_groups - set(GROUP_ORDER)
    if unknown_groups:
        raise ValueError(f"GROUP_ORDER に未定義のグループ: {sorted(unknown_groups)}")

    column_width = (
        max(len(entry["name"]) for entry in COMMAND_REGISTRY.values()) + COMMAND_NAME_COLUMN_PADDING
    )
    lines: list[str] = ["コマンド一覧（ライフサイクル順）:"]
    for group in GROUP_ORDER:
        lines.append(f"\n{GROUP_HEADINGS[group]}:")
        for entry in COMMAND_REGISTRY.values():
            if entry["group"] != group:
                continue
            name = entry["name"].ljust(column_width)
            lines.append(f"  {name}{entry['summary']}")
    lines.append("\n各コマンドの詳細は `orchex <コマンド> --help` を参照。")
    return "\n".join(lines)


TOP_LEVEL_EPILOG = """\
よくある使い方:

  初回導入:
    orchex init --project .
    orchex setup essential --project .
    orchex status --project .

  日常運用:
    orchex install <package> --project .
    orchex status --project .
    orchex run audit dashboard

  テンプレート更新:
    orchex context build
    orchex context check
    orchex context sync --project .
    orchex facet build --project .

次に見る:
  orchex list      パッケージ一覧
  orchex scripts   スクリプト一覧
  orchex setup     プリセット一覧（プリセット省略時）
"""


def create_parser() -> tuple[argparse.ArgumentParser, Any]:
    """registry から CLI parser を構築する。

    戻り値は (parser, subparsers) のタプル。subparsers は
    add_subparsers() の戻り値そのもの（トップレベル subparsers action）。
    """
    parser = argparse.ArgumentParser(
        description="AI-ORCHESTRA パッケージ管理 CLI\n\n" + _render_grouped_command_listing(),
        epilog=TOP_LEVEL_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--orchestra-dir",
        type=Path,
        help="ai-orchestra ディレクトリ（デフォルト: スクリプトの親の親）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"orchex {ORCHEX_VERSION}",
    )

    subparsers = parser.add_subparsers(dest="command", help=argparse.SUPPRESS)
    for entry in COMMAND_REGISTRY.values():
        entry["build_parser"](subparsers)
    return parser, subparsers


def main() -> None:
    """メインエントリポイント"""
    parser, subparsers = create_parser()
    context_parser = subparsers.choices["context"]
    proxy_parser = subparsers.choices["proxy"]
    facet_parser = subparsers.choices["facet"]

    argv, script_args = _split_run_passthrough(sys.argv[1:])

    args = parser.parse_args(argv)

    if args.orchestra_dir:
        orchestra_dir = args.orchestra_dir.resolve()
    else:
        orchestra_dir = Path(__file__).parent.parent.resolve()

    manager = OrchestraManager(orchestra_dir)

    if args.command == "init":
        manager.init(args.project, args.dry_run)
    elif args.command == "list":
        manager.list_packages()
    elif args.command == "status":
        manager.status(args.project)
    elif args.command == "install":
        if len(args.package) == 1:
            manager.install(args.package[0], args.project, args.dry_run, force=args.force)
        else:
            ordered = manager.resolve_install_order(args.package)
            for pkg_name in ordered:
                manager.install(
                    pkg_name,
                    args.project,
                    args.dry_run,
                    _skip_dep_check=True,
                    force=args.force,
                )
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
    elif args.command == "meta":
        meta_script = orchestra_dir / "packages" / "meta-harness" / "scripts" / "meta_harness.py"
        if not meta_script.is_file():
            print(f"エラー: meta-harness スクリプトが存在しません: {meta_script}", file=sys.stderr)
            sys.exit(1)
        result = subprocess.run(
            [sys.executable, str(meta_script)] + args.meta_args,
            env={**os.environ, "AI_ORCHESTRA_DIR": str(orchestra_dir)},
        )
        sys.exit(result.returncode)
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
