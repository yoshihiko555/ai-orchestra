#!/usr/bin/env python3
"""skill-evolution の共通ライブラリ（決定論ロジック。hook ではない）。

責務:
- データ保存先（`.claude/skill-evolution/`）の解決と metrics/lessons I/O
- lessons の追記と肥大化管理（行数上限＋archive 退避）
- `[critical]` チェックリスト解析と success 判定
- 二軸スコアリング（judge 総合スコア）と履歴サマリ
- スキル provenance 判別（facet 製/非 facet 製）と改善反映先の解決
- オフライン反復の停止条件・3 ガード評価
- スキル単位ロック（起動口の競合防止）

LLM を要する処理（シナリオ実行・改善案生成）は本 lib の責務外（skill が担う）。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

PACKAGE_NAME = "skill-evolution"
CONFIG_FILENAME = "skill-evolution.yaml"

# config が読めない場合のフォールバック既定値（正本は skill-evolution.yaml）。
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "storage": {"dir": ".claude/skill-evolution"},
    "lessons": {"max_lines": 40, "inject_max_chars": 4000},
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


def load_config(project_dir: str) -> dict:
    """skill-evolution.yaml を読み込み DEFAULTS にマージする。

    hook_common.load_package_config が使える場合はそれを使い、無ければ DEFAULTS を返す。
    """
    try:
        _core_hooks = os.path.join(
            os.environ.get("AI_ORCHESTRA_DIR", ""), "packages", "core", "hooks"
        )
        if _core_hooks and _core_hooks not in os.sys.path:
            os.sys.path.insert(0, _core_hooks)
        from hook_common import load_package_config

        loaded = load_package_config(PACKAGE_NAME, CONFIG_FILENAME, project_dir)
    except Exception:
        loaded = {}
    return _deep_merge(DEFAULTS, loaded or {})


# ---------------------------------------------------------------------------
# パス解決 / ディレクトリ
# ---------------------------------------------------------------------------


def data_dir(project_dir: str, config: dict | None = None) -> str:
    """データルート（`.claude/skill-evolution`）の絶対パスを返す。"""
    cfg = config or {}
    rel = (cfg.get("storage") or {}).get("dir") or DEFAULTS["storage"]["dir"]
    return os.path.join(project_dir, rel)


def _slug(skill: str) -> str:
    """スキル名をファイル名に使える slug に正規化する。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", skill or "unknown")


def metrics_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """metrics/<skill>.jsonl の絶対パスを返す。"""
    return os.path.join(data_dir(project_dir, config), "metrics", f"{_slug(skill)}.jsonl")


def lessons_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """lessons/<skill>.md の絶対パスを返す。"""
    return os.path.join(data_dir(project_dir, config), "lessons", f"{_slug(skill)}.md")


def lessons_archive_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """lessons/<skill>.archive.md の絶対パスを返す。"""
    return os.path.join(data_dir(project_dir, config), "lessons", f"{_slug(skill)}.archive.md")


def pending_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """発火→完了の突合用 pending 記録（run_id・開始時刻）のパスを返す。"""
    return os.path.join(data_dir(project_dir, config), "pending", f"{_slug(skill)}.json")


def lock_path(project_dir: str, skill: str, config: dict | None = None) -> str:
    """スキル単位ロックファイルのパスを返す。"""
    return os.path.join(data_dir(project_dir, config), "locks", f"{_slug(skill)}.lock")


