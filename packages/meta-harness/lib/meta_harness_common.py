#!/usr/bin/env python3
"""meta-harness の共通ライブラリ（決定論ロジック。Phase 1a スコープ）。

責務（docs/design/meta-harness-detailed.md が正本）:
- メインルート解決（Sec2-0）・config 読み込み（config-loading ルール準拠）
- store I/O（init/register/ledger append/frontier cache）と ledger 畳み込み（Sec1-2）
- Pareto frontier 判定（Sec3-5）・quality_score ヘルパー（Sec3-2）
- 最小限の JSON Schema 検証器（Sec1 の 10 スキーマ向け。依存追加はしない）
- overlay / config-patch 検証（Sec1-7, Sec1-8）
- store.lock による排他制御（Sec2-3。Phase 1a は store.lock のみ）
- cand_id 採番（Sec1-1, Sec2-4）

evaluator（worktree ライフサイクル・ヘッドレス実行・oracle 判定）は Phase 1b のスコープであり、
本モジュールには含まれない（Sec9 参照）。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

PACKAGE_NAME = "meta-harness"
CONFIG_FILENAME = "meta-harness.yaml"
PACKAGE_DIR = Path(__file__).resolve().parent.parent
TARGET_PATTERN = re.compile(r"^(?:claude-harness|skill:[a-z0-9-]+)$")
DEFAULT_TARGET = "claude-harness"

# config が読めない場合のフォールバック既定値（正本は config/meta-harness.yaml、Sec5）。
DEFAULTS: dict[str, Any] = {
    "storage": {"root": None, "dir": ".claude/meta-harness"},
    "evaluate": {
        "worktree_root": ".worktrees/meta",
        "repeat_default": 1,
        "repeat_frontier": 3,
        "timeout_ms_default": 300000,
        "permission_mode": "acceptEdits",
        "allowed_tools": [
            "Read",
            "Glob",
            "Grep",
            "Edit",
            "Write",
            "Bash(git *)",
            "Bash(python *)",
            "Bash(python3 *)",
            "Bash(pytest *)",
        ],
        "model": None,
        "cli_version_pin": None,
        "isolation": {
            "backend": "docker",
            "execution_backend": "docker",
            "image": "ai-orchestra/meta-harness-scenario:2.1.207",
            "image_pin": "2.1.207 (Claude Code)",
            "auto_build_images": True,
            "resources": {
                "pids_limit": 128,
                "memory": "2g",
                "cpus": 2.0,
                "workspace_size": "512m",
                "workspace_max_files": 10000,
            },
            "broker": {
                "image": "ai-orchestra/meta-harness-broker:0.1.0",
                "port_range": [8790, 8990],
                "idle_timeout_sec": 300,
                "startup_timeout_sec": 30,
                "max_requests": 64,
                "max_total_tokens": 500000,
                "max_upstream_bytes": 50000000,
                "pricing_upper_bound_usd_per_million": {
                    "input": 15.0,
                    "output": 75.0,
                    "cache_creation": 18.75,
                    "cache_read": 1.5,
                },
            },
        },
    },
    "scenario_run": {
        "max_turns_default": 30,
        "max_budget_usd_default": 3.0,
        "max_output_tokens_default": 4096,
    },
    "judge": {"tool": "claude-bare", "model": None, "effort": "high", "max_turns": 4},
    "scoring": {
        "critical_weight": 70,
        "penalty_base": 30,
        "penalty_per_item": 5,
        "penalty_missing_report": 6,
    },
    "frontier": {"cost_axis": "total_tokens"},
    "regression": {
        "enabled": True,
        "max_affected_suites": 4,
        "max_budget_usd": 12.0,
    },
    "overlay": {
        "allowed_prefixes": ["facets/"],
        "denied_prefixes": [
            "packages/meta-harness/",
            ".claude/meta-harness/",
            "docs/evaluation/",
            ".github/",
        ],
    },
    "config_patch": {"allowlist": []},
    "proposer": {
        "tool": "codex",
        "max_iterations": 10,
        "divergence_rounds": 3,
        "overfit_drop_pt": 15,
        "budget_usd_per_iteration": 1.0,
        "max_turns": 40,
        "timeout_seconds": 600,
        "max_focus_runs": 5,
        "max_overlay_bytes": 200000,
        "model": None,
        "effort": "high",
        "isolation": {
            "backend": "srt",
            "srt_version_pin": None,
            "allow_read_extra": [],
        },
    },
    "loop": {
        "budget_usd": None,
        "quality_epsilon_pt": 0.5,
        "convergence": {"enabled": True, "quality_band_pt": 3, "rounds": 2},
    },
    "promote": {
        "verify_command": None,
        "allow_stale": False,
        "reservation_ttl_hours": 24,
    },
    "locks": {
        "store_ttl_seconds": 60,
        "evaluate_heartbeat_seconds": 60,
        "evaluate_stale_seconds": 300,
    },
    "retention": {"keep_generations": 5},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """override で base を再帰的に上書きした新しい dict を返す。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(project_dir: str | Path) -> dict:
    """meta-harness.yaml を読み込み DEFAULTS にマージする。

    hook_common.load_package_config が使える場合はそれを使う（`.claude/config/
    meta-harness/meta-harness.yaml` > パッケージ既定 `config/meta-harness.yaml`、
    さらに `.local.yaml` 上書きを config-loading ルールどおり適用する）。
    使えない場合も、このパッケージ自身で base/local YAML を読み込む。
    """
    try:
        orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        core_hooks = os.path.join(orchestra_dir, "packages", "core", "hooks")
        if os.path.isabs(core_hooks) and os.path.isdir(core_hooks) and core_hooks not in sys.path:
            sys.path.insert(0, core_hooks)
        from hook_common import load_package_config

        loaded = load_package_config(PACKAGE_NAME, CONFIG_FILENAME, str(project_dir))
    except ImportError:
        try:
            loaded = _load_config_without_hook_common(Path(project_dir))
        except Exception as exc:
            print(
                f"warning: failed to load meta-harness config, falling back to defaults: {exc}",
                file=sys.stderr,
            )
            loaded = {}
    except Exception as exc:
        print(
            f"warning: failed to load meta-harness config, falling back to defaults: {exc}",
            file=sys.stderr,
        )
        loaded = {}
    return _deep_merge(DEFAULTS, loaded or {})


def _load_config_without_hook_common(project_dir: Path) -> dict:
    """hook_common 不在時の最小 config loader（config-loading ルールの fallback）。"""
    base = _read_yaml_config(PACKAGE_DIR / "config" / CONFIG_FILENAME)
    name, ext = os.path.splitext(CONFIG_FILENAME)
    local = _read_yaml_config(
        project_dir / ".claude" / "config" / PACKAGE_NAME / f"{name}.local{ext}"
    )
    return _deep_merge(base, local) if local else base


def _read_yaml_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# メインルート解決（Sec2-0）
# ---------------------------------------------------------------------------

GIT_TIMEOUT_SECONDS = 10


class MetaHarnessRootError(RuntimeError):
    """main root（store の配置先）が解決できない場合に送出する（CLI は exit 2）。"""


def resolve_main_root(project_dir: Path, config: dict) -> Path:
    """store / 評価用 worktree の配置基準となる main root を解決する（Sec2-0）。

    `storage.root` が絶対パスで明示されていればそれを使う。未指定（null）なら
    `git rev-parse --git-common-dir` の親ディレクトリを main root とする。
    """
    storage_root = (config.get("storage") or {}).get("root")
    if storage_root:
        root = Path(storage_root)
        if not root.is_absolute():
            raise MetaHarnessRootError(
                f"storage.root must be an absolute path, got: {storage_root}"
            )
        return root

    common_dir = _git_common_dir(project_dir)
    if common_dir is None:
        raise MetaHarnessRootError(
            "could not resolve main root via `git rev-parse --git-common-dir`"
            f" (project_dir={project_dir}); set storage.root explicitly for bare repos"
        )
    return common_dir.parent


