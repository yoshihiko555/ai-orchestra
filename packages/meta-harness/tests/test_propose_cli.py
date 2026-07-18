"""Phase 2 M4: `meta propose` CLI の mocked backend E2E テスト。"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_propose_cli",
    "packages/meta-harness/lib/meta_harness_common.py",
)
propose_cli = load_module(
    "meta_harness_propose_cli_test",
    "packages/meta-harness/lib/propose_cli.py",
)
loop_cli = load_module(
    "meta_harness_loop_cli_propose_test",
    "packages/meta-harness/lib/loop_cli.py",
)

_PARENT_ID = "cand-20260708-020000-parent-abcd"
_RUN_ID = "run-20260708-020000-parent-scn-a1-abcd"
_HOLDOUT_RUN_ID = "run-20260708-020000-parent-scn-h1-abcd"
_ROUTING_PARENT_ID = "cand-20260717-080000-routing-baseline-abcd"
_ROUTING_RUN_ID = "run-20260717-080000-routing-baseline-train-a1-abcd"
_HASH = "a" * 64
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROUTING_CONFIG_RELATIVE = Path("packages/agent-routing/config/cli-tools.yaml")


def test_routing_config_without_citable_runs_rejects_before_backend(
    git_project: Path, git_run, monkeypatch, capsys
) -> None:
    _commit_facets(git_project, git_run)
    mh.init_store(git_project, mh.DEFAULTS)
    mh.write_frontier_cache(
        git_project,
        mh.DEFAULTS,
        mh._empty_frontier_doc(mh.DEFAULTS, "routing-config"),
        "routing-config",
    )

    def fail_if_launched(**_kwargs):
        raise AssertionError("backend must not launch without a citable baseline run")

    monkeypatch.setattr(propose_cli.pb, "launch_proposer_backend", fail_if_launched)

    exit_code = propose_cli.cmd_propose(str(git_project), "routing-config", None, None, False)

    assert exit_code == propose_cli.EXIT_VALIDATION_ERROR
    assert "no citable non-holdout runs for target: routing-config" in capsys.readouterr().err


def test_proposal_schema_exposes_both_simple_payload_shapes_without_one_of() -> None:
    schema = mh.load_schema(propose_cli._SCHEMA_DIR, "proposal.schema.json")
    serialized = json.dumps(schema, sort_keys=True)

    assert schema["properties"]["changes"]["items"]["properties"]["path"]["pattern"] == (
        "^facets/.+$"
    )
    assert set(schema["properties"]["config_patch"]["items"]["required"]) == {
        "file",
        "key_path",
        "value",
    }
    assert "oneOf" not in serialized


def _sample_sk_key(key_kind: str | None = None) -> str:
    """外部 scanner に触れるキーリテラルを置かず、検査用 sk- key を返す。"""
    parts = ["sk"]
    if key_kind:
        parts.append(key_kind)
    parts.append("abcdef0123456789ABCDEFghij")
    return "-".join(parts)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _install_stub_tools(bin_dir: Path, proposal: dict) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    proposal_json = json.dumps(proposal, ensure_ascii=False)
    _write_executable(
        bin_dir / "srt",
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("@anthropic-ai/sandbox-runtime 0.0.64")
    raise SystemExit(0)
if args[:1] == ["--settings"]:
    args = args[2:]
base = os.path.basename(args[0]) if args else ""
if base == "codex":
    os.execvp(args[0], args)
if base == "cat":
    print("cat: Operation not permitted", file=sys.stderr)
    raise SystemExit(1)
if base == "curl":
    print("curl: (56) connection reset by proxy", file=sys.stderr)
    raise SystemExit(56)
raise SystemExit(1)
""",
    )
    _write_executable(
        bin_dir / "codex",
        f"""#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
out = args[args.index("-o") + 1]
with open(out, "w", encoding="utf-8") as handle:
    handle.write({proposal_json!r})
    handle.write("\\n")
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":7,"output_tokens":3}}}}))
raise SystemExit(0)
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env python3
print("canary reachable")
raise SystemExit(0)
""",
    )


def _prepare_codex_auth(home: Path) -> None:
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")


def _commit_facets(git_project: Path, git_run) -> str:
    facet = git_project / "facets" / "example" / "SKILL.md"
    facet.parent.mkdir(parents=True, exist_ok=True)
    facet.write_text("# Example\n\nBaseline.\n", encoding="utf-8")
    git_run("add", "facets/example/SKILL.md", cwd=git_project)
    git_run("commit", "-m", "add facets", cwd=git_project)
    return git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()


