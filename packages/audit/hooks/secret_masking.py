"""機密情報マスキングの共通ロジック。

audit-prompt.py / audit-cli.py と agent-routing の jev_router.py（Jev への送信前）で共用する。
"""

from __future__ import annotations

import re
from collections.abc import Callable

# 汎用 key=value / key: value のトリガー語。
# - トリガー語（api_key/token/password/passwd/pwd/secret/credential）の前後に任意の
#   英数字・アンダースコア・ハイフンを許容し、`AWS_SECRET_ACCESS_KEY` の
#   ようにトリガー語が識別子の一部に埋め込まれた複合トークンも捕捉する
#   （packages/loop-harness/lib/loop_common.py の強化パターンを移植）。
# - トリガー語直後の任意の引用符 1 文字（`"api_key": "..."` のような JSON
#   クォート付きキー）を許容してから `[:=]` を探す（Issue #134 レビュー指摘:
#   クォートキーが従来マッチしなかった）。
# - 値はクォート文字列（空白・カンマ・エスケープシーケンスを含んでよい）
#   またはカンマ/セミコロン/改行までの非クォート値のいずれかを捕捉し、
#   引用符付き・複数語の値が最初の空白やエスケープ済み引用符で
#   途切れないようにする。
_SECRET_KV_PATTERN = re.compile(
    r"\b([A-Za-z0-9_-]{0,20}(?:api[_-]?key|token|password|passwd|pwd|secret|credential)"
    r"[A-Za-z0-9_-]{0,20})[\"']?\s*[:=]\s*"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^,;\n]+)",
    re.IGNORECASE,
)

# トリガー語がそのまま（複合されずに単独で）使われているキー名。これらは
# 値の形にかかわらず常にマスクする（fail-safe を崩さない）。
_BARE_TRIGGER_WORDS = frozenset(
    {"apikey", "token", "password", "passwd", "pwd", "secret", "credential"}
)

# 数値・真偽値・null 等のリテラルのみからなる値。複合キー（下記 _mask_secret_kv
# 参照）でこの形の値は、秘匿値ではなく一般設定である可能性が高いとみなす。
_NON_SECRET_VALUE_PATTERN = re.compile(
    r"^(?:\d+|true|false|yes|no|on|off|null|none)$", re.IGNORECASE
)

# END のない PEM は本文に限定し、後続の文章までマスクしない。
# - 本文行は base64 の文字だけの行（空行を含む）か、従来形式の暗号化ヘッダー
#   （Proc-Type / DEK-Info）の行。空白を含まない 1 語だけの行も本文とみなす
#   （伏せすぎる側に倒す）。
# - 改行は CRLF / LF / CR と、JSON エスケープ済みの `\r\n` / `\n` / `\r`。
# - 行内の空白を読む量指定子は 1 か所だけにする。前後の両方に `[ \t]*` を
#   置くと、行末の判定に失敗したときに空白の分け方を全通り試し、長い空白で
#   処理時間が 2 乗に伸びる。
_PEM_LABEL = r"(?:[A-Z0-9]+ )*PRIVATE KEY"
_PEM_LINE_BREAK = r"(?:\r\n|[\r\n]|\\r\\n|\\[rn])"
_PEM_UNTERMINATED_PATTERN = re.compile(
    rf"-----BEGIN {_PEM_LABEL}-----[ \t]*"
    rf"(?:{_PEM_LINE_BREAK}[ \t]*"
    r"(?:[A-Za-z0-9+/=]+[ \t]*|(?:Proc-Type|DEK-Info):[^\r\n\\]*)?"
    rf"(?={_PEM_LINE_BREAK}|[\"']|\Z))*"
)


