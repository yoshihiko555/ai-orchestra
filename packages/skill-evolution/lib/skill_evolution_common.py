#!/usr/bin/env python3
"""skill-evolution の共通ライブラリ（決定論ロジック。hook ではない）。

責務:
- lessons と蓄積データ（metrics/pending/locks）の保存先解決・I/O
- lessons の追記と肥大化管理（行数上限＋archive 退避）
- `[critical]` チェックリスト解析と success 判定
- 二軸スコアリング（judge 総合スコア）と履歴サマリ
- スキル provenance 判別（facet 製/非 facet 製）と改善反映先の解決
- オフライン反復の停止条件・3 ガード評価
- スキル単位ロック（起動口の競合防止）

LLM を要する処理（シナリオ実行・改善案生成）は本 lib の責務外（skill が担う）。
"""

from __future__ import annotations

import functools
import glob
import json
import os
import random
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

try:
    import fcntl  # Unix のみ。lessons の read-modify-write を排他化する
except ImportError:  # 非 Unix 環境ではロックなしにフォールバック
    fcntl = None  # type: ignore[assignment]

PACKAGE_NAME = "skill-evolution"
CONFIG_FILENAME = "skill-evolution.yaml"
# fail-logs の GIT_BUDGET_SECONDS（capture-failures.py）と同じ、hook 全体の予算値。
# resolve_log_root 内部ではこの予算がさらに複数ステップへ deadline 分割される
# （1 ステップ全体の上限ではない）。
LOG_ROOT_RESOLUTION_TIMEOUT_SECONDS = 3.5
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600
MIGRATION_MAX_BYTES = 1024 * 1024
MIGRATION_TOTAL_BUDGET_BYTES = 4 * 1024 * 1024
RECENT_RUN_IDS_BASE_TAIL_BYTES = 128 * 1024
# 1 回の migration 追記で直前の run_id が重複確認窓から押し出されないよう、
# migration の最大追記量を既存の末尾読み込み幅へ加える。
RECENT_RUN_IDS_TAIL_BYTES = RECENT_RUN_IDS_BASE_TAIL_BYTES + MIGRATION_MAX_BYTES