def _prepare_store(
    git_project: Path,
    git_run,
    *,
    inherited_rel: str = "facets/parent-only/SKILL.md",
    inherited_content: str | bytes = "# Parent only\n\nInherited content.\n",
) -> None:
    config = mh.DEFAULTS
    source_commit = _commit_facets(git_project, git_run)
    mh.init_store(git_project, config)
    cand_dir = mh.candidates_dir(git_project, config) / _PARENT_ID
    overlay_file = cand_dir / "overlay" / "facets" / "example" / "SKILL.md"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("# Example\n\nParent overlay.\n", encoding="utf-8")
    inherited_file = cand_dir / "overlay" / inherited_rel
    inherited_file.parent.mkdir(parents=True)
    if isinstance(inherited_content, bytes):
        inherited_file.write_bytes(inherited_content)
    else:
        inherited_file.write_text(inherited_content, encoding="utf-8")
    overlay_dir = cand_dir / "overlay"
    manifest = {
        "schema_version": "1.0",
        "cand_id": _PARENT_ID,
        "parent_id": None,
        "generation": 0,
        "created_at": mh.now_iso(),
        "created_by": "human",
        "target": "claude-harness",
        "source_commit": source_commit,
        "config_hash": mh.compute_config_hash(overlay_dir, config),
        "model_versions": {},
        "overlay_files": mh.list_overlay_files(overlay_dir),
        "description": "parent",
    }
    (cand_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    run_dir = mh.runs_dir(git_project, config) / _RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"run_id": _RUN_ID, "holdout": False}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text('{"verdict":"fail"}\n', encoding="utf-8")
    with gzip.open(run_dir / "events.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write('{"type":"result"}\n')
    mh.write_frontier_cache(
        git_project,
        config,
        {
            "schema_version": "1.0",
            "generated_at": mh.now_iso(),
            "ledger_line_count": 1,
            "suite_hash": "c" * 64,
            "evaluator_hash": "d" * 64,
            "cost_axis": "total_tokens",
            "points": [
                {
                    "cand_id": _PARENT_ID,
                    "quality_mean": 80.0,
                    "quality_var": 0.0,
                    "quality_min": 80.0,
                    "cost_mean": 100.0,
                    "runs": 1,
                }
            ],
            "frontier": [_PARENT_ID],
            "dominated": [],
        },
    )
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "run_completed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "run_id": _RUN_ID,
            "cand_id": _PARENT_ID,
            "target": "claude-harness",
            "holdout": False,
        },
    )


def _prepare_routing_store(git_project: Path, git_run) -> None:
    """no-op baseline の register→evaluate→frontier 後に相当する store を作る。"""
    config = mh.DEFAULTS
    _commit_facets(git_project, git_run)
    routing_config = git_project / _ROUTING_CONFIG_RELATIVE
    routing_config.parent.mkdir(parents=True)
    routing_config.write_bytes((_REPO_ROOT / _ROUTING_CONFIG_RELATIVE).read_bytes())
    git_run("add", _ROUTING_CONFIG_RELATIVE.as_posix(), cwd=git_project)
    git_run("commit", "-m", "add routing config", cwd=git_project)
    source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
    mh.init_store(git_project, config)
    overlay_dir = mh.tmp_dir(git_project, config) / "routing-baseline-overlay"
    overlay_dir.mkdir(parents=True)
    config_patch = [
        {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": "agents.debugger.tool",
            "value": "codex",
        }
    ]
    (overlay_dir / mh.CONFIG_PATCH_FILENAME).write_bytes(
        mh.canonical_config_patch_bytes(config_patch)
    )
    manifest = mh.build_candidate_manifest(
        cand_id=_ROUTING_PARENT_ID,
        parent_id=None,
        generation=0,
        target="routing-config",
        source_commit=source_commit,
        config_hash=mh.compute_config_hash(overlay_dir, config),
        overlay_files=[],
        description="routing baseline using the current debugger tool",
        config_patch_hash=mh.compute_config_patch_hash(config_patch),
    )
    mh.register_candidate(
        git_project,
        config,
        cand_id=_ROUTING_PARENT_ID,
        manifest=manifest,
        overlay_dir=overlay_dir,
        overlay_files=[],
        target="routing-config",
        created_by="human",
        baseline_root=git_project,
    )
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "candidate_registered",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": _ROUTING_PARENT_ID,
            "parent_id": None,
            "generation": 0,
            "target": "routing-config",
            "created_by": "human",
        },
    )
    run_dir = mh.runs_dir(git_project, config) / _ROUTING_RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"run_id": _ROUTING_RUN_ID, "holdout": False}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text('{"verdict":"pass"}\n', encoding="utf-8")
    with gzip.open(run_dir / "events.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write('{"type":"result"}\n')
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "run_completed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "run_id": _ROUTING_RUN_ID,
            "cand_id": _ROUTING_PARENT_ID,
            "target": "routing-config",
            "holdout": False,
        },
    )
    mh.write_frontier_cache(
        git_project,
        config,
        {
            "schema_version": "1.0",
            "target": "routing-config",
            "generated_at": mh.now_iso(),
            "ledger_line_count": 2,
            "suite_hash": _HASH,
            "evaluator_hash": _HASH,
            "cost_axis": "total_cost_usd",
            "points": [
                {
                    "cand_id": _ROUTING_PARENT_ID,
                    "quality_mean": 100.0,
                    "quality_var": 0.0,
                    "quality_min": 100.0,
                    "cost_mean": 0.01,
                    "runs": 1,
                }
            ],
            "frontier": [_ROUTING_PARENT_ID],
            "dominated": [],
        },
        "routing-config",
    )


