#!/usr/bin/env python3
"""単一ファイルの有界移行プリミティブ（fail-logs / skill-evolution 共通）。

「旧配置の 1 ファイルを新配置へ一回限り、末尾 N バイトに有界化して移行する」処理は
fail-logs（``log_migration.py``）と skill-evolution（``skill_evolution_common.py``）で
二重実装されていた。共通部分（claim による排他・行境界を保った有界 tail の決定・
確定 rename・stale claim の非破壊）だけをここへ抽出し、実際の書き込み方式（flock の
要否・write の分割方針・fchmod の有無など）は呼び出し側が ``writer`` として注入する。

書き込み方式を注入型にしている理由: fail-logs は複数 write を許容するストリーム
コピー（flock 排他が前提）、skill-evolution は単発 write のみで整合性を担保する設計
（flock なし）と、書き込み方式の前提そのものが異なるため、writer を共通化すると
どちらかの整合性前提を壊しかねない。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import BinaryIO

# 移行 claim/completion 名は試行ごとに一意にし、クラッシュで残った
# ``.migrating.*`` は上書き・削除せず、手動での確認と復旧対象として残す。
_MIGRATION_DIR_MODE = 0o700


def migrate_bounded_file(
    source_path: str,
    destination_path: str,
    *,
    max_bytes: int,
    writer: Callable[[BinaryIO, str], None],
) -> None:
    """source_path の内容を destination_path へ一回限り、有界で移行する。

    手順:
    1. source_path と destination_path の実体が同一なら何もしない
    2. source_path を ``<source_path>.migrating.<pid>-<monotonic_ns>`` へ原子的に
       rename して claim する（rename 失敗＝既に他プロセスが claim 済みとみなし
       何もせず return。stale な他 claim には一切触れない）
    3. ファイルサイズが max_bytes を超える場合、末尾 max_bytes に収まるよう先頭側の
       途中行を読み捨て、次の完全な行境界から移行対象を開始する
    4. ``writer(source, destination_path)`` を呼び出し、実際の書き込みを委譲する。
       writer は destination の open 方式・排他制御・write 分割方針を自由に選べる
    5. writer が例外を送出した場合はそのまま呼び出し元へ伝播させ、claim は
       ``.migrating.*`` のまま復旧用に残す（ここでは握りつぶさない。fail-open に
       するかどうかは呼び出し側ラッパーの責務）。destination 側の親ディレクトリ
       作成（``os.makedirs``）や destination の open 自体が失敗した場合も同様に
       例外が伝播し、claim は ``.migrating.*`` のまま残る
    6. writer が正常終了した場合のみ claim を ``<source_path>.migrated.<同一suffix>``
       へ確定 rename する
    """
    if os.path.realpath(destination_path) == os.path.realpath(source_path):
        return

    claim_suffix = f"{os.getpid()}-{time.monotonic_ns()}"
    migrating_path = f"{source_path}.migrating.{claim_suffix}"
    try:
        # rename は source を原子的に消すため、競合した別 process からは
        # source_path が消えて見え、その process 側は自然に no-op となる。
        os.rename(source_path, migrating_path)
    except OSError:
        return

    os.makedirs(os.path.dirname(destination_path), mode=_MIGRATION_DIR_MODE, exist_ok=True)
    with open(migrating_path, "rb") as source:
        file_size = os.fstat(source.fileno()).st_size
        if file_size > max_bytes:
            cut = file_size - max_bytes
            source.seek(cut - 1)
            boundary_byte = source.read(1)
            # cut がちょうど改行直後（完全な行の先頭）なら読み捨て不要。
            # 直前バイトが改行でなければ途中行なので readline() で部分行を読み捨てる。
            # 改行が見つからないまま EOF に到達した場合は writer に空データが渡る
            # （巨大な単一行の決定的な縮退動作）。
            if boundary_byte != b"\n":
                source.readline()

        writer(source, destination_path)

    os.rename(migrating_path, f"{source_path}.migrated.{claim_suffix}")