# config が読めない場合のフォールバック既定値（正本は skill-evolution.yaml）。
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "storage": {
        "dir": ".claude/skill-evolution",  # lessons 専用。root worktree 解決しない
        "logs_dir": ".claude/logs/skill-evolution",  # metrics は root、pending/locks は local
    },
    "lessons": {"max_lines": 40, "inject_max_chars": 4000},
    "pending": {"stale_after_seconds": 600},
    "offline": {
        "max_iterations": 10,
        "max_cost_usd": 5.0,
        "holdout_ratio": 0.3,
        "stop": {"consecutive": 2, "accuracy_delta_pt": 3, "steps_pct": 10, "time_pct": 15},
        "guards": {"divergence_rounds": 3, "overfit_drop_pt": 15},
    },
    "trigger": {"lessons_threshold": 20},
}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """override で base を再帰的に上書きした新しい dict を返す。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_hook_common() -> Any | None:
    """core の hook_common を遅延 import する。解決不能時は None。"""
    try:
        orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
        core_hooks = os.path.join(orchestra_dir, "packages", "core", "hooks")
        # 絶対パスかつ実在する場合のみ sys.path を汚染する（cwd 相対の残留を防ぐ）。
        if os.path.isabs(core_hooks) and os.path.isdir(core_hooks) and core_hooks not in sys.path:
            sys.path.insert(0, core_hooks)
        import hook_common

        return hook_common
    except Exception:
        return None


def load_config(project_dir: str) -> dict:
    """skill-evolution.yaml を読み込み DEFAULTS にマージする。

    hook_common.load_package_config が使える場合はそれを使い、無ければ DEFAULTS を返す。
    """
    try:
        hook_common = _load_hook_common()
        loaded = (
            hook_common.load_package_config(PACKAGE_NAME, CONFIG_FILENAME, project_dir)
            if hook_common is not None
            else {}
        )
    except Exception:
        loaded = {}
    return _deep_merge(DEFAULTS, loaded or {})


# ---------------------------------------------------------------------------
# パス解決 / ディレクトリ
# ---------------------------------------------------------------------------


def data_dir(project_dir: str, config: dict | None = None) -> str:
    """lessons 専用ルート（`.claude/skill-evolution`）の絶対パスを返す。

    config の storage.dir が project_dir の外を指す場合（`../` 等）は既定値に戻す
    （設定経由のパストラバーサル防止）。
    ADR-20260728-046 決定4により、意図的に root worktree 解決を行わない。
    """
    cfg = config or {}
    rel = (cfg.get("storage") or {}).get("dir") or DEFAULTS["storage"]["dir"]
    base = os.path.abspath(project_dir)
    candidate = os.path.abspath(os.path.join(base, rel))
    if os.path.commonpath([base, candidate]) != base:
        candidate = os.path.abspath(os.path.join(base, DEFAULTS["storage"]["dir"]))
    return candidate


@functools.cache
def _resolve_log_root_cached(project_dir: str) -> str:
    """蓄積データ用 root worktree をプロセス中 1 回だけ解決する。

    単一 project_dir・短命プロセス（1 hook 呼び出し = 1 プロセス）前提のキャッシュ。
    長寿命プロセスから複数 project を切り替えて処理する用途では、キャッシュが
    古い project_dir の解決結果を返し続け陳腐化するため使用しないこと。
    """
    fallback = os.path.abspath(project_dir)
    try:
        hook_common = _load_hook_common()
        if hook_common is None:
            return fallback
        resolved = hook_common.resolve_log_root(
            fallback,
            timeout=LOG_ROOT_RESOLUTION_TIMEOUT_SECONDS,
        )
        return os.path.abspath(resolved) if resolved else fallback
    except Exception:
        return fallback


def _is_path_within_real_base(real_base: str, candidate: str) -> bool:
    """candidate の実体が real_base 配下に収まるかを返す。"""
    real_candidate = os.path.realpath(candidate)
    return real_candidate == real_base or real_candidate.startswith(real_base + os.sep)


def _resolve_path_within_local(base_dir: str, relative: str, filename: str) -> str | None:
    """hook_common.resolve_path_within の非依存版（フォールバック専用）。

    `AI_ORCHESTRA_DIR` 未設定の直接実行（例: `skill_evolution.py status` を単体で
    叩く場合）では `_load_hook_common()` が None を返し得る。その場合でも
    project-local な移行は必ず実行できるよう、境界検証ロジックのみを複製する
    （hook_common 側と挙動を同一に保つため実装は意図的に重複させている）。
    """
    real_base = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base_dir, relative, filename))
    if candidate == real_base or candidate.startswith(real_base + os.sep):
        return candidate
    return None


def _resolve_logs_subdir(base: str, config: dict | None) -> str:
    """base 配下に storage.logs_dir を安全に解決する。

    symlink 経由で base の外を指す設定も realpath 解決で検出し、既定値へ
    フォールバックする（字句比較の abspath/commonpath では symlink 経由の脱出を
    検出できないため）。既定値も base 外へ解決される場合は、安全な保存先がないため
    ValueError を送出する。hook 呼び出しでは hook レベルの `_safe()` が例外を捕捉して
    そのターンの書き込みだけを fail-open でスキップし、同じ保護がない CLI 呼び出し
    では安全でない保存先へ黙って書かず、明示的なエラーとして表面化する。
    """
    cfg = config or {}
    rel = (cfg.get("storage") or {}).get("logs_dir") or DEFAULTS["storage"]["logs_dir"]
    real_base = os.path.realpath(base)
    candidate = os.path.abspath(os.path.join(base, rel))
    if not _is_path_within_real_base(real_base, candidate):
        candidate = os.path.abspath(os.path.join(base, DEFAULTS["storage"]["logs_dir"]))
        if not _is_path_within_real_base(real_base, candidate):
            raise ValueError("storage.logs_dir の安全な保存先を base 配下に解決できません")
    return candidate


def logs_dir(project_dir: str, config: dict | None = None) -> str:
    """metrics 用の root worktree 解決済みルートを返す。

    config の storage.logs_dir が解決済み root の外（symlink 経由含む）を指す場合は
    既定値に戻す。root 解決または hook_common import に失敗した場合は project_dir へ戻る。
    """
    base = _resolve_log_root_cached(os.path.abspath(project_dir))
    return _resolve_logs_subdir(base, config)


def _local_logs_dir(project_dir: str, config: dict | None = None) -> str:
    """pending/locks 用の project_dir ローカル（root 解決なし）ルートを返す。

    pending/locks はセッション単位の一時状態であり、複数 worktree 間で共有すると
    他 worktree の Stop hook が実行中の pending を stale と誤判定して回収し、偽の
    失敗メトリクス確定と完了時の二重記録を招く（Issue: PR#331 レビュー指摘）。
    そのため蓄積データである metrics のみを root 集約し、pending/locks は
    project_dir ローカルに留める（ADR-20260728-046 決定3の対象は metrics のみ）。
    """
    base = os.path.abspath(project_dir)
    return _resolve_logs_subdir(base, config)


def _slug(skill: str) -> str:
    """スキル名をファイル名に使える slug に正規化する。

    パストラバーサル/隠しファイル化を防ぐため先頭のドットは `_` に置換する
    （`..` → `__`、`.env` → `_env`）。長すぎる名前は切り詰める。
    """
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", skill or "unknown")
    slug = re.sub(r"^\.+", lambda m: "_" * len(m.group()), slug)
    return slug[:120] or "unknown"


def metrics_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """metrics/<skill>.jsonl の絶対パスを返す。"""
    return os.path.join(logs_dir(project_dir, config), "metrics", f"{_slug(skill)}.jsonl")


def lessons_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """lessons/<skill>.md の絶対パスを返す。"""
    return os.path.join(data_dir(project_dir, config), "lessons", f"{_slug(skill)}.md")


def lessons_archive_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """lessons/<skill>.archive.md の絶対パスを返す。"""
    return os.path.join(data_dir(project_dir, config), "lessons", f"{_slug(skill)}.archive.md")


def pending_path(project_dir: str, run_id: str, config: dict | None = None) -> str:
    """発火→完了の突合用 pending 記録のパスを返す（run_id キー・並行実行安全）。"""
    return os.path.join(_local_logs_dir(project_dir, config), "pending", f"{_slug(run_id)}.json")


def lock_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """スキル単位ロックファイルのパスを返す。"""
    return os.path.join(_local_logs_dir(project_dir, config), "locks", f"{_slug(skill)}.lock")


def _ensure_parent(path: str) -> None:
    """親ディレクトリを作成する。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)


