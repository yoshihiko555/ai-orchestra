#!/usr/bin/env python3
"""meta-harness proposer backend launch helpers（Phase 2 M4）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import isolation as iso  # noqa: E402
import meta_harness_common as mh  # noqa: E402

PROPOSAL_SCHEMA_NAME = "proposal.schema.json"
DEFAULT_PROPOSER_TIMEOUT_SECONDS = 600
_CODEX_HOME_PREFIX = "meta-harness-codex-home-"
_CODEX_ORPHAN_MAX_AGE = timedelta(hours=24)

SubprocessRunner = Callable[..., subprocess.CompletedProcess]


class ProposerError(RuntimeError):
    """proposer launch / proposal handling を fail-closed すべき場合に送出する。"""


class ProposerRuntimeError(ProposerError):
    """backend 起動・timeout・subprocess 異常など、CLI exit 1 に分類するエラー。"""


class ProposalValidationError(ProposerError):
    """proposal 出力・設定値の検証失敗など、CLI exit 2 に分類するエラー。"""


@dataclass(frozen=True)
class ProposerBackendResult:
    """proposer backend が返した proposal と実行メタデータ。"""

    proposal: dict[str, Any]
    backend: str
    output_path: Path
    stdout: str
    stderr: str
    tokens_used: int | None


def launch_proposer_backend(
    *,
    view_dir: Path,
    prompt: str,
    schema_dir: Path,
    config: dict,
    isolation_launch: iso.IsolationLaunch,
    runner: SubprocessRunner | None = None,
) -> ProposerBackendResult:
    """設定された proposer backend を srt 経由で 1 回だけ起動する。"""
    proposer_cfg = config.get("proposer") or {}
    tool = proposer_cfg.get("tool", "codex")
    output_path = view_dir / "proposal-output.json"
    if output_path.exists():
        output_path.unlink()
    if tool == "codex":
        completed = _launch_codex_backend(
            view_dir=view_dir,
            prompt=prompt,
            schema_dir=schema_dir,
            output_path=output_path,
            proposer_cfg=proposer_cfg,
            isolation_launch=isolation_launch,
            runner=runner,
        )
    elif tool == "claude-bare":
        completed = _launch_claude_bare_backend(
            view_dir=view_dir,
            prompt=prompt,
            schema_dir=schema_dir,
            output_path=output_path,
            proposer_cfg=proposer_cfg,
            isolation_launch=isolation_launch,
            runner=runner,
        )
    else:
        raise ProposalValidationError(f"unsupported proposer.tool: {tool!r}")
    proposal = load_and_validate_proposal_output(output_path, schema_dir)
    stdout = completed.stdout or ""
    return ProposerBackendResult(
        proposal=proposal,
        backend=tool,
        output_path=output_path,
        stdout=stdout,
        stderr=completed.stderr or "",
        tokens_used=parse_tokens_used(stdout),
    )


def load_and_validate_proposal_output(output_path: Path, schema_dir: Path) -> dict[str, Any]:
    """`-o` output file だけを正として proposal JSON を読み込む。"""
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise ProposalValidationError("proposal output file is missing or empty")
    try:
        proposal = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProposalValidationError(f"proposal output is not valid JSON: {exc}") from exc
    if not isinstance(proposal, dict):
        raise ProposalValidationError("proposal output must be a JSON object")
    schema = mh.load_schema(schema_dir, PROPOSAL_SCHEMA_NAME)
    errors = mh.validate_against_schema(proposal, schema, schema_dir)
    if errors:
        raise ProposalValidationError(f"proposal schema mismatch: {'; '.join(errors[:5])}")
    return proposal


@contextmanager
def temporary_codex_home(source_home: Path | None = None):
    """実 `auth.json` だけをコピーした ephemeral CODEX_HOME を用意する。"""
    _sweep_orphan_codex_homes()
    path = Path(tempfile.mkdtemp(prefix=_CODEX_HOME_PREFIX, dir=tempfile.gettempdir()))
    path.chmod(0o700)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle_sigterm(signum: int, _frame: object) -> None:
        shutil.rmtree(path, ignore_errors=True)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        _populate_codex_home(path, source_home=source_home)
        yield path
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        shutil.rmtree(path, ignore_errors=True)


def _launch_codex_backend(
    *,
    view_dir: Path,
    prompt: str,
    schema_dir: Path,
    output_path: Path,
    proposer_cfg: dict,
    isolation_launch: iso.IsolationLaunch,
    runner: SubprocessRunner | None,
) -> subprocess.CompletedProcess:
    srt_path = _require_tool("srt")
    _require_tool("codex")
    command = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
        "--output-schema",
        str(schema_dir / PROPOSAL_SCHEMA_NAME),
        "-o",
        str(output_path),
        "--json",
    ]
    model = proposer_cfg.get("model")
    if model:
        command += ["--model", str(model)]
    command.append(prompt)
    return _run_isolated_backend(
        srt_path=srt_path,
        settings_path=isolation_launch.settings_path,
        command=command,
        view_dir=view_dir,
        env=isolation_launch.env,
        timeout_seconds=_proposer_timeout_seconds(proposer_cfg),
        label="codex proposer",
        runner=runner,
    )


def _launch_claude_bare_backend(
    *,
    view_dir: Path,
    prompt: str,
    schema_dir: Path,
    output_path: Path,
    proposer_cfg: dict,
    isolation_launch: iso.IsolationLaunch,
    runner: SubprocessRunner | None,
) -> subprocess.CompletedProcess:
    srt_path = _require_tool("srt")
    _require_tool("claude")
    schema_text = (schema_dir / PROPOSAL_SCHEMA_NAME).read_text(encoding="utf-8")
    command = [
        "claude",
        "-p",
        prompt,
        "--bare",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        schema_text,
        "--max-turns",
        str(proposer_cfg.get("max_turns", 40)),
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        f"Read({view_dir.resolve()}/**)",
    ]
    model = proposer_cfg.get("model")
    if model:
        command += ["--model", str(model)]
    effort = proposer_cfg.get("effort")
    if effort:
        command += ["--effort", str(effort)]
    completed = _run_isolated_backend(
        srt_path=srt_path,
        settings_path=isolation_launch.settings_path,
        command=command,
        view_dir=view_dir,
        env=isolation_launch.env,
        timeout_seconds=_proposer_timeout_seconds(proposer_cfg),
        label="claude-bare proposer",
        runner=runner,
    )
    proposal = _extract_claude_bare_json(completed.stdout or "")
    output_path.write_text(json.dumps(proposal, ensure_ascii=False) + "\n", encoding="utf-8")
    return completed


def _run_isolated_backend(
    *,
    srt_path: str,
    settings_path: Path,
    command: list[str],
    view_dir: Path,
    env: dict[str, str],
    timeout_seconds: int,
    label: str,
    runner: SubprocessRunner | None,
) -> subprocess.CompletedProcess:
    command_runner = runner or _run_process_tree
    try:
        completed = command_runner(
            iso.wrap_srt_command(srt_path, settings_path, command),
            cwd=view_dir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProposerRuntimeError(f"{label} timed out after {timeout_seconds}s") from exc
    except OSError as exc:
        raise ProposerRuntimeError(f"{label} failed to run: {exc}") from exc
    if completed.returncode != 0:
        raise ProposerRuntimeError(
            f"{label} exited {completed.returncode}: {(completed.stderr or '').strip()[:500]}"
        )
    return completed


def _run_process_tree(
    args: list[str],
    *,
    cwd: Path,
    capture_output: bool,
    text: bool,
    timeout: int | float,
    stdin: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        text=text,
        stdin=stdin,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process.pid)
        out, err = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err) from exc
    return subprocess.CompletedProcess(args, process.returncode, out, err)


def _kill_process_group(pid: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return


_TOKENS_USED_RE = re.compile(r"\btokens\s+used:\s*([0-9][0-9,]*)\b", re.IGNORECASE)


def parse_tokens_used(stdout: str) -> int | None:
    """codex stdout の `tokens used: N` を抽出する。見つからなければ None。"""
    match = _TOKENS_USED_RE.search(stdout)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def _extract_claude_bare_json(stdout: str) -> dict[str, Any]:
    # claude --bare の実 stdout 形状は本環境で未検証のため、既知の 2 形状だけを受け付ける。
    try:
        payload = json.loads(stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProposalValidationError(f"claude-bare output is not valid JSON: {exc}") from exc
    if isinstance(payload, dict) and payload.get("schema_version") == "1.0":
        return payload
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, str):
        try:
            nested = json.loads(result)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProposalValidationError(f"claude-bare result is not valid JSON: {exc}") from exc
        if isinstance(nested, dict):
            return nested
    if isinstance(result, dict):
        return result
    raise ProposalValidationError("claude-bare output did not contain a proposal object")


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ProposerRuntimeError(
            f"required proposer backend tool is not available on PATH: {name}"
        )
    return path


def _proposer_timeout_seconds(proposer_cfg: dict) -> int:
    value = proposer_cfg.get("timeout_seconds", DEFAULT_PROPOSER_TIMEOUT_SECONDS)
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ProposalValidationError(
            f"proposer.timeout_seconds must be an integer, got: {value!r}"
        ) from exc
    if timeout <= 0:
        raise ProposalValidationError(f"proposer.timeout_seconds must be > 0, got: {timeout}")
    return timeout


def _populate_codex_home(ephemeral_home: Path, source_home: Path | None) -> None:
    source = source_home or _default_codex_home()
    auth_src = source / "auth.json"
    if not auth_src.is_file():
        raise ProposerRuntimeError(f"codex auth.json not found: {auth_src}")
    auth_dst = ephemeral_home / "auth.json"
    shutil.copyfile(auth_src, auth_dst)
    auth_dst.chmod(0o600)
    config_dst = ephemeral_home / "config.toml"
    config_dst.write_text("# meta-harness ephemeral codex home\n", encoding="utf-8")
    config_dst.chmod(0o600)
    agents_dst = ephemeral_home / "AGENTS.md"
    agents_dst.write_text("", encoding="utf-8")
    agents_dst.chmod(0o600)


def _default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def _sweep_orphan_codex_homes() -> None:
    tmp_root = Path(tempfile.gettempdir())
    cutoff = datetime.now().timestamp() - _CODEX_ORPHAN_MAX_AGE.total_seconds()
    for path in tmp_root.glob(f"{_CODEX_HOME_PREFIX}*"):
        try:
            if not path.is_dir() or path.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue
