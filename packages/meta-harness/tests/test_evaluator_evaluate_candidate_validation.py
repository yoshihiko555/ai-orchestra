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

from pathlib import Path

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


def _call_evaluate_candidate(
    tmp_path: Path, *, scenario_ids: list[str] | None = None, repeat_override: int | None = None
) -> list[dict]:
    manifest = {"target": "claude-harness", "source_commit": "0" * 40, "config_hash": "b" * 64}
    return ev.evaluate_candidate(
        main_root=tmp_path,
        config=mh.DEFAULTS,
        schema_dir=_SCHEMA_DIR,
        package_dir=_PACKAGE_DIR,
        project_dir=tmp_path,
        cand_id="cand-20260707-120000-slug-ab12",
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
        """--repeat 省略時（None）はシナリオ既定値を使うため検証エラーにならない。"""
        calls: list[int] = []

        def fake_run_single_attempt(**kwargs):
            calls.append(kwargs["attempt"])
            return {"run_id": f"run-fake-{kwargs['attempt']}", "verdict": "pass"}

        monkeypatch.setattr(ev, "run_single_attempt", fake_run_single_attempt)
        results = _call_evaluate_candidate(
            tmp_path, scenario_ids=["summarize-readme"], repeat_override=None
        )
        assert len(results) >= 1
        assert calls  # run_single_attempt was actually invoked


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