# ---------------------------------------------------------------------------
# metrics one-shot migration
# ---------------------------------------------------------------------------


def _migrate_metric_file(legacy_path: str, destination_path: str) -> None:
    """旧 metrics 1 ファイルを claim して root 側へ有界追記する。

    destination への書き込みは append_metric() と同じ「O_APPEND オープン + 単発
    write() の atomicity」のみに依拠する。移行対象は MIGRATION_MAX_BYTES（1 MiB）
    で有界なので、コピー範囲を丸ごとメモリに読み、単一の write() 呼び出しで
    追記する。複数回 write（旧: shutil.copyfileobj）にすると、ロックを取らない
    append_metric() の書き込みが途中に割り込み JSONL レコードが壊れ得るため、
    flock は使わず「全 writer が単発 write のみ」に統一して整合性を担保する。

    OS が短い write を返す稀な異常時だけ残りを追加 write し、payload 全体の完了を
    確認できた場合に限って ``.migrated.*`` へ rename する。例外または進捗のない
    write が起きた場合は ``.migrating.*`` claim を手動復旧用に残す。途中まで
    書き込んだ断片には best-effort で改行を追記し、後続レコードまで連結破損する
    ことを防ぐ。復旧 write 自体は full disk 等で失敗し得て、その場合は末尾が
    未終端のまま残るが、一般的な途中 write 例外は失われる 1 行だけに封じ込める。
    """
    try:
        if os.path.realpath(destination_path) == os.path.realpath(legacy_path):
            return

        claim_suffix = f"{os.getpid()}-{time.monotonic_ns()}"
        migrating_path = f"{legacy_path}.migrating.{claim_suffix}"
        try:
            os.rename(legacy_path, migrating_path)
        except OSError:
            return

        os.makedirs(os.path.dirname(destination_path), mode=LOG_DIR_MODE, exist_ok=True)
        with open(migrating_path, "rb") as source:
            file_size = os.fstat(source.fileno()).st_size
            if file_size > MIGRATION_MAX_BYTES:
                cut = file_size - MIGRATION_MAX_BYTES
                source.seek(cut - 1)
                boundary_byte = source.read(1)
                # cut がちょうど改行直後（完全レコードの先頭）なら readline() は不要。
                # 直前バイトが改行でなければ途中行なので readline() で部分行を読み捨てる。
                if boundary_byte != b"\n":
                    source.readline()
            payload = source.read()
        if payload and not payload.endswith(b"\n"):
            payload += b"\n"

        destination_fd = os.open(
            destination_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            LOG_FILE_MODE,
        )
        try:
            try:
                os.fchmod(destination_fd, LOG_FILE_MODE)
            except OSError:
                pass
            written_bytes = 0
            try:
                while written_bytes < len(payload):
                    bytes_written = os.write(destination_fd, payload[written_bytes:])
                    if bytes_written == 0:
                        raise OSError("legacy metrics migration write made no progress")
                    written_bytes += bytes_written
            except Exception:
                try:
                    os.write(destination_fd, b"\n")
                except OSError:
                    pass
                raise
        finally:
            os.close(destination_fd)

        os.rename(migrating_path, f"{legacy_path}.migrated.{claim_suffix}")
    except Exception:
        pass


def migrate_legacy_metrics(project_dir: str, config: dict | None = None) -> None:
    """旧 worktree/root-local metrics を root worktree へ一回限り移行する。

    ADR-20260728-046 決定3に基づき、蓄積データである metrics のみを移行する。
    pending/locks はセッション単位の一時データなので移行せず、新配置で開始する。
    stale な ``.migrating.*`` は手動復旧用に残し、例外はすべて fail-open とする。

    linked worktree では worktree と root checkout の両方の旧配置を走査する。
    1 プロセスの移行 I/O は両方を合わせて MIGRATION_TOTAL_BUDGET_BYTES 以内に
    制限し、超過するファイル以降は旧パスに残して次回へ繰り越す。プロセス内では
    ``_MIGRATION_ATTEMPTED_PROJECT_DIRS`` により一度だけ試行するが、次の hook
    プロセスでは再試行され、rename 済みでないファイルから自然に移行を継続する。
    """
    try:
        project_root = os.path.abspath(project_dir)
        log_root = _resolve_log_root_cached(project_root)
        destination_metrics_dir = os.path.join(logs_dir(project_root, config), "metrics")
        hook_common = _load_hook_common()
        resolve_within = (
            hook_common.resolve_path_within
            if hook_common is not None
            else _resolve_path_within_local
        )
        destination_relative = os.path.relpath(destination_metrics_dir, log_root)
        legacy_roots = [project_root]
        if os.path.realpath(log_root) != os.path.realpath(project_root):
            legacy_roots.append(log_root)

        processed_bytes = 0
        for legacy_root in legacy_roots:
            legacy_metrics_dir = os.path.join(data_dir(legacy_root, config), "metrics")
            if not os.path.isdir(legacy_metrics_dir):
                continue
            if os.path.realpath(legacy_metrics_dir) == os.path.realpath(destination_metrics_dir):
                continue
            legacy_relative = os.path.relpath(legacy_metrics_dir, legacy_root)
            for name in sorted(os.listdir(legacy_metrics_dir)):
                if not name.endswith(".jsonl"):
                    continue
                legacy_path = os.path.abspath(os.path.join(legacy_metrics_dir, name))
                if os.path.islink(legacy_path):
                    continue
                try:
                    legacy_stat = os.lstat(legacy_path)
                except OSError:
                    continue
                if not stat.S_ISREG(legacy_stat.st_mode):
                    continue
                resolved_legacy_path = resolve_within(
                    legacy_root,
                    legacy_relative,
                    name,
                )
                destination_path = resolve_within(
                    log_root,
                    destination_relative,
                    name,
                )
                if resolved_legacy_path is None or destination_path is None:
                    continue
                if not os.path.isfile(legacy_path):
                    continue
                file_cost = min(legacy_stat.st_size, MIGRATION_MAX_BYTES)
                if processed_bytes + file_cost > MIGRATION_TOTAL_BUDGET_BYTES:
                    return
                processed_bytes += file_cost
                _migrate_metric_file(legacy_path, destination_path)
    except Exception:
        pass


