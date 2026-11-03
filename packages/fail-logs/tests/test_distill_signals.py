"""教訓化の前段判定（lib/distill_signals.py・lib/transcript_reader.py の分類）を検証する。

評価セット docs/evaluation/fail-logs.md の EV-41〜EV-45 に対応する（設計 §7.3）。合成した評価用の記録
（tests/fixtures/distill_corpus/）を使い、LLM は呼ばない。
"""

from __future__ import annotations

import lessons_helpers as h
import pytest

from tests.module_loader import load_module

signals_lib = h.signals_lib
reader = h.reader
eval_distill = load_module("eval_distill_under_test", "packages/fail-logs/scripts/eval_distill.py")
CONFIG = eval_distill.worker.resolve_distill_config({})
INTERRUPTS = frozenset(CONFIG["interrupt_markers"])


def _signals(turns: list[list], failures: list[dict] | None = None) -> list[signals_lib.Signal]:
    messages = reader.classify(eval_distill.build_records(turns), INTERRUPTS)
    return signals_lib.detect(
        messages, failures or [], eval_distill.worker._signal_config(CONFIG, {})
    )


def _cases(category: str) -> list[dict]:
    cases = eval_distill.load_corpus(category)
    assert len(cases) >= 3
    return cases


# EV-41: 手動の拒否・中断を含むセッションは必ず「合図あり」（固定の評価用記録で 100%）。
@pytest.mark.parametrize("case", _cases("intervention"), ids=lambda case: case["id"])
def test_interventions_always_signal(case: dict) -> None:
    found = eval_distill.detect_case_signals(case)
    assert signals_lib.has_intervention(found)


# EV-41: 許可ルールの拒否・auto mode の分類器の拒否・セッションの終了による中断は、人間の介入に数えない。
@pytest.mark.parametrize(
    "turn",
    [
        [
            "bash",
            "rm -rf x",
            {"error": "Permission to use Bash with command rm -rf x has been denied."},
        ],
        [
            "bash",
            "curl x",
            {
                "error": "Permission for this action was denied by the Claude Code auto mode classifier."
            },
        ],
        [
            "user",
            [
                {
                    "type": "text",
                    "text": "[Request interrupted: the session ended before this agent finished.]",
                }
            ],
        ],
    ],
)
def test_non_human_denials_are_not_interventions(turn: list) -> None:
    found = _signals([["user", "作業して"], turn, ["assistant", "了解"]])
    assert not signals_lib.has_intervention(found)


# EV-42: TDD の red → green だけのセッションと、意図した非ゼロ終了・一過性の失敗だけのセッションは「合図なし」（100%）。
@pytest.mark.parametrize(
    "case", _cases("tdd") + _cases("transient") + _cases("benign"), ids=lambda case: case["id"]
)
def test_no_signal_sessions(case: dict) -> None:
    assert eval_distill.detect_case_signals(case) == []


# EV-43: テスト・lint の失敗は 3 回で反復、それ以外は 2 回で反復。未解決はそれだけで合図。
def test_repeat_thresholds_and_unresolved() -> None:
    fail = {"error": "Exit code 1\n1 failed"}
    ok = {"output": "ok"}
    assert (
        _signals(
            [["bash", "pytest -q", fail], ["bash", "pytest -q", fail], ["bash", "pytest -q", ok]]
        )
        == []
    )
    three = _signals([["bash", "pytest -q", fail]] * 3 + [["bash", "pytest -q", ok]])
    assert [signal.kind for signal in three] == ["machine"]
    two = _signals(
        [["bash", "make build", fail], ["bash", "make build", fail], ["bash", "make build", ok]]
    )
    assert [signal.kind for signal in two] == ["machine"]
    unresolved = _signals([["bash", "make build", fail]])
    assert [signal.kind for signal in unresolved] == ["machine"]


# EV-43: ツール結果のエラーと失敗イベントは tool_use_id で突き合わせ、同じ呼び出しを二重に数えない。
def test_failure_events_are_matched_by_tool_use_id() -> None:
    records = eval_distill.build_records(
        [
            ["bash", "make build", {"error": "Exit code 1\nboom"}],
            ["bash", "make build", {"output": "ok"}],
        ]
    )
    messages = reader.classify(records, INTERRUPTS)
    tool_use_id = next(
        message.tool_use_id for message in messages if message.kind == reader.KIND_TOOL_USE
    )
    failure = {
        "data": {"tool_use_id": tool_use_id, "command": "make build", "command_kind": "shell"}
    }
    found = signals_lib.detect(messages, [failure], eval_distill.worker._signal_config(CONFIG, {}))
    assert found == []


# EV-43: tool_use_id を持たない失敗イベント（出力パターンで検知したテスト）は反復の数にだけ足す。
def test_legacy_failure_events_count_toward_repeats() -> None:
    legacy = [
        {"data": {"command": "pytest -q", "command_kind": "test", "failure_type": "test_failure"}}
    ] * 3
    found = signals_lib.detect([], legacy, eval_distill.worker._signal_config(CONFIG, {}))
    assert [signal.kind for signal in found] == ["machine"]


# EV-44: 機械的な失敗がなく訂正だけを含むセッションの 8 割以上が「合図あり」（5 件以上）。
def test_corrections_detected_at_least_80_percent() -> None:
    cases = _cases("correction")
    assert len(cases) >= 5
    detected = [
        case
        for case in cases
        if signals_lib.has_intervention(eval_distill.detect_case_signals(case))
    ]
    assert len(detected) / len(cases) >= 0.8


# EV-45: スキルの展開・hook の注入・`!` コマンドの出力・会話の要約の中の訂正の語彙では「合図あり」にしない。
@pytest.mark.parametrize(
    "record_turn",
    [
        ["meta", "Stop hook feedback: そうじゃない、まだ終わっていない"],
        ["user", "<command-name>/goal</command-name> 違う方法で"],
        ["user", "<bash-stdout>やめて</bash-stdout>"],
        ["user", "<system-reminder>そうじゃない</system-reminder>"],
    ],
)
def test_non_human_text_is_ignored(record_turn: list) -> None:
    assert _signals([record_turn, ["assistant", "了解"]]) == []


# EV-44（レビュー指摘）: セッション冒頭の依頼文（エージェントが動く前）の語は訂正に数えない。
def test_opening_request_is_not_a_correction() -> None:
    found = _signals(
        [["user", "ダメなテストを直して。違う書き方でもいい"], ["assistant", "直しました"]]
    )
    assert found == []


def test_correction_counts_after_resume() -> None:
    messages = reader.classify(
        eval_distill.build_records([["user", "ちがう、unit だけでいい"]]), INTERRUPTS
    )
    config = eval_distill.worker._signal_config(CONFIG, {})
    assert signals_lib.detect(messages, [], config) == []
    resumed = signals_lib.detect(messages, [], config, agent_acted_before=True)
    assert signals_lib.has_intervention(resumed)


def test_compact_summary_and_sidechain_are_ignored() -> None:
    records = eval_distill.build_records([["user", "そうじゃない"], ["user", "違う"]])
    records[0]["isCompactSummary"] = True
    records[1]["isSidechain"] = True
    messages = reader.classify(records, INTERRUPTS)
    assert signals_lib.detect(messages, [], eval_distill.worker._signal_config(CONFIG, {})) == []
