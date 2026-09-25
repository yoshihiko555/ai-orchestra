"""audit-bootstrap.py の移行案内を検証するテスト。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

_audit_hooks = str(REPO_ROOT / "packages" / "audit" / "hooks")
_core_hooks = str(REPO_ROOT / "packages" / "core" / "hooks")
for hooks_dir in (_audit_hooks, _core_hooks):
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)

audit_bootstrap = load_module("audit_bootstrap", "packages/audit/hooks/audit-bootstrap.py")


def _stub_session_start_dependencies(monkeypatch: pytest.MonkeyPatch, project_dir: Path) -> None:
    monkeypatch.setattr(
        audit_bootstrap,
        "read_hook_input",
        lambda: {"session_id": "session-1", "cwd": str(project_dir)},
    )
    monkeypatch.setattr(audit_bootstrap, "init_session_dir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(audit_bootstrap, "generate_id", lambda: "trace-1")
    monkeypatch.setattr(audit_bootstrap, "save_trace_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(audit_bootstrap, "emit_event", lambda *_args, **_kwargs: None)


def test_main_prints_one_line_notice_for_moved_local_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """旧 local 設定に移動済みキーがあれば、キー名付きの案内を1行だけ出す。"""
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    (config_dir / "audit-flags.local.json").write_text(
        json.dumps(
            {
                "features": {"quality_gate": {"enabled": False}},
                "paths": {"state_dir": ".claude/legacy-state"},
            }
        ),
        encoding="utf-8",
    )
    _stub_session_start_dependencies(monkeypatch, tmp_path)

    audit_bootstrap.main()

    output_lines = capsys.readouterr().out.splitlines()
    assert len(output_lines) == 1
    assert "audit-flags.local.json" in output_lines[0]
    assert "features.quality_gate" in output_lines[0]
    assert "paths.state_dir" in output_lines[0]


def test_main_notice_for_moved_base_keys_says_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """配布 base に旧キーが残っている場合は「読み込まれない」と案内する。"""
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    (config_dir / "audit-flags.json").write_text(
        json.dumps({"features": {"quality_gate": {"block_on_failed_test": False}}}),
        encoding="utf-8",
    )
    _stub_session_start_dependencies(monkeypatch, tmp_path)

    audit_bootstrap.main()

    output_lines = capsys.readouterr().out.splitlines()
    assert len(output_lines) == 1
    assert "audit-flags.json" in output_lines[0]
    assert "features.quality_gate" in output_lines[0]
    assert "読み込まれません" in output_lines[0]


def test_main_notice_mentions_both_files_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """local と base の両方に旧キーがあっても案内は 1 行にまとまる。"""
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    (config_dir / "audit-flags.local.json").write_text(
        json.dumps({"features": {"quality_gate": {"enabled": False}}}), encoding="utf-8"
    )
    (config_dir / "audit-flags.json").write_text(
        json.dumps({"paths": {"state_dir": ".claude/state"}}), encoding="utf-8"
    )
    _stub_session_start_dependencies(monkeypatch, tmp_path)

    audit_bootstrap.main()

    output_lines = capsys.readouterr().out.splitlines()
    assert len(output_lines) == 1
    assert "audit-flags.local.json" in output_lines[0]
    assert "audit-flags.json" in output_lines[0]
    assert "features.quality_gate" in output_lines[0]
    assert "paths.state_dir" in output_lines[0]


def test_find_moved_quality_gates_keys_ignores_non_dict_feature_values(tmp_path: Path) -> None:
    """非 dict 値は誤検出防止のため無視し、案内対象にしない。"""
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    (config_dir / "audit-flags.local.json").write_text(
        json.dumps({"features": {"quality_gate": False}, "paths": {"state_dir": 1}}),
        encoding="utf-8",
    )

    assert audit_bootstrap.find_moved_quality_gates_keys(str(tmp_path)) == {}


def test_main_does_not_print_notice_without_moved_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """移動済みキーが無い場合は既存どおり stdout に何も出さない。"""
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    (config_dir / "audit-flags.local.json").write_text(
        json.dumps({"features": {"route_audit": {"enabled": False}}}),
        encoding="utf-8",
    )
    _stub_session_start_dependencies(monkeypatch, tmp_path)

    audit_bootstrap.main()

    assert capsys.readouterr().out == ""


def test_find_moved_quality_gates_keys_ignores_invalid_json(tmp_path: Path) -> None:
    """壊れた旧設定を読んでも例外を出さず、移動済みキーなしとして扱う。"""
    config_dir = tmp_path / ".claude" / "config" / "audit"
    config_dir.mkdir(parents=True)
    (config_dir / "audit-flags.local.json").write_text("{invalid", encoding="utf-8")

    assert audit_bootstrap.find_moved_quality_gates_keys(str(tmp_path)) == {}
