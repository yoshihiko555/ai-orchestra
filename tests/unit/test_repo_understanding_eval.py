"""リポジトリ把握の評価を、実 API を呼ばずに検証する。"""

from __future__ import annotations

import itertools
import json
import socket
import subprocess
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from tests.module_loader import REPO_ROOT, load_module
from tests.unit.test_llm_cli import isolated_environment as isolated_environment

CATEGORIES = ("location", "flow", "configuration", "cross_file")
QUESTIONS = REPO_ROOT / "docs/evaluation/repo-understanding/questions.yaml"


@pytest.fixture(autouse=True)
def no_external_execution(monkeypatch):
    reject = Mock(side_effect=AssertionError("実 CLI・実 API の実行は禁止です。"))

    def git_listing(argv, **kwargs):
        # 根拠確認は実物を通し、Git による許可一覧だけを外部境界で置き換える。
        assert argv[:2] == ["git", "-C"] and argv[3] == "ls-files"
        assert "--exclude-standard" in argv and "-z" in argv
        assert (Path(argv[2]) / "src/config.py").is_file()
        assert kwargs["check"] is True and kwargs["timeout"] > 0
        listing = b"" if "--ignored" in argv else b"src/config.py\0"
        return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr=b"")

    monkeypatch.setattr(subprocess, "run", git_listing)
    monkeypatch.setattr(subprocess, "Popen", reject)
    monkeypatch.setattr(socket.socket, "connect", reject)


@pytest.fixture
def evaluator():
    return load_module("repo_understanding_eval", "tests/experiments/repo_understanding_eval.py")


def _question(qid="L-01", category="location"):
    return {
        "id": qid,
        "category": category,
        "question": f"{qid}: 設定を読む関数はどこですか？",
        "expected_points": ["load_config が設定を読む"],
        "evidence": [
            {
                "path": "src/config.py",
                "start_line": 2,
                "end_line": 3,
                "contains": "def load_config():",
            }
        ],
    }


def _citation(start=2, end=3, **extra):
    return {
        "path": "src/config.py",
        "start_line": start,
        "end_line": end,
        "claim": "設定を読む関数",
        **extra,
    }


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src/config.py").write_text(
        "# 設定\ndef load_config():\n    return {}\n# 終わり\n", encoding="utf-8"
    )
    return root


def _write_questions(tmp_path, questions):
    path = tmp_path / "questions.yaml"
    path.write_text(yaml.safe_dump({"questions": questions}, allow_unicode=True), encoding="utf-8")
    return path


def _main(evaluator, argv, **kwargs):
    try:
        return evaluator.main([str(arg) for arg in argv], **kwargs)
    except SystemExit as exc:
        return exc.code


def _run_args(path, project, output, *, confirm=True):
    args = [
        "run",
        "--questions",
        path,
        "--project",
        project,
        "--profile",
        "read-profile",
        "--out",
        output,
    ]
    if confirm:
        args.append("--confirm-real-api")
    return args


def _agent_result(**extra):
    return {
        "status": "complete",
        "stop_reason": None,
        "turns": 2,
        "tokens": {"input": 11, "output": 3, "total": 14},
        "answer": "対象方式の回答",
        "citations": [_citation(verified=True)],
        "files_read": ["src/config.py"],
        "unverified_count": 0,
        **extra,
    }


def _baseline_stream(project):
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read-1",
                        "name": "Read",
                        "input": {
                            "file_path": str(project / "src/config.py"),
                            "offset": 2,
                            "limit": 2,
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read-1",
                        "content": "2→def load_config():\n3→    return {}",
                        "is_error": False,
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(
                {
                    "answer": "基準方式の回答",
                    "citations": [_citation(), _citation(4, 4)],
                },
                ensure_ascii=False,
            ),
            "usage": {
                "input_tokens": 5,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 11,
                "output_tokens": 3,
            },
        },
    ]
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


