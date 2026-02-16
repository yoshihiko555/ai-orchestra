import csv
from datetime import UTC, datetime

from tests.module_loader import load_module

mod = load_module("analyze_cli_usage", "packages/cli-logging/scripts/analyze-cli-usage.py")


def test_extract_keywords_detects_multiple_categories() -> None:
    prompt = "Design and debug the issue, then review performance and write tests."
    assert mod.extract_keywords(prompt) == [
        "design",
        "debug",
        "review",
        "implement",
        "test",
        "performance",
    ]


def test_extract_keywords_returns_other_when_no_match() -> None:
    assert mod.extract_keywords("totally unrelated prompt") == ["other"]


def test_extract_keywords_deduplicates_keyword_category() -> None:
    prompt = "Security vulnerability in auth permissions and access controls"
    assert mod.extract_keywords(prompt) == ["security"]


def test_create_bar_returns_empty_when_max_value_is_zero() -> None:
    assert mod.create_bar(5, 0) == ""


def test_create_bar_scales_to_requested_width() -> None:
    bar = mod.create_bar(5, 10, width=10)
    assert len(bar) == 10
    assert bar.count("\u2588") == 5
    assert bar.count("\u2591") == 5


def test_create_bar_uses_floor_rounding_for_partial_values() -> None:
    bar = mod.create_bar(2, 3, width=10)
    assert len(bar) == 10
    assert bar.count("\u2588") == 6
    assert bar.count("\u2591") == 4


