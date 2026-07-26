"""docker-runtime unit test の共通フィクスチャ。

`context_hash()`（docker_runtime_cli.py）はプロセス内メモ化される（Issue #307
review）。テストは `tests.module_loader.load_module` 経由で各ファイルごとに
`docker_runtime_cli` を独立ロードするため、モジュールオブジェクトの実体は
ファイル/セッションの読み込み順に依存する。`sys.modules` を都度検索して現在
有効なインスタンスのキャッシュを明示的にクリアすることで、どのファイルが
どの順で読み込まれても、あるテストが残したキャッシュ済みハッシュを別のテスト
が観測しないようにする。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

_MODULE_NAME = "docker_runtime_cli"


def _clear_context_hash_cache() -> None:
    module = sys.modules.get(_MODULE_NAME)
    if module is not None and hasattr(module, "clear_context_hash_cache"):
        module.clear_context_hash_cache()


@pytest.fixture(autouse=True)
def _reset_context_hash_cache() -> Iterator[None]:
    _clear_context_hash_cache()
    yield
    _clear_context_hash_cache()
