"""Subprocess E2E for loop CLI wiring through proposer, evaluator, ledger, and report."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from tests.module_loader import load_module

mh = load_module("meta_harness_common_loop_e2e", "packages/meta-harness/lib/meta_harness_common.py")

_PARENT_ID = "cand-20260711-100000-loop-parent-abcd"
_SEED_RUN_ID = "run-20260711-100000-loop-parent-seed-abcd"
_HASH = "a" * 64


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _install_stub_tools(bin_dir: Path, proposal: dict) -> None:
    bin_dir.mkdir(parents=True)
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
if base in ("bash", "codex", "claude"):
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
payload = {{"ok": True}} if '"ok"' in args[-1] else {proposal_json!r}
if isinstance(payload, str):
    payload = json.loads(payload)
with open(out, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
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
    _write_executable(
        bin_dir / "claude",
        """#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.202")
    raise SystemExit(0)
output_format = args[args.index("--output-format") + 1]
if output_format == "json":
    print(json.dumps({"type": "result"}))
    raise SystemExit(0)
prompt = args[args.index("-p") + 1]
cwd = pathlib.Path.cwd()
if "VERSION" in prompt:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    (cwd / "VERSION").write_text(sha + "\\n", encoding="utf-8")
if "summary.md" in prompt:
    (cwd / "summary.md").write_text("A deterministic summary of the test project README.\\n", encoding="utf-8")
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "duration_ms": 10,
    "total_cost_usd": 0.01,
    "num_turns": 1,
}))
raise SystemExit(0)
""",
    )


def _prepare_project(git_project: Path, git_run) -> str:
    manager = git_project / "scripts" / "orchestra-manager.py"
    manager.parent.mkdir(parents=True)
    manager.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
    facet = git_project / "facets" / "example" / "SKILL.md"
    facet.parent.mkdir(parents=True)
    facet.write_text("# Example\n\nBaseline.\n", encoding="utf-8")
    git_run("add", "scripts/orchestra-manager.py", "facets/example/SKILL.md", cwd=git_project)
    git_run("commit", "-m", "add loop fixtures", cwd=git_project)
    return git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()


def _prepare_seed_store(git_project: Path, source_commit: str) -> None:
    config = mh.DEFAULTS
    mh.init_store(git_project, config)
    cand_dir = mh.candidates_dir(git_project, config) / _PARENT_ID
    overlay = cand_dir / "overlay" / "facets" / "example" / "SKILL.md"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("# Example\n\nParent.\n", encoding="utf-8")
    overlay_dir = cand_dir / "overlay"
    manifest = mh.build_candidate_manifest(
        cand_id=_PARENT_ID,
        parent_id=None,
        generation=0,
        target="claude-harness",
        source_commit=source_commit,
        config_hash=mh.compute_config_hash(overlay_dir, config),
        overlay_files=mh.list_overlay_files(overlay_dir),
        description="loop parent",
    )
    (cand_dir / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    run_dir = mh.runs_dir(git_project, config) / _SEED_RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({"run_id": _SEED_RUN_ID, "holdout": False}) + "\n", encoding="utf-8"
    )
    (run_dir / "result.json").write_text('{"verdict":"fail"}\n', encoding="utf-8")
    with gzip.open(run_dir / "events.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write('{"type":"result"}\n')
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "candidate_registered",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "cand_id": _PARENT_ID,
            "parent_id": None,
            "generation": 0,
            "target": "claude-harness",
            "created_by": "human",
        },
    )
    mh.append_ledger_event(
        git_project,
        config,
        {
            "event": "run_completed",
            "ts": mh.now_iso(),
            "schema_version": "1.0",
            "run_id": _SEED_RUN_ID,
            "cand_id": _PARENT_ID,
            "scenario_id": "seed",
            "target": "claude-harness",
            "suite_id": "claude-harness",
            "suite_hash": _HASH,
            "scenario_hash": _HASH,
            "evaluator_hash": _HASH,
            "verdict": "fail",
            "quality_score": 10.0,
            "critical_pass_rate": 0.0,
            "cost": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "tool_uses": 0,
                "duration_ms": 1,
                "total_cost_usd": 0.0,
                "num_turns": 1,
            },
            "attempt": 1,
            "attempts_total": 1,
            "holdout": False,
        },
    )
    mh.write_frontier_cache(
        git_project,
        config,
        {
            "schema_version": "1.0",
            "generated_at": mh.now_iso(),
            "ledger_line_count": 2,
            "suite_hash": _HASH,
            "evaluator_hash": _HASH,
            "cost_axis": "total_tokens",
            "points": [
                {
                    "cand_id": _PARENT_ID,
                    "quality_mean": 10.0,
                    "quality_var": 0.0,
                    "quality_min": 10.0,
                    "cost_mean": 2.0,
                    "runs": 1,
                }
            ],
            "frontier": [_PARENT_ID],
            "dominated": [],
        },
    )


def test_loop_subprocess_fails_closed_before_candidate_worktree_when_docker_capability_missing(
    git_project: Path, git_run, tmp_path: Path, run_meta
) -> None:
    source_commit = _prepare_project(git_project, git_run)
    _prepare_seed_store(git_project, source_commit)
    local_config = git_project / ".claude" / "config" / "meta-harness"
    local_config.mkdir(parents=True)
    (local_config / "meta-harness.local.yaml").write_text(
        "proposer:\n  max_iterations: 1\nloop:\n  convergence:\n    enabled: false\n",
        encoding="utf-8",
    )
    proposal = {
        "schema_version": "1.0",
        "hypothesis": "Improve the example facet.",
        "theme": "loop e2e",
        "changes": [{"path": "facets/example/SKILL.md", "new_content": "# Example\n\nImproved.\n"}],
        "based_on_runs": [_SEED_RUN_ID],
        "expected_effect": "Pass both scenarios.",
        "risk_notes": "Fixture only.",
    }
    bin_dir = tmp_path / "bin"
    _install_stub_tools(bin_dir, proposal)
    fake_home = tmp_path / "home"
    codex_home = fake_home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"token":"test"}\n', encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(fake_home),
        "CODEX_HOME": str(codex_home),
    }

    result = run_meta(
        "loop",
        "--target",
        "claude-harness",
        "--json",
        project=git_project,
        env_extra=env,
    )

    assert result.returncode == 2
    assert "CLI capability gate failed" in result.stderr
    events = mh.read_ledger_events(git_project, mh.DEFAULTS)
    assert events[-1]["event"] == "loop_stopped"
    assert events[-1]["reason"] == "error"
    worktree_root = git_project / ".worktrees" / "meta"
    assert not worktree_root.exists() or not any(worktree_root.iterdir())
