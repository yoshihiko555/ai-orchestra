"""skill_evolution.py CLI のスモークテスト。"""

from __future__ import annotations

import json

from tests.module_loader import load_module

se = load_module("skill_evolution_common", "packages/skill-evolution/lib/skill_evolution_common.py")
cli = load_module("skill_evo_cli", "packages/skill-evolution/scripts/skill_evolution.py")


def test_status_outputs_summary(tmp_path, capsys) -> None:
    p = str(tmp_path)
    se.append_metric(
        p,
        "s",
        {
            "run_id": "a",
            "success": True,
            "machine": {"critical_pass_rate": 1.0, "tool_uses": 5, "duration_ms": 100},
            "self_report": None,
        },
    )
    rc = cli.main(["--project", p, "status", "s"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1 and out["skill"] == "s"


def test_check_trigger_not_triggered(tmp_path, capsys) -> None:
    p = str(tmp_path)
    se.append_lesson(p, "s", "one")
    rc = cli.main(["--project", p, "check-trigger", "s"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["triggered"] is False


def test_provenance_unknown(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("AI_ORCHESTRA_DIR", "")
    rc = cli.main(["--project", str(tmp_path), "provenance", "whatever"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["provenance"] == "unknown"
    assert out["reflection_target"] == "lessons_only"


def test_lock_acquire_then_conflict(tmp_path, capsys) -> None:
    p = str(tmp_path)
    assert cli.main(["--project", p, "lock", "acquire", "s"]) == 0
    capsys.readouterr()
    assert cli.main(["--project", p, "lock", "acquire", "s"]) == 1


def test_evaluate_cost_guard(tmp_path, capsys) -> None:
    p = str(tmp_path)
    hist = tmp_path / "hist.json"
    hist.write_text(
        json.dumps([{"score": 1, "cost_usd": 0}, {"score": 2, "cost_usd": 6}]),
        encoding="utf-8",
    )
    rc = cli.main(["--project", p, "evaluate", "--history", str(hist)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["should_stop"] is True and out["guard"] == "cost"


def test_evaluate_rejects_non_dict_elements(tmp_path) -> None:
    hist = tmp_path / "bad.json"
    hist.write_text(json.dumps([1, "a", None]), encoding="utf-8")
    rc = cli.main(["--project", str(tmp_path), "evaluate", "--history", str(hist)])
    assert rc == 2
