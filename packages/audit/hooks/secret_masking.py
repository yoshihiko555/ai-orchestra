"""機密情報マスキングの共通ロジック（audit-prompt.py / audit-cli.py で共用）。"""

from __future__ import annotations

import re

# 機密情報パターン（API キー・トークン・パスワード・クラウド認証情報・秘密鍵・接続文字列）
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    # 汎用 key=value / key: value。
    # - トリガー語（api_key/token/password/secret/credential）の前後に任意の
    #   英数字・アンダースコア・ハイフンを許容し、`AWS_SECRET_ACCESS_KEY` の
    #   ようにトリガー語が識別子の一部に埋め込まれた複合トークンも捕捉する
    #   （packages/loop-harness/lib/loop_common.py の強化パターンを移植）。
    # - 値はクォート文字列（空白・カンマを含んでよい）またはカンマ/セミコロン/
    #   改行までの非クォート値のいずれかを捕捉し、引用符付き・複数語の値が
    #   最初の空白で途切れないようにする。
    re.compile(
        r"\b[A-Za-z0-9_-]{0,20}(?:api[_-]?key|token|password|secret|credential)"
        r"[A-Za-z0-9_-]{0,20}\s*[:=]\s*"
        r"(\"[^\"]*\"|'[^']*'|[^,;\n]+)",
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
    # 接続文字列形式の認証情報（postgres://user:pass@host, mysql://... 等）。
    # user:pass@ を含む URL のみ対象にし、認証情報を含まない通常の URL
    # （https://example.com 等）は対象外にする。
    re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:@/]+:[^\s@/]+@\S+"),
    # PEM private key block（BEGIN から END までをブロック全体で 1 マッチにする。
    # ヘッダー行のみだと鍵本文が残留するため re.DOTALL で改行をまたいで捕捉する）
    re.compile(
        r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"
        r".*?"
        r"-----END (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
        re.DOTALL,
    ),
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
