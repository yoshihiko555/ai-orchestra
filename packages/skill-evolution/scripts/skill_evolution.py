#!/usr/bin/env python3
"""v1 互換の非推奨案内と、明示的な旧データの削除。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサを構築する。"""
    parser = argparse.ArgumentParser(
        prog="skill_evolution",
        description="スキル改善 v1 互換 CLI（非推奨: ADR-20261108-065）",
        allow_abbrev=False,
    )
    parser.add_argument("--project", default=os.getcwd(), help="プロジェクトルート（既定: cwd）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="metrics サマリ")
    p_status.add_argument("skill")

    p_trigger = sub.add_parser("check-trigger", help="オフライン起動判定")
    p_trigger.add_argument("skill")

    p_eval = sub.add_parser("evaluate", help="停止条件＋3ガード評価")
    p_eval.add_argument("--history", required=True, help="反復履歴 JSON パス（- で stdin）")

    p_prov = sub.add_parser("provenance", help="facet/非facet 判別と反映先")
    p_prov.add_argument("skill")

    p_lock = sub.add_parser("lock", help="スキル単位ロック")
    p_lock.add_argument("action", choices=["acquire", "release"])
    p_lock.add_argument("skill")

    p_purge = sub.add_parser(
        "purge-v1", help="全 worktree の v1 データを一覧表示", allow_abbrev=False
    )
    p_purge.add_argument("--apply", action="store_true", help="安全な v1 ディレクトリを削除")
    p_purge.add_argument("--json", action="store_true", help="JSON で結果を表示")

    return parser


def _worktrees(project: Path, core: Any) -> tuple[list[Path], list[dict]]:
    root = core.resolve_root_worktree(str(project))
    if root is None:
        raise RuntimeError("git の root worktree を解決できません")
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=project,
        env=core.sanitized_git_env(),
        capture_output=True,
        text=True,
        timeout=core.GIT_COMMAND_TIMEOUT_SECONDS,
        check=True,
    ).stdout
    entries = {}
    for record in listing.strip("\0").split("\0\0"):
        fields = record.split("\0")
        if not fields[0].startswith("worktree "):
            raise RuntimeError("git の worktree 一覧が正しくありません")
        path = Path(fields[0].removeprefix("worktree "))
        if not path.is_absolute():
            raise RuntimeError("git の worktree 一覧が正しくありません")
        entries[path.resolve()] = any(
            field == "prunable" or field.startswith("prunable ") for field in fields[1:]
        )
    if not entries:
        raise RuntimeError("git の worktree 一覧が正しくありません")
    paths, skipped = [], []
    for path in dict.fromkeys([Path(root).resolve(), *entries]):
        if entries.get(path) or not path.exists():
            skipped.append(
                {
                    "worktree": str(path),
                    "path": str(path),
                    "kind": "worktree",
                    "status": "skipped",
                    "message": "prunable のため処理しません"
                    if entries.get(path)
                    else "worktree が存在しないため処理しません",
                }
            )
        else:
            paths.append(path)
    return paths, skipped


def _count_tree(path: Path) -> tuple[int, int]:
    directories, files = 1, 0

    def raise_error(error: OSError) -> None:
        raise error

    for parent, names, filenames in os.walk(path, followlinks=False, onerror=raise_error):
        for name in [*names, *filenames]:
            if stat.S_ISDIR((Path(parent) / name).lstat().st_mode):
                directories += 1
            else:
                files += 1
    return directories, files