class EvaluationRunner:
    """runner の戻り値と agent のファイル出力だけを置き換える。"""

    def __init__(
        self, project, *, agent_result=None, agent_code=0, broken=None, grade_correct=True
    ):
        self.project = project
        self.agent_result = _agent_result() if agent_result is None else agent_result
        self.agent_code = agent_code
        self.broken = broken
        self.grade_correct = grade_correct
        self.calls = []
        self.agent_outputs = []
        self.grade_prompts = []

    def __call__(self, argv, cwd, timeout):
        argv = [str(arg) for arg in argv]
        assert Path(cwd) == self.project
        assert timeout > 0
        self.calls.append(argv)
        if "agent" in argv:
            assert argv[argv.index("agent") - 1] == "llm"
            assert argv[argv.index("--profile") + 1] == "read-profile"
            assert Path(argv[argv.index("--project") + 1]) == self.project
            output = Path(cwd) / Path(argv[argv.index("--output") + 1])
            assert not output.exists() or not any(output.iterdir())
            output.mkdir(parents=True, exist_ok=True)
            self.agent_outputs.append(output)
            if self.broken != "missing":
                text = "{" if self.broken == "invalid" else json.dumps(self.agent_result)
                (output / "result.json").write_text(text, encoding="utf-8")
            (output / "exit").write_text(f"{self.agent_code}\n", encoding="utf-8")
            return self.agent_code, ""
        assert "-p" in argv
        assert "--" in argv
        assert json.loads(argv[argv.index("--settings") + 1])["disableAllHooks"] is True
        assert "--strict-mcp-config" in argv
        assert "--no-session-persistence" in argv
        tools = argv[argv.index("--tools") + 1]
        if tools:
            assert set(tools.split(",")) == {"Read", "Grep", "Glob"}
            assert argv[argv.index("--output-format") + 1] == "stream-json"
            assert "--verbose" in argv
            return 0, _baseline_stream(self.project)
        assert argv[argv.index("--output-format") + 1] == "json"
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        assert "correct" in schema["properties"]
        prompt = argv[argv.index("--") + 1]
        self.grade_prompts.append(json.loads(prompt.split("\n", 1)[1]))
        return 0, json.dumps(
            {"structured_output": {"correct": self.grade_correct, "reason": "要点との照合結果"}}
        )


def _run(evaluator, tmp_path, project, questions=None, **runner_options):
    questions = [_question()] if questions is None else questions
    path = _write_questions(tmp_path, questions)
    output = tmp_path / "run"
    runner = EvaluationRunner(project, **runner_options)
    ticks = itertools.count()
    code = _main(
        evaluator, _run_args(path, project, output), runner=runner, clock=lambda: float(next(ticks))
    )
    assert code == 0
    return json.loads((output / "results.json").read_text(encoding="utf-8")), output, runner


def _record(
    method,
    qid="L-01",
    category="location",
    correct=True,
    verified=(True,),
    input_tokens=2,
    output_tokens=1,
    elapsed=1,
):
    return {
        "question_id": qid,
        "category": category,
        "method": method,
        "correct": correct,
        "citations": [_citation(verified=value) for value in verified],
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
        },
        "elapsed_sec": elapsed,
    }


def test_shipped_questions_have_balanced_categories_and_verified_source_anchors():
    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    assert len(questions) == len({question["id"] for question in questions}) == 20
    assert Counter(question["category"] for question in questions) == dict.fromkeys(CATEGORIES, 5)
    for question in questions:
        assert isinstance(question["question"], str) and question["question"].strip()
        assert question["expected_points"]
        assert all(
            isinstance(point, str) and point.strip() for point in question["expected_points"]
        )
        assert question["evidence"]
        for evidence in question["evidence"]:
            path = REPO_ROOT / evidence["path"]
            assert path.resolve().is_relative_to(REPO_ROOT.resolve())
            lines = path.read_text(encoding="utf-8").splitlines()
            start, end = evidence["start_line"], evidence["end_line"]
            assert type(start) is int and type(end) is int
            assert 1 <= start <= end <= len(lines)
            assert isinstance(evidence["contains"], str) and evidence["contains"]
            assert evidence["contains"] in "\n".join(lines[start - 1 : end])


