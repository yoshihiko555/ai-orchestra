"""packages/quality-gates/README.md の存在と必須記載事項を検証する（EV-24）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

_README_PATH = Path(__file__).resolve().parents[1] / "README.md"
_JSON_FENCE_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


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


def test_readme_json_examples_are_valid_json() -> None:
    """```json フェンス内のコード例は、そのままコピーしても有効な JSON であること。

    Issue #134 レビュー指摘: opt-out 例の先頭に `// path/to/file.json` という
    JSON では無効な行コメントが混在しており、コピーすると `read_json_safe` が
    空辞書へフォールバックしていた。
    """
    content = _README_PATH.read_text(encoding="utf-8")
    json_blocks = _JSON_FENCE_RE.findall(content)
    assert json_blocks, "README に ```json コードブロックが見つかりません"

    for block in json_blocks:
        json.loads(block)  # 無効な JSON なら json.JSONDecodeError で失敗する


def test_readme_clarifies_threshold_scope() -> None:
    """test_file_threshold / test_line_threshold が test-gate-checker.py 専用で
    post-implementation-review.py には適用されないことを明記していることを確認する。

    Issue #134 レビュー指摘: 旧文言はレビュー提案にも適用されるかのように
    読めたが、post-implementation-review.py は固定定数（FILE_THRESHOLD /
    LINE_THRESHOLD）を使っており、この設定値では変更できない。
    """
    content = _README_PATH.read_text(encoding="utf-8")
    assert "test-gate-checker.py" in content
    assert "FILE_THRESHOLD" in content
    assert "LINE_THRESHOLD" in content
