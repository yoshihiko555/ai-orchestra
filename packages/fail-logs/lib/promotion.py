"""昇格する文面の作成と昇格境界の検査（設計 §10.4・§10.6）。

昇格先に書くのは見出し・一般化した回避策・マーカーだけで、根拠（セッション ID・PR）は書かない。
書く直前に伏せ字を再びかけ、URL・メールアドレス・ホームディレクトリのパス・IP アドレス・ホスト名らしき語を検出し、
文字種を検査し、リポジトリの公開範囲を確かめる。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

import lessons_model as model
from sanitize import has_rejected_chars, neutralize

GH_TIMEOUT_SECONDS = 10.0
VISIBILITY_UNKNOWN = "unknown"
PRIVATE_VISIBILITIES = frozenset({"private", "internal"})
MAX_TITLE_CHARS = 80
MAX_AVOIDANCE_CHARS = 600

_FINDING_PATTERNS = (
    ("url", re.compile(r"\bhttps?://[^\s)>\]]{1,512}")),
    ("email", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}")),
    ("home_path", re.compile(r"(?:/Users/|/home/|~/)[^\s'\"]{1,256}")),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    (
        "hostname",
        re.compile(
            r"\b(?:[A-Za-z0-9-]{1,63}\.){1,8}"
            r"(?:com|net|org|io|dev|jp|co|ai|app|cloud|internal|local|corp|lan|example)\b",
            re.IGNORECASE,
        ),
    ),
)

# 昇格先（全 CLI が信頼する指示）に入ると危ない、コマンドや強い命令に見える語。
_COMMAND_LIKE = re.compile(
    r"`|\|\s*(?:sh|bash|zsh)\b|\b(?:curl|wget|sudo|eval|exec)\b|\brm\s+-[a-z]*r|"
    r"\b(?:bash|sh)\s+-c\b|>\s*/|\bchmod\s+[0-7]{3,4}",
    re.IGNORECASE,
)

Runner = Callable[..., subprocess.CompletedProcess]


def compose_text(lesson: dict) -> str:
    """昇格する文面（1 行の箇条書き + マーカー）を作る。山括弧は中和し、マーカーだけを本物にする。"""
    title = " ".join(str(lesson.get("title", "")).split())[:MAX_TITLE_CHARS]
    avoidance = " ".join(str(lesson.get("avoidance", "")).split())[:MAX_AVOIDANCE_CHARS]
    return f"- **{neutralize(title)}** — {neutralize(avoidance)} {model.marker_for(lesson['lid'])}"


def detect_findings(text: str, masked: str) -> list[dict]:
    """伏せ字で変わった箇所と、公開してよいか人が確かめるべき語を返す。"""
    findings: list[dict] = []
    if masked != text:
        findings.append({"kind": "masked", "match": "伏せ字で置き換えた機密情報がある"})
    for kind, pattern in _FINDING_PATTERNS:
        for match in pattern.finditer(masked):
            findings.append({"kind": kind, "match": match.group(0)[:120]})
    for match in _COMMAND_LIKE.finditer(masked):
        findings.append({"kind": "command_like", "match": match.group(0)[:120]})
    if has_rejected_chars(masked):
        findings.append({"kind": "rejected_chars", "match": "制御文字・書式文字などを含む"})
    return findings


def fence_for(text: str) -> str:
    """コードブロックのフェンスを、文面の中で最も長いバッククォートの並びより長くする。"""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def repo_visibility(project_dir: str, runner: Runner = subprocess.run) -> str:
    """``gh repo view --json visibility`` で公開範囲を得る。失敗したら ``unknown``（公開として扱う）。"""
    try:
        result = runner(
            ["gh", "repo", "view", "--json", "visibility", "--jq", ".visibility"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            cwd=project_dir,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return VISIBILITY_UNKNOWN
    value = (result.stdout or "").strip().lower()
    if result.returncode != 0 or not value:
        return VISIBILITY_UNKNOWN
    return value


def is_public(visibility: str) -> bool:
    """公開（または不明）か。不明は公開として扱う（設計 §10.4）。"""
    return visibility not in PRIVATE_VISIBILITIES


def check_text(lesson: dict, masker: model.Masker, visibility: str) -> dict:
    """``check-text`` の結果（文面・検出・公開範囲）を作る。"""
    text = compose_text(lesson)
    masked = masker(text)
    if model.marker_for(lesson["lid"]) not in masked:
        masked = f"{masked} {model.marker_for(lesson['lid'])}"
    return {
        "lid": lesson["lid"],
        "text": masked,
        "findings": detect_findings(text, masked),
        "visibility": visibility,
        "public": is_public(visibility),
    }


def issue_body(lesson: dict, text: str) -> str:
    """ガードの Issue の本文（固定の雛形。候補の文面はコードブロックに入れる）（設計 §10.6）。"""
    fence = fence_for(text)
    return "\n".join(
        [
            "> この Issue は、LLM またはエージェントが作った教訓候補から作った。"
            "自律エンジン（`/loop-issue`・TAKT）に渡す前に、人が内容を確かめること。",
            "",
            "## 教訓候補",
            "",
            fence,
            text,
            fence,
            "",
            "## やること",
            "",
            "- この教訓が効くガード（テスト・hook・lint）を設計して追加する",
            "",
            f"fail-logs の候補 ID: {lesson['lid']}",
        ]
    )
