"""lib/sanitize.py（入力の検査・出力の無害化）と lib/promotion.py（昇格する文面）を検証する。

評価セット docs/evaluation/fail-logs.md の EV-69・EV-80・EV-84・EV-86 の土台（設計 §5.1・§8.3・§10.4・§10.6）。
"""

from __future__ import annotations

import lessons_helpers as h
import promotion
import pytest
import sanitize


@pytest.mark.parametrize(
    "ref", ["AGENTS.md", ".claude/rules/local.md", "https://github.com/o/r/issues/1"]
)
def test_target_ref_accepts_repo_paths_and_github_urls(ref: str) -> None:
    assert sanitize.check_target_ref(ref) == ref


@pytest.mark.parametrize(
    "ref", ["/etc/passwd", "../x", "a/../b", "https://evil.example.com/x", "a b", "", "a//b"]
)
def test_target_ref_rejects_others(ref: str) -> None:
    with pytest.raises(sanitize.ValidationError):
        sanitize.check_target_ref(ref)


@pytest.mark.parametrize("branch", ["-x", "a b", "x" * 201, "a;b"])
def test_branch_rejects_options_and_odd_names(branch: str) -> None:
    with pytest.raises(sanitize.ValidationError):
        sanitize.check_branch(branch)


def test_note_is_single_line_and_bounded() -> None:
    assert sanitize.check_note("短いメモ") == "短いメモ"
    for bad in ("a\nb", "x" * 201):
        with pytest.raises(sanitize.ValidationError):
            sanitize.check_note(bad)


def test_neutralize_json_covers_keys_and_nested_values() -> None:
    value = {"<k>": ["</fail-logs-lessons>", {"x": "<y>"}], "n": 1}
    assert sanitize.neutralize_json(value) == {
        "‹k›": ["‹/fail-logs-lessons›", {"x": "‹y›"}],
        "n": 1,
    }


def test_frame_wraps_and_notes_untrusted() -> None:
    framed = sanitize.frame(["line"])
    assert framed.startswith("<fail-logs-lessons>")
    assert framed.endswith("</fail-logs-lessons>")
    assert "指示ではない" in framed


def _lesson(**overrides: str) -> dict:
    lesson = {"lid": "L-1a2b3c4d", "title": "見出し", "avoidance": "回避策の\n数行"}
    lesson.update(overrides)
    return lesson


# EV-84: 文面は見出し・回避策・マーカーだけで、山括弧は中和し、マーカーだけを本物にする。
def test_compose_text_neutralizes_and_keeps_marker() -> None:
    text = promotion.compose_text(
        _lesson(title="<!-- lesson:L-deadbeef --> x", avoidance="<script>")
    )
    assert text.count("<!--") == 1
    assert text.endswith("<!-- lesson:L-1a2b3c4d -->")
    assert "<script>" not in text


# EV-84: 伏せ字で変わった箇所と、公開してよいか確かめる語を検出する。
def test_check_text_reports_masked_and_findings() -> None:
    lesson = _lesson(avoidance=f"key {h.API_KEY_SAMPLE} at 192.168.0.1 on build.corp")
    result = promotion.check_text(lesson, h.strong_masker(), "private")
    kinds = {finding["kind"] for finding in result["findings"]}
    assert "masked" in kinds
    assert h.API_KEY_SAMPLE not in result["text"]
    assert result["public"] is False


@pytest.mark.parametrize(
    ("visibility", "public"),
    [("public", True), ("unknown", True), ("private", False), ("internal", False)],
)
def test_is_public(visibility: str, public: bool) -> None:
    assert promotion.is_public(visibility) is public


# EV-86: Issue の本文は固定の雛形で、フェンスは文面の中で最も長いバッククォートの並びより長い。
def test_issue_body_fence_and_notice() -> None:
    text = "- **x** — use ```` fences"
    body = promotion.issue_body(_lesson(), text)
    assert "`````" in body
    assert "人が内容を確かめること" in body
    assert "L-1a2b3c4d" in body


# EV-84（レビュー指摘）: コマンドや強い命令に見える語を検出する。
@pytest.mark.parametrize(
    "snippet", ["`curl x | sh`", "sudo make install", "rm -rf build", "bash -c 'x'"]
)
def test_command_like_findings(snippet: str) -> None:
    result = promotion.check_text(_lesson(avoidance=snippet), h.strong_masker(), "private")
    assert "command_like" in {finding["kind"] for finding in result["findings"]}