def test_load_logs_returns_empty_when_log_file_missing(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "missing.jsonl"
    monkeypatch.setattr(mod, "LOG_FILE", missing)

    assert mod.load_logs() == []


def test_load_logs_filters_by_since_and_skips_invalid_lines(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "cli-tools.jsonl"
    log_file.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-01-01T00:00:00Z","tool":"codex","prompt":"old"}',
                '{"timestamp":"2026-01-05T00:00:00Z","tool":"gemini","prompt":"new"}',
                '{"tool":"codex","prompt":"missing timestamp"}',
                "not json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "LOG_FILE", log_file)

    since = datetime(2026, 1, 3, tzinfo=UTC)
    entries = mod.load_logs(since=since)

    assert entries == [{"timestamp": "2026-01-05T00:00:00Z", "tool": "gemini", "prompt": "new"}]


def test_load_logs_without_since_keeps_entries_without_timestamp(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "cli-tools.jsonl"
    log_file.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-01-05T00:00:00Z","tool":"gemini","prompt":"new"}',
                '{"tool":"codex","prompt":"missing timestamp"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "LOG_FILE", log_file)

    entries = mod.load_logs()

    assert entries == [
        {"timestamp": "2026-01-05T00:00:00Z", "tool": "gemini", "prompt": "new"},
        {"tool": "codex", "prompt": "missing timestamp"},
    ]


def test_format_report_returns_empty_message_when_no_entries() -> None:
    report = mod.format_report([], period_days=None)
    assert report == "No log entries found.\n\nRun Codex/Gemini commands to generate logs."


def test_format_report_includes_expected_sections_and_metrics() -> None:
    entries = [
        {
            "timestamp": "2026-01-01T10:00:00Z",
            "tool": "codex",
            "success": True,
            "prompt": "debug bug",
            "model": "gpt-5",
        },
        {
            "timestamp": "2026-01-02T10:00:00Z",
            "tool": "codex",
            "success": False,
            "prompt": "design api",
            "model": "gpt-5",
        },
        {
            "timestamp": "2026-01-03T10:00:00Z",
            "tool": "gemini",
            "success": True,
            "prompt": "research and test",
            "model": "gemini-2.5-pro",
        },
        {
            "timestamp": "2026-01-03T12:00:00Z",
            "tool": "other",
            "success": True,
            "prompt": "misc",
            "model": "unknown-model",
        },
    ]

    report = mod.format_report(entries, period_days=7)

    assert "Codex/Gemini Usage Report" in report
    assert "Period: 2026-01-01 ~ 2026-01-03" in report
    assert "Filter: Last 7 days" in report
    assert "## Tool Usage" in report
    assert "2 calls (50%)" in report
    assert "1 calls (25%)" in report
    assert "## Daily Trend" in report
    assert "01-01:" in report
    assert "01-02:" in report
    assert "01-03:" in report
    assert "Codex:  1, Gemini:  0" in report
    assert "Codex:  0, Gemini:  1" in report
    assert "## Success Rate" in report
    assert "(1/2)" in report
    assert "(1/1)" in report
    assert "## Top Use Cases (by prompt keywords)" in report
    assert "Debug" in report
    assert "Design" in report
    assert "Research" in report
    assert "Test" in report
    assert "Other" in report
    assert "## Models Used" in report
    assert "- gpt-5: 2 calls" in report
    assert "- gemini-2.5-pro: 1 calls" in report
    assert "- unknown-model: 1 calls" in report
    assert "Total: 4 calls | Log: logs/cli-tools.jsonl" in report


def test_generate_json_report_aggregates_counts_daily_and_keywords() -> None:
    entries = [
        {
            "timestamp": "2026-01-01T10:00:00Z",
            "tool": "codex",
            "success": True,
            "prompt": "debug bug",
        },
        {
            "timestamp": "2026-01-01T12:00:00Z",
            "tool": "gemini",
            "success": False,
            "prompt": "research topic",
        },
        {
            "timestamp": "2026-01-02T09:00:00Z",
            "tool": "other",
            "success": True,
            "prompt": "misc",
        },
        {
            "timestamp": "2026-01-03T08:00:00Z",
            "tool": "codex",
            "success": False,
            "prompt": "debug and test",
        },
    ]

    report = mod.generate_json_report(entries)

    assert report["total_calls"] == 4
    assert report["codex"] == {"calls": 2, "success": 1}
    assert report["gemini"] == {"calls": 1, "success": 0}
    assert report["daily"] == {
        "2026-01-01": {"codex": 1, "gemini": 1},
        "2026-01-03": {"codex": 1, "gemini": 0},
    }
    assert report["keywords"]["debug"] == 2
    assert report["keywords"]["research"] == 1
    assert report["keywords"]["test"] == 1
    assert report["keywords"]["other"] == 1


def test_generate_json_report_handles_empty_entries() -> None:
    report = mod.generate_json_report([])

    assert report == {
        "total_calls": 0,
        "codex": {"calls": 0, "success": 0},
        "gemini": {"calls": 0, "success": 0},
        "daily": {},
        "keywords": {},
    }


def test_export_to_csv_writes_expected_file_and_truncates_columns(
    tmp_path, monkeypatch
) -> None:
    export_dir = tmp_path / "exports"
    monkeypatch.setattr(mod, "EXPORT_DIR", export_dir)

    class FixedDatetime:
        @classmethod
        def now(cls) -> datetime:
            return datetime(2026, 2, 3, 4, 5, 6)

    monkeypatch.setattr(mod, "datetime", FixedDatetime)

    entries = [
        {
            "timestamp": "2026-01-01T10:00:00Z",
            "tool": "codex",
            "model": "gpt-5",
            "success": True,
            "prompt": "p" * 600,
            "response": "r" * 700,
        },
        {
            "timestamp": "2026-01-02T10:00:00Z",
            "tool": "gemini",
        },
    ]

    csv_path = mod.export_to_csv(entries)

    assert csv_path == export_dir / "cli-usage-20260203-040506.csv"
    assert csv_path.exists()
    assert export_dir.is_dir()

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    assert rows[0]["timestamp"] == "2026-01-01T10:00:00Z"
    assert rows[0]["tool"] == "codex"
    assert rows[0]["model"] == "gpt-5"
    assert rows[0]["success"] == "True"
    assert len(rows[0]["prompt"]) == 500
    assert len(rows[0]["response"]) == 500
    assert rows[1]["timestamp"] == "2026-01-02T10:00:00Z"
    assert rows[1]["tool"] == "gemini"
    assert rows[1]["model"] == ""
    assert rows[1]["success"] == ""
    assert rows[1]["prompt"] == ""
    assert rows[1]["response"] == ""