# 単一 project_dir・短命プロセス前提のグローバル状態（_resolve_log_root_cached と同様）。
# 長寿命プロセスから複数 project を処理する用途では陳腐化しない一方、プロセスを
# 使い回す限り集合が無制限に増え続けるため、そのような用途には適さない。
_MIGRATION_ATTEMPTED_PROJECT_DIRS: set[str] = set()


def _migrate_legacy_metrics_once(project_dir: str, config: dict | None = None) -> None:
    """project_dir ごとに metrics migration をプロセス中 1 回だけ試行する。"""
    key = os.path.abspath(project_dir)
    if key in _MIGRATION_ATTEMPTED_PROJECT_DIRS:
        return
    _MIGRATION_ATTEMPTED_PROJECT_DIRS.add(key)
    migrate_legacy_metrics(project_dir, config)


# ---------------------------------------------------------------------------
# run_id / 時刻
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """現在時刻を ISO8601（UTC）で返す。"""
    return datetime.now(tz=UTC).isoformat()


def gen_run_id(skill: str) -> str:
    """スキル名 + 時刻 + 短い乱数で一意な run_id を生成する。"""
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
    suffix = f"{random.randint(0, 0xFFFF):04x}"
    return f"{_slug(skill)}-{stamp}-{suffix}"


# ---------------------------------------------------------------------------
# pending（発火→完了の突合。run_id キーで並行実行安全）
# ---------------------------------------------------------------------------


def write_pending(
    project_dir: str, run_id: str, config: dict | None = None, skill: str = ""
) -> None:
    """発火時に pending（run_id・開始時刻・skill）を run_id キーで記録する（0o600）。

    skill を保存しておくと、Stop hook の縮退記録（自己申告欠落時のフォールバック）で
    run_id からの逆算なしに skill 名を参照できる。
    """
    path = pending_path(project_dir, run_id, config)
    _ensure_parent(path)
    record: dict[str, Any] = {"run_id": run_id, "start_epoch": time.time(), "ts": now_iso()}
    if skill:
        record["skill"] = skill
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f)


_RUN_ID_SKILL_RE = re.compile(r"^(.*)-\d{8}T\d{6}-[0-9a-f]{4}$")


def _skill_from_run_id(run_id: str) -> str:
    """run_id（`<skill>-<stamp>-<suffix>`）から skill 部分を復元する。

    旧形式（skill 未保存）の pending ファイルからの best-effort フォールバック。
    """
    match = _RUN_ID_SKILL_RE.match(run_id or "")
    return match.group(1) if match else ""


def _read_pending_file(path: str) -> dict | None:
    """pending JSON を読む。読めない/壊れていれば None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_pending(project_dir: str, config: dict | None = None) -> list[dict]:
    """pending/ 配下の全エントリを返す（run_id・skill・start_epoch・path）。

    壊れた/不完全なファイルはスキップする。Stop hook の縮退記録が走査対象を得るための一覧化。
    """
    pending_dir = os.path.join(_local_logs_dir(project_dir, config), "pending")
    if not os.path.isdir(pending_dir):
        return []
    entries: list[dict] = []
    for name in sorted(os.listdir(pending_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(pending_dir, name)
        data = _read_pending_file(path)
        if data is None:
            continue
        run_id = str(data.get("run_id") or "")
        skill = str(data.get("skill") or "") or _skill_from_run_id(run_id)
        start_epoch = data.get("start_epoch")
        if not run_id or not skill or not isinstance(start_epoch, (int, float)):
            continue
        entries.append(
            {"run_id": run_id, "skill": skill, "start_epoch": float(start_epoch), "path": path}
        )
    return entries


def discard_pending(path: str) -> None:
    """pending ファイルを破棄する（存在しない/権限エラーでもクラッシュしない）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def consume_pending(
    project_dir: str, run_id: str, skill: str, config: dict | None = None
) -> tuple[str, int | None]:
    """pending を読み取り (run_id, duration_ms) を返し、当該ファイルを削除する。

    run_id が判明していればそれを優先。無ければ skill の最新 pending にフォールバックする
    （self_report 欠落時の best-effort）。
    """
    path = ""
    if run_id:
        cand = pending_path(project_dir, run_id, config)
        if os.path.isfile(cand):
            path = cand
    if not path and skill:
        pending_dir = os.path.join(_local_logs_dir(project_dir, config), "pending")
        matches = sorted(glob.glob(os.path.join(pending_dir, f"{_slug(skill)}-*.json")))
        if matches:
            path = matches[-1]
    if not path:
        return run_id, None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return run_id, None
    resolved = run_id or str(data.get("run_id") or "")
    start = data.get("start_epoch")
    try:
        # clock skew で負になり得るため max(0, ...) でガード
        duration = max(0, int((time.time() - float(start)) * 1000)) if start is not None else None
    except (TypeError, ValueError):
        duration = None
    try:
        os.remove(path)
    except OSError:
        pass
    return resolved, duration


