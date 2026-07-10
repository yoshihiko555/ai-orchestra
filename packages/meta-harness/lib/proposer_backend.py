#!/usr/bin/env python3
"""meta-harness proposer backend launch helpers（Phase 2 M4）。"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import isolation as iso  # noqa: E402
import meta_harness_common as mh  # noqa: E402

PROPOSAL_SCHEMA_NAME = "proposal.schema.json"
DEFAULT_PROPOSER_TIMEOUT_SECONDS = 600
PROCESS_KILL_DRAIN_SECONDS = 5
MAX_PROPOSAL_OUTPUT_BYTES = 5_000_000
CODEX_AUTH_CANARY_PREFIX = "meta-harness-canary-refresh-"
AUTH_TOKEN_TTL_MARGIN_SECONDS = 120
_CODEX_HOME_PREFIX = "meta-harness-codex-home-"
_CODEX_ORPHAN_MAX_AGE = timedelta(hours=24)
_CODEX_MODEL_CATALOG_FILES = ("models_cache.json", "version.json")

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
    ephemeral_home: Path | None = None,
    allowed_based_on_runs: list[str] | tuple[str, ...] | None = None,
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
            ephemeral_home=ephemeral_home,
            allowed_based_on_runs=allowed_based_on_runs,
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
    raw = _read_proposal_output_bytes(output_path)
    try:
        proposal = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalValidationError(f"proposal output is not valid JSON: {exc}") from exc
    if not isinstance(proposal, dict):
        raise ProposalValidationError("proposal output must be a JSON object")
    schema = mh.load_schema(schema_dir, PROPOSAL_SCHEMA_NAME)
    errors = mh.validate_against_schema(proposal, schema, schema_dir)
    if errors:
        raise ProposalValidationError(f"proposal schema mismatch: {'; '.join(errors[:5])}")
    return proposal


def _read_proposal_output_bytes(output_path: Path) -> bytes:
    """backend が書ける領域の出力ファイルを symlink 非追従・上限付きで読む。

    backend は view_dir に書込可能なため、output path を symlink に差し替えて
    隔離外ファイルをホスト権限で読ませる攻撃を防ぐ（O_NOFOLLOW + fstat 検査）。
    """
    try:
        fd = os.open(output_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ProposalValidationError("proposal output file is missing or empty") from None
    except OSError as exc:
        raise ProposalValidationError(
            f"proposal output is not a readable regular file: {exc}"
        ) from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ProposalValidationError("proposal output must be a regular file")
        if info.st_size == 0:
            raise ProposalValidationError("proposal output file is missing or empty")
        if info.st_size > MAX_PROPOSAL_OUTPUT_BYTES:
            raise ProposalValidationError(
                f"proposal output exceeds max bytes: {info.st_size} > {MAX_PROPOSAL_OUTPUT_BYTES}"
            )
        return handle.read(MAX_PROPOSAL_OUTPUT_BYTES + 1)


def _write_proposal_output(output_path: Path, proposal: dict[str, Any]) -> None:
    """backend 実行後の出力書込を symlink 非追従で行う（claude-bare 経路）。

    backend が output path に symlink を先置きしてもホスト権限の書込が
    隔離外ファイルへ向かわないよう、unlink 後に O_EXCL で新規作成する。
    """
    output_path.unlink(missing_ok=True)
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(proposal, ensure_ascii=False) + "\n")


@contextmanager
def temporary_codex_home(
    source_home: Path | None = None,
    *,
    min_token_ttl_seconds: int | None = None,
):
    """最小化した `auth.json` を持つ ephemeral CODEX_HOME を用意する（Sec11-3-6 L1）。"""
    _sweep_orphan_codex_homes()
    path = Path(tempfile.mkdtemp(prefix=_CODEX_HOME_PREFIX, dir=tempfile.gettempdir()))
    path.chmod(0o700)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle_sigterm(signum: int, _frame: object) -> None:
        shutil.rmtree(path, ignore_errors=True)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        _populate_codex_home(
            path, source_home=source_home, min_token_ttl_seconds=min_token_ttl_seconds
        )
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
    ephemeral_home: Path | None,
    allowed_based_on_runs: list[str] | tuple[str, ...] | None,
    runner: SubprocessRunner | None,
) -> subprocess.CompletedProcess:
    srt_path = _require_tool("srt")
    _require_tool("codex")
    output_schema_path = _stage_codex_output_schema(
        schema_dir=schema_dir,
        ephemeral_home=_resolve_codex_home(ephemeral_home, isolation_launch),
        allowed_based_on_runs=allowed_based_on_runs,
    )
    command = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
        "--output-schema",
        str(output_schema_path),
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
    _write_proposal_output(output_path, proposal)
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
            f"{label} exited {completed.returncode}: {_error_excerpt(completed)}"
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
        out, err = _drain_after_kill(process)
        raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err) from exc
    finally:
        # backend が daemon 化した子孫を残しても、正常・異常の両経路で必ず掃除する。
        _kill_process_group(process.pid)
    return subprocess.CompletedProcess(args, process.returncode, out, err)


def _drain_after_kill(process: subprocess.Popen) -> tuple[str | None, str | None]:
    """kill 済みプロセスの残余出力を期限付きで回収する。

    孤児化した孫プロセスが pipe を保持し続けると素の `communicate()` は
    無期限に停止しうるため、期限超過時は出力回収を諦める。
    """
    try:
        return process.communicate(timeout=PROCESS_KILL_DRAIN_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return None, None


def _error_excerpt(completed: subprocess.CompletedProcess) -> str:
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    parts = []
    if stderr:
        parts.append(f"stderr={stderr[:500]}")
    if stdout:
        parts.append(f"stdout={stdout[:500]}")
    return "; ".join(parts) if parts else "(no output)"


def _kill_process_group(pid: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        # start_new_session=True で起動しているため leader 終了後も pgid == pid。
        # 残存メンバーがいる可能性があるので pid を pgid として kill を試みる。
        pgid = pid
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


_TOKENS_USED_RE = re.compile(r"\btokens\s+used:\s*([0-9][0-9,]*)\b", re.IGNORECASE)
_CODEX_USAGE_EVENT_TYPE = "turn.completed"


def parse_tokens_used(stdout: str) -> int | None:
    """codex JSONL usage を抽出し、旧 plain-text 形式へ fallback する。"""
    jsonl_tokens = _parse_codex_jsonl_tokens(stdout)
    if jsonl_tokens is not None:
        return jsonl_tokens
    match = _TOKENS_USED_RE.search(stdout)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def _parse_codex_jsonl_tokens(stdout: str) -> int | None:
    latest_tokens = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != _CODEX_USAGE_EVENT_TYPE:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        token_count = _usage_token_count(usage)
        if token_count is not None:
            latest_tokens = token_count
    return latest_tokens


def _usage_token_count(usage: dict[str, Any]) -> int | None:
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0) + (output_tokens or 0)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


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


def _resolve_codex_home(ephemeral_home: Path | None, isolation_launch: iso.IsolationLaunch) -> Path:
    if ephemeral_home is not None:
        return ephemeral_home
    codex_home = isolation_launch.env.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home)
    raise ProposerRuntimeError("codex proposer requires an ephemeral CODEX_HOME")


def _stage_codex_output_schema(
    *,
    schema_dir: Path,
    ephemeral_home: Path,
    allowed_based_on_runs: list[str] | tuple[str, ...] | None = None,
) -> Path:
    schema_path = schema_dir / PROPOSAL_SCHEMA_NAME
    if not schema_path.is_file():
        raise ProposerRuntimeError(f"proposal schema file not found: {schema_path}")
    ephemeral_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged_path = ephemeral_home / PROPOSAL_SCHEMA_NAME
    if allowed_based_on_runs is None:
        shutil.copyfile(schema_path, staged_path)
    else:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema["properties"]["based_on_runs"]["items"]["enum"] = sorted(
            {str(run_id) for run_id in allowed_based_on_runs}
        )
        staged_path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    staged_path.chmod(0o644)
    return staged_path


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


def _populate_codex_home(
    ephemeral_home: Path,
    source_home: Path | None,
    *,
    min_token_ttl_seconds: int | None = None,
) -> None:
    source = source_home or _default_codex_home()
    auth_src = source / "auth.json"
    if not auth_src.is_file():
        raise ProposerRuntimeError(f"codex auth.json not found: {auth_src}")
    auth_dst = ephemeral_home / "auth.json"
    auth_dst.write_text(
        _minimize_codex_auth(
            auth_src.read_text(encoding="utf-8"),
            min_token_ttl_seconds=min_token_ttl_seconds,
        ),
        encoding="utf-8",
    )
    auth_dst.chmod(0o600)
    config_dst = ephemeral_home / "config.toml"
    config_dst.write_text("# meta-harness ephemeral codex home\n", encoding="utf-8")
    config_dst.chmod(0o600)
    _stage_codex_model_catalog(ephemeral_home=ephemeral_home, source_home=source)
    agents_dst = ephemeral_home / "AGENTS.md"
    agents_dst.write_text("", encoding="utf-8")
    agents_dst.chmod(0o600)


def _stage_codex_model_catalog(*, ephemeral_home: Path, source_home: Path) -> None:
    for name in _CODEX_MODEL_CATALOG_FILES:
        src = source_home / name
        if not src.is_file():
            continue
        dst = ephemeral_home / name
        shutil.copyfile(src, dst)
        dst.chmod(0o644)


def _minimize_codex_auth(auth_raw: str, *, min_token_ttl_seconds: int | None) -> str:
    """staged auth.json から長期資格情報を排除する（Sec11-3-6 L1）。

    - `OPENAI_API_KEY` はフィールドごと削除（実測: 削除しても codex exec は完走）
    - `refresh_token` はパーサ必須フィールドのため canary 値に置換
      （実測: 非空の無効値でも完走。漏えい誘導時に盗まれるのは canary になる）
    - access token は refresh 不能になるため、残存 TTL を preflight で検査し
      不足時は fail-closed（実資格情報への refresh 代行は行わない）
    """
    try:
        auth = json.loads(auth_raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProposalValidationError(f"codex auth.json is not valid JSON: {exc}") from exc
    if not isinstance(auth, dict):
        raise ProposalValidationError("codex auth.json must be a JSON object")
    auth.pop("OPENAI_API_KEY", None)
    tokens = auth.get("tokens")
    if isinstance(tokens, dict):
        tokens["refresh_token"] = _generate_auth_canary()
        access_token = tokens.get("access_token")
        if min_token_ttl_seconds is not None and isinstance(access_token, str):
            _assert_access_token_ttl(access_token, min_token_ttl_seconds)
    return json.dumps(auth, ensure_ascii=False) + "\n"


def _generate_auth_canary() -> str:
    return f"{CODEX_AUTH_CANARY_PREFIX}{secrets.token_hex(16)}"


def _assert_access_token_ttl(access_token: str, min_ttl_seconds: int) -> None:
    exp = _jwt_exp_epoch(access_token)
    remaining = exp - datetime.now(UTC).timestamp()
    if remaining < min_ttl_seconds:
        raise ProposalValidationError(
            "codex access token expires too soon for a refresh-less proposer run "
            f"(remaining={int(remaining)}s < required={min_ttl_seconds}s). "
            "Run any normal codex command to refresh the token, then retry."
        )


def _jwt_exp_epoch(token: str) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        raise ProposalValidationError("codex access token is not a JWT; cannot verify its TTL")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProposalValidationError(f"codex access token payload is unreadable: {exc}") from exc
    exp = claims.get("exp")
    if not isinstance(exp, int):
        raise ProposalValidationError("codex access token has no integer exp claim")
    return exp


def min_codex_token_ttl_seconds(config: dict) -> int:
    """proposer timeout + 余裕分。staging 前の access token preflight に使う。"""
    proposer_cfg = config.get("proposer") or {}
    return _proposer_timeout_seconds(proposer_cfg) + AUTH_TOKEN_TTL_MARGIN_SECONDS


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
