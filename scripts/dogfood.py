#!/usr/bin/env python3
"""ai-orchestra ドッグフーディング管理スクリプト

ai-orchestra リポジトリ自身で orchestration システムを有効化/無効化する。
- enable:  シンボリックリンク作成 + config コピー + hooks 登録
- disable: シンボリックリンク削除 + config 削除 + settings 復元
- status:  現在の状態を表示
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ORCHESTRA_DIR = Path(__file__).resolve().parent.parent
CLAUDE_DIR = ORCHESTRA_DIR / ".claude"
SETTINGS_FILE = CLAUDE_DIR / "settings.local.json"
BACKUP_FILE = CLAUDE_DIR / "settings.local.json.backup"
CONFIG_DIR = CLAUDE_DIR / "config"
PACKAGES_DIR = ORCHESTRA_DIR / "packages"

# .claude/ 直下にシンボリックリンクを作成するディレクトリ
SYMLINK_TARGETS = ["agents", "skills", "rules"]

# sync-orchestra の SessionStart hook は登録しない（自分自身なので不要）
SKIP_HOOKS = {"sync-orchestra"}


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


# ─── enable ──────────────────────────────────────────────

def enable():
    print("=== ドッグフーディング有効化 ===\n")

    # 1. シンボリックリンク作成
    print("[1/4] シンボリックリンク作成")
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    for name in SYMLINK_TARGETS:
        link = CLAUDE_DIR / name
        target = Path("..") / name  # 相対パス

        if link.is_symlink():
            print(f"  SKIP {link} (既存シンボリックリンク)")
            continue
        if link.exists():
            print(f"  ERROR {link} が既存ディレクトリとして存在します", file=sys.stderr)
            print("  手動で削除してから再実行してください", file=sys.stderr)
            sys.exit(1)

        link.symlink_to(target)
        print(f"  OK   {link} -> {target}")

    # 2. config ファイルコピー
    print("\n[2/4] config ファイルコピー")
    manifests = load_all_manifests()
    config_count = 0
    for manifest in manifests:
        pkg_dir = manifest["_dir"]
        for config_path in manifest.get("config", []):
            src = pkg_dir / config_path
            dest = CONFIG_DIR / Path(config_path).name
            if not src.exists():
                print(f"  WARN {src} が見つかりません", file=sys.stderr)
                continue
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            print(f"  OK   {dest.relative_to(ORCHESTRA_DIR)}")
            config_count += 1

    if config_count == 0:
        print("  (コピー対象なし)")

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

    # hooks を再構築（permissions は維持）
    permissions = settings.get("permissions")
    settings["hooks"] = {}

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
    print(f"\n完了: シンボリックリンク {len(SYMLINK_TARGETS)} 個, config {config_count} 個, hooks {hook_count} 個")


# ─── disable ─────────────────────────────────────────────

def disable():
    print("=== ドッグフーディング無効化 ===\n")

    # 1. シンボリックリンク削除
    print("[1/3] シンボリックリンク削除")
    for name in SYMLINK_TARGETS:
        link = CLAUDE_DIR / name
        if link.is_symlink():
            link.unlink()
            print(f"  OK   {link} を削除")
        else:
            print(f"  SKIP {link} (シンボリックリンクではない)")

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
        print("  WARN バックアップが存在しません (手動復元が必要かもしれません)", file=sys.stderr)

    print("\n完了: ドッグフーディングを無効化しました")


# ─── status ──────────────────────────────────────────────

def status():
    print("=== ドッグフーディング状態 ===\n")

    # シンボリックリンク確認
    symlink_ok = True
    print("[シンボリックリンク]")
    for name in SYMLINK_TARGETS:
        link = CLAUDE_DIR / name
        if link.is_symlink():
            print(f"  OK   {name} -> {link.resolve()}")
        else:
            print(f"  NG   {name} (未作成)")
            symlink_ok = False

    # config 確認
    print("\n[config ファイル]")
    if CONFIG_DIR.exists():
        for f in sorted(CONFIG_DIR.iterdir()):
            print(f"  OK   {f.name}")
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

    # manifest.json から期待される hooks を収集
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

    missing = expected_hooks - registered_hooks
    if missing:
        print("  未登録:")
        for cmd in sorted(missing):
            print(f"    - {cmd}")

    extra = registered_hooks - expected_hooks
    if extra:
        print("  manifest.json に定義なし:")
        for cmd in sorted(extra):
            print(f"    - {cmd}")

    # 判定
    hooks_ok = registered_count == expected_count and not extra
    enabled = symlink_ok and hooks_ok

    print(f"\n状態: {'有効' if enabled else '無効 (一部未設定)'}")
    if not enabled and symlink_ok:
        print("ヒント: `task dogfood:enable` を再実行すると hooks が最新化されます")


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