def _git_common_dir(project_dir: Path) -> Path | None:
    """`git rev-parse --git-common-dir` を実行し、絶対パスの `.git` 共通ディレクトリを返す。

    bare repo（`git rev-parse --is-bare-repository` が true）は None を返す。bare repo には
    チェックアウト済みの working tree が存在せず、`--git-common-dir` はリポジトリ自身のディレクトリ
    （`.`）を返してしまうため、そのまま解釈すると「bare repo の親ディレクトリ」という意味のない
    main root を導出してしまう（Sec2-0 が明示的に bare repo を「メインルートの親ディレクトリを
    導出できない環境」の例として挙げているのに反する。docstring 内の既存コメント「set storage.root
    explicitly for bare repos」からも、この経路は元々 fail-closed される想定だったと判断した）。
    """
    if _is_bare_repository(project_dir):
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout.strip()
    if not raw:
        return None
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = (project_dir / common_dir).resolve()
    return common_dir


def _is_bare_repository(project_dir: Path) -> bool:
    """`git rev-parse --is-bare-repository` の結果を bool で返す（失敗時は False）。"""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def git_head(cwd: Path) -> str | None:
    """`cwd` における `git rev-parse HEAD` の結果を返す。"""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def build_candidate_manifest(
    *,
    cand_id: str,
    parent_id: str | None,
    generation: int,
    target: str,
    source_commit: str,
    config_hash: str,
    overlay_files: list[str],
    description: str,
    created_by: str = "human",
    target_closure_hash: str | None = None,
) -> dict[str, Any]:
    """candidate manifest の共通組み立て処理。"""
    manifest = {
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": parent_id,
        "generation": generation,
        "created_at": now_iso(),
        "created_by": created_by,
        "target": target,
        "source_commit": source_commit,
        "config_hash": config_hash,
        "model_versions": {},
        "overlay_files": overlay_files,
        "description": description,
    }
    if target_closure_hash is not None:
        manifest["target_closure_hash"] = target_closure_hash
    return manifest


# ---------------------------------------------------------------------------
# store パス解決
# ---------------------------------------------------------------------------

STORE_SUBDIRS = ("candidates", "runs", "locks", "tmp", "rejected", "reports")

# overlay/config-patch.json（存在する場合のみ）は overlay の facets/** コンテンツではなく
# Sec1-8 の予約サイドカーファイル。overlay_files 一覧や facets/ prefix 検証の対象外にする。
CONFIG_PATCH_FILENAME = "config-patch.json"


def store_dir(main_root: Path, config: dict) -> Path:
    """store ルート（既定 `.claude/meta-harness`）の絶対パスを返す。

    `storage.dir` はプロジェクト内の相対パスであることを前提とする（main_root 外への
    書き込みを防ぐため）。以下の 2 段階で main_root 外への脱出を拒否する（PR #162
    レビュー指摘）:

    1. 相対パスの各セグメントに `..` を含む場合は即座に拒否する（最も一般的な
       トラバーサル記法）。
    2. `..` を含まない場合でも、シンボリックリンク経由で main_root 外を指す可能性が
       残るため、`(main_root / rel).resolve()` が `main_root.resolve()` 配下にあることを
       追加で検証する（defense in depth）。

    main_root 外に store を意図的に配置したい場合は `storage.dir` ではなく
    `storage.root`（絶対パス指定）を使う運用とする。
    """
    rel = (config.get("storage") or {}).get("dir") or DEFAULTS["storage"]["dir"]
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise MetaHarnessRootError(f"storage.dir must be a relative path, got: {rel}")
    if ".." in rel_path.parts:
        raise MetaHarnessRootError(
            f"storage.dir must not contain '..' path segments, got: {rel};"
            " use storage.root (an absolute path) to place the store outside main_root"
        )
    resolved_main_root = main_root.resolve()
    resolved_candidate = (main_root / rel_path).resolve()
    if (
        resolved_candidate != resolved_main_root
        and resolved_main_root not in resolved_candidate.parents
    ):
        raise MetaHarnessRootError(
            f"storage.dir must resolve to a path under main_root, got: {rel};"
            " use storage.root (an absolute path) to place the store outside main_root"
        )
    return main_root / rel_path