def test_check_accepts_current_anchors_without_external_execution(evaluator, tmp_path, project):
    path = _write_questions(tmp_path, [_question()])
    assert _main(evaluator, ["check", "--questions", path, "--project", project]) == 0


def test_check_rejects_shifted_anchor_even_when_text_still_exists(evaluator, tmp_path, project):
    path = _write_questions(tmp_path, [_question()])
    source = project / "src/config.py"
    source.write_text("# 追加行\n# 追加行\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
    assert _main(evaluator, ["check", "--questions", path, "--project", project]) != 0


@pytest.mark.parametrize(
    "invalid",
    ["duplicate", "missing_points", "missing_evidence", "missing_anchor", "invalid_range"],
)
def test_check_rejects_incomplete_question_contract(evaluator, tmp_path, project, invalid):
    question = _question()
    questions = [question]
    if invalid == "duplicate":
        questions.append(_question())
    elif invalid == "missing_points":
        question["expected_points"] = []
    elif invalid == "missing_evidence":
        question["evidence"] = []
    elif invalid == "missing_anchor":
        question["evidence"][0].pop("contains")
    else:
        question["evidence"][0]["end_line"] = 1
    path = _write_questions(tmp_path, questions)
    assert _main(evaluator, ["check", "--questions", path, "--project", project]) != 0


def test_run_requires_confirmation_before_any_runner_call(evaluator, tmp_path, project):
    path = _write_questions(tmp_path, [_question()])
    runner = Mock(side_effect=AssertionError("了承前に実行してはいけません。"))
    code = _main(
        evaluator, _run_args(path, project, tmp_path / "run", confirm=False), runner=runner
    )
    assert code != 0
    runner.assert_not_called()


def test_run_uses_same_questions_and_fresh_agent_output_directories(evaluator, tmp_path, project):
    questions = [_question(), _question("F-02", "flow")]
    results, output, runner = _run(evaluator, tmp_path, project, questions)
    assert runner.agent_outputs == [output / "agent/L-01", output / "agent/F-02"]
    records = results["records"]
    assert Counter((item["question_id"], item["method"]) for item in records) == {
        (question["id"], method): 1 for question in questions for method in ("baseline", "agent")
    }
    for question in questions:
        exploration = [
            args
            for args in runner.calls
            if question["question"] in args[-1] and "--json-schema" not in args
        ]
        assert len(exploration) == 2


def test_run_reads_relative_output_from_launch_directory_with_different_project(
    evaluator, tmp_path, project, monkeypatch
):
    path = _write_questions(tmp_path, [_question()])
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)
    runner = EvaluationRunner(project)

    assert _main(evaluator, _run_args(path, project, Path("run")), runner=runner) == 0

    output = launch_dir / "run"
    results = json.loads((output / "results.json").read_text(encoding="utf-8"))
    agent = next(record for record in results["records"] if record["method"] == "agent")
    assert agent["error"] is None
    assert agent["answer"] == "対象方式の回答"
    command = next(argv for argv in runner.calls if "agent" in argv)
    assert Path(command[command.index("--output") + 1]) == output / "agent/L-01"
    assert runner.agent_outputs == [output / "agent/L-01"]
    assert not (project / "run").exists()


def test_run_rejects_relative_symlink_output_before_runner_call(
    evaluator, tmp_path, project, monkeypatch
):
    path = _write_questions(tmp_path, [_question()])
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    target = tmp_path / "empty"
    target.mkdir()
    link = launch_dir / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.chdir(launch_dir)
    runner = Mock(side_effect=AssertionError("symlink の出力先では実行禁止です。"))

    assert _main(evaluator, _run_args(path, project, Path("link")), runner=runner) != 0

    runner.assert_not_called()
    assert link.is_symlink()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("relative", [False, True])
