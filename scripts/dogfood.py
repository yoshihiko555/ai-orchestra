#!/usr/bin/env python3
"""ai-orchestra ドッグフーディング管理スクリプト

ai-orchestra リポジトリ自身で orchestration システムを有効化/無効化する。
sync-orchestra.py ベースのファイル同期 + hooks 登録で動作する。

- enable:  sync 実行 + config コピー + hooks 登録
- disable: synced ファイル削除 + config 削除 + settings 復元
- status:  現在の状態を表示
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ORCHESTRA_DIR = Path(__file__).resolve().parent.parent
CLAUDE_DIR = ORCHESTRA_DIR / ".claude"
SETTINGS_FILE = CLAUDE_DIR / "settings.local.json"
BACKUP_FILE = CLAUDE_DIR / "settings.local.json.backup"
ORCHESTRA_JSON = CLAUDE_DIR / "orchestra.json"
CONFIG_DIR = CLAUDE_DIR / "config"
PACKAGES_DIR = ORCHESTRA_DIR / "packages"
SYNC_SCRIPT = ORCHESTRA_DIR / "scripts" / "sync-orchestra.py"


def load_settings() -> Dict[str, Any]:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {}


def save_settings(settings: Dict[str, Any]) -> None:
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


def load_all_manifests() -> List[Dict[str, Any]]:
    """packages/*/manifest.json を全走査して返す"""
    manifests = []
    for manifest_path in sorted(PACKAGES_DIR.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        manifest["_dir"] = manifest_path.parent
        manifests.append(manifest)
    return manifests


def get_hook_command(pkg_name: str, filename: str) -> str:
    """orchestra-manager.py と同じ形式のコマンドを生成"""
    return f'python3 "$AI_ORCHESTRA_DIR/packages/{pkg_name}/hooks/{filename}"'


def add_hook_to_settings(
    settings: Dict[str, Any],
    event: str,
    filename: str,
    pkg_name: str,
    matcher: Optional[str] = None,
    timeout: int = 5,
) -> None:
    """settings にフックを追加（orchestra-manager.py と同じロジック）"""
    if "hooks" not in settings:
        settings["hooks"] = {}
    if event not in settings["hooks"]:
        settings["hooks"][event] = []

    command = get_hook_command(pkg_name, filename)
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


def parse_hook_entry(entry, pkg_name: str) -> tuple:
    """manifest.json の hooks エントリをパース -> (filename, matcher)"""
    if isinstance(entry, str):
        return entry, None
    elif isinstance(entry, dict):
        return entry["file"], entry.get("matcher")
    else:
        raise ValueError(f"Unknown hook entry format in {pkg_name}: {entry}")


def init_orchestra_json() -> None:
    """orchestra.json を初期化（全パッケージをインストール済みとして登録）"""
    manifests = load_all_manifests()
    pkg_names = [m["name"] for m in manifests]
    data = {
        "orchestra_dir": str(ORCHESTRA_DIR),
        "installed_packages": sorted(pkg_names),
        "synced_files": [],
        "last_sync": None,
    }
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    ORCHESTRA_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ─── enable ──────────────────────────────────────────────


def enable():
    print("=== ドッグフーディング有効化 ===\n")

    # 1. orchestra.json を初期化
    print("[1/4] orchestra.json 初期化")
    init_orchestra_json()
    print("  OK   全パッケージを登録")

    # 2. sync-orchestra.py でファイル同期
    print("\n[2/4] ファイル同期 (sync-orchestra.py)")
    env = {
        "AI_ORCHESTRA_DIR": str(ORCHESTRA_DIR),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT)],
        cwd=str(ORCHESTRA_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR sync-orchestra.py 失敗:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    # sync-orchestra の出力を表示
    for line in result.stdout.strip().splitlines():
        print(f"  {line}")
    if not result.stdout.strip():
        print("  OK   同期完了")

    # 3. バックアップ
    print("\n[3/4] settings.local.json バックアップ")
    if BACKUP_FILE.exists():
        print(f"  SKIP {BACKUP_FILE.name} (既存バックアップを保護)")
    elif SETTINGS_FILE.exists():
        shutil.copy2(SETTINGS_FILE, BACKUP_FILE)
        print(f"  OK   {BACKUP_FILE.name} を作成")
    else:
        print("  SKIP settings.local.json が存在しません")

    # 4. hooks 登録
    print("\n[4/4] hooks 登録 (manifest.json から動的読み取り)")
    settings = load_settings()
    permissions = settings.get("permissions")
    settings["hooks"] = {}

    manifests = load_all_manifests()
    hook_count = 0
    for manifest in manifests:
        pkg_name = manifest["name"]
        hooks = manifest.get("hooks", {})
        if not hooks:
            continue

        for event, entries in hooks.items():
            for entry in entries:
                filename, matcher = parse_hook_entry(entry, pkg_name)
                add_hook_to_settings(settings, event, filename, pkg_name, matcher)
                hook_count += 1
                matcher_str = f" (matcher: {matcher})" if matcher else ""
                print(f"  OK   [{event}] {pkg_name}/{filename}{matcher_str}")

    if permissions is not None:
        settings["permissions"] = permissions

    save_settings(settings)
    print(f"\n完了: hooks {hook_count} 個登録")


# ─── disable ─────────────────────────────────────────────


def disable():
    print("=== ドッグフーディング無効化 ===\n")

    # 1. synced ファイル削除
    print("[1/3] synced ファイル削除")
    if ORCHESTRA_JSON.exists():
        data = json.loads(ORCHESTRA_JSON.read_text())
        synced_files = data.get("synced_files", [])
        removed = 0
        for rel_path in synced_files:
            target = CLAUDE_DIR / rel_path
            if target.exists():
                target.unlink()
                removed += 1
        # 空ディレクトリを削除
        for category in ("agents", "skills", "rules"):
            cat_dir = CLAUDE_DIR / category
            if cat_dir.is_dir():
                _remove_empty_dirs(cat_dir)
        ORCHESTRA_JSON.unlink()
        print(f"  OK   {removed} ファイル削除, orchestra.json 削除")
    else:
        print("  SKIP orchestra.json が存在しません")

    # 2. config ディレクトリ削除
    print("\n[2/3] config ディレクトリ削除")
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        print(f"  OK   {CONFIG_DIR.relative_to(ORCHESTRA_DIR)} を削除")
    else:
        print("  SKIP (存在しない)")

    # 3. settings.local.json 復元
    print("\n[3/3] settings.local.json 復元")
    if BACKUP_FILE.exists():
        shutil.copy2(BACKUP_FILE, SETTINGS_FILE)
        BACKUP_FILE.unlink()
        print(f"  OK   {BACKUP_FILE.name} から復元")
    else:
        print(
            "  WARN バックアップが存在しません (手動復元が必要かもしれません)",
            file=sys.stderr,
        )

    print("\n完了: ドッグフーディングを無効化しました")


def _remove_empty_dirs(path: Path) -> None:
    """空のサブディレクトリを再帰的に削除する。"""
    for child in sorted(path.iterdir(), reverse=True):
        if child.is_dir():
            _remove_empty_dirs(child)
            if not any(child.iterdir()):
                child.rmdir()
    if path.is_dir() and not any(path.iterdir()):
        path.rmdir()


# ─── status ──────────────────────────────────────────────


def status():
    print("=== ドッグフーディング状態 ===\n")

    # synced ファイル確認
    sync_ok = False
    print("[synced ファイル]")
    if ORCHESTRA_JSON.exists():
        data = json.loads(ORCHESTRA_JSON.read_text())
        synced = data.get("synced_files", [])
        existing = [f for f in synced if (CLAUDE_DIR / f).exists()]
        print(f"  同期済み: {len(existing)}/{len(synced)} ファイル")
        missing = set(synced) - set(existing)
        if missing:
            for f in sorted(missing):
                print(f"    MISSING {f}")
        sync_ok = len(existing) == len(synced) and len(synced) > 0
        last_sync = data.get("last_sync", "不明")
        print(f"  最終同期: {last_sync}")
    else:
        print("  orchestra.json が存在しません")

    # config 確認
    print("\n[config ファイル]")
    if CONFIG_DIR.exists():
        config_files = list(CONFIG_DIR.rglob("*"))
        config_files = [f for f in config_files if f.is_file()]
        for f in sorted(config_files):
            print(f"  OK   {f.relative_to(CLAUDE_DIR)}")
    else:
        print("  (なし)")

    # hooks 確認
    print("\n[hooks 登録]")
    settings = load_settings()
    registered_hooks = set()
    for event, entries in settings.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                registered_hooks.add(cmd)

    expected_hooks = set()
    manifests = load_all_manifests()
    for manifest in manifests:
        pkg_name = manifest["name"]
        hooks = manifest.get("hooks", {})
        for event, entries in hooks.items():
            for entry in entries:
                filename, _ = parse_hook_entry(entry, pkg_name)
                expected_hooks.add(get_hook_command(pkg_name, filename))

    registered_count = len(registered_hooks & expected_hooks)
    expected_count = len(expected_hooks)
    print(f"  登録済み: {registered_count}/{expected_count}")

    missing_hooks = expected_hooks - registered_hooks
    if missing_hooks:
        print("  未登録:")
        for cmd in sorted(missing_hooks):
            print(f"    - {cmd}")

    extra = registered_hooks - expected_hooks
    if extra:
        print("  manifest.json に定義なし:")
        for cmd in sorted(extra):
            print(f"    - {cmd}")

    hooks_ok = registered_count == expected_count and not extra
    enabled = sync_ok and hooks_ok

    print(f"\n状態: {'有効' if enabled else '無効 (一部未設定)'}")
    if not enabled:
        print("ヒント: `task dogfood:enable` を再実行してください")


# ─── main ────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print("Usage: dogfood.py <enable|disable|status>", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    if command == "enable":
        enable()
    elif command == "disable":
        disable()
    elif command == "status":
        status()
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print("Usage: dogfood.py <enable|disable|status>", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