def _mask_secret_kv(match: re.Match[str]) -> str:
    """key=value / key: value 形式の秘匿マッチを判定してマスクする。

    誤検知抑制（Issue #134 レビュー指摘）: `max_tokens=4096` や
    `token_count=123`、`passwordless=true` のように、トリガー語を含む複合
    キー名（token_count, max_tokens, passwordless 等）で値が単純な数値・
    真偽値・null 等のリテラルである場合は、秘匿値ではなく一般的な非機密設定
    と判断してマスクしない。

    一方、`token` / `password` / `passwd` / `pwd` / `secret` / `credential` / `api_key` そのもの
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
    # PEM 秘密鍵は key=value より先に伏せる。後にすると、`secret: -----BEGIN ...`
    # の値の 1 行目（ヘッダー行）だけが key=value で伏せられ、PEM として一致
    # しなくなって本文が残る。
    # 先にブロック全体を捕捉し、JSON のエスケープ済み改行にも対応する。
    # 本文は `[^-]` と「ダッシュの後にダッシュが続かない」`-(?!-)` だけで構成する。
    # 本文は PEM では base64 のためダッシュを含まず、最初の `-----`（END マーカー
    # 等の行）で必ず停止する。`.*?` のままだと END が見つからない入力で、BEGIN の
    # 出現ごとに末尾まで読んで入力長の 2 乗に近い時間かかる（Issue #531）。
    (
        re.compile(
            rf"-----BEGIN {_PEM_LABEL}-----"
            r"(?:[^-]|-(?!-))*?"
            rf"-----END {_PEM_LABEL}-----",
            re.DOTALL,
        ),
        "[REDACTED]",
    ),
    # END が欠けた PEM は、BEGIN と本文・従来形式の暗号化ヘッダーだけを捕捉する。
    (_PEM_UNTERMINATED_PATTERN, "[REDACTED]"),
    # OpenAI などの API キー
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    # トリガー語を含むキーと値
    (_SECRET_KV_PATTERN, _mask_secret_kv),
    # Bearer 認証（RFC 6750 の b64token 文字集合）
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE), "[REDACTED]"),
    # 単独の JWT（署名なしも含む。ヘッダーとペイロードは JSON 形式に限定）
    # \b は "-" の後でも再試行して長い非一致入力の走査が 2 乗になるため、
    # base64url 文字の直後からは開始しない（従来と異なり "-" 直後の JWT も対象外）。
    # "." を含まないセグメントには所有的量指定子を使い、後戻りを防ぐ。
    (
        re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]++\.eyJ[A-Za-z0-9_-]++\.[A-Za-z0-9_-]*"),
        "[REDACTED]",
    ),
    # GitHub の従来形式トークン（長い値も末尾まで捕捉する）
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"), "[REDACTED]"),
    # GitHub の fine-grained PAT
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"), "[REDACTED]"),
    # GitLab の PAT
    (re.compile(r"\bglpat-[A-Za-z0-9_.-]{20,}"), "[REDACTED]"),
    # Slack のトークン（文章で使われる xoxo は対象外）
    (re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    # Basic 認証は Authorization キーの後だけを対象にし、一般の文章を誤検知しない。
    (
        re.compile(r"\b(Authorization[\"']?\s*[:=]\s*[\"']?)Basic\s+[^\s\"',;]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    # AWS Access Key ID (AKIA/ASIA/A3T 等)
    (re.compile(r"\b(AKIA|ASIA|A3T)[A-Z0-9]{16}\b"), "[REDACTED]"),
    # Google API Key
    (re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"), "[REDACTED]"),
    # Azure SAS token
    (re.compile(r"\bSharedAccessSignature\s*=\s*\S+", re.IGNORECASE), "[REDACTED]"),
    # 接続文字列形式の認証情報（postgres://user:pass@host, mysql://... 等）。
    # user:pass@ を含む URL のみ対象にし、認証情報を含まない通常の URL
    # （https://example.com 等）は対象外にする。
    # scheme 部は {0,32} で上限を付ける（実際の URL scheme は十分に短い）。
    # 上限がないと、開始位置ごとの `[A-Za-z0-9+.-]*` の長い読みとバック
    # トラックで入力長の 2 乗に近い時間かかる（Issue #531）。
    (re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{0,32}://[^\s:@/]+:[^\s@/]+@\S+"), "[REDACTED]"),
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