def test_run_rejects_used_output_without_calls_or_overwriting(
    evaluator, tmp_path, project, monkeypatch, relative
):
    path = _write_questions(tmp_path, [_question()])
    output = tmp_path / "run"
    output.mkdir()
    previous = output / "results.json"
    previous.write_bytes(b"previous results\n")
    monkeypatch.chdir(tmp_path)
    output_arg = Path("run") if relative else output
    runner = Mock(side_effect=AssertionError("使用済み出力先では実行禁止です。"))
    assert _main(evaluator, _run_args(path, project, output_arg), runner=runner) != 0
    runner.assert_not_called()
    assert previous.read_bytes() == b"previous results\n"
    assert list(output.iterdir()) == [previous]


def test_baseline_read_ranges_and_cached_tokens_reach_results(evaluator, tmp_path, project):
    results, _, _ = _run(evaluator, tmp_path, project)
    baseline = next(item for item in results["records"] if item["method"] == "baseline")
    assert [item["verified"] for item in baseline["citations"]] == [True, False]
    assert baseline["tokens"] == {"input": 23, "output": 3, "total": 26}
    assert baseline["elapsed_sec"] > 0


def test_agent_unverified_citations_reach_metrics(evaluator, tmp_path, project):
    results, _, _ = _run(
        evaluator,
        tmp_path,
        project,
        agent_result=_agent_result(
            citations=[_citation(verified=False)],
            unverified_count=1,
        ),
    )
    assert results["summary"]["overall"]["agent"]["unverified_ratio"] == 1


def test_partial_answer_is_graded_and_stop_reason_is_preserved(evaluator, tmp_path, project):
    results, output, _ = _run(
        evaluator,
        tmp_path,
        project,
        agent_code=9,
        agent_result=_agent_result(
            status="partial",
            stop_reason="quota_exhausted",
            turns=4,
        ),
    )
    agent = next(item for item in results["records"] if item["method"] == "agent")
    assert agent["correct"] is True
    assert agent["answer"] == "対象方式の回答"
    assert agent["status"] == "partial"
    assert agent["stop_reason"] == "quota_exhausted"
    assert agent["exit_code"] == 9
    spot_check = (output / "spot-check.md").read_text(encoding="utf-8")
    assert "quota_exhausted" in spot_check and "9" in spot_check


@pytest.mark.parametrize("broken", ["missing", "invalid"])
def test_missing_or_invalid_result_counts_as_incorrect(evaluator, tmp_path, project, broken):
    results, _, _ = _run(evaluator, tmp_path, project, broken=broken, agent_code=8)
    agent = next(item for item in results["records"] if item["method"] == "agent")
    assert agent["correct"] is False
    assert agent["exit_code"] == 8
    assert agent["error"]
    assert results["summary"]["overall"]["agent"]["accuracy"] == 0


@pytest.mark.parametrize("agent_answer", ["対象方式の回答", "対象方式の別の回答"])
def test_grader_compares_expected_points_and_spot_check_contains_both_answers(
    evaluator, tmp_path, project, agent_answer
):
    results, output, runner = _run(
        evaluator, tmp_path, project, agent_result=_agent_result(answer=agent_answer)
    )
    assert len([args for args in runner.calls if "--json-schema" in args]) == 2
    assert [prompt["answer"] for prompt in runner.grade_prompts] == [
        "基準方式の回答",
        agent_answer,
    ]
    assert [prompt["expected_points"] for prompt in runner.grade_prompts] == [
        _question()["expected_points"],
        _question()["expected_points"],
    ]
    assert all(item["correct"] is True for item in results["records"])
    spot_check = (output / "spot-check.md").read_text(encoding="utf-8")
    for value in (
        _question()["question"],
        "load_config が設定を読む",
        "基準方式の回答",
        agent_answer,
        "要点との照合結果",
    ):
        assert value in spot_check


def test_grader_rejection_reaches_record_and_accuracy(evaluator, tmp_path, project):
    results, _, _ = _run(evaluator, tmp_path, project, grade_correct=False)
    assert all(item["correct"] is False for item in results["records"])
    for method in ("baseline", "agent"):
        assert results["summary"]["overall"][method]["accuracy"] == 0