def _inspect_target(
    worktree: Path, relative: object, name: str, core: Any, v2_storage_dirs: set[Path]
) -> dict | None:
    configured = Path(relative) if isinstance(relative, str) else None
    path = Path(os.path.abspath(worktree / configured / name)) if configured else worktree / name
    target = {
        "worktree": str(worktree),
        "path": str(path),
        "kind": "unknown",
        "status": "skipped",
    }
    refused = {**target, "message": "`.claude` の外のため消さない"}
    boundary = worktree / ".claude"
    if (
        configured is None
        or not relative
        or configured.is_absolute()
        or ".." in configured.parts
        or configured == Path(".claude")
        or not configured.is_relative_to(".claude")
    ):
        return refused
    resolved_parent = core.resolve_path_within(str(worktree), relative, "")
    if resolved_parent is None or not Path(resolved_parent).is_relative_to(boundary):
        return refused
    if Path(resolved_parent) == boundary:
        return refused
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and stat.S_ISLNK(mode):
        return {**target, "kind": "symlink", "message": "symlink のため消さない"}
    resolved = core.resolve_path_within(str(worktree), relative, name)
    if (
        resolved is None
        or not Path(resolved).is_relative_to(boundary)
        or Path(resolved) == boundary
    ):
        return refused
    if mode is None:
        return None
    if any(directory.is_relative_to(Path(resolved)) for directory in v2_storage_dirs):
        return {**target, "message": "v2 の保存先を含むため消さない"}
    if not stat.S_ISDIR(mode):
        return {**target, "kind": "file", "message": "ディレクトリではないため消さない"}
    directories, files = _count_tree(path)
    return {
        **target,
        "kind": "directory",
        "status": "would_delete",
        "directories": directories,
        "files": files,
        "message": "削除予定",
    }


def purge_v1(project_dir: str, *, apply: bool = False) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    import skill_evolution_common as common

    core = common._load_hook_common()
    if core is None:
        raise RuntimeError("core のパス検証を読み込めません")
    worktrees, targets = _worktrees(Path(project_dir).resolve(), core)
    configs = {worktree: common.load_config(str(worktree)) for worktree in worktrees}
    v2_storage_dirs = {
        Path(common.logs_dir(str(worktree), config)).resolve()
        for worktree, config in configs.items()
    }
    v2_storage_dirs.update(
        Path(common._resolve_logs_subdir(str(worktree), config)).resolve()
        for worktree, config in configs.items()
    )
    for worktree, config in configs.items():
        storage = config.get("storage") or {}
        for key, names in (("dir", ("lessons",)), ("logs_dir", ("metrics", "pending", "locks"))):
            relative = storage.get(key, common.DEFAULTS["storage"][key])
            for name in names:
                target = _inspect_target(worktree, relative, name, core, v2_storage_dirs)
                if target is not None:
                    targets.append(target)
    if apply:
        # 一覧の取得や走査の失敗では、まだどの worktree からも削除しない。
        for target in targets:
            if target["status"] != "would_delete":
                continue
            path = Path(target["path"])
            worktree = Path(target["worktree"])
            relative = str(path.parent.relative_to(worktree))
            try:
                current = _inspect_target(worktree, relative, path.name, core, v2_storage_dirs)
                if current is None or current["status"] != "would_delete":
                    target.update(
                        current or {"status": "skipped", "message": "対象がなくなりました"}
                    )
                    continue
                shutil.rmtree(path)
                target.update(status="deleted", message="削除しました")
            except OSError as exc:
                target.update(status="failed", message=f"削除できません: {exc}")
    return {"apply": apply, "targets": targets}


def _purge_command(args: argparse.Namespace) -> int:
    try:
        payload = purge_v1(args.project, apply=args.apply)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[skill-evolution] v1 データを削除できません: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for target in payload["targets"]:
            counts = (
                f"、ディレクトリ {target['directories']} 件、ファイル {target['files']} 件"
                if target["kind"] == "directory"
                else ""
            )
            print(f"{target['path']} [{target['kind']}{counts}]: {target['message']}")
    return 1 if any(target["status"] == "failed" for target in payload["targets"]) else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "purge-v1":
        return _purge_command(args)
    payload = {
        "deprecated": True,
        "message": "v1 の処理は非推奨です。ADR-20261108-065 を参照してください。",
    }
    if args.command == "check-trigger":
        payload["triggered"] = False
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if args.command == "check-trigger" else 0


if __name__ == "__main__":
    sys.exit(main())