def candidates_dir(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "candidates"


def runs_dir(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "runs"


def holdout_runs_dir(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "holdout" / "runs"


def locks_dir(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "locks"


def tmp_dir(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "tmp"


def rejected_dir(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "rejected"


def reports_dir(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "reports"


def ledger_path(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "ledger.jsonl"


def validate_target(target: str) -> str:
    """Return a validated target or fail closed."""
    if not TARGET_PATTERN.fullmatch(target):
        raise ValueError(f"unknown target: {target!r}")
    return target


def target_slug(target: str) -> str:
    """Map a target to its deterministic cache filename component."""
    return validate_target(target).replace(":", "-")


def frontier_path(main_root: Path, config: dict, target: str = DEFAULT_TARGET) -> Path:
    return store_dir(main_root, config) / f"frontier-{target_slug(target)}.json"


def legacy_frontier_path(main_root: Path, config: dict) -> Path:
    return store_dir(main_root, config) / "frontier.json"


def now_iso() -> str:
    """現在時刻を ISO8601 で返す（date-time 形式）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# store I/O
# ---------------------------------------------------------------------------


def init_store(main_root: Path, config: dict) -> None:
    """store ディレクトリ一式を冪等に初期化する（Sec6 `init`）。"""
    base = store_dir(main_root, config)
    for name in STORE_SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    holdout_runs_dir(main_root, config).mkdir(parents=True, exist_ok=True)

    ledger = ledger_path(main_root, config)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if not ledger.exists():
        ledger.touch()

    if not frontier_path(main_root, config, DEFAULT_TARGET).exists():
        legacy = legacy_frontier_path(main_root, config)
        if legacy.is_file():
            write_frontier_cache(
                main_root,
                config,
                read_frontier_cache(main_root, config, DEFAULT_TARGET),
                DEFAULT_TARGET,
            )
        else:
            write_frontier_cache(
                main_root,
                config,
                _empty_frontier_doc(config, DEFAULT_TARGET),
                DEFAULT_TARGET,
            )


def _empty_frontier_doc(config: dict, target: str = DEFAULT_TARGET) -> dict:
    """runs が 1 件も無い状態の frontier.json スタブ（Sec1-5）。"""
    zero_hash = "0" * 64
    return {
        "schema_version": "1.0",
        "target": validate_target(target),
        "generated_at": now_iso(),
        "ledger_line_count": 0,
        "suite_hash": zero_hash,
        "evaluator_hash": zero_hash,
        "cost_axis": (config.get("frontier") or {}).get("cost_axis", "total_tokens"),
        "points": [],
        "frontier": [],
        "dominated": [],
    }


def list_candidate_ids(main_root: Path, config: dict) -> list[str]:
    """登録済み候補の cand_id 一覧（昇順）を返す。"""
    base = candidates_dir(main_root, config)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def read_candidate_manifest(main_root: Path, config: dict, cand_id: str) -> dict | None:
    """candidates/<cand_id>/manifest.json を読む。存在しなければ None。"""
    path = candidates_dir(main_root, config) / cand_id / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_overlay_files(overlay_dir: Path) -> list[str]:
    """overlay_dir 配下の通常ファイル（`config-patch.json` を除く）を昇順で返す。

    `config-patch.json` は overlay の facets/** コンテンツではない予約サイドカー
    ファイルのため、candidate manifest / overlay-manifest の `overlay_files` /
    `files` には含めない（overlay.schema.json の `files[]` パターンが `facets/`
    prefix を必須にしているため、含めるとスキーマ検証で不整合になる）。
    """
    return sorted(
        rel
        for entry in overlay_dir.rglob("*")
        if entry.is_file() and not entry.is_symlink()
        for rel in [entry.relative_to(overlay_dir).as_posix()]
        if rel != CONFIG_PATCH_FILENAME
    )


def compute_config_hash(overlay_dir: Path, config: dict) -> str:
    """candidate.manifest の `config_hash` を計算する（Sec1-1）。

    【判断】設計書はハッシュ対象の厳密なアルゴリズムまでは規定していないため、
    以下を Phase 1a の確定アルゴリズムとする（監査可能性のためここに明記する）:

    overlay_dir 配下の通常ファイル（symlink 除く）を相対 posix パスの昇順で走査し、
    各エントリについて `<相対パス> + NUL + <生バイト内容> + NUL` を順に sha256 に
    投入した値。config_patch の allowlist（`config_patch.allowlist`）は Phase 1a で
    常に空集合であり（Sec1-8）、config patch を伴う候補は register 時点で拒否される
    ため、"allowlist 対象の config ファイル群" は Phase 1a では常に空集合になる。
    したがって実質的に overlay ファイル群のみがハッシュ対象になる。Phase 2 で
    allowlist が解放された際は、この関数を拡張し `source_commit` 時点の allowlist
    対象ファイルの内容もハッシュ対象に含める必要がある。
    """
    hasher = hashlib.sha256()
    for rel in list_overlay_files(overlay_dir):
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update((overlay_dir / rel).read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def next_generation(main_root: Path, config: dict, parent_id: str | None) -> int:
    """親候補の generation + 1 を返す（parent_id が None なら 0）。"""
    if parent_id is None:
        return 0
    parent = read_candidate_manifest(main_root, config, parent_id)
    if parent is None:
        raise ValueError(f"parent candidate not found: {parent_id}")
    return int(parent.get("generation", 0)) + 1


def register_candidate(
    main_root: Path,
    config: dict,
    *,
    cand_id: str,
    manifest: dict,
    overlay_dir: Path,
    overlay_files: list[str],
    target: str = DEFAULT_TARGET,
    baseline_root: Path | None = None,
    inherited_overlay_dir: Path | None = None,
    skill_allowed_paths: frozenset[str] | None = None,
) -> Path:
    """candidates/<cand_id>/ を immutable に配置する。

    既に同名の候補ディレクトリが存在する場合は `FileExistsError` を送出する
    （immutability 原則、Sec1-1「基本設計からの変更点」参照）。
    """
    tmp_root = tmp_dir(main_root, config)
    tmp_root.mkdir(parents=True, exist_ok=True)
    staging_dir = tmp_root / f"register-{os.urandom(4).hex()}"
    staging_dir.mkdir(parents=True)
    try:
        _copy_overlay_tree(overlay_dir, staging_dir / "overlay")
        copied_overlay = staging_dir / "overlay"
        copied_errors = validate_overlay(
            copied_overlay,
            config,
            target=target,
            baseline_root=baseline_root,
            inherited_overlay_dir=inherited_overlay_dir,
            skill_allowed_paths=skill_allowed_paths,
        )
        if copied_errors:
            raise ValueError(f"copied overlay validation failed: {'; '.join(copied_errors)}")
        if list_overlay_files(copied_overlay) != sorted(overlay_files):
            raise ValueError("copied overlay files differ from validated overlay manifest")
        _write_json(staging_dir / "manifest.json", manifest)
        _write_json(
            staging_dir / "overlay-manifest.json",
            {"schema_version": "1.0", "files": overlay_files},
        )
        base_dir = candidates_dir(main_root, config)
        cand_dir = base_dir / cand_id
        if cand_dir.exists():
            raise FileExistsError(f"candidate already registered (immutable): {cand_id}")
        base_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(staging_dir, cand_dir)
        except FileExistsError:
            raise FileExistsError(f"candidate already registered (immutable): {cand_id}") from None
        except OSError:
            if cand_dir.exists():
                raise FileExistsError(
                    f"candidate already registered (immutable): {cand_id}"
                ) from None
            raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
    return cand_dir


def _copy_overlay_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.rglob("*")):
        if entry.is_dir() or entry.is_symlink():
            continue
        rel = entry.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.read_bytes())


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_ledger_event(main_root: Path, config: dict, event: dict) -> None:
    """ledger.jsonl に 1 行追記する（O_APPEND + flock + fsync、Sec2-3）。"""
    path = ledger_path(main_root, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        start_size = os.fstat(fd).st_size
        try:
            view = memoryview(line)
            written = 0
            while written < len(line):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError(
                        f"short ledger write: expected {len(line)} bytes, wrote {written}"
                    )
                written += count
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, start_size)
            os.fsync(fd)
            raise
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def append_ledger_events_atomically(main_root: Path, config: dict, events: list[dict]) -> None:
    """ledger.jsonl に複数イベントを 1 回の書き込みで原子的に追記する。

    `append_ledger_event`（単一イベント版）と同じ耐障害パターン（O_APPEND + flock + fsync、
    失敗時は開始サイズへ ftruncate）を踏襲しつつ、複数イベントを事前に encode してから
    単一の write にまとめる。これにより、書き込み途中でプロセスが死んでも「一部のイベントだけ
    ledger に残る」状態を防ぐ（全イベントが書けるか、何も書けないかの二値になる）。
    """
    if not events:
        return
    path = ledger_path(main_root, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = b"".join(
        (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        for event in events
    )
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        start_size = os.fstat(fd).st_size
        try:
            view = memoryview(lines)
            written = 0
            while written < len(lines):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError(
                        f"short ledger write: expected {len(lines)} bytes, wrote {written}"
                    )
                written += count
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, start_size)
            os.fsync(fd)
            raise
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def read_ledger_events(main_root: Path, config: dict) -> list[dict]:
    """ledger.jsonl の全イベントを時系列順に読む（不正な行は無視する）。"""
    path = ledger_path(main_root, config)
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in _read_ledger_lines(path):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def read_ledger_events_strict(main_root: Path, config: dict) -> list[dict]:
    """Read every non-empty ledger line or fail with its physical line number."""
    path = ledger_path(main_root, config)
    if not path.is_file():
        return []
    events: list[dict] = []
    for line_number, line in enumerate(_read_ledger_lines(path), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid ledger JSON at line {line_number}: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"ledger line {line_number} must contain a JSON object")
        events.append(event)
    return events


def _read_ledger_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return handle.read().splitlines()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_frontier_cache(
    main_root: Path, config: dict, target: str = DEFAULT_TARGET
) -> dict[str, Any]:
    """Read a target cache, with legacy fallback for claude-harness only."""
    target = validate_target(target)
    path = frontier_path(main_root, config, target)
    if path.is_file():
        source = path
    elif target == DEFAULT_TARGET and legacy_frontier_path(main_root, config).is_file():
        source = legacy_frontier_path(main_root, config)
    else:
        raise FileNotFoundError(f"frontier cache is missing: {path}")
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"frontier cache is invalid: {source}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"frontier cache must be an object: {source}")
    normalized = {**doc, "target": doc.get("target", DEFAULT_TARGET)}
    if normalized["target"] != target:
        raise ValueError(
            f"frontier cache target mismatch: expected {target!r}, got {normalized['target']!r}"
        )
    return normalized


def write_frontier_cache(
    main_root: Path,
    config: dict,
    frontier_doc: dict,
    target: str = DEFAULT_TARGET,
) -> None:
    """Write one target frontier cache atomically."""
    target = validate_target(target)
    doc_target = frontier_doc.get("target", target)
    if doc_target != target:
        raise ValueError(f"frontier cache target mismatch: expected {target!r}, got {doc_target!r}")
    frontier_doc = {**frontier_doc, "target": target}
    path = frontier_path(main_root, config, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(
        json.dumps(frontier_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# ledger 畳み込み（Sec1-2 状態畳み込み規則）
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = frozenset({"promoted", "retired"})


def fold_candidate_states(events: list[dict]) -> dict[str, dict]:
    """ledger イベント列を cand_id ごとの状態へ畳み込む（Sec1-2）。

    events は ledger.jsonl の追記順（時系列順）であることを前提とする（本関数は
    再ソートしない）。戻り値は cand_id をキーとし、各値は少なくとも
    `{"status": str | None, "warnings": list[str], "has_active_promotion_hold": bool}`
    を持つ dict。`has_active_promotion_hold` は未解放の `promotion_reserved` /
    `promotion_opened` を表し、`purge`（Sec12-3）の削除保護判定に使う。
    """
    states: dict[str, dict] = {}
    for event in events:
        _fold_one_event(states, event)
    return states


def _fold_one_event(states: dict[str, dict], event: dict) -> None:
    cand_id = event.get("cand_id")
    if not cand_id:
        return
    state = states.setdefault(
        cand_id, {"status": None, "warnings": [], "has_active_promotion_hold": False}
    )
    kind = event.get("event")
    if kind == "candidate_registered":
        state["status"] = "candidate"
    elif kind == "run_completed":
        _fold_run_completed(state)
    elif kind == "status_changed":
        state["status"] = event.get("to", state["status"])
    elif kind in ("promotion_reserved", "promotion_opened"):
        state["has_active_promotion_hold"] = True
    elif kind == "promotion_released":
        state["has_active_promotion_hold"] = False


def _fold_run_completed(state: dict) -> None:
    if state["status"] in TERMINAL_STATUSES:
        state["warnings"].append(
            "run_completed received after terminal status (unexpected re-evaluation)"
        )
        return
    if state["status"] in (None, "candidate"):
        state["status"] = "evaluated"


# ---------------------------------------------------------------------------
# スコアリングと Pareto frontier（Sec3-2, Sec3-5）
# ---------------------------------------------------------------------------


def quality_score(critical_pass_rate: float, penalty: float, config: dict) -> float:
    """quality_score = critical_pass_rate * critical_weight + max(0, penalty_base - penalty * penalty_per_item)（Sec3-2）。"""
    scoring = config.get("scoring") or {}
    critical_weight = scoring.get("critical_weight", DEFAULTS["scoring"]["critical_weight"])
    penalty_base = scoring.get("penalty_base", DEFAULTS["scoring"]["penalty_base"])
    penalty_per_item = scoring.get("penalty_per_item", DEFAULTS["scoring"]["penalty_per_item"])
    return critical_pass_rate * critical_weight + max(
        0.0, penalty_base - penalty * penalty_per_item
    )


def latest_non_holdout_run_completed(
    events: list[dict], target: str = DEFAULT_TARGET
) -> dict | None:
    """ledger イベント列から最新の non-holdout `run_completed` イベントを返す（Sec3-5 スコープ選定）。

    frontier 比較スコープの (suite_hash, evaluator_hash) 選定は、holdout run により
    scope が汚染されないよう、この関数で選ばれた non-holdout run を基準にする。
    呼び出し側（`aggregate_run_points` と `meta_harness.py` の hash メタデータ算出）は
    同じ選定結果を使い、points と表示メタデータの不整合を避けること。
    """
    target = validate_target(target)
    for event in reversed(events):
        if (
            event.get("event") == "run_completed"
            and event.get("target") == target
            and not event.get("holdout")
        ):
            return event
    return None


def latest_evaluation_completed(
    events: list[dict],
    cand_id: str,
    target: str,
    *,
    holdout: bool,
    suite_hash: str | None = None,
    evaluator_hash: str | None = None,
    evaluation_id: str | None = None,
) -> dict | None:
    """Return the latest completed evaluation batch matching one candidate scope."""
    for event in reversed(events):
        if (
            event.get("event") != "evaluation_completed"
            or event.get("cand_id") != cand_id
            or event.get("target") != target
            or bool(event.get("holdout")) != holdout
        ):
            continue
        if suite_hash is not None and event.get("own_suite_hash") != suite_hash:
            continue
        if evaluator_hash is not None and event.get("evaluator_hash") != evaluator_hash:
            continue
        if evaluation_id is not None and event.get("evaluation_id") != evaluation_id:
            continue
        return event
    return None


def candidate_has_evaluation_completed(events: list[dict], cand_id: str) -> bool:
    """Return True if this candidate has any evaluation_completed event in the ledger.

    Used to gate the strict `evaluation_completed`-based frontier/loop-resume checks
    introduced for cross-skill regression (b92dd84): candidates evaluated before that
    change never gain such an event retroactively, so callers fall back to the legacy
    attempt-completeness check for them instead of silently dropping them from
    frontier/loop resumption.
    """
    return any(
        event.get("event") == "evaluation_completed" and event.get("cand_id") == cand_id
        for event in events
    )


KNOWN_COST_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "tool_uses",
        "duration_ms",
        "total_cost_usd",
        "num_turns",
    }
)


def _validate_cost_axis(cost_axis: str) -> None:
    """config `frontier.cost_axis` を result/ledger の cost オブジェクトの既知フィールド
    allowlist に照らして検証する（PR #162 レビュー指摘）。

    typo（例: `totl_tokens`）を無効な値のまま通すと、全 run のコストが黙って 0 として
    扱われ Pareto frontier がコストを無視して計算されてしまう。ここで fail-closed する。
    """
    if cost_axis not in KNOWN_COST_FIELDS:
        raise MetaHarnessRootError(
            f"frontier.cost_axis must be one of {sorted(KNOWN_COST_FIELDS)}, got: {cost_axis!r}"
        )


def aggregate_run_points(
    events: list[dict], config: dict, target: str = DEFAULT_TARGET
) -> list[dict]:
    """run_completed イベントを cand_id ごとに集計し frontier 用の point を作る（Sec3-4, Sec3-5）。

    比較スコープは ledger 内で最新の **non-holdout** `run_completed` が観測された
    `(suite_hash, evaluator_hash)` の組に限定する（Sec3-5「frontier 比較のスコープ」）。
    holdout run が末尾にあっても non-holdout run のスコープを汚染しないよう、
    `latest_non_holdout_run_completed` で選定する。non-holdout run が 1 件も無ければ
    空リストを返す。

    候補の対象化は以下をすべて満たす場合のみ行う（PR #162 レビュー指摘を反映）:

    - スコープ内の non-holdout run を持つこと（holdout-only 候補は points に一切
      含めない。runs:0 の point は frontier.schema.json の `minimum: 1` に違反するため）。
    - ledger 畳み込み状態（`fold_candidate_states`）が `evaluated` であること。
      `retired` / `promoted` になった候補は畳み込み上の terminal 状態であり、
      Sec3-5 の「支配されない **evaluated** 候補」という frontier の定義から外れる。
      これらを points に残すと、purge（Sec12-3）の frontier 保護判定に誤って
      巻き込まれてしまう。

    各 point の `eligible` は以下の 2 条件がともに満たされる場合のみ True になる:

    1. スコープ内の non-holdout run が全て `verdict=pass`（Sec3-5 の基本要件）。
    2. スコープ内で観測された non-holdout `scenario_id` の全候補横断の**和集合**
       （「要求シナリオ集合」）を、この候補の最新 attempt 群が全てカバーしていること。

    【判断】2 の「要求シナリオ集合」は、`evaluate --scenario <id>` 等の部分評価で
    1 本だけ pass した候補が frontier に紛れ込むのを防ぐための近似規則である。
    設計書 Sec3-5 は「全 non-holdout シナリオで verdict=pass」とのみ規定し、
    シナリオスイートの完全な一覧（`scenario.schema.json` 由来）を ledger だけからは
    参照できないため、「同一比較スコープ内で実際に観測された scenario_id の和集合」を
    スイート全体の近似として採用する。suite_hash がスイート内容そのものを固定する
    （Sec3-5「hash 定義」）ため、同一 suite_hash 内で少なくとも 1 候補が実行した
    シナリオ集合は、そのスイートが持つシナリオ集合の下界（部分集合）になる。実務上は
    frontier 評価（`evaluate.repeat_frontier`）を経た候補が全シナリオを実行しているため、
    この和集合はスイート全体に一致するとみなせる。
    """
    cost_axis = (config.get("frontier") or {}).get("cost_axis", DEFAULTS["frontier"]["cost_axis"])
    _validate_cost_axis(cost_axis)
    target = validate_target(target)
    run_events = [
        e for e in events if e.get("event") == "run_completed" and e.get("target") == target
    ]
    if not run_events:
        return []
    latest_non_holdout = latest_non_holdout_run_completed(events, target)
    if latest_non_holdout is None:
        return []
    latest_pair = (latest_non_holdout.get("suite_hash"), latest_non_holdout.get("evaluator_hash"))
    # holdout run はここで完全に除外する（PR #162 レビュー指摘: holdout-only 候補が
    # runs:0 の point として points に混入するのを防ぐ）。holdout run は元々
    # `_summarize_candidate_runs` 内でも除外されていたため、集計結果への影響はない。
    matching = [
        e
        for e in run_events
        if not e.get("holdout") and (e.get("suite_hash"), e.get("evaluator_hash")) == latest_pair
    ]
    required_scenarios = frozenset(e.get("scenario_id") for e in matching)

    states = fold_candidate_states(events)
    by_cand: dict[str, list[dict]] = {}
    for event in matching:
        cand_id = event["cand_id"]
        if states.get(cand_id, {}).get("status") != "evaluated":
            continue
        by_cand.setdefault(cand_id, []).append(event)
    points: list[dict] = []
    for cand_id, runs in sorted(by_cand.items()):
        latest_runs = _latest_attempt_groups_per_scenario(runs)
        point = _summarize_candidate_runs(cand_id, latest_runs, cost_axis, required_scenarios)
        # No legacy-ledger fallback here (unlike `loop_state.current_run_events`): a candidate
        # whose own run(s) completed but has no evaluation_completed event is exactly the
        # "batch interrupted after own run" state that EV-54 requires frontier to exclude
        # (see test_incomplete_batch_with_only_own_pass_is_not_frontier_eligible). Ledger
        # content alone cannot distinguish that state from a genuinely pre-b92dd84 legacy
        # candidate, so relaxing this check would reopen the reward-hacking gap the strict
        # evaluation_completed gate was added to close. Legacy candidates regain frontier
        # eligibility once `evaluate` is re-run for them (see docs/evaluation/meta-harness.md).
        evaluation = latest_evaluation_completed(
            events,
            cand_id,
            target,
            holdout=False,
            suite_hash=str(latest_pair[0]),
            evaluator_hash=str(latest_pair[1]),
        )
        run_ids = {str(run.get("run_id")) for run in latest_runs}
        evaluation_run_ids = {str(run_id) for run_id in (evaluation or {}).get("own_run_ids") or []}
        point["eligible"] = bool(
            point["eligible"]
            and evaluation is not None
            and evaluation.get("verdict") == "pass"
            and run_ids == evaluation_run_ids
        )
        points.append(point)
    return points


def _latest_attempt_groups_per_scenario(runs: list[dict]) -> list[dict]:
    """cand_id 内の run 群を scenario_id（かつ holdout 区分）ごとに絞り込み、各シナリオの
    最新 attempt 群（Sec3-4）のみを残す。`runs` は ledger 出現順（追記順）であることを前提とする。

    attempt == 1 のたびに新しい試行グループを開始する。`attempt` フィールドが欠落した run は
    単独グループとして扱う。グループ化のキーは `(scenario_id, holdout)` とする。holdout
    シナリオは物理的に別トラック（Sec3-6）であり、attempt 採番も non-holdout run とは独立
    のため、シナリオ ID だけで束ねると holdout run が non-holdout run の attempt グループを
    上書きしてしまう（逆も同様）。シナリオ（かつ holdout 区分）ごとに最後のグループのみを
    集計対象として残す。
    """
    groups_by_key: dict[Any, list[list[dict]]] = {}
    for run in runs:
        key = (run.get("scenario_id"), bool(run.get("holdout")))
        groups = groups_by_key.setdefault(key, [])
        attempt = run.get("attempt")
        starts_new_group = attempt is None or attempt == 1 or not groups
        if starts_new_group:
            groups.append([run])
        else:
            groups[-1].append(run)

    latest_runs: list[dict] = []
    for groups in groups_by_key.values():
        latest_runs.extend(groups[-1])
    return latest_runs


def _summarize_candidate_runs(
    cand_id: str, runs: list[dict], cost_axis: str, required_scenarios: frozenset[str]
) -> dict:
    """cand_id 単位で run 群を集計し frontier point を作る。

    `runs` は呼び出し元（`aggregate_run_points`）の時点で既に holdout run と
    ledger 畳み込み terminal 状態の候補を除外済みである（PR #162 レビュー指摘）。
    ここでの `non_holdout_runs` フィルタは冗長防御として維持する。
    """
    non_holdout_runs = [r for r in runs if not r.get("holdout")]
    qualities = [r["quality_score"] for r in non_holdout_runs]
    costs = [_run_cost(r, cost_axis) for r in non_holdout_runs]
    non_holdout_pass = bool(non_holdout_runs) and all(
        r.get("verdict") == "pass" for r in non_holdout_runs
    )
    covered_scenarios = frozenset(r.get("scenario_id") for r in non_holdout_runs)
    scenario_coverage_ok = required_scenarios <= covered_scenarios
    eligible = non_holdout_pass and scenario_coverage_ok
    if not non_holdout_runs:
        return {
            "cand_id": cand_id,
            "quality_mean": 0.0,
            "quality_var": 0.0,
            "quality_min": 0.0,
            "cost_mean": 0.0,
            "runs": 0,
            "eligible": False,
        }
    mean_quality = sum(qualities) / len(qualities)
    variance = sum((q - mean_quality) ** 2 for q in qualities) / len(qualities)
    return {
        "cand_id": cand_id,
        "quality_mean": mean_quality,
        "quality_var": variance,
        "quality_min": min(qualities),
        "cost_mean": sum(costs) / len(costs),
        "runs": len(non_holdout_runs),
        "eligible": eligible,
    }


def _run_cost(run: dict, cost_axis: str) -> float:
    """run の cost オブジェクトから `cost_axis` フィールドを取り出す。

    欠落している場合は黙って 0 にせず、run_id を含むエラーを送出する
    （PR #162 レビュー指摘: cost_axis の typo/不整合を隠蔽しない）。
    """
    cost_obj = run.get("cost") or {}
    if cost_axis not in cost_obj:
        raise MetaHarnessRootError(
            f"run {run.get('run_id')!r} is missing cost field {cost_axis!r}"
            " required by frontier.cost_axis"
        )
    return cost_obj[cost_axis]


def compute_pareto_frontier(points: list[dict]) -> tuple[list[str], list[str]]:
    """Sec3-5: quality_mean 最大化 x cost_mean 最小化の非支配集合を返す。

    呼び出し側は `eligible`（全 non-holdout シナリオで verdict=pass）な point
    のみを渡すこと（このフィルタリングは本関数の責務外、呼び出し側の前提条件）。
    同率タイブレークは quality_min の高い方を優先する。戻り値は
    `(frontier_cand_ids, dominated_cand_ids)`。
    """
    frontier: list[str] = []
    dominated: list[str] = []
    for candidate in points:
        if _is_dominated(candidate, points):
            dominated.append(candidate["cand_id"])
        else:
            frontier.append(candidate["cand_id"])
    return frontier, dominated


def _is_dominated(candidate: dict, points: list[dict]) -> bool:
    return any(
        other["cand_id"] != candidate["cand_id"] and _dominates(other, candidate)
        for other in points
    )


def _dominates(a: dict, b: dict) -> bool:
    """a が b を支配するか（Sec3-5、quality_min タイブレーク込み）。"""
    quality_ge = a["quality_mean"] >= b["quality_mean"]
    cost_le = a["cost_mean"] <= b["cost_mean"]
    if not (quality_ge and cost_le):
        return False
    if a["quality_mean"] > b["quality_mean"] or a["cost_mean"] < b["cost_mean"]:
        return True
    return a["quality_min"] > b["quality_min"]


# ---------------------------------------------------------------------------
# 最小限の JSON Schema 検証器（依存追加なし。Sec1 の 10 スキーマ向け実用サブセット）
# ---------------------------------------------------------------------------

_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def load_schema(schema_dir: Path, name: str) -> dict:
    """schemas/<name> を読み込む。"""
    return json.loads((schema_dir / name).read_text(encoding="utf-8"))


def validate_against_schema(instance: Any, schema: dict, schema_dir: Path) -> list[str]:
    """`instance` を `schema` に対して検証し、エラー文字列のリストを返す（空 = valid）。

    `type`/`required`/`enum`/`const`/`pattern`/`minimum`/`maximum`/`minItems`/
    `items`/`properties`/`additionalProperties: false`/`oneOf`/`$ref`（同一文書内
    `#/$defs/...` および他ファイル `other.schema.json#/$defs/...`）のみをサポート
    する実用サブセット。完全な JSON Schema 準拠は目的としない（`allOf`/`if`/`then`/
    `format`/`propertyNames` 等は無視され、ブロックしない）。
    """
    cache: dict[str, dict] = {}
    return _validate_node(instance, schema, "$", schema, schema_dir, cache)


def _load_schema_file(schema_dir: Path, filename: str, cache: dict[str, dict]) -> dict:
    if filename not in cache:
        cache[filename] = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
    return cache[filename]


def _resolve_ref(ref: str, root_schema: dict, schema_dir: Path, cache: dict[str, dict]) -> dict:
    file_part, _, pointer = ref.partition("#")
    doc = root_schema if not file_part else _load_schema_file(schema_dir, file_part, cache)
    node = doc
    for part in pointer.strip("/").split("/"):
        if part:
            node = node[part]
    return node


def _check_type(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expected = _JSON_TYPE_MAP.get(type_name)
    return True if expected is None else isinstance(value, expected)


def _validate_node(
    instance: Any,
    schema: dict,
    path: str,
    root_schema: dict,
    schema_dir: Path,
    cache: dict[str, dict],
) -> list[str]:
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root_schema, schema_dir, cache)
        return _validate_node(instance, resolved, path, root_schema, schema_dir, cache)

    errors: list[str] = []
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")

    type_spec = schema.get("type")
    if type_spec is not None:
        types = type_spec if isinstance(type_spec, list) else [type_spec]
        if not any(_check_type(instance, t) for t in types):
            errors.append(f"{path}: expected type {type_spec!r}, got {type(instance).__name__}")
            return errors

    errors.extend(_validate_constraints(instance, schema, path))
    if isinstance(instance, list):
        errors.extend(_validate_array(instance, schema, path, root_schema, schema_dir, cache))
    if isinstance(instance, dict):
        errors.extend(_validate_object(instance, schema, path, root_schema, schema_dir, cache))
    # 【判断】"oneOf" は他の object キーワード（type/required/additionalProperties 等）と
    # 併存しうる（例: ledger.event.schema.json の status_changed def は "type": "object" +
    # "required"/"properties"/"additionalProperties" と "oneOf"（from/to の許容遷移）を同居させて
    # いる）。以前の実装は "oneOf" があると他キーワードの検証を完全にスキップしており、
    # additionalProperties: false や required 違反が status_changed イベントで検出されない
    # バグがあった。JSON Schema の意味論どおり、oneOf は他キーワードと**併せて**評価する。
    if "oneOf" in schema:
        errors.extend(
            _validate_one_of(instance, schema["oneOf"], path, root_schema, schema_dir, cache)
        )
    return errors


def _validate_constraints(instance: Any, schema: dict, path: str) -> list[str]:
    errors: list[str] = []
    if (
        isinstance(instance, str)
        and "pattern" in schema
        and re.search(schema["pattern"], instance) is None
    ):
        errors.append(f"{path}: {instance!r} does not match pattern {schema['pattern']!r}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")
    return errors


def _validate_one_of(
    instance: Any,
    branches: list[dict],
    path: str,
    root_schema: dict,
    schema_dir: Path,
    cache: dict[str, dict],
) -> list[str]:
    valid_count = 0
    for branch in branches:
        if not _validate_node(instance, branch, path, root_schema, schema_dir, cache):
            valid_count += 1
    if valid_count == 1:
        return []
    if valid_count == 0:
        return [f"{path}: no oneOf branch matched"]
    return [f"{path}: {valid_count} oneOf branches matched (expected exactly 1)"]


def _validate_array(
    instance: list,
    schema: dict,
    path: str,
    root_schema: dict,
    schema_dir: Path,
    cache: dict[str, dict],
) -> list[str]:
    errors: list[str] = []
    min_items = schema.get("minItems")
    if min_items is not None and len(instance) < min_items:
        errors.append(f"{path}: array has {len(instance)} items, expected >= {min_items}")
    item_schema = schema.get("items")
    if item_schema is not None:
        for idx, item in enumerate(instance):
            errors.extend(
                _validate_node(item, item_schema, f"{path}[{idx}]", root_schema, schema_dir, cache)
            )
    return errors


def _validate_object(
    instance: dict,
    schema: dict,
    path: str,
    root_schema: dict,
    schema_dir: Path,
    cache: dict[str, dict],
) -> list[str]:
    errors: list[str] = []
    for key in schema.get("required", []):
        if key not in instance:
            errors.append(f"{path}: missing required key '{key}'")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for key in instance:
            if key not in properties:
                errors.append(f"{path}: unexpected key '{key}' (additionalProperties: false)")
    for key, sub_schema in properties.items():
        if key in instance:
            errors.extend(
                _validate_node(
                    instance[key], sub_schema, f"{path}.{key}", root_schema, schema_dir, cache
                )
            )
    return errors


# ---------------------------------------------------------------------------
# overlay / config-patch 検証（Sec1-7, Sec1-8）
# ---------------------------------------------------------------------------


def validate_overlay(
    overlay_dir: Path,
    config: dict,
    *,
    target: str,
    baseline_root: Path | None = None,
    inherited_overlay_dir: Path | None = None,
    skill_allowed_paths: frozenset[str] | None = None,
) -> list[str]:
    """overlay ディレクトリを安全制約（Sec1-7）に照らして検証する。

    `overlay/config-patch.json`（存在する場合のみ）は overlay コンテンツではなく
    `facets/**` prefix ルールの対象外の予約ファイルであるため、ここではスキップ
    する（Sec1-8）。その内容自体の検証は `validate_config_patch` が別途担う。
    """
    if not overlay_dir.is_dir():
        return [f"overlay directory does not exist: {overlay_dir}"]
    try:
        validate_target(target)
    except ValueError as exc:
        return [str(exc)]
    overlay_cfg = config.get("overlay") or {}
    allowed_prefixes = tuple(
        overlay_cfg.get("allowed_prefixes") or DEFAULTS["overlay"]["allowed_prefixes"]
    )
    denied_prefixes = tuple(
        overlay_cfg.get("denied_prefixes") or DEFAULTS["overlay"]["denied_prefixes"]
    )

    if target.startswith("skill:"):
        if skill_allowed_paths is None:
            if baseline_root is None:
                return ["baseline_root is required for skill target overlay validation"]
            try:
                import skill_targets

                resolution = skill_targets.allowed_overlay_paths(baseline_root, target, config)
                skill_allowed_paths = skill_targets.overlay_allowlist(resolution, config)
            except (OSError, ValueError) as exc:
                return [str(exc)]

    inherited_files: dict[str, Path] = {}
    if inherited_overlay_dir is not None:
        if not inherited_overlay_dir.is_dir():
            return [f"inherited overlay directory does not exist: {inherited_overlay_dir}"]
        inherited_files = {
            rel: inherited_overlay_dir / rel for rel in list_overlay_files(inherited_overlay_dir)
        }

    errors: list[str] = []
    current_files: set[str] = set()
    for entry in sorted(overlay_dir.rglob("*")):
        rel = entry.relative_to(overlay_dir).as_posix()
        if entry.is_symlink():
            errors.append(f"{rel}: symlinks are not allowed")
            continue
        if rel == CONFIG_PATCH_FILENAME:
            continue
        if entry.is_dir():
            continue
        current_files.add(rel)
        errors.extend(_validate_overlay_file(entry, overlay_dir, allowed_prefixes, denied_prefixes))
        inherited = inherited_files.get(rel)
        # `inherited_overlay_dir` is an internal trust input: callers may pass only an
        # immutable registered-candidate overlay whose manifest/hash was revalidated.
        # Equality bypasses the current generation's category gate only for that exact
        # inherited content; a changed byte is checked against `skill_allowed_paths`.
        unchanged_inherited = (
            inherited is not None
            and inherited.is_file()
            and not inherited.is_symlink()
            and entry.read_bytes() == inherited.read_bytes()
        )
        if (
            skill_allowed_paths is not None
            and not unchanged_inherited
            and rel not in skill_allowed_paths
        ):
            errors.append(f"{rel}: outside private facet closure for {target}")
    missing_inherited = sorted(set(inherited_files) - current_files)
    if missing_inherited:
        errors.append(
            "candidate overlay is missing inherited files: " + ", ".join(missing_inherited[:5])
        )
    return errors


def _validate_overlay_file(
    entry: Path,
    overlay_dir: Path,
    allowed_prefixes: tuple[str, ...],
    denied_prefixes: tuple[str, ...],
) -> list[str]:
    rel = entry.relative_to(overlay_dir).as_posix()
    errors: list[str] = []
    if rel.startswith("/"):
        errors.append(f"{rel}: absolute paths are not allowed")
    if ".." in rel.split("/"):
        errors.append(f"{rel}: '..' path segments are not allowed")
    if not rel.startswith(allowed_prefixes):
        errors.append(f"{rel}: outside allowed prefixes {allowed_prefixes}")
    if denied_prefixes and rel.startswith(denied_prefixes):
        errors.append(f"{rel}: matches a denied prefix {denied_prefixes}")
    return errors


# Phase 1a では config-patch.json を config.config_patch.allowlist の値に関わらず
# 常に全面拒否する（Sec1-8）。Phase 2 でこの定数を True に切り替えると、下の allowlist
# 検証ロジックが有効になる（allowlist が空なら拒否、非空なら許可）。
CONFIG_PATCH_ENABLED = False


def validate_config_patch(config_patch: list, config: dict, schema_dir: Path) -> list[str]:
    """config-patch.json の形状検証 + Phase 1a 全面拒否（Sec1-8）。"""
    schema = load_schema(schema_dir, "config_patch.schema.json")
    errors = validate_against_schema(config_patch, schema, schema_dir)
    if errors:
        return errors
    if not config_patch:
        return []
    if not CONFIG_PATCH_ENABLED:
        return [
            "config_patch is rejected in Phase 1a (CONFIG_PATCH_ENABLED=False);"
            " overlays must not include a config-patch.json"
        ]
    allowlist = (config.get("config_patch") or {}).get("allowlist") or []
    if not allowlist:
        return [
            "config_patch is rejected: config_patch.allowlist is empty;"
            " overlays must not include a config-patch.json"
        ]
    return []


# ---------------------------------------------------------------------------
# 排他制御（Sec2-3。store.lock は Phase 1a、evaluate.lock は Phase 1b で追加）
# ---------------------------------------------------------------------------


class LockAcquisitionError(RuntimeError):
    """store.lock / evaluate.lock が取得できない場合に送出する（CLI は exit 3）。"""


_LOCK_ACQUIRE_ATTEMPTS = 2


@contextmanager
def store_lock(main_root: Path, config: dict):
    """store.lock を取得するコンテキストマネージャ（短期 TTL、Sec2-3）。"""
    lock_file = locks_dir(main_root, config) / "store.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    ttl_seconds = (config.get("locks") or {}).get(
        "store_ttl_seconds", DEFAULTS["locks"]["store_ttl_seconds"]
    )
    token = _acquire_store_lock(lock_file, ttl_seconds)
    try:
        yield
    finally:
        try:
            if lock_file.read_text(encoding="utf-8") == token:
                lock_file.unlink(missing_ok=True)
        except (FileNotFoundError, OSError):
            pass


def _read_lock_snapshot(lock_file: Path) -> tuple[str, float] | None:
    """lock ファイルの内容 + mtime を返す。読めなければ None（既に消えている等）。"""
    try:
        content = lock_file.read_text(encoding="utf-8")
        mtime = lock_file.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None
    return content, mtime


def _unlink_if_unchanged(lock_file: Path, expected: tuple[str, float]) -> None:
    """stale と判定した lock を compare-before-unlink で奪取する。

    stale 判定時に読んだ token 内容 + mtime（`expected`）を、unlink 直前に再読して一致する
    場合のみ unlink する。不一致（= 別プロセスが既に unlink + 再作成 済み）なら奪取を
    スキップし、呼び出し側の再試行ループに委ねる。

    既知の制約: 再読と unlink の間には極小の TOCTOU ウィンドウが残る（existence-based lock
    方式の原理的な限界であり、完全な原子性は得られない）。store.lock 解放時の
    compare-and-delete（`store_lock` の finally 節）と対になる、奪取側の対策として設けている。
    """
    current = _read_lock_snapshot(lock_file)
    if current is None or current != expected:
        return
    try:
        lock_file.unlink()
    except FileNotFoundError:
        pass


def _acquire_store_lock(lock_file: Path, ttl_seconds: float) -> str:
    for _ in range(_LOCK_ACQUIRE_ATTEMPTS):
        token = f"{os.getpid()}:{time.time_ns()}:{os.urandom(4).hex()}"
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # 【判断】PR #162 レビュー指摘 (FIX P1): staleness 判定と snapshot 取得を
            # 別々のタイミングで行うと（旧実装: `_is_lock_stale()` で mtime を stat →
            # 別途 `_read_lock_snapshot()` で内容+mtime を再取得）、この 2 回の読み取りの
            # 間に別プロセスが lock を unlink + 再作成すると、stale と判定したのは古い
            # lock なのに、実際に compare-and-delete するのは（2 回目の読み取りで得た）
            # 新しい fresh な lock になってしまう。fresh lock は当然「自分自身の
            # snapshot と一致」するため無条件に削除されてしまう（stale 判定を回避した
            # fresh lock が奪取される）。
            # 修正: snapshot（内容 + mtime）を 1 回だけ取得し、その snapshot 自身の mtime
            # を根拠に staleness を判定する。判定に使った snapshot をそのまま
            # `_unlink_if_unchanged` に渡すことで、判定対象と削除対象を常に同一の
            # 読み取り結果に固定する（TOCTOU ウィンドウをこの関数内では作らない）。
            snapshot = _read_lock_snapshot(lock_file)
            if snapshot is None:
                # 既に別プロセスが unlink 済み（lock 消滅）。次のループで再取得を試みる。
                continue
            if not _is_snapshot_stale(snapshot, ttl_seconds):
                raise LockAcquisitionError(
                    f"store.lock is held by another process: {lock_file}"
                ) from None
            _unlink_if_unchanged(lock_file, snapshot)
            continue
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(token)
            return token
    raise LockAcquisitionError(
        f"could not acquire store.lock after {_LOCK_ACQUIRE_ATTEMPTS} attempts: {lock_file}"
    )


def _is_lock_stale(lock_file: Path, ttl_seconds: float) -> bool:
    """lock ファイルを直接 stat して staleness を判定する（既存挙動、単体テスト用に維持）。

    `_acquire_store_lock` の奪取判定では、この関数ではなく `_is_snapshot_stale` を使う
    （snapshot-first の原則、PR #162 レビュー指摘）。
    """
    try:
        age_seconds = time.time() - lock_file.stat().st_mtime
    except FileNotFoundError:
        return True
    return age_seconds > ttl_seconds


def _is_snapshot_stale(snapshot: tuple[str, float], ttl_seconds: float) -> bool:
    """既に取得済みの lock snapshot（内容 + mtime）の mtime を根拠に staleness を判定する。

    `_acquire_store_lock` は staleness 判定と compare-and-delete の両方に同一の
    snapshot を使うことで、判定対象と削除対象がすり替わる TOCTOU を避ける
    （PR #162 レビュー指摘、FIX P1）。
    """
    _, mtime = snapshot
    return time.time() - mtime > ttl_seconds


# ---------------------------------------------------------------------------
# evaluate.lock（Sec2-3。`evaluate` コマンド全体を通して保持する PID + heartbeat 方式の長期 lock）
# ---------------------------------------------------------------------------


@contextmanager
def evaluate_lock(main_root: Path, config: dict):
    """`evaluate` コマンド全体を通して保持する長期 singleton lock（Sec2-3）。

    固定 TTL ではなく heartbeat（既定 60 秒ごとに mtime 更新）+ staleness 閾値（既定 300 秒、
    heartbeat が途絶えたプロセスクラッシュ時のみ奪取可）方式を採る。実行時間の長い evaluate が
    固定 TTL 到達で誤って lock を奪われることを防ぐ。
    """
    lock_file = locks_dir(main_root, config) / "evaluate.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    locks_cfg = config.get("locks") or {}
    stale_seconds = locks_cfg.get(
        "evaluate_stale_seconds", DEFAULTS["locks"]["evaluate_stale_seconds"]
    )
    heartbeat_seconds = locks_cfg.get(
        "evaluate_heartbeat_seconds", DEFAULTS["locks"]["evaluate_heartbeat_seconds"]
    )
    token = _acquire_evaluate_lock(lock_file, stale_seconds)
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_evaluate_lock_heartbeat_loop,
        args=(lock_file, heartbeat_seconds, stop_event),
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        yield
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=heartbeat_seconds + 5)
        _release_evaluate_lock(lock_file, token)


def _acquire_evaluate_lock(lock_file: Path, stale_seconds: float) -> str:
    """store_lock と同じ snapshot-first 奪取ロジックを stale 閾値違いで再利用する。"""
    for _ in range(_LOCK_ACQUIRE_ATTEMPTS):
        token = f"{os.getpid()}:{time.time_ns()}:{os.urandom(4).hex()}"
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            snapshot = _read_lock_snapshot(lock_file)
            if snapshot is None:
                continue
            if not _is_snapshot_stale(snapshot, stale_seconds):
                raise LockAcquisitionError(
                    f"evaluate.lock is held by another process: {lock_file}"
                ) from None
            _unlink_if_unchanged(lock_file, snapshot)
            continue
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(token)
            return token
    raise LockAcquisitionError(
        f"could not acquire evaluate.lock after {_LOCK_ACQUIRE_ATTEMPTS} attempts: {lock_file}"
    )


def _release_evaluate_lock(lock_file: Path, token: str) -> None:
    try:
        if lock_file.read_text(encoding="utf-8") == token:
            lock_file.unlink(missing_ok=True)
    except (FileNotFoundError, OSError):
        pass


def _evaluate_lock_heartbeat_loop(
    lock_file: Path, heartbeat_seconds: float, stop_event: threading.Event
) -> None:
    """`stop_event` がセットされるまで `heartbeat_seconds` ごとに lock の mtime を更新する。"""
    while not stop_event.wait(heartbeat_seconds):
        _touch_evaluate_lock(lock_file)


def _touch_evaluate_lock(lock_file: Path) -> None:
    """lock ファイルの mtime を現在時刻に更新する（heartbeat 1 回分、単体テストからも直接呼べる）。"""
    try:
        os.utime(lock_file, None)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# ID 採番（Sec1-1, Sec2-4）
# ---------------------------------------------------------------------------

CAND_ID_PATTERN = re.compile(r"^cand-[0-9]{8}-[0-9]{6}-[a-z0-9-]+$")
_FALLBACK_SLUG = "manual"


def slugify(text: str) -> str:
    """任意テキストを cand_id 用の kebab-case slug に正規化する。"""
    lowered = (text or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or _FALLBACK_SLUG


def generate_cand_id(slug: str, now: datetime | None = None) -> str:
    """`cand-<yyyymmdd>-<hhmmss>-<slug>-<nonce>` 形式の cand_id を生成する。

    同一秒・同一 slug（既定 "manual"）でも衝突しないよう、4 桁 hex の nonce
    （`os.urandom(2)` 由来）を付与する。nonce は `CAND_ID_PATTERN`（末尾 `[a-z0-9-]+`）の
    範囲内に収まるため、schema / パターンの変更は不要。
    """
    moment = now or datetime.now()
    nonce = os.urandom(2).hex()
    return f"cand-{moment:%Y%m%d}-{moment:%H%M%S}-{slugify(slug)}-{nonce}"