# ---------------------------------------------------------------------------
# metrics I/O
# ---------------------------------------------------------------------------


def append_metric(project_dir: str, skill: str, record: dict, config: dict | None = None) -> None:
    """metrics/<skill>.jsonl に 1 行追記する（所有者のみ読み書き 0o600）。"""
    _migrate_legacy_metrics_once(project_dir, config)
    path = metrics_path(project_dir, skill, config)
    _ensure_parent(path)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_metrics(project_dir: str, skill: str, config: dict | None = None) -> list[dict]:
    """metrics/<skill>.jsonl を読み、dict のリストを返す。壊れた行はスキップする。"""
    _migrate_legacy_metrics_once(project_dir, config)
    path = metrics_path(project_dir, skill, config)
    if not os.path.isfile(path):
        return []
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def recent_run_ids(
    project_dir: str, skill: str, config: dict | None = None, limit: int = 500
) -> set[str]:
    """metrics の末尾から run_id 集合を返す（重複記録チェック用の有界読み込み）。

    既存の末尾 128 KiB に migration 1 回分の上限を加えた範囲のみ読むため、
    migration 直前に記録された run_id を保持しつつ、肥大化しても I/O が一定。
    byte window 内の全行を検査し、migration や短いレコードの集中追記で現在の
    run_id が行数上限から押し出されることを防ぐ。``limit`` は後方互換のため
    受理するが、I/O/メモリは byte window で既に有界なので機能上は使用しない。
    """
    _migrate_legacy_metrics_once(project_dir, config)
    path = metrics_path(project_dir, skill, config)
    if not os.path.isfile(path):
        return set()
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - RECENT_RUN_IDS_TAIL_BYTES))
        chunk = f.read().decode("utf-8", "replace")
    ids: set[str] = set()
    # 先頭は途中行の可能性があるため 1 行目を捨てる（size>tail のときのみ）
    lines = chunk.splitlines()
    if size > RECENT_RUN_IDS_TAIL_BYTES and lines:
        lines = lines[1:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        rid = obj.get("run_id") if isinstance(obj, dict) else None
        if rid:
            ids.add(str(rid))
    return ids


# ---------------------------------------------------------------------------
# lessons I/O ＋ 肥大化管理
# ---------------------------------------------------------------------------

_LESSONS_TEMPLATE = "# Lessons: {skill}\n\n## [critical] チェックリスト\n\n## 学び（新しい順）\n"
_LEARN_HEADER = "## 学び（新しい順）"


def read_lessons(project_dir: str, skill: str, config: dict | None = None) -> str:
    """lessons/<skill>.md の内容を返す。無ければ空文字。"""
    path = lessons_path(project_dir, skill, config)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def append_lesson(project_dir: str, skill: str, lesson: str, config: dict | None = None) -> None:
    """「学び」セクション先頭に 1 行追記し、上限超過分を archive へ退避する。

    lesson は日付なしの本文でよい（本関数が日付を付ける）。
    """
    cfg = config or {}
    max_lines = int((cfg.get("lessons") or {}).get("max_lines") or DEFAULTS["lessons"]["max_lines"])
    path = lessons_path(project_dir, skill, config)
    _ensure_parent(path)
    # 改行はセクション分割を壊すため 1 行に畳む。
    dated = f"- {datetime.now(tz=UTC).strftime('%Y-%m-%d')}: {' '.join(lesson.split())}"

    # 排他ロック下で read-modify-write（並行 append による上書き喪失を防ぐ）。
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)
        text = f.read() or _LESSONS_TEMPLATE.format(skill=skill)
        head, learn_items = _split_learn_section(text)
        learn_items.insert(0, dated)
        kept, overflow = learn_items[:max_lines], learn_items[max_lines:]
        if overflow:
            _archive_lessons(project_dir, skill, overflow, config)
        new_text = head.rstrip() + "\n\n" + "\n".join(kept) + "\n"
        f.seek(0)
        f.write(new_text)
        f.truncate()
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_UN)


def _split_learn_section(text: str) -> tuple[str, list[str]]:
    """本文を「学びヘッダより前」と「学び項目リスト」に分割する。"""
    idx = text.find(_LEARN_HEADER)
    if idx == -1:
        return text.rstrip() + "\n\n" + _LEARN_HEADER, []
    head = text[: idx + len(_LEARN_HEADER)]
    tail = text[idx + len(_LEARN_HEADER) :]
    items = [ln for ln in tail.splitlines() if ln.strip().startswith("- ")]
    return head, items


def _archive_lessons(
    project_dir: str, skill: str, items: list[str], config: dict | None = None
) -> None:
    """溢れた学び項目を archive ファイルへ追記する。"""
    path = lessons_archive_path(project_dir, skill, config)
    _ensure_parent(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(items) + "\n")


