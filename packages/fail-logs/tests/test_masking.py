"""lib/masking.py（強化版マスキングの読み込み・PII・伏せてから切り詰める処理）を検証する。

評価セット docs/evaluation/fail-logs.md の EV-27・EV-28・EV-51 の土台になる部品のテスト。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

masking = load_module("fail_logs_masking", "packages/fail-logs/lib/masking.py")

API_KEY_SAMPLE = "sk-" + "abcdefghijklmnopqrstuvwxyz012345"
AWS_KEY_SAMPLE = "AK" + "IA" + "IOSFODNN7EXAMPLE"
GITHUB_TOKEN_SAMPLE = "gh" + "p_" + "B" * 36
MODULE_FILE_NAME = "secret_" + "masking.py"


def _fake_orchestra(tmp_path: Path, body: str) -> Path:
    """AI_ORCHESTRA_DIR に見立てたディレクトリに、偽の強化版を置く。"""
    hooks = tmp_path / "packages" / "audit" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / MODULE_FILE_NAME).write_text(body, encoding="utf-8")
    return tmp_path


# EV-28: AI_ORCHESTRA_DIR の強化版を読み込み、既知の機密情報を伏せる。
def test_loads_strong_masker_from_orchestra_dir(monkeypatch) -> None:
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    masker = masking.load_masker()

    assert masker is not None
    masked = masker(f"a {API_KEY_SAMPLE} b {AWS_KEY_SAMPLE} c {GITHUB_TOKEN_SAMPLE}")
    assert API_KEY_SAMPLE not in masked
    assert AWS_KEY_SAMPLE not in masked
    assert GITHUB_TOKEN_SAMPLE not in masked


# EV-28: AI_ORCHESTRA_DIR が無ければ、このファイルからの相対パスで読み込む。
def test_falls_back_to_relative_path(monkeypatch) -> None:
    monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

    assert masking.load_masker() is not None


# EV-28: 読み込んだ関数がカナリアを伏せなければ「使えない」（None）。
def test_canary_failure_returns_none(monkeypatch, tmp_path) -> None:
    root = _fake_orchestra(tmp_path, "def mask_secrets(text):\n    return text\n")
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(root))

    assert masking.load_masker() is None


# EV-28: 読み込みで例外が出る・関数が無いときも None。
@pytest.mark.parametrize(
    "body",
    [
        "raise RuntimeError('broken module')\n",
        "def other(text):\n    return text\n",
        "def mask_secrets(text):\n    raise ValueError('boom')\n",
    ],
)
def test_broken_module_returns_none(monkeypatch, tmp_path, body: str) -> None:
    root = _fake_orchestra(tmp_path, body)
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(root))

    assert masking.load_masker() is None


# EV-51: ホームディレクトリのパス・メールアドレス・IPv4・URL の認証情報とクエリを伏せる。
@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("open /Users/alice/projects/x", "/Users/alice"),
        ("open /home/bob/.config", "/home/bob"),
        ("mail alice@example.com now", "alice@example.com"),
        ("host 192.168.10.20 down", "192.168.10.20"),
        ("clone https://user:pa55@git.example.com/r.git", "user:pa55"),
        ("get https://api.example.com/v1?key=abc&sig=xyz done", "key=abc"),
    ],
)
def test_mask_pii(text: str, leaked: str) -> None:
    masked = masking.mask_pii(text)

    assert leaked not in masked


def test_mask_pii_keeps_relative_structure() -> None:
    assert masking.mask_pii("/Users/alice/repo/file.py") == "~/repo/file.py"
    assert masking.mask_pii("plain text") == "plain text"


# EV-27: 伏せてから切り詰めるので、切り詰めの境界をまたぐ機密情報も残らない。
def test_mask_then_truncate_masks_before_cutting(monkeypatch) -> None:
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    masker = masking.load_masker()
    assert masker is not None
    text = "x" * 35 + " " + API_KEY_SAMPLE

    result = masking.mask_then_truncate(text, 40, masker)

    assert len(result) <= 40
    assert "sk-abc" not in result
    assert "abcdefghij" not in result


# EV-27: 固定の上限で切ったとき、切断位置で途中まで残った機密情報を落とす。
def test_mask_then_truncate_drops_partial_secret_at_pre_limit(monkeypatch) -> None:
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    masker = masking.load_masker()
    assert masker is not None
    # 固定の上限の位置で `sk-` の直後が 8 文字しか残らないように置く（パターンの最小長に届かない）。
    partial_len = 8
    filler = "a" * (masking.PRE_MASK_LIMIT_CHARS - len("sk-") - partial_len - 1) + " "
    text = filler + API_KEY_SAMPLE + " tail"

    result = masking.mask_then_truncate(text, masking.PRE_MASK_LIMIT_CHARS, masker)

    assert "sk-" not in result
    assert len(result) <= masking.PRE_MASK_LIMIT_CHARS - masking.PRE_MASK_TAIL_GUARD_CHARS


def test_mask_then_truncate_handles_empty_and_non_positive() -> None:
    assert masking.mask_then_truncate("", 10, str.upper) == ""
    assert masking.mask_then_truncate("abc", 0, str.upper) == ""
    assert masking.mask_then_truncate("abc", 2, str.upper) == "AB"


# EV-28: 古い版や差し替えた版（API キーしか伏せない）はカナリアで見分けて「使えない」にする。
def test_partial_masker_fails_multi_canary(monkeypatch, tmp_path) -> None:
    body = "import re\ndef mask_secrets(text):\n    return re.sub(r'sk-\\S+', '[REDACTED]', text)\n"
    root = _fake_orchestra(tmp_path, body)
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(root))

    assert masking.load_masker() is None


# EV-51: 空白区切りのフラグで渡す機密情報も伏せる。
@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("mysql --password hunter2 db", "hunter2"),
        ("tool --token=abc123def run", "abc123def"),
        ("tool --api-key 'k e y' run", "k e y"),
        ("curl -u admin:hunter2 https://x.example.com", "hunter2"),
        ("curl --user=admin:hunter2 https://x.example.com", "hunter2"),
    ],
)
def test_mask_pii_flag_secrets(text: str, leaked: str) -> None:
    assert leaked not in masking.mask_pii(text)


# NF-01 / NF-03: PII の正規表現は 64 KiB の悪意のある入力でも短時間で終わる（ReDoS の回帰防止）。
@pytest.mark.parametrize(
    "text",
    [
        "a." * 32768,
        "a." * 32767 + "@",
        "http://" * 9362,
        "1." * 32768,
        "://a:" * 13000,
        "--token " * 8000,
        " -u a:" * 10000,
    ],
)
def test_mask_pii_is_linear_on_adversarial_input(text: str) -> None:
    started = time.monotonic()
    masking.mask_pii(text)
    assert time.monotonic() - started < 1.0
