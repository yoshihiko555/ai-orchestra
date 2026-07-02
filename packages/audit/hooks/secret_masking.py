"""機密情報マスキングの共通ロジック（audit-prompt.py / audit-cli.py で共用）。"""

from __future__ import annotations

import re

# 機密情報パターン（API キー・トークン・パスワード・クラウド認証情報・秘密鍵）
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"\b[A-Za-z0-9_-]{0,20}(api[_-]?key|token|password|secret|credential)\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    # AWS Access Key ID (AKIA/ASIA/A3T 等)
    re.compile(r"\b(AKIA|ASIA|A3T)[A-Z0-9]{16}\b"),
    # Google API Key
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    # Azure SAS token
    re.compile(r"\bSharedAccessSignature\s*=\s*\S+", re.IGNORECASE),
    # PEM private key block
    re.compile(r"-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
]


def mask_secrets(text: str) -> str:
    """テキストから既知の機密情報パターンをマスクする。

    Args:
        text: 検査対象のテキスト。

    Returns:
        マスク済みテキスト。該当しない場合は元の文字列を返す。
    """
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text