def lessons_count(project_dir: str, skill: str, config: dict | None = None) -> int:
    """「学び」セクションの項目数を返す（オフライン起動口の判定用）。"""
    text = read_lessons(project_dir, skill, config)
    if not text:
        return 0
    _head, items = _split_learn_section(text)
    return len(items)


# ---------------------------------------------------------------------------
# [critical] チェックリスト ＋ success 判定
# ---------------------------------------------------------------------------


def parse_critical_items(lessons_text: str) -> list[str]:
    """lessons 本文の `[critical]` チェックリストから項目テキストを抽出する。"""
    items: list[str] = []
    in_section = False
    for line in lessons_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = "[critical]" in stripped
            continue
        if in_section and re.match(r"^- \[[ xX]\]", stripped):
            items.append(re.sub(r"^- \[[ xX]\]\s*", "", stripped))
    return items


def compute_success(critical_results: dict[str, bool]) -> bool:
    """`[critical]` 全項目が達成なら True。項目が空なら False（成功条件未定義）。"""
    if not critical_results:
        return False
    return all(bool(v) for v in critical_results.values())


def critical_pass_rate(critical_results: dict[str, bool]) -> float:
    """`[critical]` 達成率（0.0–1.0）を返す。項目が空なら 0.0。"""
    if not critical_results:
        return 0.0
    passed = sum(1 for v in critical_results.values() if v)
    return passed / len(critical_results)


# ---------------------------------------------------------------------------
# 自己申告ブロックのパース
# ---------------------------------------------------------------------------

_SELF_REPORT_RE = re.compile(r"\[skill-self-report\](.*?)\[/skill-self-report\]", re.DOTALL)


def extract_text(node: object) -> str:
    """dict/list/str から文字列葉を再帰収集して連結する。

    `json.dumps` すると自己申告ブロック内の `"` がエスケープされ `parse_self_report` が
    読めなくなるため、生の文字列値をそのまま取り出す。
    """
    chunks: list[str] = []

    def _walk(value: object) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        elif value is not None and not isinstance(value, bool):
            chunks.append(str(value))

    _walk(node)
    return "\n".join(chunks)


def parse_self_reports(text: str) -> list[dict]:
    """テキストからすべての `[skill-self-report]{json}[/skill-self-report]` を抽出する。

    見つからない/壊れている/辞書でなければ除外し、有効なものを出現順で返す。
    """
    if not text:
        return []
    reports = []
    for raw in _SELF_REPORT_RE.findall(text):
        try:
            obj = json.loads(raw.strip())
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            reports.append(obj)
    return reports


def parse_self_report(text: str) -> dict | None:
    """テキストから `[skill-self-report]{json}[/skill-self-report]` を抽出する。

    複数あれば最後のものを採用する。見つからない/壊れていれば None。
    """
    reports = parse_self_reports(text)
    return reports[-1] if reports else None


def _safe_int(value: object, default: int = 0) -> int:
    """信頼できない値を安全に int 化する（非数値・None は default）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


_TRUE_STRINGS = {"true", "1", "yes", "y", "on"}
_FALSE_STRINGS = {"false", "0", "no", "n", "off", ""}


def _coerce_bool(value: object) -> bool:
    """信頼できない値を厳格に真偽化する。

    `bool("false")` が True になる罠を避ける。認識できない文字列は安全側で False
    （untrusted な自己申告で `[critical]` 未達を誤って成功にしない）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_STRINGS:
            return True
        return False  # _FALSE_STRINGS も未知文字列もすべて False（安全側）
    return False


def build_metric_record(
    skill: str,
    run_id: str,
    self_report: dict | None,
    duration_ms: int | None,
    tool_uses: int | None = None,
) -> dict:
    """自己申告と機械計測から metrics 1 行分のレコードを組み立てる。

    self_report は信頼できない tool_response 由来のため、値を安全に正規化する。
    """
    critical: dict = {}
    sr_clean = None
    if isinstance(self_report, dict):
        raw_critical = self_report.get("critical")
        if isinstance(raw_critical, dict):
            critical = {str(k): _coerce_bool(v) for k, v in raw_critical.items()}
        sr_clean = {
            "ambiguities": _safe_int(self_report.get("ambiguities")),
            "discretion_fills": _safe_int(self_report.get("discretion_fills")),
            "retries": _safe_int(self_report.get("retries")),
        }
    return {
        "ts": now_iso(),
        "skill": skill,
        "run_id": run_id,
        "self_report": sr_clean,
        "machine": {
            "tool_uses": tool_uses,
            "duration_ms": duration_ms,
            "critical_pass_rate": critical_pass_rate(critical),
        },
        "success": compute_success(critical),
    }


# ---------------------------------------------------------------------------
# 二軸スコアリング（judge 総合スコア）
# ---------------------------------------------------------------------------


def score_run(record: dict) -> float:
    """1 実行の judge 総合スコア（0–100）を返す。

    主軸は critical_pass_rate（70%）。自己申告の減点（不明瞭点・裁量補完・再試行）を 30%。
    自己申告が欠落（null）なら機械軸のみで算出する。
    """
    machine = record.get("machine") or {}
    cpr = float(machine.get("critical_pass_rate") or 0.0)
    base = cpr * 70.0

    self_report = record.get("self_report")
    if not isinstance(self_report, dict):
        # 自己申告欠落: 機械軸のみを 100 スケールへ引き上げる
        return round(cpr * 100.0, 2)

    penalty = (
        _safe_int(self_report.get("ambiguities"))
        + _safe_int(self_report.get("discretion_fills"))
        + _safe_int(self_report.get("retries"))
    )
    quality = max(0.0, 30.0 - penalty * 5.0)
    return round(base + quality, 2)