def _ensure_parent(path: str) -> None:
    """親ディレクトリを作成する。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)


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
# metrics I/O
# ---------------------------------------------------------------------------


def append_metric(project_dir: str, skill: str, record: dict, config: dict | None = None) -> None:
    """metrics/<skill>.jsonl に 1 行追記する。"""
    path = metrics_path(project_dir, skill, config)
    _ensure_parent(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_metrics(project_dir: str, skill: str, config: dict | None = None) -> list[dict]:
    """metrics/<skill>.jsonl を読み、dict のリストを返す。壊れた行はスキップする。"""
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

    text = read_lessons(project_dir, skill, config) or _LESSONS_TEMPLATE.format(skill=skill)
    head, learn_items = _split_learn_section(text)
    dated = f"- {datetime.now(tz=UTC).strftime('%Y-%m-%d')}: {lesson.strip()}"
    learn_items.insert(0, dated)

    kept, overflow = learn_items[:max_lines], learn_items[max_lines:]
    if overflow:
        _archive_lessons(project_dir, skill, overflow, config)

    new_text = head.rstrip() + "\n\n" + "\n".join(kept) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)


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


def parse_self_report(text: str) -> dict | None:
    """テキストから `[skill-self-report]{json}[/skill-self-report]` を抽出する。

    複数あれば最後のものを採用する。見つからない/壊れていれば None。
    """
    if not text:
        return None
    matches = _SELF_REPORT_RE.findall(text)
    if not matches:
        return None
    try:
        obj = json.loads(matches[-1].strip())
    except (ValueError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def build_metric_record(
    skill: str,
    run_id: str,
    self_report: dict | None,
    duration_ms: int | None,
    tool_uses: int | None = None,
) -> dict:
    """自己申告と機械計測から metrics 1 行分のレコードを組み立てる。"""
    critical = {}
    sr_clean = None
    if isinstance(self_report, dict):
        critical = self_report.get("critical") or {}
        if not isinstance(critical, dict):
            critical = {}
        sr_clean = {
            "ambiguities": int(self_report.get("ambiguities") or 0),
            "discretion_fills": int(self_report.get("discretion_fills") or 0),
            "retries": int(self_report.get("retries") or 0),
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
        int(self_report.get("ambiguities") or 0)
        + int(self_report.get("discretion_fills") or 0)
        + int(self_report.get("retries") or 0)
    )
    quality = max(0.0, 30.0 - penalty * 5.0)
    return round(base + quality, 2)


def summarize(records: list[dict]) -> dict:
    """metrics 履歴のサマリ（件数・成功率・平均スコア・平均ステップ/時間）を返す。"""
    if not records:
        return {"count": 0, "success_rate": 0.0, "avg_score": 0.0, "avg_steps": 0.0, "avg_ms": 0.0}
    n = len(records)
    successes = sum(1 for r in records if r.get("success"))
    scores = [score_run(r) for r in records]
    steps = [int((r.get("machine") or {}).get("tool_uses") or 0) for r in records]
    times = [int((r.get("machine") or {}).get("duration_ms") or 0) for r in records]
    return {
        "count": n,
        "success_rate": round(successes / n, 3),
        "avg_score": round(sum(scores) / n, 2),
        "avg_steps": round(sum(steps) / n, 2),
        "avg_ms": round(sum(times) / n, 2),
    }


# ---------------------------------------------------------------------------
# provenance 判別（facet 製 / 非 facet 製）と反映先解決
# ---------------------------------------------------------------------------

FACET = "facet"
NON_FACET = "non_facet"
UNKNOWN = "unknown"


def _managed_skill_names() -> set[str] | None:
    """AI Orchestra 管理下のスキル名集合を manifest から集める。読めなければ None。"""
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if not orchestra_dir:
        return None
    packages_dir = os.path.join(orchestra_dir, "packages")
    if not os.path.isdir(packages_dir):
        return None
    names: set[str] = set()
    found = False
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
    return names if found else None


def detect_provenance(skill: str, managed: set[str] | None = None) -> str:
    """スキルが facet 製か非 facet 製かを manifest 照合で判別する。

    `facets/` の有無では判定しない（導入先には facets/ が無いため）。
    manifest が読めない場合は UNKNOWN（安全側）を返す。
    """
    names = managed if managed is not None else _managed_skill_names()
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


@dataclass
class StopDecision:
    """停止判定の結果。"""

    should_stop: bool
    reason: str = ""
    guard: str = ""  # "" / "divergence" / "overfit" / "cost" / "max_iterations"
    detail: dict = field(default_factory=dict)


def _within(a: float, b: float, pct: float) -> bool:
    """b に対する a の相対変化が ±pct% 以内かを判定する（b=0 は絶対差 0 のみ許容）。"""
    if b == 0:
        return a == 0
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


def acquire_lock(project_dir: str, skill: str, config: dict | None = None) -> bool:
    """ロックを取得する。既に存在すれば False（同一スキルは同時 1 インスタンス）。"""
    path = lock_path(project_dir, skill, config)
    _ensure_parent(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "ts": now_iso(), "epoch": time.time()}))
    return True


def release_lock(project_dir: str, skill: str, config: dict | None = None) -> None:
    """ロックを解放する（存在しなくてもエラーにしない）。"""
    path = lock_path(project_dir, skill, config)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
