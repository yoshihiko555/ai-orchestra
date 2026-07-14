"""check_image_gen_enabled.py のテスト（EV-16, Issue #133）。

codex.enabled kill-switch が image-generation パッケージにも波及することを
検証する。実 CLI（codex）は呼ばず、cli-tools.yaml の読み込み結果のみで判定する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

check_image_gen_enabled = load_module(
    "check_image_gen_enabled",
    "packages/image-generation/scripts/check_image_gen_enabled.py",
)


def _write_cli_tools_yaml(project_dir: Path, *, enabled: bool) -> None:
    config_dir = project_dir / ".claude" / "config" / "agent-routing"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli-tools.yaml").write_text(
        f"codex:\n  enabled: {str(enabled).lower()}\n",
        encoding="utf-8",
    )


def _write_cli_tools_local_yaml(project_dir: Path, *, enabled: bool) -> None:
    config_dir = project_dir / ".claude" / "config" / "agent-routing"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli-tools.local.yaml").write_text(
        f"codex:\n  enabled: {str(enabled).lower()}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolate_orchestra_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """デフォルトでは実リポジトリの AI_ORCHESTRA_DIR を切り離す。

    fallback（AI_ORCHESTRA_DIR/packages/agent-routing/config/cli-tools.yaml）を
    使うテストだけ個別に setenv する。
    """
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)


# EV-16
def test_codex_enabled_true_prints_enabled_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """codex.enabled: true → ENABLED / exit 0。"""
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    _write_cli_tools_yaml(tmp_path, enabled=True)

    exit_code = check_image_gen_enabled.main(["--project", str(tmp_path)])

    out = capsys.readouterr().out
    assert exit_code == check_image_gen_enabled.EXIT_ENABLED
    assert out.strip() == "ENABLED"


# EV-16
def test_codex_enabled_false_prints_disabled_and_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """codex.enabled: false → DISABLED / exit 3, stderr に理由。"""
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    _write_cli_tools_yaml(tmp_path, enabled=False)

    exit_code = check_image_gen_enabled.main(["--project", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == check_image_gen_enabled.EXIT_DISABLED
    assert captured.out.strip() == "DISABLED"
    assert "codex.enabled: false" in captured.err


# EV-16
def test_base_true_local_false_overrides_to_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """base true + .local.yaml で false 上書き → DISABLED（レイヤードマージの検証）。"""
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    _write_cli_tools_yaml(tmp_path, enabled=True)
    _write_cli_tools_local_yaml(tmp_path, enabled=False)

    exit_code = check_image_gen_enabled.main(["--project", str(tmp_path)])

    assert exit_code == check_image_gen_enabled.EXIT_DISABLED
    assert capsys.readouterr().out.strip() == "DISABLED"


# EV-16
def test_base_false_local_true_overrides_to_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """base false + .local.yaml で true 上書き → ENABLED。"""
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    _write_cli_tools_yaml(tmp_path, enabled=False)
    _write_cli_tools_local_yaml(tmp_path, enabled=True)

    exit_code = check_image_gen_enabled.main(["--project", str(tmp_path)])

    assert exit_code == check_image_gen_enabled.EXIT_ENABLED
    assert capsys.readouterr().out.strip() == "ENABLED"


# EV-16
def test_missing_codex_section_falls_back_to_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """codex セクション欠落 → ENABLED（後方互換フォールバック）。"""
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    config_dir = tmp_path / ".claude" / "config" / "agent-routing"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli-tools.yaml").write_text("antigravity:\n  enabled: true\n", encoding="utf-8")

    exit_code = check_image_gen_enabled.main(["--project", str(tmp_path)])

    assert exit_code == check_image_gen_enabled.EXIT_ENABLED
    assert capsys.readouterr().out.strip() == "ENABLED"


# EV-16
def test_missing_cli_tools_yaml_fails_open_to_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cli-tools.yaml 自体が存在しない（AI_ORCHESTRA_DIR 未設定）→ ENABLED（fail-open）。"""
    exit_code = check_image_gen_enabled.main(["--project", str(tmp_path)])

    assert exit_code == check_image_gen_enabled.EXIT_ENABLED
    assert capsys.readouterr().out.strip() == "ENABLED"


def test_hook_common_import_failure_fails_open_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """hook_common が import できない予期しないエラー時も fail-open（ENABLED / exit 0 / stderr 警告）。"""

    def _raise_import_error(project_dir: str) -> bool:
        raise ImportError("hook_common unavailable")

    monkeypatch.setattr(check_image_gen_enabled, "check_enabled", _raise_import_error)

    exit_code = check_image_gen_enabled.main(["--project", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == check_image_gen_enabled.EXIT_ENABLED
    assert captured.out.strip() == "ENABLED"
    assert "Warning" in captured.err
