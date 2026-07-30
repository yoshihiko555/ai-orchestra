"""packages/quality-gates/README.md の存在と必須記載事項を検証する（EV-24）。"""

from __future__ import annotations

from pathlib import Path

_README_PATH = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_exists() -> None:
    assert _README_PATH.is_file(), f"{_README_PATH} が存在しません"


def test_readme_documents_hook_inventory() -> None:
    """独自 README を持つ他パッケージ（audit / fail-logs 等）と同様に hook 一覧を記載する。"""
    content = _README_PATH.read_text(encoding="utf-8")

    for hook_name in (
        "check-context-optimization.py",
        "post-implementation-review.py",
        "post-test-analysis.py",
        "lint-on-save.py",
        "test-tampering-detector.py",
        "test-gate-checker.py",
        "turn-end-summary.py",
        "evaluation-set-checker.py",
    ):
        assert hook_name in content, f"hook {hook_name} が README に記載されていません"


def test_readme_documents_responsibility_and_config_keys() -> None:
    content = _README_PATH.read_text(encoding="utf-8")

    # 責務セクション
    assert "何をするか" in content

    # 設定キー（quality_gate.*）
    assert "quality_gate.enabled" in content
    assert "quality_gate.block_on_failed_test" in content
