"""強化版マスキングの読み込みと、PII の追加処理・伏せてから切り詰める処理（設計 §7.4）。

fail-logs は audit に依存しない方針のため、audit の強化版（``secret_masking.py``）を
パッケージとしては import せず、絶対パスからファイルとして読み込む。読み込みや
カナリアの確認に失敗したら「伏せ字が使えない」として ``None`` を返し、呼び出し側は
機密情報を含みうる項目を空にする（記録や LLM への受け渡しを原文で行わない）。
"""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Callable

Masker = Callable[[str], str]

# 伏せる前に切る固定の上限。巨大な本文でも hook の時間の予算を守る（設計 §3(h)・§7.4）。
PRE_MASK_LIMIT_CHARS = 64 * 1024
# 固定の上限で切ったときに、切断位置の手前で途中まで残った機密情報を落とす幅。
PRE_MASK_TAIL_GUARD_CHARS = 1024

_MASKING_MODULE_PARTS = ("packages", "audit", "hooks", "secret_" + "masking.py")
_MASKING_FUNCTION_NAME = "mask_secrets"
_MODULE_NAME = "fail_logs_strong_masking"

# 読み込んだ強化版が実際に伏せることを確かめる見本。古い版や差し替えた版を見分けるため、
# API キー・key=value・Bearer の 3 つの形のどれも原文が残らないことを求める。
_CANARY_TOKEN = "FailLogsCanary" + "0123456789abcdef"
_CANARIES = (
    ("sk-" + _CANARY_TOKEN, "sk-" + _CANARY_TOKEN),
    ("password=" + _CANARY_TOKEN, _CANARY_TOKEN),
    ("Authorization: Bear" + "er " + _CANARY_TOKEN, _CANARY_TOKEN),
)

# PII の正規表現は、64 KiB の入力でも時間が線形に収まるよう、量指定子に上限を付ける。
# メールアドレスのローカル部は、同じ文字種が続く区間の先頭からだけ照合する（後読み）。
# 区間の途中の位置から何度も読み直すと、`a.a.a.…` のような入力で 2 乗に伸びるため。
_URL_USERINFO = re.compile(r"(?<=://)[^/\s:@]{1,256}:[^/\s@]{1,512}@")
_URL_QUERY = re.compile(r"(https?://[^\s?#'\"]{1,512})\?[^\s#'\"]{0,4096}")
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@"
    r"[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}\b"
)
_HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s'\"]{1,256}")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# フラグで渡す機密情報は、引用符・エスケープを含めて値のシェル単語全体を伏せる。
# 閉じていない引用符は行末まで伏せる。各選択肢の先頭を分け、長さの上限なしでも線形に照合する。
# 強化版の key=value の形（`[:=]` 区切り）に当たらないものを補う。`mysql -pSECRET` のような
# 1 文字のフラグに続けて書く形は、`cp -pR` などと区別できないため伏せない（設計 §7.4 の限界）。
_SHELL_WORD = r"""(?:"(?:[^"\\\n]|\\.)*"?|'[^'\n]*'?|\\.|[^\s"'\\])+"""
_FLAG_SECRET = re.compile(
    r"(?i)(--?(?:password|passwd|token|secret|api[-_]?key|auth[-_]?token)(?:=|\s+))" + _SHELL_WORD
)
_USER_PASS_FLAG = re.compile(r"((?:^|\s)(?:-u|--user)(?:=|\s+))" + f"({_SHELL_WORD})")


def _candidate_module_path() -> str | None:
    """強化版のファイルのパスを返す。``$AI_ORCHESTRA_DIR`` を優先し、無ければこのファイルからの相対パス。"""
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if orchestra_dir:
        path = os.path.join(orchestra_dir, *_MASKING_MODULE_PARTS)
        if os.path.isfile(path):
            return path
    here = os.path.dirname(os.path.abspath(__file__))
    relative = os.path.normpath(os.path.join(here, "..", "..", *_MASKING_MODULE_PARTS[1:]))
    if os.path.isfile(relative):
        return relative
    return None


def load_strong_masker(path: str | None = None) -> Masker | None:
    """強化版の ``mask_secrets`` を読み込み、カナリアで確かめてから返す。

    Args:
        path: 強化版のファイルのパス。省略時は ``_candidate_module_path`` で決める。

    Returns:
        伏せる関数。読み込み・関数の取得・カナリアのいずれかで失敗したら None。
    """
    module_path = path or _candidate_module_path()
    if not module_path:
        return None
    try:
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, module_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        function = getattr(module, _MASKING_FUNCTION_NAME, None)
        if not callable(function):
            return None
        results = [(function(f"x {sample} y"), leaked) for sample, leaked in _CANARIES]
    except Exception:  # noqa: BLE001 - 読み込みの失敗はすべて「使えない」に倒す
        return None
    if any(not isinstance(masked, str) or leaked in masked for masked, leaked in results):
        return None

    def _strong(text: str) -> str:
        return str(function(text))

    return _strong


def _mask_user_pass(match: re.Match[str]) -> str:
    """-u / --user の値は、認証情報の区切りを持つ単語だけ伏せる。"""
    return match.group(1) + "[REDACTED]" if ":" in match.group(2) else match.group(0)


def mask_pii(text: str) -> str:
    """URL の認証情報とクエリ・メールアドレス・ホームディレクトリのパス・IPv4 アドレス・
    空白区切りのフラグで渡す機密情報を伏せる。該当する記号を含まない入力では照合を省く。
    """
    if "://" in text:
        text = _URL_USERINFO.sub("[REDACTED]@", text)
        text = _URL_QUERY.sub(r"\1?[REDACTED]", text)
    if "@" in text:
        text = _EMAIL.sub("[EMAIL]", text)
    if "/Users/" in text or "/home/" in text:
        text = _HOME_PATH.sub("~", text)
    if "-" in text:
        text = _FLAG_SECRET.sub(r"\1[REDACTED]", text)
        text = _USER_PASS_FLAG.sub(_mask_user_pass, text)
    return _IPV4.sub("[IP]", text)


def load_masker(path: str | None = None) -> Masker | None:
    """強化版と PII の追加処理を合わせた伏せる関数を返す。強化版が使えなければ None。"""
    strong = load_strong_masker(path)
    if strong is None:
        return None

    def _mask(text: str) -> str:
        return mask_pii(strong(text))

    return _mask


def mask_then_truncate(text: str, max_chars: int, masker: Masker) -> str:
    """固定の上限で切り、伏せてから、``max_chars`` で切り詰める。

    切り詰めで機密情報の途中が切れて正規表現に当たらなくなるのを防ぐため、伏せてから切る。
    固定の上限で切った場合は、切断位置の手前に途中まで残りうる機密情報を落とすため、
    伏せた結果の末尾の一定幅を捨ててから切り詰める。

    Raises:
        Exception: ``masker`` が送出した例外はそのまま伝える（呼び出し側で「使えない」扱いにする）。
    """
    if max_chars <= 0 or not text:
        return ""
    was_cut = len(text) > PRE_MASK_LIMIT_CHARS
    masked = masker(text[:PRE_MASK_LIMIT_CHARS])
    if was_cut:
        masked = masked[: max(0, len(masked) - PRE_MASK_TAIL_GUARD_CHARS)]
    return masked[:max_chars]