def _avg_present(records: list[dict], key: str) -> float | None:
    """machine[key] が None でない値のみで平均する。全て欠損なら None を返す。"""
    vals = [
        (r.get("machine") or {}).get(key)
        for r in records
        if (r.get("machine") or {}).get(key) is not None
    ]
    if not vals:
        return None
    return round(sum(float(v) for v in vals) / len(vals), 2)


def summarize(records: list[dict]) -> dict:
    """metrics 履歴のサマリ（件数・成功率・平均スコア・平均ステップ/時間）を返す。

    tool_uses / duration_ms が欠損（None）の実行は平均計算から除外し、
    全欠損時は None を返す（欠損を 0 と誤集計してステップ評価を歪めない）。
    """
    if not records:
        return {
            "count": 0,
            "success_rate": 0.0,
            "avg_score": 0.0,
            "avg_steps": None,
            "avg_ms": None,
        }
    n = len(records)
    successes = sum(1 for r in records if r.get("success"))
    scores = [score_run(r) for r in records]
    return {
        "count": n,
        "success_rate": round(successes / n, 3),
        "avg_score": round(sum(scores) / n, 2),
        "avg_steps": _avg_present(records, "tool_uses"),
        "avg_ms": _avg_present(records, "duration_ms"),
    }


# ---------------------------------------------------------------------------
# provenance 判別（facet 製 / 非 facet 製）と反映先解決
# ---------------------------------------------------------------------------

FACET = "facet"
NON_FACET = "non_facet"
UNKNOWN = "unknown"


def _managed_skill_names(project_dir: str | None = None) -> set[str] | None:
    """AI Orchestra 管理下（facet 製）のスキル名集合を集める。解決不能なら None。

    2 系統を union する:
    1. `$AI_ORCHESTRA_DIR/packages/*/manifest.json` の `skills`（開発/ソース環境）
    2. `<project>/.agents/skills/<name>/`（導入先の facet build 生成物）
       — 導入先には AI_ORCHESTRA_DIR も facets/ も無いため、生成物ディレクトリを正本とする。
    """
    names: set[str] = set()
    found = False

    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    packages_dir = os.path.join(orchestra_dir, "packages") if orchestra_dir else ""
    if packages_dir and os.path.isdir(packages_dir):
        for entry in os.listdir(packages_dir):
            manifest = os.path.join(packages_dir, entry, "manifest.json")
            if not os.path.isfile(manifest):
                continue
            try:
                with open(manifest, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            found = True
            for s in data.get("skills") or []:
                names.add(str(s))

    if project_dir:
        agents_skills = os.path.join(project_dir, ".agents", "skills")
        if os.path.isdir(agents_skills):
            for entry in os.listdir(agents_skills):
                if os.path.isdir(os.path.join(agents_skills, entry)):
                    names.add(entry)
                    found = True

    return names if found else None


def detect_provenance(
    skill: str, managed: set[str] | None = None, project_dir: str | None = None
) -> str:
    """スキルが facet 製か非 facet 製かを判別する。

    判別根拠は manifest の skills と `.agents/skills/` 生成物（`facets/` 有無では判定しない）。
    解決不能な場合は UNKNOWN（安全側）を返す。
    """
    names = managed if managed is not None else _managed_skill_names(project_dir)
    if names is None:
        return UNKNOWN
    return FACET if skill in names else NON_FACET


def resolve_reflection_target(provenance: str) -> str:
    """provenance から改善反映先を解決する。

    - facet 製 → "facet"（facet ソース更新 ＋ facet build、人間承認）
    - 非 facet 製 → "lessons_skill_md"（lessons ＋ SKILL.md diff、人間承認）
    - 判別不能 → "lessons_only"（安全側。生成物・facet ソースは触らない）
    """
    if provenance == FACET:
        return "facet"
    if provenance == NON_FACET:
        return "lessons_skill_md"
    return "lessons_only"


# ---------------------------------------------------------------------------
# 停止条件 ＋ 3 ガード
# ---------------------------------------------------------------------------


@dataclass
class IterationRecord:
    """オフライン反復 1 回分の評価結果。"""

    score: float  # judge 総合スコア（精度）
    steps: float  # 平均ステップ（tool_uses）
    time_ms: float  # 平均所要
    holdout_score: float = 0.0
    cost_usd: float = 0.0
    new_ambiguities: int = 0  # この反復で新規に生じた不明瞭点（収束条件: 0 が必須）


@dataclass
class StopDecision:
    """停止判定の結果。"""

    should_stop: bool
    reason: str = ""
    guard: str = ""  # "" / "divergence" / "overfit" / "cost" / "max_iterations"
    detail: dict = field(default_factory=dict)


def _within(a: float, b: float, pct: float) -> bool:
    """b に対する a の相対変化が ±pct% 以内かを判定する（b=0 は微小許容）。"""
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) * 100.0 <= pct


