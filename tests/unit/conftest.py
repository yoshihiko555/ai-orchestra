"""unit テスト共通フィクスチャ。"""

from __future__ import annotations

import sys

import pytest

from tests.module_loader import load_module

# scripts/ を sys.path に載せ、lib.facet_builder を import 可能にする。
load_module("orchestra_manager", "scripts/orchestra-manager.py")


@pytest.fixture(autouse=True)
def disable_facet_markdown_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """facet build の prettier 整形を無効化する。

    整形の有無は prettier が解決できるかどうかに依存するため、有効なままだと
    ローカル（prettier あり）と CI（なし）で生成物が変わり、テストが不安定になる。
    整形そのものを検証するテストは、このフィクスチャを明示的に解除する。
    """
    facet_builder = sys.modules.get("lib.facet_builder")
    if facet_builder is None:
        return
    monkeypatch.setattr(
        facet_builder,
        "format_markdown_files",
        lambda paths, cwd: True,
    )
