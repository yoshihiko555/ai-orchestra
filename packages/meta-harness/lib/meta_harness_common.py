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

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

PACKAGE_NAME = "meta-harness"
CONFIG_FILENAME = "meta-harness.yaml"

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
            "Bash(pytest *)",
        ],
        "model": None,
        "cli_version_pin": None,
    },
    "scenario_run": {"max_turns_default": 30, "max_budget_usd_default": 2.0},
    "judge": {"model": None, "effort": "high", "max_turns": 4},
    "scoring": {
        "critical_weight": 70,
        "penalty_base": 30,
        "penalty_per_item": 5,
        "penalty_missing_report": 6,
    },
    "frontier": {"cost_axis": "total_tokens"},
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
        "max_iterations": 10,
        "divergence_rounds": 3,
        "overfit_drop_pt": 15,
        "budget_usd_per_iteration": 1.0,
        "max_turns": 40,
        "max_focus_runs": 5,
        "max_overlay_bytes": 200000,
        "model": None,
        "effort": "high",
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
    使えない場合は DEFAULTS のみを返す。
    """
    try:
        orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        core_hooks = os.path.join(orchestra_dir, "packages", "core", "hooks")
        if os.path.isabs(core_hooks) and os.path.isdir(core_hooks) and core_hooks not in sys.path:
            sys.path.insert(0, core_hooks)
        from hook_common import load_package_config

        loaded = load_package_config(PACKAGE_NAME, CONFIG_FILENAME, str(project_dir))
    except Exception:
        loaded = {}
    return _deep_merge(DEFAULTS, loaded or {})


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


# ---------------------------------------------------------------------------
# store パス解決
# ---------------------------------------------------------------------------

STORE_SUBDIRS = ("candidates", "runs", "locks", "tmp", "rejected", "reports")

# overlay/config-patch.json（存在する場合のみ）は overlay の facets/** コンテンツではなく
# Sec1-8 の予約サイドカーファイル。overlay_files 一覧や facets/ prefix 検証の対象外にする。
CONFIG_PATCH_FILENAME = "config-patch.json"


def store_dir(main_root: Path, config: dict) -> Path:
    """store ルート（既定 `.claude/meta-harness`）の絶対パスを返す。"""
    rel = (config.get("storage") or {}).get("dir") or DEFAULTS["storage"]["dir"]
    return main_root / rel


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


def frontier_path(main_root: Path, config: dict) -> Path:
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

    if not frontier_path(main_root, config).exists():
        write_frontier_cache(main_root, config, _empty_frontier_doc(config))


def _empty_frontier_doc(config: dict) -> dict:
    """runs が 1 件も無い状態の frontier.json スタブ（Sec1-5）。"""
    zero_hash = "0" * 64
    return {
        "schema_version": "1.0",
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
) -> Path:
    """candidates/<cand_id>/ を immutable に配置する。

    既に同名の候補ディレクトリが存在する場合は `FileExistsError` を送出する
    （immutability 原則、Sec1-1「基本設計からの変更点」参照）。
    """
    cand_dir = candidates_dir(main_root, config) / cand_id
    if cand_dir.exists():
        raise FileExistsError(f"candidate already registered (immutable): {cand_id}")
    cand_dir.mkdir(parents=True)
    _copy_overlay_tree(overlay_dir, cand_dir / "overlay")
    _write_json(cand_dir / "manifest.json", manifest)
    _write_json(
        cand_dir / "overlay-manifest.json", {"schema_version": "1.0", "files": overlay_files}
    )
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
    """ledger.jsonl に 1 行追記する（O_APPEND + 単一 write + fsync、Sec2-3）。"""
    path = ledger_path(main_root, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_ledger_events(main_root: Path, config: dict) -> list[dict]:
    """ledger.jsonl の全イベントを時系列順に読む（不正な行は無視する）。"""
    path = ledger_path(main_root, config)
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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


def write_frontier_cache(main_root: Path, config: dict, frontier_doc: dict) -> None:
    """frontier.json を atomic write する（tmp file + os.replace、Sec2-3）。"""
    path = frontier_path(main_root, config)
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


def aggregate_run_points(events: list[dict], config: dict) -> list[dict]:
    """run_completed イベントを cand_id ごとに集計し frontier 用の point を作る（Sec3-4, Sec3-5）。

    比較スコープは ledger 内で最後に観測された `(suite_hash, evaluator_hash)` の
    組に限定する（Sec3-5「frontier 比較のスコープ」）。各 point には `eligible`
    （全 non-holdout run が `verdict=pass` かどうか）を含める。
    """
    cost_axis = (config.get("frontier") or {}).get("cost_axis", DEFAULTS["frontier"]["cost_axis"])
    run_events = [e for e in events if e.get("event") == "run_completed"]
    if not run_events:
        return []
    latest_pair = (run_events[-1].get("suite_hash"), run_events[-1].get("evaluator_hash"))
    matching = [
        e for e in run_events if (e.get("suite_hash"), e.get("evaluator_hash")) == latest_pair
    ]

    by_cand: dict[str, list[dict]] = {}
    for event in matching:
        by_cand.setdefault(event["cand_id"], []).append(event)
    return [
        _summarize_candidate_runs(cand_id, runs, cost_axis)
        for cand_id, runs in sorted(by_cand.items())
    ]


def _summarize_candidate_runs(cand_id: str, runs: list[dict], cost_axis: str) -> dict:
    qualities = [r["quality_score"] for r in runs]
    costs = [r.get("cost", {}).get(cost_axis, 0) for r in runs]
    non_holdout_pass = all(r.get("verdict") == "pass" for r in runs if not r.get("holdout"))
    mean_quality = sum(qualities) / len(qualities)
    variance = sum((q - mean_quality) ** 2 for q in qualities) / len(qualities)
    return {
        "cand_id": cand_id,
        "quality_mean": mean_quality,
        "quality_var": variance,
        "quality_min": min(qualities),
        "cost_mean": sum(costs) / len(costs),
        "runs": len(runs),
        "eligible": non_holdout_pass,
    }


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


def validate_overlay(overlay_dir: Path, config: dict) -> list[str]:
    """overlay ディレクトリを安全制約（Sec1-7）に照らして検証する。

    `overlay/config-patch.json`（存在する場合のみ）は overlay コンテンツではなく
    `facets/**` prefix ルールの対象外の予約ファイルであるため、ここではスキップ
    する（Sec1-8）。その内容自体の検証は `validate_config_patch` が別途担う。
    """
    if not overlay_dir.is_dir():
        return [f"overlay directory does not exist: {overlay_dir}"]
    overlay_cfg = config.get("overlay") or {}
    allowed_prefixes = tuple(
        overlay_cfg.get("allowed_prefixes") or DEFAULTS["overlay"]["allowed_prefixes"]
    )
    denied_prefixes = tuple(
        overlay_cfg.get("denied_prefixes") or DEFAULTS["overlay"]["denied_prefixes"]
    )

    errors: list[str] = []
    for entry in sorted(overlay_dir.rglob("*")):
        rel = entry.relative_to(overlay_dir).as_posix()
        if rel == CONFIG_PATCH_FILENAME:
            continue
        if entry.is_symlink():
            errors.append(f"{rel}: symlinks are not allowed")
            continue
        if entry.is_dir():
            continue
        errors.extend(_validate_overlay_file(entry, overlay_dir, allowed_prefixes, denied_prefixes))
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


def validate_config_patch(config_patch: list, config: dict, schema_dir: Path) -> list[str]:
    """config-patch.json の形状検証 + Phase 1a 全面拒否（Sec1-8）。"""
    schema = load_schema(schema_dir, "config_patch.schema.json")
    errors = validate_against_schema(config_patch, schema, schema_dir)
    if errors:
        return errors
    allowlist = (config.get("config_patch") or {}).get("allowlist") or []
    if config_patch and not allowlist:
        return [
            "config_patch is rejected in Phase 1a (config_patch.allowlist is always empty);"
            " overlays must not include a config-patch.json"
        ]
    return []


# ---------------------------------------------------------------------------
# 排他制御（Sec2-3。Phase 1a は store.lock のみ）
# ---------------------------------------------------------------------------


class LockAcquisitionError(RuntimeError):
    """store.lock が取得できない場合に送出する（CLI は exit 3）。"""


_LOCK_ACQUIRE_ATTEMPTS = 2


@contextmanager
def store_lock(main_root: Path, config: dict):
    """store.lock を取得するコンテキストマネージャ（短期 TTL、Sec2-3）。"""
    lock_file = locks_dir(main_root, config) / "store.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    ttl_seconds = (config.get("locks") or {}).get(
        "store_ttl_seconds", DEFAULTS["locks"]["store_ttl_seconds"]
    )
    _acquire_store_lock(lock_file, ttl_seconds)
    try:
        yield
    finally:
        lock_file.unlink(missing_ok=True)


def _acquire_store_lock(lock_file: Path, ttl_seconds: float) -> None:
    for _ in range(_LOCK_ACQUIRE_ATTEMPTS):
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _is_lock_stale(lock_file, ttl_seconds):
                lock_file.unlink(missing_ok=True)
                continue
            raise LockAcquisitionError(
                f"store.lock is held by another process: {lock_file}"
            ) from None
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            return
    raise LockAcquisitionError(
        f"could not acquire store.lock after {_LOCK_ACQUIRE_ATTEMPTS} attempts: {lock_file}"
    )


def _is_lock_stale(lock_file: Path, ttl_seconds: float) -> bool:
    try:
        age_seconds = time.time() - lock_file.stat().st_mtime
    except FileNotFoundError:
        return True
    return age_seconds > ttl_seconds


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
    """`cand-<yyyymmdd>-<hhmmss>-<slug>` 形式の cand_id を生成する。"""
    moment = now or datetime.now()
    return f"cand-{moment:%Y%m%d}-{moment:%H%M%S}-{slugify(slug)}"
