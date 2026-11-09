"""EV-69〜72 と承認・差し戻し・表示・出来事の契約。"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import candidates_model as model
import candidates_scenarios as scenarios
import pytest
import yaml
from candidates_checks import DraftBaseline
from candidates_draft import draft, file_hashes, load_draft
from candidates_safety import ValidationError, display
from candidates_scenario_events import latest_record
from candidates_view import show

CID = "C-00000001"
REPO = Path(__file__).resolve().parents[3]


def answers(context, number=1, *, lines=None):
    return {
        "draft": number,
        "files": file_hashes(
            load_draft(context, latest_record(context.store.read(), CID, "scenario.drafted"))
        ),
        "answers": {
            "reproduces": True,
            "holdout_distinct": True,
            "publishable": True,
            "human_lines_reviewed": bool(lines),
        },
        "human_written_lines": lines or [],
    }


@pytest.fixture
def drafted_context(candidate_context_factory, drafted_template):
    return candidate_context_factory(drafted_template)


@pytest.fixture
def approved_context(drafted_context):
    assert scenarios.approve(drafted_context, CID, answers(drafted_context))["approved"]
    return drafted_context


@pytest.fixture
def fake_git(monkeypatch):
    state = {
        "branch": "feature",
        "default_branch": "main",
        "ancestor": True,
        "worktrees": [],
        "commit": "a" * 40,
        "calls": [],
        "dirty": "",
        "blobs": None,
    }

    def read(directory, *arguments, check=True, binary=False):
        state["calls"].append((directory, arguments))
        if arguments == ("worktree", "list", "--porcelain", "-z"):
            stdout = "".join(
                f"worktree {path}\0HEAD {'a' * 40}\0branch refs/heads/feature\0\0"
                for path in state["worktrees"]
            )
        elif arguments == ("rev-parse", "--abbrev-ref", "HEAD"):
            stdout = state["branch"] + "\n"
        elif arguments == ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"):
            stdout = f"refs/remotes/origin/{state['default_branch']}\n"
        elif arguments == ("status", "--porcelain", "--", "packages/meta-harness/scenarios"):
            stdout = state["dirty"]
        elif arguments[:2] == ("ls-tree", "-rz"):
            suite = arguments[-1]
            blobs = state["blobs"]
            names = (
                blobs.keys()
                if blobs is not None
                else (path.name for path in (directory / suite).glob("*.yaml"))
            )
            stdout = "".join(f"100644 blob {'a' * 40}\t{suite}/{name}\0" for name in names)
        elif arguments[:2] == ("cat-file", "blob"):
            path = arguments[2].removeprefix("HEAD:")
            stdout = (
                state["blobs"][Path(path).name]
                if state["blobs"] is not None
                else (directory / path).read_bytes()
            )
        elif arguments[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(arguments, 0 if state["ancestor"] else 1, "", "")
        elif arguments == ("rev-parse", "HEAD"):
            stdout = state["commit"] + "\n"
        else:
            pytest.fail(f"許可していない git 呼び出し: {arguments}")
        return subprocess.CompletedProcess(arguments, 0, stdout, "")

    monkeypatch.setattr(scenarios, "git_read", read)
    return state


def installed(context, fake_git):
    fake_git["worktrees"] = [context.project]
    scenarios.install(context, CID, context.project)
    fake_git["branch"] = "main"
    local = context.project / ".claude/config/meta-harness/meta-harness.local.yaml"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("evaluate:\n  isolation:\n    execution_backend: docker\n")
    return context.project / "packages/meta-harness/scenarios/skill/review"


def test_ev69_approval_records_raw_hashes_and_replays_after_projection_loss(approved_context):
    context = approved_context
    snapshot = context.store.read()
    approval = latest_record(snapshot, CID, "scenario.approved")
    assert approval["data"]["files"] == {
        name: hashlib.sha256(raw).hexdigest() for name, raw in load_draft(context, approval).items()
    }
    assert snapshot["candidates"][CID]["state"] == "scenario_approved"
    (context.store.directory / "candidates-state.json").unlink()
    assert context.store.read() == snapshot
    corrupted = json.loads(json.dumps(snapshot, default=sorted))
    corrupted["records"][approval["eid"]]["data"]["files"] = {"bad.yaml": "not-a-hash"}
    (context.store.directory / "candidates-state.json").write_text(json.dumps(corrupted))
    assert context.store.read() == snapshot


@pytest.mark.parametrize(
    "field", ["reproduces", "holdout_distinct", "publishable", "missing", "not-bool", "old-draft"]
)
def test_ev69_approval_requires_current_draft_and_complete_confirmations(drafted_context, field):
    context = drafted_context
    payload = answers(context)
    if field == "missing":
        del payload["answers"]["publishable"]
    elif field == "not-bool":
        payload["answers"]["publishable"] = 1
    elif field == "old-draft":
        payload["draft"] = 2
    else:
        payload["answers"][field] = False
    with pytest.raises(ValidationError):
        scenarios.approve(drafted_context, CID, payload)
    assert latest_record(drafted_context.store.read(), CID, "scenario.approved") is None


def test_ev66_human_override_requires_every_exact_line_and_never_bypasses_publish(drafted_context):
    context = drafted_context
    snapshot = context.store.read()
    drafted = latest_record(snapshot, CID, "scenario.drafted")
    directory = context.store.directory / "drafts" / CID / "1"
    name = next(iter(drafted["data"]["files"]))
    file = directory / name
    file.write_text(file.read_text() + "setup:\n- git status\npermission_mode: bypassPermissions\n")
    result = scenarios.approve(context, CID, answers(context))
    assert result["approved"] is False
    lines = [
        {key: finding[key] for key in ("file", "path", "line")}
        for finding in result["checks"]["findings"]
        if finding["category"] == "shape"
    ]
    assert len(lines) == 2
    assert scenarios.approve(context, CID, answers(context, lines=lines[:1]))["approved"] is False
    payload = answers(context, lines=lines)
    payload["answers"]["human_lines_reviewed"] = False
    with pytest.raises(ValidationError):
        scenarios.approve(context, CID, payload)
    file.write_text(file.read_text() + "# owner/repository#17\n")
    assert scenarios.approve(context, CID, answers(context, lines=lines))["approved"] is False
    file.write_text(file.read_text().removesuffix("# owner/repository#17\n"))
    assert scenarios.approve(context, CID, answers(context, lines=lines))["approved"] is True
    approval = latest_record(context.store.read(), CID, "scenario.approved")
    assert approval["data"]["human_written_lines"] == lines


def test_ev69_send_back_needs_reason_and_old_approval_cannot_approve_redraft(
    approved_context, scenario_texts, fake_claude
):
    context = approved_context
    with pytest.raises(ValidationError):
        scenarios.send_back(context, CID, "")
    scenarios.send_back(context, CID, "入力を変える")
    old = latest_record(context.store.read(), CID, "scenario.approved")
    executable, _ = fake_claude(scenario_texts(suffix="new"))
    draft(context, CID, executable=executable)
    with pytest.raises(ValidationError):
        context.store.write(
            lambda snapshot: [
                scenarios.state_event(
                    snapshot["candidates"][CID], "scenario_approved", refs=[old["eid"]]
                )
            ]
        )
    with pytest.raises(ValidationError):
        scenarios.approve(context, CID, answers(context))
    assert scenarios.approve(context, CID, answers(context, 2))["approved"]


def test_ev69_changed_approved_draft_blocks_install_and_verification(approved_context, fake_git):
    context = approved_context
    suite = installed(context, fake_git)
    approval = latest_record(context.store.read(), CID, "scenario.approved")
    name = next(iter(approval["data"]["files"]))
    original = (suite / name).read_bytes()
    file = context.store.directory / "drafts" / CID / "1" / name
    file.write_bytes(original + b"# changed\n")
    with pytest.raises(ValidationError, match="下書きが変わって"):
        scenarios.install(context, CID, context.project)
    assert scenarios.verify_suite(context, CID)["verified"] is False
    assert latest_record(context.store.read(), CID, "scenario.suite_verified") is None
    assert context.store.read()["candidates"][CID]["state"] == "drafting"
    assert (suite / name).read_bytes() == original


def test_ev69_show_contains_full_control_escaped_raw_text_and_separate_lines(drafted_context):
    context = drafted_context
    name = next(iter(latest_record(context.store.read(), CID, "scenario.drafted")["data"]["files"]))
    file = context.store.directory / "drafts" / CID / "1" / name
    original = (
        file.read_text()
        + "# <full-text> \x1b\u202e\nsetup:\n- git status\n- printf 'synthetic' > result\n"
    )
    file.write_text(original)
    payload = show(context, context.store.read(), CID)
    rendered = json.loads(display(payload))
    review = rendered["draft_review"]
    assert review["files"][name] == scenarios.escaped_text(original)
    assert "<full-text>" in review["files"][name]
    assert "\\u001b" in review["files"][name] and "\\u202e" in review["files"][name]
    assert any(line["line"] == "git status" for line in review["checks"]["executable_lines"])
    assert any(
        line["line"] == "printf 'synthetic' > result"
        for line in review["checks"]["executable_lines"]
    )
    assert review["checks"]["findings"]


@pytest.mark.parametrize(
    "variation",
    [
        "unregistered",
        "main",
        "detached",
        "root-link",
        "packages-link",
        "meta-link",
        "scenarios-link",
        "skill-link",
        "slug-link",
        "existing",
        "existing-link",
    ],
)
def test_ev70_install_refuses_unsafe_destinations_without_copying(
    approved_context, fake_git, tmp_path, variation
):
    context = approved_context
    destination = tmp_path / "destination"
    destination.mkdir()
    fake_git["worktrees"] = [destination]
    if variation == "unregistered":
        fake_git["worktrees"] = []
    elif variation in {"main", "detached"}:
        fake_git["branch"] = "main" if variation == "main" else "HEAD"
    elif variation == "root-link":
        link = tmp_path / "root-link"
        link.symlink_to(destination, target_is_directory=True)
        destination = link
    elif variation.endswith("-link") and variation != "existing-link":
        parts = {
            "packages-link": "packages",
            "meta-link": "packages/meta-harness",
            "scenarios-link": "packages/meta-harness/scenarios",
            "skill-link": "packages/meta-harness/scenarios/skill",
            "slug-link": "packages/meta-harness/scenarios/skill/review",
        }
        link = destination / parts[variation]
        link.parent.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        link.symlink_to(outside, target_is_directory=True)
    else:
        suite = destination / "packages/meta-harness/scenarios/skill/review"
        suite.mkdir(parents=True)
        approval = latest_record(context.store.read(), CID, "scenario.approved")
        name = list(approval["data"]["files"])[-1]
        if variation == "existing-link":
            outside = tmp_path / "outside-file"
            outside.write_text("existing")
            (suite / name).symlink_to(outside)
        else:
            (suite / name).write_text("existing")
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*.yaml") if path.is_file()}
    with pytest.raises((ValidationError, OSError)):
        scenarios.install(context, CID, destination)
    assert before == {
        str(path): path.read_bytes() for path in tmp_path.rglob("*.yaml") if path.is_file()
    }


def test_ev70_install_copies_exact_approved_bytes_and_gives_budget_guidance(
    approved_context, fake_git, tmp_path
):
    context = approved_context
    destination = tmp_path / "new-worktree"
    destination.mkdir()
    fake_git["worktrees"] = [destination]
    result = scenarios.install(context, CID, destination)
    suite = destination / "packages/meta-harness/scenarios/skill/review"
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in suite.glob("*.yaml")
    } == result["installed"]
    assert suite.stat().st_mode & 0o777 == 0o755
    for text in (
        "regression.max_budget_usd",
        "max_affected_suites",
        ".claude/orchestra.json",
        "file_hashes",
        "昇格が無い",
    ):
        assert text in result["guidance"]
    assert any(
        args == ("merge-base", "--is-ancestor", "origin/main", "HEAD")
        for _, args in fake_git["calls"]
    )


def test_ev71_suite_hash_matches_meta_harness_for_every_existing_suite(meta_evaluator):
    evaluator = meta_evaluator
    suites = list((REPO / "packages/meta-harness/scenarios/skill").iterdir())
    assert suites
    for suite in suites:
        if suite.is_dir():
            paths = evaluator.discover_scenario_paths(suite)
            assert scenarios.installed_suite_hash(REPO, suite.name) == evaluator.compute_suite_hash(
                paths
            )
            assert scenarios.compute_suite_hash(
                list(reversed(paths))
            ) == evaluator.compute_suite_hash(paths)


def test_ev71_verify_suite_enters_improving_and_reverification_tracks_whole_suite(
    approved_context, fake_git
):
    context = approved_context
    suite = installed(context, fake_git)
    first = scenarios.verify_suite(context, CID)
    snapshot = context.store.read()
    assert snapshot["candidates"][CID]["state"] == "improving"
    record = latest_record(snapshot, CID, "scenario.suite_verified")
    assert record["data"]["suite_hash"] == scenarios.compute_suite_hash(list(suite.glob("*.yaml")))
    assert record["data"]["commit"] == "a" * 40
    baseline = suite / "existing.yaml"
    baseline.write_bytes(baseline.read_bytes() + b"# baseline changed\n")
    second = scenarios.verify_suite(context, CID)
    assert first["suite_hash"] != second["suite_hash"]
    snapshot = context.store.read()
    (context.store.directory / "candidates-state.json").unlink()
    assert context.store.read() == snapshot


@pytest.mark.parametrize("backend", [None, "srt"], ids=["real-config", "local-srt"])
def test_ev72_verify_suite_uses_real_evaluate_backend_and_local_override(
    approved_context, fake_git, backend
):
    context = approved_context
    installed(context, fake_git)
    local = context.project / ".claude/config/meta-harness/meta-harness.local.yaml"
    local.unlink()
    relative = "packages/meta-harness/config/meta-harness.yaml"
    assert (context.orchestra / relative).read_bytes() == (REPO / relative).read_bytes()
    assert not local.with_name("meta-harness.yaml").exists()
    assert not (context.orchestra / relative).with_name("meta-harness.local.yaml").exists()
    if backend is None:
        assert scenarios.verify_suite(context, CID)["verified"] is True
    else:
        local.write_text(f"evaluate:\n  isolation:\n    execution_backend: {backend}\n")
        with pytest.raises(ValidationError, match=r"evaluate\.isolation\.execution_backend"):
            scenarios.verify_suite(context, CID)
        assert latest_record(context.store.read(), CID, "scenario.suite_verified") is None
        assert context.store.read()["candidates"][CID]["state"] == "scenario_approved"


@pytest.mark.parametrize(
    "variation",
    ["missing", "hash", "unreachable", "backend", "not-main", "corrupt-config", "local-null"],
)
def test_ev72_verification_failure_records_no_suite_verification(
    approved_context, fake_git, variation
):
    context = approved_context
    suite = installed(context, fake_git)
    approval = latest_record(context.store.read(), CID, "scenario.approved")
    file = suite / next(iter(approval["data"]["files"]))
    if variation == "missing":
        file.unlink()
    elif variation == "hash":
        file.write_bytes(file.read_bytes() + b"# drift\n")
    elif variation == "unreachable":
        fake_git["ancestor"] = False
    elif variation == "not-main":
        fake_git["branch"] = "feature"
    else:
        local = context.project / ".claude/config/meta-harness/meta-harness.local.yaml"
        local.write_text(
            "[broken"
            if variation == "corrupt-config"
            else "evaluate: null\n"
            if variation == "local-null"
            else "evaluate:\n  isolation:\n    execution_backend: srt\n"
        )
    if variation in {"missing", "hash"}:
        failure = scenarios.verify_suite(context, CID)
        assert failure["verified"] is False and failure["events"]
    else:
        with pytest.raises(ValidationError):
            scenarios.verify_suite(context, CID)
    snapshot = context.store.read()
    assert latest_record(snapshot, CID, "scenario.suite_verified") is None
    assert snapshot["candidates"][CID]["state"] == (
        "drafting" if variation in {"missing", "hash"} else "scenario_approved"
    )


def test_ev72_changed_approved_main_file_sends_improving_back_to_drafting(
    approved_context, fake_git
):
    context = approved_context
    suite = installed(context, fake_git)
    scenarios.verify_suite(context, CID)
    approval = latest_record(context.store.read(), CID, "scenario.approved")
    (suite / next(iter(approval["data"]["files"]))).unlink()
    assert scenarios.verify_suite(context, CID)["verified"] is False
    snapshot = context.store.read()
    assert snapshot["candidates"][CID]["state"] == "drafting"
    assert (
        len(
            [
                record
                for record in snapshot["records"].values()
                if record["type"] == "scenario.suite_verified"
            ]
        )
        == 1
    )


def test_ev72_effective_config_mirrors_project_base_local_and_package_fallback(scenario_context):
    context = scenario_context
    base = context.orchestra / "packages/meta-harness/config/meta-harness.yaml"
    base.write_text("evaluate:\n  isolation:\n    execution_backend: srt\n    keep: yes\n")
    local = context.project / ".claude/config/meta-harness/meta-harness.local.yaml"
    local.parent.mkdir(parents=True)
    local.write_text("evaluate:\n  isolation:\n    execution_backend: docker\n")
    assert scenarios.effective_meta_config(context)["evaluate"]["isolation"] == {
        "execution_backend": "docker",
        "keep": True,
    }
    local.with_name("meta-harness.yaml").write_text(
        "evaluate:\n  isolation:\n    execution_backend: srt\n    project: yes\n"
    )
    assert scenarios.effective_meta_config(context)["evaluate"]["isolation"] == {
        "execution_backend": "docker",
        "project": True,
    }


def test_ev69_scenario_events_reject_invalid_actor_hash_and_stale_draft(drafted_context):
    snapshot = drafted_context.store.read()
    drafted = latest_record(snapshot, CID, "scenario.drafted")
    for change in (
        {"actor": "ingest"},
        {"data": {**drafted["data"], "files": {"bad.yaml": "not-a-hash"}}},
    ):
        record = {**copy.deepcopy(drafted), "eid": "deadbeef0000", **change}
        with pytest.raises(ValidationError):
            model.apply_event(copy.deepcopy(snapshot), record)
    retired = model.event(
        "scenario.retired",
        CID,
        {"files": drafted["data"]["files"], "pr_url": "https://github.com/owner/repo/pull/1"},
    )
    assert retired["eid"] in model.apply_event(snapshot, retired)["records"]


def test_ev69_admin_and_read_cli_cover_draft_approval_send_back_install_and_verify(
    scenario_context, scenario_texts, fake_claude, fake_git, monkeypatch, capsys
):
    from tests.module_loader import load_module

    context = scenario_context
    admin = load_module(
        "phase3_admin_cli", str(REPO / "packages/skill-evolution/scripts/skill_candidates_admin.py")
    )
    reader = load_module(
        "phase3_read_cli", str(REPO / "packages/skill-evolution/scripts/skill_candidates.py")
    )
    executable, _ = fake_claude(scenario_texts())
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", executable)
    assert admin.main(["draft", CID]) == 0
    capsys.readouterr()
    assert reader.main(["show", CID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["draft_review"]["files"]
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(answers(context))))
    assert admin.main(["approve", CID, "--json", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["approved"] is True
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps({"note": "人が入力を再確認"})))
    assert admin.main(["send-back", CID, "--json", "-"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(answers(context))))
    assert admin.main(["approve", CID, "--json", "-"]) == 0
    capsys.readouterr()
    fake_git["worktrees"] = [context.project]
    assert admin.main(["install", CID, "--dest", str(context.project)]) == 0
    assert json.loads(capsys.readouterr().out)["installed"]
    fake_git["branch"] = "main"
    local = context.project / ".claude/config/meta-harness/meta-harness.local.yaml"
    local.parent.mkdir(parents=True)
    local.write_text("evaluate:\n  isolation:\n    execution_backend: docker\n")
    assert admin.main(["verify-suite", CID]) == 0
    assert json.loads(capsys.readouterr().out)["suite_hash"]


def test_ev69_cli_approval_failure_returns_findings_and_exit_five(
    drafted_context, monkeypatch, capsys
):
    from tests.module_loader import load_module

    context = drafted_context
    record = latest_record(context.store.read(), CID, "scenario.drafted")
    name = next(iter(record["data"]["files"]))
    file = context.store.directory / "drafts" / CID / "1" / name
    file.write_bytes(file.read_bytes() + b"setup:\n- git status\n")
    admin = load_module(
        "phase3_refused_admin_cli",
        str(REPO / "packages/skill-evolution/scripts/skill_candidates_admin.py"),
    )
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(answers(context))))
    assert admin.main(["approve", CID, "--json", "-"]) == 5
    assert json.loads(capsys.readouterr().out)["checks"]["findings"]


def test_ev69_stale_review_hash_and_unmodified_llm_human_line_are_refused(drafted_context):
    context = drafted_context
    payload = answers(context)
    record = latest_record(context.store.read(), CID, "scenario.drafted")
    name = next(iter(record["data"]["files"]))
    file = context.store.directory / "drafts" / CID / "1" / name
    file.write_bytes(file.read_bytes() + b"# human edit\n")
    with pytest.raises(ValidationError, match="sha256"):
        scenarios.approve(context, CID, payload)
    review = show(context, context.store.read(), CID)["draft_review"]
    original_line = review["checks"]["executable_lines"][0]
    with pytest.raises(ValidationError, match="元の行"):
        scenarios.approve(context, CID, answers(context, lines=[original_line]))
    assert review["sha256"] == answers(context)["files"]
    assert review["evaluation_section"] == "評価に渡る文"
    assert review["checks"]["evaluation_texts"]


def test_ev69_show_marks_changed_approval_invalid_without_state_change(approved_context):
    context = approved_context
    record = latest_record(context.store.read(), CID, "scenario.drafted")
    name = next(iter(record["data"]["files"]))
    assert show(context, context.store.read(), CID)["draft_review"]["approval_valid"] is True
    file = context.store.directory / "drafts" / CID / "1" / name
    file.write_bytes(file.read_bytes() + b"# edited\n")
    assert show(context, context.store.read(), CID)["draft_review"]["approval_valid"] is False
    with pytest.raises(ValidationError):
        scenarios.install(context, CID, context.project)
    assert context.store.read()["candidates"][CID]["state"] == "scenario_approved"


@pytest.mark.parametrize(
    "variation",
    ["dirty", "untracked-sevo", "unapproved-sevo", "git-error", "permission", "symlink"],
)
def test_ev72_non_mismatch_errors_preserve_state_and_record_no_events(
    approved_context, fake_git, monkeypatch, variation
):
    context = approved_context
    suite = installed(context, fake_git)
    before = context.store.read()
    if variation in {"dirty", "untracked-sevo"}:
        fake_git["dirty"] = (
            "?? packages/meta-harness/scenarios/skill/review/sevo-untracked.yaml\n"
            if variation == "untracked-sevo"
            else " M packages/meta-harness/scenarios/skill/review/existing.yaml\n"
        )
    elif variation == "unapproved-sevo":
        (suite / "sevo-unapproved.yaml").write_text("id: sevo-unapproved\n")
    elif variation == "git-error":
        monkeypatch.setattr(
            scenarios,
            "committed_suite",
            lambda *args: (_ for _ in ()).throw(ValidationError("git 失敗")),
        )
    elif variation == "permission":
        monkeypatch.setattr(
            scenarios,
            "load_draft",
            lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("権限不足")),
        )
    else:
        file = suite / "existing.yaml"
        file.rename(suite / "source.txt")
        file.symlink_to(suite / "source.txt")
    with pytest.raises((ValidationError, OSError)):
        scenarios.verify_suite(context, CID)
    assert context.store.read() == before


def test_ev71_verify_hashes_committed_blobs_instead_of_working_files(approved_context, fake_git):
    context = approved_context
    suite = installed(context, fake_git)
    fake_git["blobs"] = {file.name: file.read_bytes() for file in suite.glob("*.yaml")}
    expected = scenarios.compute_suite_hash(list(suite.glob("*.yaml")))
    (suite / "existing.yaml").write_bytes(b"different worktree bytes\r\n")
    verified = scenarios.verify_suite(context, CID)
    assert verified["suite_hash"] == expected
    assert "git fetch origin main" in verified["note"]


@pytest.mark.parametrize("slug", ["../escape", "review/evil", "review\n", "Review", ""])
def test_ev70_suite_descriptor_rejects_non_slug_names(tmp_path, slug):
    with pytest.raises(ValidationError):
        with scenarios.suite_descriptor(tmp_path, slug, create=True):
            pytest.fail("不正な slug を受け付けた")
    assert not (tmp_path / "packages").exists()


def test_ev70_missing_destination_is_input_error(approved_context, tmp_path):
    with pytest.raises(ValidationError) as caught:
        scenarios.install(approved_context, CID, tmp_path / "absent")
    assert caught.value.code == 5


def test_ev70_install_and_ev71_verify_use_real_git_linked_worktree(
    approved_context, tmp_path, monkeypatch
):
    import os

    context = approved_context
    root = tmp_path / "git repo"
    root.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args, input=None):
        completed = subprocess.run(
            ["git", "-C", str(root), *args], input=input, capture_output=True, env=env, check=True
        )
        return completed.stdout.decode().strip()

    git("init", "--initial-branch=trunk", "--template=")
    suite = root / "packages/meta-harness/scenarios/skill/review"
    suite.mkdir(parents=True)
    approval = latest_record(context.store.read(), CID, "scenario.approved")
    for name, raw in load_draft(context, approval).items():
        (suite / name).write_bytes(raw)
    (suite / "human.yaml").write_bytes(b"# human baseline\r\n")
    git("add", "packages")
    tree = git("write-tree")
    commit_bytes = f"tree {tree}\nauthor Test <test@example.invalid> 1 +0000\ncommitter Test <test@example.invalid> 1 +0000\n\nfixture\n".encode()
    commit = git("hash-object", "-w", "-t", "commit", "--stdin", input=commit_bytes)
    git("update-ref", "refs/heads/trunk", commit)
    git("update-ref", "refs/remotes/origin/trunk", commit)
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    linked = tmp_path / "linked worktree with spaces"
    git("worktree", "add", "--no-checkout", "-b", "feature", str(linked), "trunk")
    monkeypatch.setattr(context, "orchestra", root)
    monkeypatch.setattr(context, "project", root)
    with pytest.raises(ValidationError, match="既定ブランチ"):
        scenarios.install(context, CID, root)
    assert scenarios.install(context, CID, linked)["installed"] == approval["data"]["files"]
    wrong = tmp_path / "non-ancestor worktree"
    git("worktree", "add", "--no-checkout", "-b", "wrong", str(wrong), "trunk")
    (root / ".git/refs/remotes/origin/trunk").write_text("f" * 40 + "\n")
    with pytest.raises(ValidationError, match="祖先"):
        scenarios.install(context, CID, wrong)
    git("update-ref", "refs/remotes/origin/trunk", commit)
    local = root / ".claude/config/meta-harness/meta-harness.local.yaml"
    local.parent.mkdir(parents=True)
    local.write_text("evaluate:\n  isolation:\n    execution_backend: docker\n")
    untracked = suite / "sevo-untracked.yaml"
    untracked.write_text("# untracked\n")
    with pytest.raises(ValidationError, match="未コミット"):
        scenarios.verify_suite(context, CID)
    untracked.unlink()
    assert context.store.read()["candidates"][CID]["state"] == "scenario_approved"
    assert scenarios.verify_suite(context, CID)["suite_hash"] == scenarios.compute_suite_hash(
        list(suite.glob("*.yaml"))
    )
    assert context.store.read()["candidates"][CID]["state"] == "improving"


def test_ev69_missing_approved_draft_is_invalid_and_only_verify_sends_back(
    approved_context, fake_git
):
    context = approved_context
    installed(context, fake_git)
    approval = latest_record(context.store.read(), CID, "scenario.approved")
    name = next(iter(approval["data"]["files"]))
    (context.store.directory / "drafts" / CID / "1" / name).unlink()
    assert show(context, context.store.read(), CID)["draft_review"]["approval_valid"] is False
    assert context.store.read()["candidates"][CID]["state"] == "scenario_approved"
    failure = scenarios.verify_suite(context, CID)
    assert failure["verified"] is False
    assert failure["events"][0]["data"]["to"] == "drafting"


def test_ev70_default_branch_falls_back_to_main_only_for_absent_tracking_head(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        scenarios, "git_read", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "")
    )
    assert scenarios.default_branch(tmp_path) == "main"
    monkeypatch.setattr(
        scenarios, "git_read", lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, "", "")
    )
    with pytest.raises(ValidationError):
        scenarios.default_branch(tmp_path)


@pytest.mark.parametrize("variation", ["broken", "schema-invalid"])
def test_ev68_existing_scenario_warnings_are_shown_without_blocking_draft_approval(
    drafted_context, scenario_texts, variation
):
    context = drafted_context
    before = show(context, context.store.read(), CID)["draft_review"]["checks"]
    file = context.orchestra / "packages/meta-harness/scenarios/skill/review/invalid.yaml"
    document = yaml.safe_load(scenario_texts()[0])
    document.update(
        id="invalid-existing-train",
        schema_version="invalid",
        allowed_tools=["Bash"],
        graded=document["critical"],
        repeat=999,
        setup=["git status"],
    )
    file.write_text("[broken" if variation == "broken" else yaml.safe_dump(document))
    baseline = DraftBaseline(context.orchestra)
    assert all(path != file for path, _ in baseline.existing)
    assert all(path != file for path, _ in baseline.human)
    rendered = json.loads(display(show(context, context.store.read(), CID)))
    checks = rendered["draft_review"]["checks"]
    assert checks["passed"] is True
    assert checks["findings"] == before["findings"] == []
    assert len(checks["warnings"]) == 1
    warning = checks["warnings"][0]
    assert warning["file"] == "packages/meta-harness/scenarios/skill/review/invalid.yaml"
    assert warning["category"] == "schema"
    assert warning["reason"].startswith("既存 YAML を検査できません:")
    assert scenarios.approve(context, CID, answers(context))["approved"] is True
    drafted = latest_record(context.store.read(), CID, "scenario.drafted")
    draft_file = (
        context.store.directory / "drafts" / CID / "1" / next(iter(drafted["data"]["files"]))
    )
    draft_file.write_text("[broken")
    checks = show(context, context.store.read(), CID)["draft_review"]["checks"]
    assert checks["passed"] is False
    assert any(finding["category"] == "schema" for finding in checks["findings"])
    assert all(
        finding["file"] != str(file.relative_to(context.orchestra))
        for finding in checks["findings"]
    )


@pytest.mark.parametrize("default", ["trunk", "release/stable"])
def test_ev70_install_and_ev71_verify_share_resolved_default_branch(
    approved_context, fake_git, default
):
    context = approved_context
    fake_git["default_branch"] = default
    fake_git["worktrees"] = [context.project]
    fake_git["branch"] = default
    with pytest.raises(ValidationError, match="既定ブランチ"):
        scenarios.validate_destination(context, context.project)
    fake_git["branch"] = "feature"
    installed(context, fake_git)
    assert (
        context.project,
        ("merge-base", "--is-ancestor", f"origin/{default}", "HEAD"),
    ) in fake_git["calls"]
    fake_git["branch"] = "main"
    with pytest.raises(ValidationError, match="既定ブランチ"):
        scenarios.verify_suite(context, CID)
    fake_git["branch"] = default
    fake_git["ancestor"] = False
    with pytest.raises(ValidationError, match=f"origin/{default}"):
        scenarios.verify_suite(context, CID)
    fake_git["ancestor"] = True
    verified = scenarios.verify_suite(context, CID)
    assert verified["verified"] is True
    assert f"git fetch origin {default}" in verified["note"]
    assert (
        context.project,
        ("merge-base", "--is-ancestor", "HEAD", f"origin/{default}"),
    ) in fake_git["calls"]
    assert all("origin/main" not in args for _, args in fake_git["calls"])


def test_ev71_verify_cli_accepts_approved_files_from_merged_candidate_aliases(
    approved_context,
    fake_git,
    fake_claude,
    scenario_texts,
    observation_event,
    candidate_event,
    capsys,
):
    from tests.module_loader import load_module

    context = approved_context
    installed(context, fake_git)
    context.store.write(
        lambda _: [
            record
            for number in (2, 3, 4)
            for record in (observation_event(number), candidate_event(number))
        ]
    )
    merged_cid = "C-00000002"
    executable, _ = fake_claude(
        [
            text.replace("sevo-00000001-", "sevo-00000002-")
            for text in scenario_texts(suffix="merged")
        ]
    )
    draft(context, merged_cid, executable=executable)
    record = latest_record(context.store.read(), merged_cid, "scenario.drafted")
    payload = {**answers(context), "files": file_hashes(load_draft(context, record))}
    assert scenarios.approve(context, merged_cid, payload)["approved"] is True
    fake_git["branch"] = "feature"
    scenarios.install(context, merged_cid, context.project)
    context.store.write(
        lambda snapshot: [
            scenarios.state_event(
                snapshot["candidates"][merged_cid], "rejected", note="重複を整理"
            ),
            model.event(
                "candidate.state_changed", merged_cid, {"from": "rejected", "to": "candidate"}
            ),
            model.event("candidate.merged", merged_cid, {"into": "C-00000003"}),
            model.event("candidate.merged", "C-00000003", {"into": "C-00000004"}),
        ]
    )
    snapshot = context.store.read()
    assert merged_cid not in snapshot["candidates"]
    assert model.require_candidate(snapshot, merged_cid)["cid"] == "C-00000004"
    assert latest_record(snapshot, merged_cid, "scenario.approved") is not None
    fake_git["branch"] = "main"
    admin = load_module(
        "phase3_merged_candidate_admin_cli",
        str(REPO / "packages/skill-evolution/scripts/skill_candidates_admin.py"),
    )
    assert admin.main(["verify-suite", CID]) == 0
    completed = capsys.readouterr()
    assert completed.err == ""
    assert json.loads(completed.out)["verified"] is True
    assert context.store.read()["candidates"][CID]["state"] == "improving"


@pytest.mark.parametrize("tool", ["Bash", "Bash(git status)"])
def test_ev66_bash_subset_requires_reviewed_human_edit(
    scenario_context, scenario_texts, fake_claude, tool
):
    context = scenario_context
    baseline = context.orchestra / "packages/meta-harness/scenarios/skill/review/existing.yaml"
    document = yaml.safe_load(baseline.read_text())
    document["allowed_tools"].append(tool)
    baseline.write_text(yaml.safe_dump(document))
    executable, _ = fake_claude(scenario_texts(allowed_tools=[tool]))
    drafted = draft(context, CID, executable=executable)
    assert drafted["checks"]["passed"] is False
    refused = scenarios.approve(context, CID, answers(context))
    assert refused["approved"] is False
    lines = [
        {key: finding[key] for key in ("file", "path", "line")}
        for finding in refused["checks"]["findings"]
        if finding["category"] == "shape"
    ]
    assert len(lines) == 2
    assert all(line["path"] == "$.allowed_tools" for line in lines)
    with pytest.raises(ValidationError, match="元の行"):
        scenarios.approve(context, CID, answers(context, lines=lines))
    record = latest_record(context.store.read(), CID, "scenario.drafted")
    for name, raw in load_draft(context, record).items():
        document = yaml.safe_load(raw)
        document["allowed_tools"] = ["Read", tool]
        file = context.store.directory / "drafts" / CID / "1" / name
        file.write_text(yaml.safe_dump(document, allow_unicode=True))
    refused = scenarios.approve(context, CID, answers(context))
    assert refused["approved"] is False
    lines = [
        {key: finding[key] for key in ("file", "path", "line")}
        for finding in refused["checks"]["findings"]
        if finding["category"] == "shape"
    ]
    assert scenarios.approve(context, CID, answers(context, lines=lines[:1]))["approved"] is False
    payload = answers(context, lines=lines)
    payload["answers"]["human_lines_reviewed"] = False
    with pytest.raises(ValidationError, match="人が書いた実行行"):
        scenarios.approve(context, CID, payload)
    assert scenarios.approve(context, CID, answers(context, lines=lines))["approved"] is True


@pytest.mark.parametrize("as_json", [False, True])
def test_ev69_show_prints_per_file_original_diff_of_human_changes_and_additions(
    drafted_context, capsys, as_json
):
    from tests.module_loader import load_module

    context = drafted_context
    review = show(context, context.store.read(), CID)["draft_review"]
    assert all(diff == "" for diff in review["diffs"].values())
    name, unchanged = review["files"]
    file = context.store.directory / "drafts" / CID / "1" / name
    original = file.read_text()
    old_line = "description: 人工的な手順不足の再現"
    new_line = "description: 人が再現手順を確認"
    added_line = "# 人の追記 <raw> \x1b\u202e"
    file.write_text(
        original.replace(old_line, new_line).replace("holdout: false\n", "") + added_line + "\n"
    )
    review = show(context, context.store.read(), CID)["draft_review"]
    diff = review["diffs"][name]
    assert diff.startswith(f"--- original/{name}\\u000a+++ {name}\\u000a@@ ")
    assert f"-{old_line}\\u000a" in diff
    assert f"+{new_line}\\u000a" in diff
    assert "-holdout: false\\u000a" in diff
    assert "+# 人の追記 <raw> \\u001b\\u202e\\u000a" in diff
    assert review["diffs"][unchanged] == ""
    assert (file.parent / "original" / name).read_text() == original
    reader = load_module(
        "phase3_human_diff_read_cli",
        str(REPO / "packages/skill-evolution/scripts/skill_candidates.py"),
    )
    assert reader.main(["show", CID, *(["--json"] if as_json else [])]) == 0
    completed = capsys.readouterr()
    assert completed.err == ""
    assert json.dumps(diff, ensure_ascii=False) in completed.out
    assert "\x1b" not in completed.out and "\u202e" not in completed.out


def test_ev69_show_refuses_diff_against_tampered_original(drafted_context):
    context = drafted_context
    record = latest_record(context.store.read(), CID, "scenario.drafted")
    name = next(iter(record["data"]["files"]))
    original = context.store.directory / "drafts" / CID / "1" / "original" / name
    original.write_bytes(original.read_bytes() + b"# tampered\n")
    review = show(context, context.store.read(), CID)["draft_review"]
    assert "元の下書きが変わって" in review["error"]
    assert "diffs" not in review
