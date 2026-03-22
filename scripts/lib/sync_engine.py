"""パッケージ同期エンジン（SessionStart hook のコアロジック）。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from lib.hook_utils import (
    add_hook_to_settings,
    find_hook_in_settings,
    get_hook_command,
    is_orchestra_hook,
    parse_hook_entry,
    parse_pkg_from_command,
    remove_hook_from_settings,
)


def needs_sync(src: Path, dst: Path) -> bool:
    """ソースがデスティネーションより新しいか、デスティネーションが存在しないか判定する。"""
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def is_local_override(category: str, rel_path: Path) -> bool:
    """プロジェクト固有の上書きファイル（*.local.yaml / *.local.json）かどうか判定する。"""
    name = rel_path.name
    return category == "config" and (name.endswith(".local.yaml") or name.endswith(".local.json"))


def remove_stale_files(claude_dir: Path, prev_synced: list[str], current_synced: set[str]) -> int:
    """前回同期したが今回は対象外になったファイルを削除する。

    削除後に空になったディレクトリも再帰的に削除する。
    """
    removed = 0
    for file_key in prev_synced:
        if file_key in current_synced:
            continue
        parts = file_key.split("/", 1)
        if len(parts) == 2 and is_local_override(parts[0], Path(parts[1])):
            continue
        target = claude_dir / file_key
        if target.is_file():
            target.unlink()
            removed += 1
            parent = target.parent
            while parent != claude_dir and parent.is_dir():
                try:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break
    return removed


def collect_facet_managed_paths(orchestra_path: Path, project_dir: Path) -> set[str]:
    """facet composition で管理される skill/rule のパスを収集する。

    返すパスは .claude/ 相対（例: "skills/review/SKILL.md", "rules/coding-principles.md"）。
    """
    managed: set[str] = set()
    dirs = []
    compositions_dir = orchestra_path / "facets" / "compositions"
    if compositions_dir.is_dir():
        dirs.append(compositions_dir)
    local_dir = project_dir / ".claude" / "facets" / "compositions"
    if local_dir.is_dir():
        dirs.append(local_dir)

    for d in dirs:
        for ypath in d.glob("*.yaml"):
            try:
                with open(ypath, encoding="utf-8") as f:
                    if yaml:
                        comp = yaml.safe_load(f)
                    else:
                        text = f.read()
                        m_name = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
                        m_type = re.search(r"^type:\s*(.+)$", text, re.MULTILINE)
                        comp = {}
                        if m_name:
                            comp["name"] = m_name.group(1).strip()
                        if m_type:
                            comp["type"] = m_type.group(1).strip()
            except OSError:
                continue
            if not isinstance(comp, dict) or "name" not in comp:
                continue
            name = comp["name"]
            comp_type = comp.get("type", "skill")
            if comp_type == "rule":
                managed.add(f"rules/{name}.md")
            else:
                managed.add(f"skills/{name}/SKILL.md")
                for kname in comp.get("knowledge") or []:
                    if isinstance(kname, str):
                        managed.add(f"skills/{name}/references/{kname}.md")
                for sname in comp.get("scripts") or []:
                    if isinstance(sname, str):
                        managed.add(f"skills/{name}/scripts/{sname}")
    return managed


def collect_manifest_compositions(
    orchestra_path: Path,
) -> dict[str, str]:
    """Collect composition names from skills/rules fields of ALL package manifests.

    Scans every package directory (not just installed ones) so that
    package-owned compositions can be distinguished from global ones.

    Returns:
        {composition_name: package_name} mapping
    """
    compositions: dict[str, str] = {}
    packages_dir = orchestra_path / "packages"
    if not packages_dir.is_dir():
        return compositions
    for pkg_dir in sorted(packages_dir.iterdir()):
        manifest_path = pkg_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        pkg_name = manifest.get("name", pkg_dir.name)
        for field in ("skills", "rules"):
            for name in manifest.get(field, []):
                if isinstance(name, str):
                    if name in compositions:
                        print(
                            f"[warn] composition '{name}' claimed by both"
                            f" '{compositions[name]}' and '{pkg_name}'",
                            file=sys.stderr,
                        )
                    compositions[name] = pkg_name
    return compositions


def sync_hooks(
    project_dir: Path,
    orchestra_path: Path,
    installed_packages: list[str],
) -> int:
    """manifest.json の hooks と settings.local.json を比較し差分を同期する。

    Returns:
        変更があった hook 数（追加 + 削除）
    """
    settings_path = project_dir / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return 0

    try:
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0

    settings_hooks = settings.get("hooks", {})

    sync_hook_command = 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"'

    expected_hooks: set[tuple[str, str, str | None]] = set()
    installed_set = set(installed_packages)

    for pkg_name in installed_packages:
        manifest_path = orchestra_path / "packages" / pkg_name / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        for event, entries in manifest.get("hooks", {}).items():
            for raw_entry in entries:
                filename, matcher = parse_hook_entry(raw_entry)
                if not filename:
                    continue
                command = get_hook_command(pkg_name, filename)
                expected_hooks.add((event, command, matcher))

    added = 0
    for event, command, matcher in expected_hooks:
        if not find_hook_in_settings(settings_hooks, event, command, matcher):
            add_hook_to_settings(settings_hooks, event, command, matcher)
            added += 1

    removed = 0
    for event, entries in list(settings_hooks.items()):
        for entry in list(entries):
            matcher = entry.get("matcher")
            for hook in list(entry.get("hooks", [])):
                command = hook.get("command", "")
                if command == sync_hook_command:
                    continue
                if not is_orchestra_hook(command):
                    continue
                pkg_name = parse_pkg_from_command(command)
                if pkg_name is not None and pkg_name not in installed_set:
                    remove_hook_from_settings(settings_hooks, event, command, matcher)
                    removed += 1
                    continue
                if (event, command, matcher) not in expected_hooks:
                    remove_hook_from_settings(settings_hooks, event, command, matcher)
                    removed += 1

    changes = added + removed
    if changes > 0:
        settings["hooks"] = settings_hooks
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass

    return changes


def sync_packages(
    claude_dir: Path,
    orchestra_path: Path,
    installed_packages: list[str],
    facet_managed: set[str],
) -> tuple[int, set[str]]:
    """パッケージ単位のファイル同期を実行する。

    Returns:
        (synced_count, synced_files)
    """
    synced_count = 0
    synced_files: set[str] = set()

    for pkg_name in installed_packages:
        manifest_path = orchestra_path / "packages" / pkg_name / "manifest.json"
        if not manifest_path.exists():
            continue

        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        pkg_dir = orchestra_path / "packages" / pkg_name

        # "skills" は facet build に完全委譲（manifest-SSOT: Issue #20）
        for category in ("agents", "rules", "config"):
            file_list = manifest.get(category, [])
            for rel_path in file_list:
                src = pkg_dir / rel_path
                if not src.exists():
                    continue

                if src.is_dir():
                    for src_file in src.rglob("*"):
                        if not src_file.is_file():
                            continue
                        file_rel = str(src_file.relative_to(pkg_dir))
                        if file_rel in facet_managed:
                            continue
                        synced_files.add(file_rel)
                        dst = claude_dir / file_rel
                        if not needs_sync(src_file, dst):
                            continue
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dst)
                        synced_count += 1
                else:
                    if category == "config":
                        filename = Path(rel_path).name
                        dst = claude_dir / "config" / pkg_name / filename
                        dst_key = f"config/{pkg_name}/{filename}"
                    else:
                        dst = claude_dir / rel_path
                        dst_key = rel_path

                    if dst_key in facet_managed:
                        continue

                    synced_files.add(dst_key)

                    if not needs_sync(src, dst):
                        continue

                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    synced_count += 1

    return synced_count, synced_files


def build_facets(
    orchestra_path: Path,
    project_dir: Path,
    installed_packages: list[str] | None = None,
) -> int:
    """facet composition から SKILL.md / ルール .md を自動生成する。"""

    compositions_dir = orchestra_path / "facets" / "compositions"
    local_compositions_dir = project_dir / ".claude" / "facets" / "compositions"

    has_orchestra = compositions_dir.is_dir() and any(compositions_dir.glob("*.yaml"))
    has_local = local_compositions_dir.is_dir() and any(local_compositions_dir.glob("*.yaml"))
    if not has_orchestra and not has_local:
        return 0

    yamls: list[Path] = []
    if has_orchestra:
        yamls.extend(compositions_dir.glob("*.yaml"))
    if has_local:
        yamls.extend(local_compositions_dir.glob("*.yaml"))

    latest_src = max(p.stat().st_mtime for p in yamls)
    facets_dir = orchestra_path / "facets"
    facet_mds = list(facets_dir.glob("**/*.md"))
    if facet_mds:
        latest_src = max(latest_src, max(p.stat().st_mtime for p in facet_mds))
    local_facets_dir = project_dir / ".claude" / "facets"
    if local_facets_dir.is_dir():
        local_facet_mds = list(local_facets_dir.glob("**/*.md"))
        if local_facet_mds:
            latest_src = max(latest_src, max(p.stat().st_mtime for p in local_facet_mds))

    orch_json = project_dir / ".claude" / "orchestra.json"
    if orch_json.is_file():
        try:
            orch_data = json.loads(orch_json.read_text(encoding="utf-8"))
            pkgs_str = ",".join(sorted(orch_data.get("installed_packages", [])))
        except (json.JSONDecodeError, OSError):
            pkgs_str = ""
        pkgs_hash = hashlib.md5(pkgs_str.encode()).hexdigest()
        hash_file = project_dir / ".claude" / ".facet-packages-hash"
        prev_hash = ""
        if hash_file.is_file():
            try:
                prev_hash = hash_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if pkgs_hash != prev_hash:
            try:
                hash_file.write_text(pkgs_hash, encoding="utf-8")
            except OSError:
                pass
            import time

            latest_src = time.time()

    claude_skills = project_dir / ".claude" / "skills"
    claude_rules = project_dir / ".claude" / "rules"
    generated: list[Path] = []
    if claude_skills.is_dir():
        generated.extend(claude_skills.glob("*/SKILL.md"))
    if claude_rules.is_dir():
        generated.extend(claude_rules.glob("*.md"))
    if generated and min(p.stat().st_mtime for p in generated) >= latest_src:
        return 0

    script = orchestra_path / "scripts" / "orchestra-manager.py"
    if not script.is_file():
        return 0

    targets = ["claude"]
    if installed_packages and "codex-suggestions" in installed_packages:
        targets.append("codex")

    total_built = 0
    for target in targets:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "facet",
                    "build",
                    "--target",
                    target,
                    "--project",
                    str(project_dir),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            print(f"[orchestra] facet build ({target}) timed out", file=sys.stderr)
            continue
        except OSError as e:
            print(f"[orchestra] facet build ({target}) failed: {e}", file=sys.stderr)
            continue

        if result.returncode != 0:
            print(
                f"[orchestra] facet build ({target}) error: {result.stderr.strip()}",
                file=sys.stderr,
            )
            continue

        total_built += result.stdout.count("[facet] built")
        for line in result.stdout.splitlines():
            if "[facet] removed" in line or "[facet] cleanup" in line:
                print(line)

    return total_built
