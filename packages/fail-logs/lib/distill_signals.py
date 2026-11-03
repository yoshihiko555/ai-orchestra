"""教訓化の前段判定（LLM を使わない。設計 docs/design/fail-logs.md §7.3）。

合図は、人間の介入（手動の拒否・中断・訂正）と、絞り込んだ機械の失敗（未解決・反復）の 2 種。
どれか 1 つでもあれば「合図あり」とし、教訓化セッションを呼ぶ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import failure_detector as fd
from command_signature import is_expected_nonzero, signature_head
from transcript_reader import KIND_TOOL_RESULT, KIND_TOOL_USE, ROLE_ASSISTANT, Message

SIGNAL_REJECT = "reject"
SIGNAL_INTERRUPT = "interrupt"
SIGNAL_CORRECTION = "correction"
SIGNAL_MACHINE = "machine"
STRONG_SIGNALS = frozenset({SIGNAL_REJECT, SIGNAL_INTERRUPT, SIGNAL_CORRECTION})

# 実機（Claude Code 2.1.317）で確かめた定型文（設計 §12・§15）。
DEFAULT_REJECT_MARKERS = (
    "The user doesn't want to proceed with this tool use.",
    "The user doesn't want to take this action right now.",
)
DEFAULT_INTERRUPT_MARKERS = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
)
# 訂正の語彙（文字列の部分一致。英語は小文字にして照合する）。評価用の記録で調整する。
DEFAULT_CORRECTION_CUES = (
    "そうじゃない",
    "じゃない",
    "そうではない",
    "ではなく",
    "しないで",
    "と言った",
    "決まりだ",
    "ダメ",
    "だめ",
    "そうではなく",
    "じゃなくて",
    "ではなくて",
    "違います",
    "違う",
    "ちがう",
    "やめて",
    "戻して",
    "勝手に",
    "頼んでない",
    "頼んでいない",
    "指示していない",
    "言ったのに",
    "って言った",
    "やり直して",
    "間違って",
    "おかしい",
    "余計な",
    "that's not what",
    "not what i asked",
    "that's wrong",
    "this is wrong",
    "you shouldn't",
    "you should not",
    "don't do that",
    "i didn't ask",
    "i did not ask",
    "revert that",
    "undo that",
    "stop doing",
    "i told you",
    "why did you",
)

# 意図した非ゼロ終了とみなすコマンドの既定（fail-logs.yaml の summary.expected_nonzero_commands と同じ）。
DEFAULT_EXPECTED_NONZERO = frozenset(
    {
        "grep",
        "rg",
        "test",
        "[",
        "diff",
        "cmp",
        "which",
        "pgrep",
        "git diff",
        "git grep",
        "git ls-files",
    }
)

_EXIT_CODE_LINE = re.compile(r"^Exit code (-?\d+)")
_FAILURE_TYPE_BY_KIND = {
    fd.KIND_TEST: fd.FAILURE_TEST,
    fd.KIND_LINT: fd.FAILURE_LINT,
    fd.KIND_CLI: fd.FAILURE_CLI,
}
TEST_LINT_TYPES = frozenset({fd.FAILURE_TEST, fd.FAILURE_LINT})


@dataclass
class Signal:
    kind: str
    index: int
    detail: str = ""


@dataclass
class SignalConfig:
    reject_markers: tuple[str, ...] = DEFAULT_REJECT_MARKERS
    interrupt_markers: tuple[str, ...] = DEFAULT_INTERRUPT_MARKERS
    correction_cues: tuple[str, ...] = DEFAULT_CORRECTION_CUES
    expected_nonzero_commands: frozenset[str] = frozenset()
    repeat_threshold: int = 2
    repeat_threshold_test: int = 3


def _is_reject(message: Message, markers: tuple[str, ...]) -> bool:
    if message.kind != KIND_TOOL_RESULT or not message.is_error:
        return False
    text = message.text.lstrip()
    return any(text.startswith(marker) for marker in markers)


def _has_correction(text: str, cues: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(cue.lower() in lowered for cue in cues)


def intervention_signals(
    messages: list[Message], cfg: SignalConfig, *, agent_acted_before: bool = False
) -> list[Signal]:
    """人間の介入（手動の拒否・中断・訂正）の合図。

    訂正は、エージェントが何か発言・操作した後の利用者の発言だけを数える（セッション冒頭の依頼文の
    「ダメなテストを直して」のような語を訂正と取り違えない）。
    """
    signals: list[Signal] = []
    agent_acted = agent_acted_before
    for message in messages:
        if _is_reject(message, cfg.reject_markers):
            signals.append(Signal(SIGNAL_REJECT, message.index))
        elif message.interrupt:
            signals.append(Signal(SIGNAL_INTERRUPT, message.index))
        elif message.human and agent_acted and _has_correction(message.text, cfg.correction_cues):
            signals.append(Signal(SIGNAL_CORRECTION, message.index))
        if message.role == ROLE_ASSISTANT:
            agent_acted = True
    return signals


def _call_signature(call: Message) -> tuple[tuple[str, str], str]:
    """ツール呼び出しのシグネチャと失敗種別（設計 §4 の 2・7 と同じ考え方）。"""
    if call.tool_name != "Bash":
        return ("tool", call.tool_name or "unknown"), fd.FAILURE_TOOL_ERROR
    kind = fd.classify_command(call.command)
    return (kind, signature_head(call.command, kind)), _FAILURE_TYPE_BY_KIND.get(
        kind, fd.FAILURE_TOOL_ERROR
    )


def _exit_code(text: str) -> int | None:
    match = _EXIT_CODE_LINE.match(text.lstrip())
    return int(match.group(1)) if match else None


def machine_signals(
    messages: list[Message], failure_events: list[dict], cfg: SignalConfig
) -> list[Signal]:
    """未解決のまま終わった、または閾値以上繰り返した機械の失敗の合図（設計 §7.3）。

    ツール結果のエラーと失敗イベントは ``tool_use_id`` で突き合わせ、同じ呼び出しを二重に数えない。
    ``tool_use_id`` を持たない失敗イベント（出力パターンで検知したテスト・lint）は、反復の数にだけ足す。
    """
    calls = {
        message.tool_use_id: message
        for message in messages
        if message.kind == KIND_TOOL_USE and message.tool_use_id
    }
    event_ids = {
        str((event.get("data") or {}).get("tool_use_id") or "") for event in failure_events
    } - {""}
    attempts: dict[tuple[str, str], list[tuple[int, bool]]] = {}
    failure_types: dict[tuple[str, str], str] = {}
    for message in messages:
        if message.kind != KIND_TOOL_RESULT or _is_reject(message, cfg.reject_markers):
            continue
        call = calls.get(message.tool_use_id)
        if call is None:
            continue
        signature, failure_type = _call_signature(call)
        failed = message.is_error or message.tool_use_id in event_ids
        exit_code = _exit_code(message.text) if message.is_error else None
        if failed and is_expected_nonzero(call.command, exit_code, cfg.expected_nonzero_commands):
            continue
        attempts.setdefault(signature, []).append((message.index, failed))
        failure_types[signature] = failure_type
    extra_failures: dict[tuple[str, str], int] = {}
    for event in failure_events:
        data = event.get("data") or {}
        if data.get("tool_use_id") or not data.get("command"):
            continue
        kind = str(data.get("command_kind") or fd.classify_command(str(data["command"])))
        signature = (kind, signature_head(str(data["command"]), kind))
        extra_failures[signature] = extra_failures.get(signature, 0) + 1
        failure_types.setdefault(signature, str(data.get("failure_type") or fd.FAILURE_TOOL_ERROR))
    signals: list[Signal] = []
    for signature in set(attempts) | set(extra_failures):
        history = attempts.get(signature, [])
        failures = sum(1 for _index, failed in history if failed) + extra_failures.get(signature, 0)
        if failures == 0:
            continue
        threshold = (
            cfg.repeat_threshold_test
            if failure_types[signature] in TEST_LINT_TYPES
            else cfg.repeat_threshold
        )
        unresolved = bool(history) and history[-1][1]
        if unresolved or failures >= threshold:
            index = next(
                (i for i, failed in reversed(history) if failed),
                history[-1][0] if history else 0,
            )
            reason = "unresolved" if unresolved else "repeated"
            signals.append(Signal(SIGNAL_MACHINE, index, f"{signature[1]} ({reason}, {failures})"))
    return sorted(signals, key=lambda signal: signal.index)


def detect(
    messages: list[Message],
    failure_events: list[dict],
    cfg: SignalConfig,
    *,
    agent_acted_before: bool = False,
) -> list[Signal]:
    """前段判定。人間の介入と機械の失敗の合図を、発言の位置の順に返す。"""
    return sorted(
        intervention_signals(messages, cfg, agent_acted_before=agent_acted_before)
        + machine_signals(messages, failure_events, cfg),
        key=lambda signal: signal.index,
    )


def has_intervention(signals: list[Signal]) -> bool:
    return any(signal.kind in STRONG_SIGNALS for signal in signals)
