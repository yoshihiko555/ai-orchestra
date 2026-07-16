"""`evaluate_candidate` の入力検証テスト（Sec6, CLI exit code 2 契約）。

PR #168 レビュー指摘（Codex #1498 / #1531, CodeRabbit meta_harness.py:633）:
- `--repeat` に 1 未満を渡すと `range(1, repeat+1)` が空になり、0 回実行のまま
  "成功" として返ってしまう（何も評価しなかったことが黙って握り潰される）。
- 複数 `--scenario` の一部だけが未知でも、他の scenario が一致すれば黙って無視される
  （要求された評価の一部が silent に欠落する）。

いずれも `evaluate_candidate` / `_select_scenarios` が `ValueError` を送出し、CLI 側
（`meta_harness.py` の `except ValueError` → exit code 2）に正しく伝播することを検証する。
実 `claude`/`codex` は一切呼ばない（`run_single_attempt` に到達する前に検証で止まるため、
runner を使う機会自体が無い）。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_evaluate_candidate_validation",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_evaluate_candidate_validation",
    "packages/meta-harness/lib/meta_harness_common.py",
)

_SCHEMA_DIR = Path("packages/meta-harness/schemas").resolve()
_PACKAGE_DIR = Path("packages/meta-harness").resolve()
_CAND_ID = "cand-20260707-120000-slug-ab12"


def _manifest(*, cand_id: str = _CAND_ID, target: str = "claude-harness") -> dict:
    return {
        "cand_id": cand_id,
        "parent_id": None,
        "created_by": "human",
        "target": target,
        "source_commit": "0" * 40,
        "config_hash": "b" * 64,
    }


def _append_registration(main_root: Path, config: dict, manifest: dict) -> None:
    mh.append_ledger_event(
        main_root,
        config,
        {
            "event": "candidate_registered",
            "cand_id": manifest["cand_id"],
            "created_by": manifest["created_by"],
            "target": manifest["target"],
        },
    )


def _call_evaluate_candidate(
    tmp_path: Path, *, scenario_ids: list[str] | None = None, repeat_override: int | None = None
) -> list[dict]:
    manifest = _manifest()
    _append_registration(tmp_path, mh.DEFAULTS, manifest)
    return ev.evaluate_candidate(
        main_root=tmp_path,
        config=mh.DEFAULTS,
        schema_dir=_SCHEMA_DIR,
        package_dir=_PACKAGE_DIR,
        project_dir=tmp_path,
        cand_id=_CAND_ID,
        manifest=manifest,
        scenario_ids=scenario_ids,
        repeat_override=repeat_override,
        cli_capabilities={"claude_version": "2.1.202", "ok": True},
    )


class TestRepeatValidation:
    def test_repeat_zero_is_rejected(self, tmp_path: Path) -> None:
        try:
            _call_evaluate_candidate(tmp_path, repeat_override=0)
        except ValueError as exc:
            assert "repeat" in str(exc)
        else:
            raise AssertionError("--repeat 0 should raise ValueError, not silently no-op")

    def test_repeat_negative_is_rejected(self, tmp_path: Path) -> None:
        try:
            _call_evaluate_candidate(tmp_path, repeat_override=-1)
        except ValueError as exc:
            assert "repeat" in str(exc)
        else:
            raise AssertionError(
                "--repeat -1 should raise ValueError, not silently run zero attempts"
            )

    def test_repeat_none_is_allowed(self, tmp_path: Path, monkeypatch) -> None:
        """--repeat 省略時（None）は設定既定値を使うため検証エラーにならない。"""
        calls: list[int] = []

        def fake_run_single_attempt(**kwargs):
            calls.append(kwargs["attempt"])
            return {
                "run_id": f"run-fake-{kwargs['attempt']}",
                "cand_id": kwargs["cand_id"],
                "scenario_id": kwargs["scenario"]["id"],
                "verdict": "pass",
                "quality_score": 100.0,
                "critical_pass_rate": 1.0,
                "cost": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "tool_uses": 0,
                    "duration_ms": 1,
                    "total_cost_usd": 0.0,
                    "num_turns": 1,
                },
                "attempt": kwargs["attempt"],
                "attempts_total": kwargs["attempts_total"],
            }

        monkeypatch.setattr(ev, "run_single_attempt", fake_run_single_attempt)
        monkeypatch.setattr(ev.siso, "execution_boundary_available", lambda _config: True)
        monkeypatch.setattr(
            ev,
            "candidate_impact_context",
            lambda **_kwargs: ev.skill_targets.SkillImpactContext((), "c" * 64),
        )
        monkeypatch.setattr(ev, "_append_evaluation_events", lambda *_args, **_kwargs: None)
        results = _call_evaluate_candidate(
            tmp_path, scenario_ids=["summarize-readme"], repeat_override=None
        )
        assert len(results) >= 1
        assert calls  # run_single_attempt was actually invoked

    def test_repeat_none_uses_holdout_dependent_config_defaults(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        config = copy.deepcopy(mh.DEFAULTS)
        config["evaluate"]["repeat_default"] = 2
        config["evaluate"]["repeat_frontier"] = 3
        emitted: list[dict] = []

        def fake_run_single_attempt(**kwargs):
            scenario_id = kwargs["scenario"]["id"]
            attempt = kwargs["attempt"]
            return {
                "run_id": f"run-{scenario_id}-{attempt}",
                "cand_id": kwargs["cand_id"],
                "scenario_id": scenario_id,
                "verdict": "pass",
                "quality_score": 100.0,
                "critical_pass_rate": 1.0,
                "cost": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "tool_uses": 0,
                    "duration_ms": 1,
                    "total_cost_usd": 0.0,
                    "num_turns": 1,
                },
                "attempt": attempt,
                "attempts_total": kwargs["attempts_total"],
            }

        monkeypatch.setattr(ev, "run_single_attempt", fake_run_single_attempt)
        monkeypatch.setattr(ev.siso, "execution_boundary_available", lambda _config: True)
        monkeypatch.setattr(
            ev,
            "candidate_impact_context",
            lambda **_kwargs: ev.skill_targets.SkillImpactContext((), "c" * 64),
        )
        monkeypatch.setattr(
            ev,
            "_append_evaluation_events",
            lambda _root, _config, _schema, events: emitted.extend(events),
        )
        cand_id = "cand-20260715-120000-repeat-ab12"
        manifest = _manifest(cand_id=cand_id, target="skill:handoff")
        _append_registration(tmp_path, config, manifest)

        ev.evaluate_candidate(
            main_root=tmp_path,
            config=config,
            schema_dir=_SCHEMA_DIR,
            package_dir=_PACKAGE_DIR,
            project_dir=tmp_path,
            cand_id=cand_id,
            manifest=manifest,
            scenario_ids=None,
            repeat_override=None,
            cli_capabilities={"claude_version": "2.1.207", "ok": True},
        )

        run_events = [event for event in emitted if event["event"] == "run_completed"]
        train_events = [event for event in run_events if not event["holdout"]]
        holdout_events = [event for event in run_events if event["holdout"]]
        assert len(train_events) == config["evaluate"]["repeat_default"]
        assert {event["attempts_total"] for event in train_events} == {
            config["evaluate"]["repeat_default"]
        }
        assert len(holdout_events) == config["evaluate"]["repeat_frontier"]
        assert {event["attempts_total"] for event in holdout_events} == {
            config["evaluate"]["repeat_frontier"]
        }


class TestLedgerProvenanceValidation:
    def test_evaluate_rejects_tampered_manifest_created_by(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        overlay_file = overlay_dir / "facets/example/SKILL.md"
        overlay_file.parent.mkdir(parents=True)
        overlay_file.write_text("# example\n", encoding="utf-8")
        manifest = {
            **_manifest(),
            "config_hash": mh.compute_config_hash(overlay_dir, mh.DEFAULTS),
            "overlay_files": ["facets/example/SKILL.md"],
        }
        mh.register_candidate(
            tmp_path,
            mh.DEFAULTS,
            cand_id=_CAND_ID,
            manifest=manifest,
            overlay_dir=overlay_dir,
            overlay_files=manifest["overlay_files"],
            target="claude-harness",
            created_by="human",
            schema_dir=_SCHEMA_DIR,
        )
        _append_registration(tmp_path, mh.DEFAULTS, manifest)
        manifest_path = mh.candidates_dir(tmp_path, mh.DEFAULTS) / _CAND_ID / "manifest.json"
        manifest_path.write_text(
            json.dumps({**manifest, "created_by": "proposer"}) + "\n",
            encoding="utf-8",
        )
        tampered = mh.read_candidate_manifest(tmp_path, mh.DEFAULTS, _CAND_ID)
        assert tampered is not None

        with pytest.raises(ev.EvaluatorStageError, match=r"created_by.*ledger provenance"):
            ev.evaluate_candidate(
                main_root=tmp_path,
                config=mh.DEFAULTS,
                schema_dir=_SCHEMA_DIR,
                package_dir=_PACKAGE_DIR,
                project_dir=tmp_path,
                cand_id=_CAND_ID,
                manifest=tampered,
                scenario_ids=None,
                repeat_override=None,
                cli_capabilities={},
            )


class TestScenarioIdValidation:
    def test_unknown_scenario_id_alone_is_rejected(self, tmp_path: Path) -> None:
        try:
            _call_evaluate_candidate(tmp_path, scenario_ids=["not-a-real-scenario"])
        except ValueError as exc:
            assert "not-a-real-scenario" in str(exc)
        else:
            raise AssertionError("unknown --scenario id should raise ValueError")

    def test_one_unknown_scenario_id_among_valid_ones_is_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """既知の scenario id と未知の id が混在する場合、既知の分だけで黙って実行しない。"""
        monkeypatch.setattr(
            ev, "run_single_attempt", lambda **kwargs: {"run_id": "run-x", "verdict": "pass"}
        )
        try:
            _call_evaluate_candidate(
                tmp_path, scenario_ids=["summarize-readme", "not-a-real-scenario"]
            )
        except ValueError as exc:
            assert "not-a-real-scenario" in str(exc)
        else:
            raise AssertionError(
                "a single unknown --scenario id must reject the whole request"
                " (partial silent execution is not acceptable)"
            )
