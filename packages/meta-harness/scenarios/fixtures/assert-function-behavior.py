#!/usr/bin/env python3
"""Assert a candidate function's behavior against known-good input/output cases.

Design intent (PR #381 review, P1 -- structural fix, not a patch): earlier claude-harness
behavior oracles ran the candidate's function calls and their assertions *in the same Python
process* as the oracle script itself, e.g. ``python3 -c "from slugify import slugify;
assert slugify(...) == ...; assert slugify(...) == ...; ..."``. That trusted the *oracle
process's own exit code* to mean "every assertion held" -- but a candidate function that calls
``raise SystemExit(0)`` or ``os._exit(0)`` on its first invocation terminates that whole process
with exit 0 immediately, before any assertion after the first call ever runs. A fully broken
implementation could satisfy every graded item (behavior, type hints, conventions) just by
self-destructing early, since ``command_exit`` only ever sees the process-level exit code.

Rather than trying to enumerate and block every exit primitive (``sys.exit``, ``os._exit``,
``os.abort``, signal handlers, ...) -- another game that never structurally ends -- this fixture
runs each behavior case in its own **subprocess**, one call per subprocess, and never trusts that
subprocess's exit code alone as a signal of success:

- The parent process (this script) launches one fresh ``python3 -c ...`` subprocess per case.
  That subprocess imports the candidate module, calls the required function with that case's
  args, and prints the result as a JSON payload on its *last* stdout line only after the call
  returns normally.
- The parent asserts, per case: the subprocess exited 0 **and** its stdout's last line parses as
  the expected JSON result payload **and** the payload's ``result`` matches the case's expected
  value.
- ``SystemExit(0)``/``os._exit(0)`` called from inside the candidate function terminate that
  case's subprocess *before* the trailing ``print()`` ever executes -- so even though the
  subprocess exits 0, there is no result payload on stdout, and the parent marks that case
  failed regardless of the exit code. Because each case runs in an isolated subprocess, an early
  exit inside one case's call cannot swallow any other case's evaluation -- unlike a single
  shared process where the first ``SystemExit(0)`` also killed the assertions after it.

Result comparison is done on the *canonical JSON serialization* of the result and the expected
value, not Python ``!=``: Python treats ``1 == True`` and ``0 == False``, so a candidate returning
``1``/``0`` instead of the required ``True``/``False`` would otherwise silently pass a case whose
``expected`` is a boolean. ``json.dumps`` distinguishes them (``"true"`` vs ``"1"``), matching the
strict ``is True``/``is False`` identity the earlier single-process oracle used to enforce.

Case format: a JSON list of objects, each ``{"args": [...], "expected": <value>}`` (an optional
``"id"`` names the case in failure output). ``--cases`` accepts either a path (resolved against
``AI_ORCHESTRA_DIR``, mirroring every other fixture in this suite) to a JSON file, or an inline
JSON list on the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_CASE_TIMEOUT_SECONDS = 10

_CASE_RUNNER_TEMPLATE = """\
import importlib.util, json

spec = importlib.util.spec_from_file_location("_candidate_module", {module_path!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
fn = getattr(module, {function!r})
args = json.loads({args_json!r})
result = fn(*args)
print(json.dumps({{"result": result}}))
"""


def _load_cases(project_root: Path, cases_arg: str) -> list[dict[str, Any]]:
    """Load the case list from a project-root-relative JSON file, or parse ``cases_arg`` inline."""
    candidate_path = project_root / cases_arg
    payload = candidate_path.read_text(encoding="utf-8") if candidate_path.is_file() else cases_arg
    cases = json.loads(payload)
    assert isinstance(cases, list) and cases, (
        f"--cases must be a non-empty JSON list, got {cases!r}"
    )
    for case in cases:
        assert isinstance(case, dict) and "args" in case and "expected" in case, (
            f"each case must be an object with 'args' and 'expected' keys, got {case!r}"
        )
    return cases


def _run_case(
    python_executable: str, module_path: Path, function: str, args: list[Any]
) -> dict[str, Any]:
    """Run exactly one case in an isolated subprocess; return ``{"result": ...}`` or ``{"error": ...}``.

    Never trusts the subprocess's exit code alone: a well-formed result payload must also be the
    last line printed on stdout (see the module docstring for why this defeats ``SystemExit(0)``/
    ``os._exit(0)`` inside the candidate function).
    """
    harness_code = _CASE_RUNNER_TEMPLATE.format(
        module_path=str(module_path), function=function, args_json=json.dumps(args)
    )
    try:
        proc = subprocess.run(
            [python_executable, "-B", "-c", harness_code],
            capture_output=True,
            text=True,
            timeout=_CASE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {_CASE_TIMEOUT_SECONDS}s"}

    if proc.returncode != 0:
        stderr_lines = proc.stderr.strip().splitlines()
        stderr_tail = stderr_lines[-1] if stderr_lines else "(no stderr)"
        return {
            "error": f"candidate process exited {proc.returncode} instead of returning a result "
            f"(SystemExit/os._exit/an uncaught exception all land here): {stderr_tail}"
        }

    stdout_lines = proc.stdout.strip().splitlines()
    if not stdout_lines:
        return {
            "error": "candidate process exited 0 but printed nothing -- SystemExit(0)/os._exit(0) "
            "called from inside the function terminate the process before the result is printed, "
            "so an early exit still fails this case even though the exit code was 0"
        }
    try:
        payload = json.loads(stdout_lines[-1])
    except ValueError:
        return {
            "error": f"candidate process's last stdout line was not valid JSON: {stdout_lines[-1]!r}"
        }
    if "result" not in payload:
        return {"error": f"result payload missing 'result' key: {payload!r}"}
    return {"result": payload["result"]}


def check_behavior(
    python_executable: str,
    project_root: Path,
    module_rel: Path,
    function: str,
    cases: list[dict[str, Any]],
) -> list[str]:
    module_path = project_root / module_rel
    assert module_path.is_file() and not module_path.is_symlink(), (
        f"missing regular file: {module_rel}"
    )

    problems: list[str] = []
    for index, case in enumerate(cases):
        case_id = case.get("id", f"case[{index}]")
        args = case["args"]
        expected = case["expected"]
        outcome = _run_case(python_executable, module_path, function, args)
        if "error" in outcome:
            problems.append(f"{case_id}: {outcome['error']}")
            continue
        # Compare canonical JSON, not Python `!=` (PR #381 review, round 4 follow-up): Python
        # treats `1 == True` and `0 == False`, so a candidate returning `1`/`0` instead of the
        # required `True`/`False` would otherwise silently pass. `json.dumps` distinguishes them
        # (`"true"` vs `"1"`), matching the strict `is True`/`is False` identity the earlier
        # single-process oracle used to enforce.
        if json.dumps(outcome["result"], sort_keys=True) != json.dumps(expected, sort_keys=True):
            problems.append(
                f"{case_id}: {function}(*{args!r}) == {outcome['result']!r}, expected {expected!r}"
            )
    return problems


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument(
        "--cases",
        required=True,
        help="path (relative to AI_ORCHESTRA_DIR) to a JSON case file, or an inline JSON list",
    )
    args = parser.parse_args(argv)

    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    cases = _load_cases(project_root, args.cases)
    problems = check_behavior(sys.executable, project_root, args.module, args.function, cases)
    assert not problems, "behavior mismatches:\n" + "\n".join(problems)


if __name__ == "__main__":
    main()