def evaluate_stop(history: list[IterationRecord], config: dict) -> StopDecision:
    """反復履歴に停止条件と 3 ガードを適用して判定する。

    優先順: max_iterations → cost → overfit → 収束（stop 条件）→ 発散。
    収束を発散より先に見る（安定＝収束成功、非収束で無改善のみ発散扱い）。
    """
    off = (config or {}).get("offline") or DEFAULTS["offline"]
    stop_cfg = off.get("stop") or DEFAULTS["offline"]["stop"]
    guards = off.get("guards") or DEFAULTS["offline"]["guards"]

    if not history:
        return StopDecision(False)

    n = len(history)
    if n >= int(off.get("max_iterations") or DEFAULTS["offline"]["max_iterations"]):
        return StopDecision(True, "最大反復上限に到達", "max_iterations")

    latest = history[-1]
    if latest.cost_usd >= float(off.get("max_cost_usd") or DEFAULTS["offline"]["max_cost_usd"]):
        return StopDecision(True, "コスト上限に到達", "cost")

    # 過学習: holdout が最良から overfit_drop_pt 超下落
    best_holdout = max(h.holdout_score for h in history)
    drop = best_holdout - latest.holdout_score
    if drop > float(
        guards.get("overfit_drop_pt") or DEFAULTS["offline"]["guards"]["overfit_drop_pt"]
    ):
        return StopDecision(
            True, f"過学習検知（holdout {drop:.1f}pt 下落）", "overfit", {"drop_pt": drop}
        )

    # 収束（停止条件）: 直近 consecutive 回すべてが微小変化 → 完了として停止
    consecutive = int(stop_cfg.get("consecutive") or DEFAULTS["offline"]["stop"]["consecutive"])
    if n >= consecutive + 1:
        window = history[-(consecutive + 1) :]
        acc_pt = float(
            stop_cfg.get("accuracy_delta_pt") or DEFAULTS["offline"]["stop"]["accuracy_delta_pt"]
        )
        steps_pct = float(stop_cfg.get("steps_pct") or DEFAULTS["offline"]["stop"]["steps_pct"])
        time_pct = float(stop_cfg.get("time_pct") or DEFAULTS["offline"]["stop"]["time_pct"])
        converged = True
        for prev, cur in zip(window, window[1:]):
            # 停止条件（設計 3.3）: 新規不明瞭点 0 が必須。1 つでも残れば未収束。
            if cur.new_ambiguities != 0:
                converged = False
                break
            if abs(cur.score - prev.score) > acc_pt:
                converged = False
                break
            if not _within(cur.steps, prev.steps, steps_pct):
                converged = False
                break
            if not _within(cur.time_ms, prev.time_ms, time_pct):
                converged = False
                break
        if converged:
            return StopDecision(True, "収束（停止条件を連続達成）", "")

    # 発散: 直近 divergence_rounds 回で改善なし（かつ未収束）→ 人間通知して停止
    div_rounds = int(
        guards.get("divergence_rounds") or DEFAULTS["offline"]["guards"]["divergence_rounds"]
    )
    if n >= div_rounds + 1:
        window = history[-(div_rounds + 1) :]
        baseline = window[0].score
        if all(h.score <= baseline for h in window[1:]):
            return StopDecision(True, f"{div_rounds}回改善なし（発散）→ 人間通知", "divergence")

    return StopDecision(False)


# ---------------------------------------------------------------------------
# スキル単位ロック（起動口の競合防止）
# ---------------------------------------------------------------------------


LOCK_TTL_SECONDS = 3600  # この時間を超えたロックは stale とみなす


def _read_lock(path: str) -> dict | None:
    """ロックファイルを読む。読めない/壊れていれば None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_stale(data: dict | None) -> bool:
    """ロック内容が stale かを **TTL のみ**で判定する。

    ロックは「オフライン実行セッション」を表し、`lock acquire` CLI は取得後すぐ終了する
    （PID は短命）ため、PID 生存確認は使わない（使うと即 stale と誤判定して奪取されてしまう）。
    保持プロセスがクラッシュしても TTL 経過で必ず解放され、恒久デッドロックにはならない。
    """
    if data is None:
        return True  # 読めない/壊れたロックは stale 扱い
    epoch = data.get("epoch")
    if not isinstance(epoch, (int, float)):
        return True  # epoch 不明は stale 扱い
    return time.time() - epoch > LOCK_TTL_SECONDS


def _is_stale_lock(path: str) -> bool:
    """パス指定で stale 判定する（後方互換の薄いラッパ）。"""
    return _is_stale(_read_lock(path))


def acquire_lock(project_dir: str, skill: str, config: dict | None = None) -> bool:
    """ロックを取得する。既存でも stale なら奪取する（同一スキルは同時 1 インスタンス）。"""
    path = lock_path(project_dir, skill, config)
    _ensure_parent(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        snapshot = _read_lock(path)
        if not _is_stale(snapshot):
            return False
        # TOCTOU 緩和: 削除直前に内容が読んだ時点と同一のときだけ奪取する
        # （他プロセスが既に新ロックを張っていたら諦め、その新ロックを消さない）。
        try:
            if _read_lock(path) != snapshot:
                return False
            os.remove(path)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (OSError, FileExistsError):
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "ts": now_iso(), "epoch": time.time()}))
    return True


def release_lock(project_dir: str, skill: str, config: dict | None = None) -> None:
    """ロックを解放する（存在しない/権限エラーでもクラッシュしない）。"""
    path = lock_path(project_dir, skill, config)
    try:
        os.remove(path)
    except OSError:
        pass