def test_metrics_are_weighted_by_citations_and_grouped_by_category(evaluator):
    records = [
        _record("baseline"),
        _record("agent", verified=(False,), input_tokens=5, output_tokens=2, elapsed=2),
        _record("baseline", "F-02", "flow"),
        _record(
            "agent",
            "F-02",
            "flow",
            correct=False,
            verified=(True,) * 9,
            input_tokens=7,
            output_tokens=4,
            elapsed=3,
        ),
    ]
    summary = evaluator.summarize(records)
    agent = summary["overall"]["agent"]
    assert agent["accuracy"] == 0.5
    assert agent["unverified_ratio"] == 0.1
    assert agent["tokens"] == {"input": 12, "output": 6, "total": 18}
    assert agent["elapsed_sec"] == 5
    assert summary["overall"]["baseline"]["accuracy"] == 1
    assert summary["by_category"]["location"]["agent"]["accuracy"] == 1
    assert summary["by_category"]["location"]["agent"]["unverified_ratio"] == 1
    assert summary["by_category"]["flow"]["agent"]["accuracy"] == 0
    assert summary["by_category"]["flow"]["agent"]["unverified_ratio"] == 0


@pytest.mark.parametrize(
    "baseline_correct,correct,unverified,citations,accepted,accuracy_ok,citations_ok",
    [
        (20, 18, 1, 10, True, True, True),
        (20, 17, 0, 10, False, False, True),
        (20, 18, 2, 19, False, True, False),
        (20, 20, 0, 0, False, True, False),
        (10, 9, 1, 10, True, True, True),
        (10, 8, 1, 10, False, False, True),
    ],
)
def test_adoption_uses_both_thresholds_and_reports_failed_condition(
    evaluator, baseline_correct, correct, unverified, citations, accepted, accuracy_ok, citations_ok
):
    records = []
    flags = (False,) * unverified + (True,) * (citations - unverified)
    for index in range(20):
        qid = f"L-{index + 1:02}"
        records.append(_record("baseline", qid, correct=index < baseline_correct))
        records.append(
            _record("agent", qid, correct=index < correct, verified=flags if index == 0 else ())
        )
    summary = evaluator.summarize(records)
    adoption = summary["adoption"]
    assert adoption["accepted"] is accepted
    assert adoption["accuracy_ok"] is accuracy_ok
    assert adoption["citations_ok"] is citations_ok
    if citations == 0:
        assert summary["overall"]["agent"]["unverified_ratio"] is None


def test_report_rebuilds_metrics_and_links_spot_check_without_api(evaluator, tmp_path, project):
    _, output, _ = _run(evaluator, tmp_path, project)
    report_dir = tmp_path / "report"
    runner = Mock(side_effect=AssertionError("保存結果の報告に実 API は不要です。"))
    assert (
        _main(
            evaluator,
            ["report", "--results", output / "results.json", "--out", report_dir],
            runner=runner,
        )
        == 0
    )
    runner.assert_not_called()
    draft = (report_dir / "report-draft.md").read_text(encoding="utf-8")
    assert "spot-check.md" in draft
    cells = {cell.strip() for line in draft.splitlines() if "|" in line for cell in line.split("|")}
    assert {"26", "14"} <= cells
    assert (report_dir / "spot-check.md").is_file()


def test_results_do_not_serialize_environment_or_key_fields(
    evaluator, tmp_path, project, monkeypatch
):
    marker = "test-only-private-marker"
    monkeypatch.setenv("GEMINI_API_KEY", marker)
    results, output, _ = _run(evaluator, tmp_path, project)
    assert results["records"]

    def check(value):
        if isinstance(value, dict):
            for name, item in value.items():
                assert name.casefold() not in {"env", "environment", "api_key", "gemini_api_key"}
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif isinstance(value, str):
            assert marker not in value

    check(results)
    for path in output.glob("*.md"):
        assert marker not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("read_result", ["failed", "missing"])
