"""inject-failure-summary hook の集計・フィルタ・注入整形を検証する。"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.module_loader import load_module

inject = load_module("inject_failure_summary", "packages/fail-logs/hooks/inject-failure-summary.py")

LOG_REL = Path(".claude") / "logs" / "fail-logs" / "failures.jsonl"


def _make_project(tmp_path: Path) -> Path:
    """`.claude` を持つプロジェクトルートを用意する。"""
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _ts(days_ago: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _record(
    *,
    failure_type: str = "tool_error",
    command_kind: str = "",
    tool: str = "Bash",
    command: str = "",
    error_excerpt: str = "",
    days_ago: float = 0.0,
) -> dict:
    return {
        "v": 1,
        "ts": _ts(days_ago),
        "sid": "sess",
        "type": "failure",
        "data": {
            "failure_type": failure_type,
            "command_kind": command_kind,
            "tool": tool,
            "command": command,
            "error_excerpt": error_excerpt,
        },
    }


def _write_log(project: Path, records: list[dict]) -> None:
    log_path = project / LOG_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _run(monkeypatch, project: Path) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(project)})))
    inject.main()


def test_recurring_signature_is_injected(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest tests/a", error_excerpt="FAILED a"),
            _record(command_kind="test", command="pytest tests/b", error_excerpt="FAILED b"),
            _record(command_kind="test", command="pytest tests/c", error_excerpt="FAILED c"),
        ],
    )
    _run(monkeypatch, project)
    out = capsys.readouterr().out
    assert "[fail-logs]" in out
    assert "×3" in out
    assert "pytest" in out
    # 見出しに failure_type 別カウントが出る
    assert "tool_error 3" in out


def test_single_occurrence_is_suppressed(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest tests/a"),
            _record(command_kind="lint", command="ruff check ."),
        ],
    )
    _run(monkeypatch, project)
    # どのシグネチャも 1 回きり（再発なし）→ 注入しない
    assert capsys.readouterr().out == ""


def test_window_days_filters_old_records(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest x", days_ago=30),
            _record(command_kind="test", command="pytest y", days_ago=30),
        ],
    )
    _run(monkeypatch, project)
    # デフォルト window_days=7 で 30 日前は除外 → 再発ゼロ → 無出力
    assert capsys.readouterr().out == ""


def test_window_zero_includes_all(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("summary:\n  window_days: 0\n")
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest x", days_ago=30),
            _record(command_kind="test", command="pytest y", days_ago=30),
        ],
    )
    _run(monkeypatch, project)
    out = capsys.readouterr().out
    assert "×2" in out
    assert "全期間" in out


def test_disabled_via_summary_config(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("summary:\n  enabled: false\n")
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest x"),
            _record(command_kind="test", command="pytest y"),
        ],
    )
    _run(monkeypatch, project)
    assert capsys.readouterr().out == ""


def test_disabled_via_top_level_config(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("enabled: false\n")
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest x"),
            _record(command_kind="test", command="pytest y"),
        ],
    )
    _run(monkeypatch, project)
    assert capsys.readouterr().out == ""


def test_missing_log_is_noop(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    _run(monkeypatch, project)
    assert capsys.readouterr().out == ""


def test_broken_lines_are_skipped(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    log_path = project / LOG_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(_record(command_kind="test", command="pytest z"))
    log_path.write_text(
        "\n".join(["{ broken json", good, "also not json", good]) + "\n",
        encoding="utf-8",
    )
    _run(monkeypatch, project)
    out = capsys.readouterr().out
    # 壊れた行を飛ばし、正常な 2 行で再発を検出する
    assert "×2" in out


def test_non_bash_fallback_signature(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    _write_log(
        project,
        [
            _record(tool="Edit", command="", error_excerpt="String to replace not found"),
            _record(tool="Edit", command="", error_excerpt="String to replace not found"),
        ],
    )
    _run(monkeypatch, project)
    out = capsys.readouterr().out
    assert "×2" in out
    assert "[Edit]" in out


def test_show_examples_false_omits_excerpt(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("summary:\n  show_examples: false\n")
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest x", error_excerpt="SECRET-MARKER"),
            _record(command_kind="test", command="pytest y", error_excerpt="SECRET-MARKER"),
        ],
    )
    _run(monkeypatch, project)
    out = capsys.readouterr().out
    assert "×2" in out
    assert "SECRET-MARKER" not in out


def test_top_signatures_limit(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    config_dir = project / ".claude" / "config" / "fail-logs"
    config_dir.mkdir(parents=True)
    (config_dir / "fail-logs.local.yaml").write_text("summary:\n  top_signatures: 1\n")
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest a"),
            _record(command_kind="test", command="pytest a"),
            _record(command_kind="lint", command="ruff check x"),
            _record(command_kind="lint", command="ruff check y"),
            _record(command_kind="lint", command="ruff check z"),
        ],
    )
    _run(monkeypatch, project)
    out = capsys.readouterr().out
    # 上位 1 件のみ → ruff（×3）が出て pytest（×2）は出ない
    assert "ruff" in out
    assert "pytest" not in out


def test_signature_aggregates_across_failure_types(monkeypatch, tmp_path, capsys) -> None:
    # ADR-20260630-027: command ベースのシグネチャは failure_type を含めない。
    # 同一コマンドが failure_type 違いで失敗しても 1 シグネチャに集約され再発判定される。
    project = _make_project(tmp_path)
    _write_log(
        project,
        [
            _record(failure_type="test_failure", command_kind="test", command="pytest x"),
            _record(failure_type="tool_error", command_kind="test", command="pytest y"),
        ],
    )
    _run(monkeypatch, project)
    out = capsys.readouterr().out
    assert "×2" in out
    assert "pytest" in out


def test_injection_is_wrapped_in_trust_boundary(monkeypatch, tmp_path, capsys) -> None:
    # 間接プロンプトインジェクション対策: ログ由来データは境界フレームで囲み、
    # 抜粋には [log] プレフィックスを付与する。
    project = _make_project(tmp_path)
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest x", error_excerpt="boom"),
            _record(command_kind="test", command="pytest y", error_excerpt="boom"),
        ],
    )
    _run(monkeypatch, project)
    out = capsys.readouterr().out
    assert "<fail-logs-summary>" in out
    assert "</fail-logs-summary>" in out
    assert "↳ [log] boom" in out


def test_empty_stdin_falls_back_to_cwd(monkeypatch, tmp_path, capsys) -> None:
    project = _make_project(tmp_path)
    _write_log(
        project,
        [
            _record(command_kind="test", command="pytest x"),
            _record(command_kind="test", command="pytest y"),
        ],
    )
    # stdin が空でも cwd 解決にフォールバックし、クラッシュせずログを拾える
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.chdir(project)
    inject.main()
    out = capsys.readouterr().out
    assert "×2" in out
