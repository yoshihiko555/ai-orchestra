#!/usr/bin/env python3
"""Stop hook: ターン終了時に未回収のスキル実行テレメトリを捕捉する。

PostToolUse(Skill) がスキル起動時に発火する main-loop 実行を補完する。
transcript 内の自己申告を pending の run_id と突合し、期限切れ pending は
自己申告なしの機械メトリクスとしてフォールバック記録する。
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable

_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(os.path.dirname(_HOOK_DIR), "lib")
for _p in (_HOOK_DIR, _LIB_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import skill_evolution_common as se
except ImportError:
    se = None  # type: ignore[assignment]

MAX_STDIN_BYTES = 1024 * 1024  # 1 MB。巨大 stdin による OOM を防ぐ
MAX_TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024  # 2 MB。transcript 読み込み量を制限する


def _safe(func: Callable[[], None]) -> Callable[[], None]:
    def wrapper() -> None:
        try:
            func()
        except Exception as e:  # noqa: BLE001
            print(f"Hook error (capture-skill-stop): {e}", file=sys.stderr)
            sys.exit(0)

    return wrapper


def _project_dir(data: dict) -> str:
    return data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def _default_transcript_root() -> str:
    """transcript 読み取りを許可するルートディレクトリを返す。

    Claude Code の transcript は `~/.claude/projects/` 配下に生成される。
    stdin の `transcript_path` は改ざんや symlink により任意ファイルを指しうる
    ため（CWE-22）、ここで返すルート配下に realpath 解決後で収まる場合のみ
    読み取りを許可する。テストからは本関数を monkeypatch して差し替える。
    """
    return os.path.expanduser("~/.claude")


def _read_transcript_tail(path: object, allowed_root: str | None = None) -> str:
    """transcript の末尾を上限付きで読み、読めなければ空文字を返す。

    `allowed_root`（既定: `_default_transcript_root()`）配下に realpath 解決後で
    収まらないパスは読み取りを拒否し空文字を返す。symlink による脱出も
    realpath 解決で検出する（fail-logs の resolve_path_within と同じ方針）。
    """
    if not isinstance(path, str) or not path:
        return ""
    root = os.path.realpath(
        allowed_root if allowed_root is not None else _default_transcript_root()
    )
    resolved = os.path.realpath(path)
    if resolved != root and not resolved.startswith(root + os.sep):
        return ""
    try:
        with open(resolved, "rb") as transcript:
            transcript.seek(0, os.SEEK_END)
            size = transcript.tell()
            transcript.seek(max(0, size - MAX_TRANSCRIPT_TAIL_BYTES))
            return transcript.read(MAX_TRANSCRIPT_TAIL_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def _self_reports_by_run_id(transcript_text: str) -> dict[str, dict]:
    """transcript JSONL 内の自己申告を run_id で索引化する。"""
    reports: dict[str, dict] = {}
    for line in transcript_text.splitlines():
        if not line.strip() or "skill-self-report" not in line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        for report in se.parse_self_reports(se.extract_text(obj)):
            reports[str(report.get("run_id"))] = report
    return reports


def _already_recorded(project_dir: str, skill: str, run_id: str, config: dict) -> bool:
    """同一 run_id が既に metrics に記録済みかを返す。"""
    return run_id in se.recent_run_ids(project_dir, skill, config)


def _append_failure_lesson(
    project_dir: str, skill: str, run_id: str, record: dict, config: dict
) -> None:
    """失敗または不明瞭点がある matched run の学びを追記する。"""
    sr = record.get("self_report") or {}
    if not record.get("success") or int(sr.get("ambiguities") or 0) > 0:
        note = (
            f"run {run_id or 'n/a'}: success={record.get('success')} "
            f"critical_pass_rate={record['machine']['critical_pass_rate']:.2f} "
            f"ambiguities={sr.get('ambiguities', 'n/a')}"
        )
        se.append_lesson(project_dir, skill, note, config)


def _record_matched(project_dir: str, pending: dict, report: dict, config: dict) -> None:
    """自己申告と突合できた pending を記録する。"""
    skill, run_id = pending["skill"], pending["run_id"]
    if _already_recorded(project_dir, skill, run_id, config):
        se.discard_pending(pending["path"])
        return
    resolved_run_id, duration_ms = se.consume_pending(project_dir, run_id, skill, config)
    tool_uses = (
        se._safe_int(report.get("tool_uses")) if report.get("tool_uses") is not None else None
    )
    record = se.build_metric_record(
        skill, resolved_run_id, report, duration_ms, tool_uses=tool_uses
    )
    se.append_metric(project_dir, skill, record, config)
    _append_failure_lesson(project_dir, skill, resolved_run_id, record, config)


def _record_stale(project_dir: str, pending: dict, config: dict) -> None:
    """期限切れ pending を自己申告なしの機械メトリクスとして記録する。"""
    skill, run_id = pending["skill"], pending["run_id"]
    if _already_recorded(project_dir, skill, run_id, config):
        se.discard_pending(pending["path"])
        return
    _, duration_ms = se.consume_pending(project_dir, run_id, skill, config)
    record = se.build_metric_record(skill, run_id, None, duration_ms)
    se.append_metric(project_dir, skill, record, config)


def _stale_after_seconds(config: dict) -> float:
    """pending のフォールバック記録までの猶予秒数を返す。"""
    value = (config.get("pending") or {}).get("stale_after_seconds")
    fallback = se.DEFAULTS["pending"]["stale_after_seconds"]
    try:
        return float(fallback if value is None else value)
    except (TypeError, ValueError):
        return float(fallback)


def _process_pending(
    project_dir: str, entries: list[dict], reports: dict[str, dict], config: dict
) -> None:
    """pending を matched または stale-fallback として処理する。"""
    stale_after_seconds = _stale_after_seconds(config)
    for pending in entries:
        report = reports.get(pending["run_id"])
        if report is not None:
            _record_matched(project_dir, pending, report, config)
            continue
        if time.time() - pending["start_epoch"] < stale_after_seconds:
            continue
        _record_stale(project_dir, pending, config)


@_safe
def main() -> None:
    """Stop hook のエントリポイント。"""
    if se is None:
        return
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES).decode("utf-8", "replace")
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    project_dir = _project_dir(data)
    config = se.load_config(project_dir)
    if not config.get("enabled", True):
        return
    entries = se.list_pending(project_dir, config)
    if not entries:
        return
    transcript_text = _read_transcript_tail(data.get("transcript_path"), _default_transcript_root())
    reports = _self_reports_by_run_id(transcript_text)
    _process_pending(project_dir, entries, reports, config)


if __name__ == "__main__":
    main()