def _events(project: Path) -> list[dict]:
    return mh.read_ledger_events(project, mh.DEFAULTS)


def _prepare_stubbed_codex(tmp_path: Path, proposal: dict) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    _prepare_codex_auth(fake_home)
    _install_stub_tools(bin_dir, proposal)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(fake_home),
        "CODEX_HOME": str(fake_home / ".codex"),
        "TMPDIR": str(temp_root),
    }


def _set_env(monkeypatch, values: dict[str, str]) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _assert_no_srt_settings_dirs(tmp_path: Path) -> None:
    assert not list((tmp_path / "tmp").glob("meta-harness-srt-*"))


def _add_candidate_manifest(
    git_project: Path, cand_id: str, *, target: str, quality_mean: float
) -> dict:
    parent_manifest = json.loads(
        (mh.candidates_dir(git_project, mh.DEFAULTS) / _PARENT_ID / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = {**parent_manifest, "cand_id": cand_id, "target": target}
    cand_dir = mh.candidates_dir(git_project, mh.DEFAULTS) / cand_id
    cand_dir.mkdir(parents=True)
    (cand_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "cand_id": cand_id,
        "quality_mean": quality_mean,
        "quality_var": 0.0,
        "quality_min": quality_mean,
        "cost_mean": 100.0,
        "runs": 1,
    }


def _write_holdout_run(git_project: Path) -> None:
    holdout_dir = mh.holdout_runs_dir(git_project, mh.DEFAULTS) / _HOLDOUT_RUN_ID
    holdout_dir.mkdir(parents=True)
    (holdout_dir / "metadata.json").write_text(
        json.dumps({"run_id": _HOLDOUT_RUN_ID, "holdout": True}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (holdout_dir / "result.json").write_text('{"verdict":"fail"}\n', encoding="utf-8")
    mh.append_ledger_event(
        git_project,
        mh.DEFAULTS,
        {
            "event": "run_completed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "run_id": _HOLDOUT_RUN_ID,
            "cand_id": _PARENT_ID,
            "target": "claude-harness",
            "holdout": True,
        },
    )


def _valid_proposal(*, content: str = "# Example\n\nImproved by proposer.\n") -> dict:
    return {
        "schema_version": "1.0",
        "hypothesis": "Tighten the example facet.",
        "theme": "tighten example facet",
        "changes": [
            {
                "path": "facets/example/SKILL.md",
                "new_content": content,
            }
        ],
        "based_on_runs": [_RUN_ID],
        "expected_effect": "The failing run should pass.",
        "risk_notes": "Low risk fixture.",
    }


def _valid_routing_proposal() -> dict:
    return {
        "schema_version": "1.0",
        "hypothesis": "Direct routing improves the debugger behavior scenario.",
        "theme": "route debugger directly",
        "config_patch": [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "agents.debugger.tool",
                "value": "claude-direct",
            }
        ],
        "based_on_runs": [_ROUTING_RUN_ID],
        "expected_effect": "The routing-sensitive train scenario should pass.",
        "risk_notes": "Deep debugging may lose Codex-specific behavior.",
    }


def _routing_model_proposal(model: str) -> dict:
    proposal = _valid_routing_proposal()
    proposal["config_patch"] = [
        {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": "antigravity.model",
            "value": model,
        }
    ]
    return proposal


def _prepare_stale_routing_config(git_project: Path, git_run, monkeypatch) -> str:
    routing_config = git_project / _ROUTING_CONFIG_RELATIVE
    routing_config.write_text(
        "agents:\n"
        "  debugger:\n"
        "    tool: codex\n"
        "antigravity:\n"
        "  model: source-model\n"
        "  model_allowlist:\n"
        "    - source-model\n",
        encoding="utf-8",
    )
    git_run("add", _ROUTING_CONFIG_RELATIVE.as_posix(), cwd=git_project)
    git_run("commit", "-m", "source routing allowlist", cwd=git_project)
    source_commit = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()
    current_routing_config = {
        "agents": {"debugger": {"tool": "codex"}},
        "antigravity": {
            "model": "current-model",
            "model_allowlist": ["current-model"],
        },
    }
    routing_config.write_text(
        "agents:\n"
        "  debugger:\n"
        "    tool: codex\n"
        "antigravity:\n"
        "  model: current-model\n"
        "  model_allowlist:\n"
        "    - current-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        propose_cli.mh,
        "_load_agent_routing_config",
        lambda _schema_dir: current_routing_config,
    )
    return source_commit


def test_propose_registers_candidate_from_stubbed_codex_backend(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_store(git_project, git_run)
    proposal = _valid_proposal()

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        "--json",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
        check=True,
    )

    payload = json.loads(result.stdout)
    cand_id = payload["cand_id"]
    cand_dir = mh.candidates_dir(git_project, mh.DEFAULTS) / cand_id
    manifest = json.loads((cand_dir / "manifest.json").read_text(encoding="utf-8"))
    overlay = cand_dir / "overlay" / "facets" / "example" / "SKILL.md"
    inherited = cand_dir / "overlay" / "facets" / "parent-only" / "SKILL.md"
    events = _events(git_project)
    registered = [event for event in events if event.get("cand_id") == cand_id]

    assert manifest["created_by"] == "proposer"
    assert manifest["parent_id"] == _PARENT_ID
    assert manifest["generation"] == 1
    assert overlay.read_text(encoding="utf-8") == "# Example\n\nImproved by proposer.\n"
    assert inherited.read_text(encoding="utf-8") == "# Parent only\n\nInherited content.\n"
    assert manifest["overlay_files"] == [
        "facets/example/SKILL.md",
        "facets/parent-only/SKILL.md",
    ]
    assert registered[-1]["created_by"] == "proposer"
    assert registered[-1]["proposal"]["based_on_runs"] == [_RUN_ID]
    assert registered[-1]["proposal"]["tokens_used"] == 10
    _assert_no_srt_settings_dirs(tmp_path)


def test_routing_config_baseline_bootstrap_provides_citable_frontier_run(
    git_project: Path, git_run
) -> None:
    _prepare_routing_store(git_project, git_run)

    snapshot = propose_cli._snapshot_propose_store(git_project, mh.DEFAULTS, "routing-config")

    assert propose_cli._citable_run_ids(snapshot, "routing-config") == (_ROUTING_RUN_ID,)
    assert snapshot.frontier_doc["frontier"] == [_ROUTING_PARENT_ID]
    assert (
        propose_cli._select_proposal_parent(
            git_project,
            mh.DEFAULTS,
            snapshot.frontier_doc,
            target="routing-config",
            focus_candidate=None,
        )
        == _ROUTING_PARENT_ID
    )


def test_propose_registers_allowed_routing_config_patch_from_stubbed_backend(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_routing_store(git_project, git_run)
    proposal = _valid_routing_proposal()

    result = run_meta(
        "propose",
        "--target",
        "routing-config",
        "--json",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
        check=True,
    )

    cand_id = json.loads(result.stdout)["cand_id"]
    cand_dir = mh.candidates_dir(git_project, mh.DEFAULTS) / cand_id
    manifest = json.loads((cand_dir / "manifest.json").read_text(encoding="utf-8"))
    config_patch = json.loads(
        (cand_dir / "overlay" / mh.CONFIG_PATCH_FILENAME).read_text(encoding="utf-8")
    )
    registered = [event for event in _events(git_project) if event.get("cand_id") == cand_id]

    assert manifest["created_by"] == "proposer"
    assert manifest["target"] == "routing-config"
    assert manifest["parent_id"] == _ROUTING_PARENT_ID
    assert manifest["overlay_files"] == []
    assert manifest["config_patch_hash"] == mh.compute_config_patch_hash(config_patch)
    assert config_patch == proposal["config_patch"]
    assert registered[-1]["created_by"] == "proposer"
    assert registered[-1]["proposal"]["based_on_runs"] == [_ROUTING_RUN_ID]
    _assert_no_srt_settings_dirs(tmp_path)


def test_propose_path_rejects_model_valid_only_in_current_checkout(
    git_project: Path, git_run, monkeypatch
) -> None:
    _prepare_routing_store(git_project, git_run)
    source_commit = _prepare_stale_routing_config(git_project, git_run, monkeypatch)
    proposal = _routing_model_proposal("current-model")

    assert (
        propose_cli.mh.validate_config_patch(
            proposal["config_patch"],
            mh.DEFAULTS,
            propose_cli._SCHEMA_DIR,
            target="routing-config",
            created_by="proposer",
        )
        == []
    )

    with pytest.raises(
        propose_cli.prop.ProposerError,
        match="antigravity model is not in model_allowlist: current-model",
    ):
        propose_cli._register_proposed_candidate(
            main_root=git_project,
            config=mh.DEFAULTS,
            target="routing-config",
            parent_id=None,
            source_commit=source_commit,
            proposal=proposal,
            included_run_ids=frozenset({_ROUTING_RUN_ID}),
            tokens_used=10,
        )


def test_source_model_passes_propose_path_then_current_checkout_gate_rejects(
    git_project: Path, git_run, monkeypatch
) -> None:
    _prepare_routing_store(git_project, git_run)
    source_commit = _prepare_stale_routing_config(git_project, git_run, monkeypatch)
    proposal = _routing_model_proposal("source-model")
    source_routing_config = propose_cli.prop._load_source_agent_routing_config(
        git_project, source_commit
    )

    assert (
        mh.validate_config_patch(
            proposal["config_patch"],
            mh.DEFAULTS,
            propose_cli._SCHEMA_DIR,
            target="routing-config",
            created_by="proposer",
            agent_routing_config=source_routing_config,
        )
        == []
    )

    with pytest.raises(
        propose_cli.prop.ProposerError,
        match="copied config patch validation failed: .*source-model",
    ):
        propose_cli._register_proposed_candidate(
            main_root=git_project,
            config=mh.DEFAULTS,
            target="routing-config",
            parent_id=None,
            source_commit=source_commit,
            proposal=proposal,
            included_run_ids=frozenset({_ROUTING_RUN_ID}),
            tokens_used=10,
        )


def test_unrelated_register_value_error_is_not_converted_to_proposal_rejection(
    git_project: Path, git_run, monkeypatch
) -> None:
    _prepare_routing_store(git_project, git_run)

    def fail_unrelated(*_args, **_kwargs):
        raise ValueError("unrelated register failure")

    monkeypatch.setattr(propose_cli.mh, "register_candidate", fail_unrelated)

    with pytest.raises(ValueError, match="unrelated register failure"):
        propose_cli._register_proposed_candidate(
            main_root=git_project,
            config=mh.DEFAULTS,
            target="routing-config",
            parent_id=None,
            source_commit=git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip(),
            proposal=_valid_routing_proposal(),
            included_run_ids=frozenset({_ROUTING_RUN_ID}),
            tokens_used=10,
        )


def test_loop_continues_when_current_checkout_rejects_source_valid_proposal(
    git_project: Path,
    git_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_routing_store(git_project, git_run)
    source_commit = _prepare_stale_routing_config(git_project, git_run, monkeypatch)
    current_routing_config = {
        "agents": {"debugger": {"tool": "codex"}},
        "antigravity": {
            "model": "current-model",
            "model_allowlist": ["current-model"],
        },
    }
    monkeypatch.setattr(
        loop_cli.propose_cli.mh,
        "_load_agent_routing_config",
        lambda _schema_dir: current_routing_config,
    )
    parent_manifest_path = (
        mh.candidates_dir(git_project, mh.DEFAULTS) / _ROUTING_PARENT_ID / "manifest.json"
    )
    parent_manifest = json.loads(parent_manifest_path.read_text(encoding="utf-8"))
    parent_manifest["source_commit"] = source_commit
    parent_manifest_path.write_text(json.dumps(parent_manifest), encoding="utf-8")
    _set_env(monkeypatch, _prepare_stubbed_codex(tmp_path, _routing_model_proposal("source-model")))

    config = copy.deepcopy(loop_cli.mh.DEFAULTS)
    config["proposer"]["divergence_rounds"] = 10
    loop_id = "loop-20260718-120000-source-current-drift"
    loop_cli.mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "loop_started",
            "ts": loop_cli.mh.now_iso(),
            "schema_version": "1.0",
            "loop_id": loop_id,
            "target": "routing-config",
            "budget_usd": None,
            "max_iterations": 3,
            "baseline_best_quality": 0.0,
        },
    )
    spec = loop_cli.LoopSpec(loop_id, "routing-config", None, 3, 0.0, 0)

    reason = loop_cli._drive_loop(git_project, config, git_project, spec)

    events = loop_cli.mh.read_ledger_events_strict(git_project, config)
    iterations = loop_cli._iteration_events(events, loop_id)
    rejected = [event for event in events if event.get("event") == "proposal_rejected"]
    assert reason == "max_iterations"
    assert rejected[-1]["iteration"] == 1
    assert iterations[1]["outcome"] == "proposal_rejected"
    assert [iterations[index]["outcome"] for index in (2, 3)] == [
        "cooldown_wait",
        "cooldown_wait",
    ]


def test_propose_rejects_routing_config_codex_model_patch(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_routing_store(git_project, git_run)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    proposal = _valid_routing_proposal()
    proposal["config_patch"] = [
        {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": "codex.model",
            "value": "gpt-5.6-sol",
        }
    ]

    result = run_meta(
        "propose",
        "--target",
        "routing-config",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    assert result.returncode == 2
    assert "created_by='proposer' is not allowed" in result.stderr
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before


def test_loop_routing_proposal_rejection_records_error_cooldown_event(
    git_project: Path, git_run, tmp_path: Path, monkeypatch
) -> None:
    _prepare_routing_store(git_project, git_run)
    proposal = _valid_routing_proposal()
    proposal["config_patch"] = [
        {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": "codex.model",
            "value": "gpt-5.6-sol",
        }
    ]
    _set_env(monkeypatch, _prepare_stubbed_codex(tmp_path, proposal))
    snapshot = propose_cli._snapshot_propose_store(git_project, mh.DEFAULTS, "routing-config")
    loop_id = "loop-20260717-120000-routing"

    with pytest.raises(propose_cli.prop.ProposerError, match="created_by='proposer'"):
        propose_cli._run_propose_pipeline(
            main_root=git_project,
            config=mh.DEFAULTS,
            project_dir=git_project,
            target="routing-config",
            focus_run=None,
            focus_candidate=None,
            snapshot=snapshot,
            loop_id=loop_id,
            iteration=1,
        )

    rejected = [event for event in _events(git_project) if event["event"] == "proposal_rejected"]
    assert rejected[-1] == {
        "event": "proposal_rejected",
        "ts": rejected[-1]["ts"],
        "schema_version": "1.0",
        "target": "routing-config",
        "loop_id": loop_id,
        "iteration": 1,
        "verdict": "error",
    }


def test_propose_rejects_routing_config_mixed_key_kinds(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_routing_store(git_project, git_run)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    proposal = _valid_routing_proposal()
    proposal["config_patch"].append(
        {
            "file": "agent-routing/cli-tools.yaml",
            "key_path": "antigravity.model",
            "value": "gemini-3.1-pro-high",
        }
    )

    result = run_meta(
        "propose",
        "--target",
        "routing-config",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    assert result.returncode == 2
    assert "must use exactly one key kind" in result.stderr
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before


def test_propose_rejects_combined_config_patch_and_overlay_payload(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_routing_store(git_project, git_run)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    proposal = _valid_routing_proposal()
    proposal["changes"] = [{"path": "facets/example/SKILL.md", "new_content": "# mixed payload\n"}]

    result = run_meta(
        "propose",
        "--target",
        "routing-config",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    assert result.returncode == 2
    assert "exactly one of changes or config_patch" in result.stderr
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before


def test_propose_rejects_invalid_proposal_and_saves_rejected_file(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_store(git_project, git_run)
    proposal = {
        "schema_version": "1.0",
        "hypothesis": "Unsafe change.",
        "theme": "unsafe",
        "changes": [{"path": "docs/evaluation/meta-harness.md", "new_content": "bad"}],
        "based_on_runs": [_RUN_ID],
        "expected_effect": "none",
        "risk_notes": "unsafe",
    }

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    rejected_files = sorted(mh.rejected_dir(git_project, mh.DEFAULTS).glob("*-proposal.json"))
    assert result.returncode == 2
    assert rejected_files
    rejected = json.loads(rejected_files[-1].read_text(encoding="utf-8"))
    assert "proposal schema mismatch" in rejected["reason"]
    _assert_no_srt_settings_dirs(tmp_path)


def test_propose_rejects_overlay_size_excess_and_saves_rejected_file(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_store(git_project, git_run)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    proposal = _valid_proposal(content="x" * 200001)

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    rejected_files = sorted(mh.rejected_dir(git_project, mh.DEFAULTS).glob("*-proposal.json"))
    assert result.returncode == 2
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before
    assert rejected_files
    rejected = json.loads(rejected_files[-1].read_text(encoding="utf-8"))
    assert "max_overlay_bytes" in rejected["reason"]


def test_propose_rejects_holdout_based_on_run_and_saves_rejected_file(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    _prepare_store(git_project, git_run)
    _write_holdout_run(git_project)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    proposal = _valid_proposal()
    proposal["based_on_runs"] = [_HOLDOUT_RUN_ID]

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    rejected_files = sorted(mh.rejected_dir(git_project, mh.DEFAULTS).glob("*-proposal.json"))
    assert result.returncode == 2
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before
    assert rejected_files
    rejected = json.loads(rejected_files[-1].read_text(encoding="utf-8"))
    assert "based_on_runs references holdout run_id" in rejected["reason"]


def test_parent_selection_filters_mixed_target_frontier(git_project: Path, git_run) -> None:
    _prepare_store(git_project, git_run)
    other_id = "cand-20260710-010000-other-target-abcd"
    other_point = _add_candidate_manifest(
        git_project,
        other_id,
        target="skill:other",
        quality_mean=99.0,
    )
    frontier_doc = {
        "frontier": [other_id, _PARENT_ID],
        "points": [other_point, {"cand_id": _PARENT_ID, "quality_mean": 80.0}],
    }

    selected = propose_cli._select_proposal_parent(
        git_project,
        mh.DEFAULTS,
        frontier_doc,
        target="claude-harness",
        focus_candidate=None,
    )

    assert selected == _PARENT_ID


def test_parent_selection_returns_none_without_same_target(git_project: Path, git_run) -> None:
    _prepare_store(git_project, git_run)

    selected = propose_cli._select_proposal_parent(
        git_project,
        mh.DEFAULTS,
        {"frontier": [_PARENT_ID], "points": []},
        target="skill:missing",
        focus_candidate=None,
    )

    assert selected is None


def test_focus_candidate_target_mismatch_exits_2_before_backend(
    git_project: Path, git_run, monkeypatch, capsys
) -> None:
    _prepare_store(git_project, git_run)
    before = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))

    def fail_if_launched(**_kwargs):
        raise AssertionError("backend must not launch")

    monkeypatch.setattr(propose_cli.pb, "launch_proposer_backend", fail_if_launched)

    exit_code = propose_cli.cmd_propose(
        str(git_project),
        "skill:other",
        None,
        _PARENT_ID,
        False,
    )

    assert exit_code == 2
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before
    assert "expected skill:other, got claude-harness" in capsys.readouterr().err


def test_propose_rolls_back_candidate_when_ledger_append_fails(
    git_project: Path, git_run, tmp_path: Path, monkeypatch, capsys
) -> None:
    _prepare_store(git_project, git_run)
    before_candidates = set(mh.list_candidate_ids(git_project, mh.DEFAULTS))
    before_events = _events(git_project)
    _set_env(monkeypatch, _prepare_stubbed_codex(tmp_path, _valid_proposal()))

    def fail_append(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(propose_cli.mh, "append_ledger_event", fail_append)

    exit_code = propose_cli.cmd_propose(
        str(git_project),
        "claude-harness",
        None,
        None,
        False,
    )

    assert exit_code == 2
    assert set(mh.list_candidate_ids(git_project, mh.DEFAULTS)) == before_candidates
    assert _events(git_project) == before_events
    assert "rolled back candidate after ledger append failure" in capsys.readouterr().err
    _assert_no_srt_settings_dirs(tmp_path)


def test_propose_rejects_empty_citable_run_set_before_backend(
    git_project: Path, git_run, monkeypatch, capsys
) -> None:
    _commit_facets(git_project, git_run)
    mh.init_store(git_project, mh.DEFAULTS)

    def fail_if_launched(**_kwargs):
        raise AssertionError("backend must not launch")

    monkeypatch.setattr(propose_cli.pb, "launch_proposer_backend", fail_if_launched)

    exit_code = propose_cli.cmd_propose(
        str(git_project),
        "claude-harness",
        None,
        None,
        False,
    )

    assert exit_code == 2
    assert mh.list_candidate_ids(git_project, mh.DEFAULTS) == []
    assert "no citable non-holdout runs for target: claude-harness" in capsys.readouterr().err


def test_register_proposed_candidate_converts_parent_overlay_stage_error(
    tmp_path: Path, monkeypatch
) -> None:
    @contextmanager
    def fail_baseline(*_args, **_kwargs):
        raise propose_cli.ev.EvaluatorStageError(
            "overlay_apply", "overlay_error", "forced parent overlay failure"
        )
        yield

    monkeypatch.setattr(propose_cli, "_inherit_parent_overlay", lambda *_args: None)
    monkeypatch.setattr(
        propose_cli.ev,
        "materialized_candidate_baseline",
        fail_baseline,
    )

    with pytest.raises(
        propose_cli.prop.ProposerError,
        match="parent overlay is invalid: forced parent overlay failure",
    ):
        propose_cli._register_proposed_candidate(
            main_root=tmp_path,
            config={},
            target="skill:issue-create",
            parent_id=_PARENT_ID,
            source_commit="a" * 40,
            proposal=_valid_proposal(),
            included_run_ids=frozenset({_RUN_ID}),
            tokens_used=10,
        )


def test_run_propose_pipeline_converts_parent_overlay_stage_error(
    tmp_path: Path, monkeypatch
) -> None:
    target = "skill:issue-create"
    parent_manifest = {
        "cand_id": _PARENT_ID,
        "target": target,
        "source_commit": "a" * 40,
    }
    run_event = {
        "event": "run_completed",
        "run_id": _RUN_ID,
        "target": target,
        "holdout": False,
    }
    snapshot = propose_cli.prop.FilteredStoreSnapshot(
        frontier_doc={"frontier": [], "points": []},
        ledger_events=(run_event,),
        candidate_ids=(_PARENT_ID,),
        non_holdout_run_ids=(_RUN_ID,),
        holdout_run_ids=frozenset(),
    )
    view_path = tmp_path / "filtered-view"
    (view_path / "baseline").mkdir(parents=True)
    view = propose_cli.prop.FilteredView(
        path=view_path,
        included_run_ids=frozenset({_RUN_ID}),
        holdout_run_ids=frozenset(),
    )

    def fail_overlay(*_args, **_kwargs):
        raise propose_cli.ev.EvaluatorStageError(
            "overlay_apply", "overlay_error", "forced parent overlay failure"
        )

    monkeypatch.setattr(
        propose_cli.mh,
        "read_candidate_manifest",
        lambda *_args: parent_manifest,
    )
    monkeypatch.setattr(
        propose_cli.prop,
        "build_filtered_view",
        lambda **_kwargs: view,
    )
    monkeypatch.setattr(propose_cli.ev, "apply_parent_lineage_to_baseline", fail_overlay)

    with pytest.raises(
        propose_cli.prop.ProposerError,
        match="parent overlay is invalid: forced parent overlay failure",
    ):
        propose_cli._run_propose_pipeline(
            main_root=tmp_path,
            config={"proposer": {"tool": "claude"}},
            project_dir=tmp_path,
            target=target,
            focus_run=None,
            focus_candidate=_PARENT_ID,
            snapshot=snapshot,
        )

    assert not view_path.exists()


def test_propose_rejects_secret_in_proposal_and_records_violation(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    """L3: proposal 本文に sk- 系 API key が混入したら登録拒否 + violation 記録。"""
    _prepare_store(git_project, git_run)
    proposal = _valid_proposal(content=f"# Example\n\nleaked {_sample_sk_key()}\n")

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, proposal),
    )

    events = _events(git_project)
    violations = [e for e in events if e.get("event") == "proposer_security_violation"]
    assert result.returncode == 2
    assert violations and violations[-1]["detector"] == "L3_secret_scan"
    assert not any(e.get("event") == "candidate_registered" for e in events)
    assert sorted(mh.rejected_dir(git_project, mh.DEFAULTS).glob("*-proposal.json"))
    _assert_no_srt_settings_dirs(tmp_path)


def test_propose_scans_non_utf8_inherited_overlay(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    """非 UTF-8 の親overlayでもASCII部分のsecretを読み飛ばさない。"""
    _prepare_store(
        git_project,
        git_run,
        inherited_content=b"\xffleaked " + _sample_sk_key("ant").encode() + b"\n",
    )

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, _valid_proposal()),
    )

    violations = [
        e for e in _events(git_project) if e.get("event") == "proposer_security_violation"
    ]
    assert result.returncode == 2
    assert violations and violations[-1]["detector"] == "L3_secret_scan"


def test_propose_scans_inherited_overlay_path(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    """親から継承したoverlay path自体にsecretが含まれる場合も拒否する。"""
    _prepare_store(
        git_project,
        git_run,
        inherited_rel=f"facets/{_sample_sk_key('ant')}/SKILL.md",
    )

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_stubbed_codex(tmp_path, _valid_proposal()),
    )

    violations = [
        e for e in _events(git_project) if e.get("event") == "proposer_security_violation"
    ]
    assert result.returncode == 2
    assert violations and violations[-1]["detector"] == "L3_secret_scan"


def _fake_jwt(exp_epoch: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp_epoch}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature"


def _install_exfil_codex(bin_dir: Path) -> None:
    """staged auth.json の canary を読んで proposal に埋め込む敵対 stub を用意する。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(
        bin_dir / "srt",
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("@anthropic-ai/sandbox-runtime 0.0.64")
    raise SystemExit(0)
if args[:1] == ["--settings"]:
    args = args[2:]
base = os.path.basename(args[0]) if args else ""
if base == "codex":
    os.execvp(args[0], args)
if base == "cat":
    print("cat: Operation not permitted", file=sys.stderr)
    raise SystemExit(1)
if base == "curl":
    print("curl: (56) connection reset by proxy", file=sys.stderr)
    raise SystemExit(56)
raise SystemExit(1)
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env python3
print("canary reachable")
raise SystemExit(0)
""",
    )
    _write_executable(
        bin_dir / "codex",
        f"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
out = args[args.index("-o") + 1]
auth = json.load(open(os.path.join(os.environ["CODEX_HOME"], "auth.json"), encoding="utf-8"))
canary = auth["tokens"]["refresh_token"]
proposal = {{
    "schema_version": "1.0",
    "hypothesis": "Tighten the example facet.",
    "theme": "tighten example facet",
    "changes": [
        {{"path": "facets/example/SKILL.md", "new_content": "# Example\\n\\n" + canary + "\\n"}}
    ],
    "based_on_runs": [{_RUN_ID!r}],
    "expected_effect": "The failing run should pass.",
    "risk_notes": "Low risk fixture.",
}}
with open(out, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(proposal, ensure_ascii=False))
    handle.write("\\n")
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 7, "output_tokens": 3}}}}))
raise SystemExit(0)
""",
    )


def _prepare_exfil_codex(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    fake_home = tmp_path / "home"
    codex_home = fake_home / ".codex"
    codex_home.mkdir(parents=True)
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": _fake_jwt(int(time.time()) + 10 * 86400),
            "refresh_token": "real-refresh-token-value",
            "account_id": "account-1234",
        },
    }
    (codex_home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    _install_exfil_codex(bin_dir)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(fake_home),
        "CODEX_HOME": str(codex_home),
        "TMPDIR": str(temp_root),
    }


def test_propose_rejects_auth_canary_exfil_and_records_violation(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    """L2 / 到達不能テスト 11: staged auth.json の canary を proposal に混入させると拒否。"""
    _prepare_store(git_project, git_run)

    result = run_meta(
        "propose",
        "--target",
        "claude-harness",
        project=git_project,
        env_extra=_prepare_exfil_codex(tmp_path),
    )

    events = _events(git_project)
    violations = [e for e in events if e.get("event") == "proposer_security_violation"]
    assert result.returncode == 2, result.stderr
    assert violations and violations[-1]["detector"] == "L2_canary", result.stderr
    assert not any(e.get("event") == "candidate_registered" for e in events)
    rejected_files = sorted(mh.rejected_dir(git_project, mh.DEFAULTS).glob("*-proposal.json"))
    assert rejected_files
    # 検知した canary が quarantine ファイルへ平文で残らないこと（二次漏洩防止）。
    rejected_text = rejected_files[-1].read_text(encoding="utf-8")
    assert propose_cli.pb.CODEX_AUTH_CANARY_PREFIX not in rejected_text
    assert "[REDACTED:auth canary" in rejected_text
    _assert_no_srt_settings_dirs(tmp_path)
