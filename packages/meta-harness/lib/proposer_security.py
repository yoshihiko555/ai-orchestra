#!/usr/bin/env python3
"""proposer 出力経路の secret 検知（Sec11-3-6 L2/L3）。

主対策は L1（資格情報の最小化, proposer_backend）であり、本モジュールは唯一の
exfil 経路である「proposal / overlay の出力」に重ねる**検知層**である。
エンコード・分割による回避が可能なため単独対策とはみなさない（設計固定）。

- L2: staged auth.json の `refresh_token` に置いた canary 値の変形（平文 / base64 /
  hex / URL エンコード）を proposal 全文 + overlay 全ファイルから照合する。
- L3: 汎用 secret パターン（`sk-` 系 API key・JWT 3 セグメント形式等）をスキャンする。
  登録時に加え promote 前提条件でも再実行し、スキャン導入前の登録候補を遡及的に防御する。
"""

from __future__ import annotations

import base64
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import redaction  # noqa: E402

DETECTOR_CANARY = "L2_canary"
DETECTOR_SECRET_SCAN = "L3_secret_scan"

# L3: redaction の実績パターン（sk-, ghp_, AKIA, PEM 等）を検知層として再利用し、
# 設計が明示する JWT 3 セグメント形式を追加する。JWT は `eyJ`（`{"` の base64）で
# 始まる 3 セグメント構造に限定し、汎用 base64 blob による誤検知を抑える。
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")
_L3_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    *redaction.REDACTION_PATTERNS,
    ("JWT (3-segment)", _JWT_PATTERN),
]


@dataclass(frozen=True)
class SecurityViolation:
    """検知した出力経路 secret の 1 件。"""

    detector: str
    reason: str


def canary_variants(canary: str) -> dict[str, str]:
    """canary 値の検査対象エンコード変形を返す（空値は探索から除外する）。"""
    raw = canary.encode("utf-8")
    variants = {
        "plaintext": canary,
        "base64": base64.b64encode(raw).decode("ascii"),
        "base64-nopad": base64.b64encode(raw).decode("ascii").rstrip("="),
        "base64url": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
        "hex": raw.hex(),
        "url": urllib.parse.quote(canary, safe=""),
    }
    return {name: value for name, value in variants.items() if value}


def scan_text_for_canary(text: str, canary: str | None) -> list[str]:
    """`text` に canary の変形が含まれれば、hit した変形名を返す。"""
    if not canary:
        return []
    return [name for name, value in canary_variants(canary).items() if value in text]


def scan_text_for_secrets(text: str) -> list[str]:
    """`text` に含まれる汎用 secret パターン名（L3）を返す。"""
    return [name for name, pattern in _L3_SECRET_PATTERNS if pattern.search(text)]


def redact_for_storage(text: str, *, auth_canary: str | None = None) -> str:
    """rejected 保存用に L3 secret + canary + JWT をマスクする。

    `redaction.redact_secrets` は sk-/ghp_ 等をカバーするが JWT 3 セグメントと
    L2 canary は対象外のため、検知に成功した実 access token（JWT）や canary が
    quarantine ファイルへ平文で残らないよう、ここで追加のマスクを重ねる。
    """
    result = redaction.redact_secrets(text)
    result = _JWT_PATTERN.sub("[REDACTED:JWT (3-segment)]", result)
    for name, value in canary_variants(auth_canary or "").items():
        if value:
            result = result.replace(value, f"[REDACTED:auth canary ({name})]")
    return result


def scan_named_texts(
    named_texts: dict[str, str], *, auth_canary: str | None
) -> list[SecurityViolation]:
    """名前付きテキスト群を L2（canary）→ L3（secret）の順に走査する。

    `named_texts` は `{"proposal": <全文>, "overlay:<相対パス>": <本文>, ...}` を想定する。
    各テキストにつき、同一テキスト内で canary hit を優先し、無ければ汎用 secret hit を
    1 件報告する（テキストをまたいだ優先ではない）。
    """
    violations: list[SecurityViolation] = []
    for name, text in named_texts.items():
        canary_hits = scan_text_for_canary(text, auth_canary)
        if canary_hits:
            variants = ", ".join(canary_hits)
            violations.append(
                SecurityViolation(
                    detector=DETECTOR_CANARY,
                    reason=f"auth canary leaked into {name} (variants: {variants})",
                )
            )
            continue
        secret_hits = scan_text_for_secrets(text)
        if secret_hits:
            patterns = ", ".join(secret_hits)
            violations.append(
                SecurityViolation(
                    detector=DETECTOR_SECRET_SCAN,
                    reason=f"secret-like content in {name} (patterns: {patterns})",
                )
            )
    return violations
