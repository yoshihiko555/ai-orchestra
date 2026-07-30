"""機密情報マスキングの共通ロジック（audit-prompt.py / audit-cli.py で共用）。"""

from __future__ import annotations

import re
from collections.abc import Callable

# 汎用 key=value / key: value のトリガー語。
# - トリガー語（api_key/token/password/secret/credential）の前後に任意の
#   英数字・アンダースコア・ハイフンを許容し、`AWS_SECRET_ACCESS_KEY` の
#   ようにトリガー語が識別子の一部に埋め込まれた複合トークンも捕捉する
#   （packages/loop-harness/lib/loop_common.py の強化パターンを移植）。
# - トリガー語直後の任意の引用符 1 文字（`"api_key": "..."` のような JSON
#   クォート付きキー）を許容してから `[:=]` を探す（Issue #134 レビュー指摘:
#   クォートキーが従来マッチしなかった）。
# - 値はクォート文字列（空白・カンマを含んでよい）またはカンマ/セミコロン/
#   改行までの非クォート値のいずれかを捕捉し、引用符付き・複数語の値が
#   最初の空白で途切れないようにする。
_SECRET_KV_PATTERN = re.compile(
    r"\b([A-Za-z0-9_-]{0,20}(?:api[_-]?key|token|password|secret|credential)"
    r"[A-Za-z0-9_-]{0,20})[\"']?\s*[:=]\s*"
    r"(\"[^\"]*\"|'[^']*'|[^,;\n]+)",
    re.IGNORECASE,
)

# トリガー語がそのまま（複合されずに単独で）使われているキー名。これらは
# 値の形にかかわらず常にマスクする（fail-safe を崩さない）。
_BARE_TRIGGER_WORDS = frozenset({"apikey", "token", "password", "secret", "credential"})

# 数値・真偽値・null 等のリテラルのみからなる値。複合キー（下記 _mask_secret_kv
# 参照）でこの形の値は、秘匿値ではなく一般設定である可能性が高いとみなす。
_NON_SECRET_VALUE_PATTERN = re.compile(
    r"^(?:\d+|true|false|yes|no|on|off|null|none)$", re.IGNORECASE
)


def _mask_secret_kv(match: re.Match[str]) -> str:
    """key=value / key: value 形式の秘匿マッチを判定してマスクする。

    誤検知抑制（Issue #134 レビュー指摘）: `max_tokens=4096` や
    `token_count=123`、`passwordless=true` のように、トリガー語を含む複合
    キー名（token_count, max_tokens, passwordless 等）で値が単純な数値・
    真偽値・null 等のリテラルである場合は、秘匿値ではなく一般的な非機密設定
    と判断してマスクしない。

    一方、`token` / `password` / `secret` / `credential` / `api_key` そのもの
    （複合されていないトリガー語単体をキーに持つ場合）は、値の形に
    かかわらず常にマスクする（fail-safe を崩さない）。クォート付きの値は
    複合キーであっても常にマスクする（数値・真偽値の見た目をした文字列の
    秘密値を取りこぼさないため）。
    """
    key = match.group(1)
    value = match.group(2)
    normalized_key = re.sub(r"[_-]", "", key).lower()
    is_quoted_value = value.startswith(('"', "'"))
    is_compound_key = normalized_key not in _BARE_TRIGGER_WORDS
    if is_compound_key and not is_quoted_value and _NON_SECRET_VALUE_PATTERN.match(value):
        return match.group(0)
    return "[REDACTED]"


# 機密情報パターン（API キー・トークン・パスワード・クラウド認証情報・秘密鍵・接続文字列）。
# 各エントリは (pattern, replacement) のタプル。replacement は固定文字列
# または re.Match を受け取り置換後文字列を返す callable のいずれか。
SECRET_PATTERNS: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (_SECRET_KV_PATTERN, _mask_secret_kv),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE), "[REDACTED]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "[REDACTED]"),
    # AWS Access Key ID (AKIA/ASIA/A3T 等)
    (re.compile(r"\b(AKIA|ASIA|A3T)[A-Z0-9]{16}\b"), "[REDACTED]"),
    # Google API Key
    (re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"), "[REDACTED]"),
    # Azure SAS token
    (re.compile(r"\bSharedAccessSignature\s*=\s*\S+", re.IGNORECASE), "[REDACTED]"),
    # 接続文字列形式の認証情報（postgres://user:pass@host, mysql://... 等）。
    # user:pass@ を含む URL のみ対象にし、認証情報を含まない通常の URL
    # （https://example.com 等）は対象外にする。
    (re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:@/]+:[^\s@/]+@\S+"), "[REDACTED]"),
    # PEM private key block（BEGIN から END までをブロック全体で 1 マッチにする。
    # ヘッダー行のみだと鍵本文が残留するため re.DOTALL で改行をまたいで捕捉する）
    (
        re.compile(
            r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"
            r".*?"
            r"-----END (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED]",
    ),
]


def mask_secrets(text: str) -> str:
    """テキストから既知の機密情報パターンをマスクする。

    Args:
        text: 検査対象のテキスト。

    Returns:
        マスク済みテキスト。該当しない場合は元の文字列を返す。
    """
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