def test_baseline_does_not_verify_failed_or_unfinished_read(evaluator, project, read_result):
    events = [json.loads(line) for line in _baseline_stream(project).splitlines()]
    if read_result == "failed":
        events[1]["message"]["content"][0]["is_error"] = True
    else:
        events.pop(1)
    result = evaluator._baseline_result("\n".join(map(json.dumps, events)), project)
    assert [item["verified"] for item in result["citations"]] == [False, False]


def test_baseline_default_read_range_is_clamped_to_file(evaluator, project):
    events = [json.loads(line) for line in _baseline_stream(project).splitlines()]
    read = events[0]["message"]["content"][0]["input"]
    read.pop("offset")
    read.pop("limit")
    result = evaluator._baseline_result("\n".join(map(json.dumps, events)), project)
    assert [item["verified"] for item in result["citations"]] == [True, True]


def test_baseline_read_outside_root_does_not_create_evidence(evaluator, project, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("公開データ\n", encoding="utf-8")
    events = [json.loads(line) for line in _baseline_stream(project).splitlines()]
    events[0]["message"]["content"][0]["input"]["file_path"] = str(outside)
    result = evaluator._baseline_result("\n".join(map(json.dumps, events)), project)
    assert [item["verified"] for item in result["citations"]] == [False, False]


@pytest.mark.parametrize("qid", ["../outside", "/absolute", "-option"])
def test_run_rejects_question_id_before_creating_output(evaluator, project, tmp_path, qid):
    path = _write_questions(tmp_path, [_question(qid)])
    runner = Mock(side_effect=AssertionError("不正な問題では実行禁止です。"))
    output = tmp_path / "run"
    assert _main(evaluator, _run_args(path, project, output), runner=runner) != 0
    runner.assert_not_called()
    assert not output.exists()


def test_runner_timeout_is_incorrect_without_exposing_exception_output(
    evaluator, project, tmp_path
):
    path = _write_questions(tmp_path, [_question()])
    output = tmp_path / "run"
    runner = Mock(side_effect=subprocess.TimeoutExpired("external", 1, output="private-marker"))
    assert _main(evaluator, _run_args(path, project, output), runner=runner) == 0
    data = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert len(data["records"]) == 2
    assert all(record["correct"] is False and record["error"] for record in data["records"])
    for record in data["records"]:
        assert "private-marker" not in record["error"]
        assert record["answer"] == ""


def test_subprocess_runner_passes_cwd_timeout_and_discards_stderr(evaluator, project, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess(["claude"], 7, stdout="結果"))
    monkeypatch.setattr(subprocess, "run", run)
    assert evaluator._subprocess_runner(["claude"], project, 17) == (7, "結果")
    assert run.call_args.args[0] == ["claude"]
    assert run.call_args.kwargs["cwd"] == project
    assert run.call_args.kwargs["timeout"] == 17
    assert run.call_args.kwargs["stderr"] == subprocess.DEVNULL


def test_run_accepts_existing_empty_output(evaluator, project, tmp_path):
    (tmp_path / "run").mkdir()
    results, output, _ = _run(evaluator, tmp_path, project)
    assert len(results["records"]) == 2
    assert not (output / ".evaluation-running").exists()


@pytest.mark.parametrize("answer", ["", "   "])
def test_empty_agent_answer_is_incorrect_with_reason(evaluator, project, tmp_path, answer):
    results, _, _ = _run(evaluator, tmp_path, project, agent_result=_agent_result(answer=answer))
    agent = next(record for record in results["records"] if record["method"] == "agent")
    assert agent["correct"] is False
    assert agent["error"]


def test_failed_grader_envelope_is_not_accepted(evaluator, project):
    runner = Mock(
        return_value=(
            0,
            json.dumps(
                {
                    "is_error": True,
                    "structured_output": {"correct": True, "reason": "採点未完了"},
                }
            ),
        )
    )
    with pytest.raises(ValueError):
        evaluator._grade(_question(), "回答", runner, project, 17)
