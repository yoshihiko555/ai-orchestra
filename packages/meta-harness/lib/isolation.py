#!/usr/bin/env python3
"""meta-harness proposer isolation backend（Phase 2 M1）。

docs/design/meta-harness-detailed.md Sec11-3-2..5 が正本。現時点では
`@anthropic-ai/sandbox-runtime`（srt）の設定生成・静的検査・起動前 canary self-test を
実装し、`propose` CLI から利用できる形にする。実際の proposer 起動は後続 M4 で接続する。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import meta_harness_common as mh  # noqa: E402

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

SRT_SELF_TEST_TIMEOUT_SECONDS = 15
SRT_VERSION_TIMEOUT_SECONDS = 10
SRT_SETTINGS_FILENAME = "srt-settings.json"
SRT_STARTUP_FAILURE_MARKERS = (
    "srt-mux",
    "listen EPERM",
    "sandbox runtime",
)

CODEX_ALLOWED_DOMAINS = ["chatgpt.com", "*.chatgpt.com", "*.openai.com", "openai.com"]
CLAUDE_BARE_ALLOWED_DOMAINS = ["api.anthropic.com"]
CODEX_TLS_TERMINATE_EXCLUDE_DOMAINS = ["chatgpt.com", "*.chatgpt.com"]
INSTRUCTION_FILE_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md"}
BASE_ENV_ALLOWLIST = (
    "PATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "NODE_EXTRA_CA_CERTS",
)


class IsolationError(RuntimeError):
    """隔離 backend を fail-closed すべき場合に送出する（CLI は exit 2）。"""


@dataclass(frozen=True)
class IsolationLaunch:
    """proposer 起動時に後続 M4 が使う隔離済み launch 情報。"""

    backend_name: str
    settings_path: Path
    settings: dict
    env: dict[str, str]
    metadata: dict
    owned_settings_dir: Path | None = None


class IsolationBackend(Protocol):
    name: str

    def prepare_launch(
        self,
        *,
        view_dir: Path,
        main_root: Path,
        config: dict,
        ephemeral_home: Path,
        settings_dir: Path,
        proposer_tool: str,
        runner: SubprocessRunner,
    ) -> IsolationLaunch:
        """設定生成・self-test を行い、起動情報を返す。"""


class SrtBackend:
    """srt を主境界として使う IsolationBackend。"""

    name = "srt"

    def prepare_launch(
        self,
        *,
        view_dir: Path,
        main_root: Path,
        config: dict,
        ephemeral_home: Path,
        settings_dir: Path,
        proposer_tool: str,
        runner: SubprocessRunner = subprocess.run,
    ) -> IsolationLaunch:
        verify_no_instruction_files(view_dir)
        if proposer_tool == "codex":
            ensure_empty_agents_file(ephemeral_home)
        run_tmp_dir = _create_run_tmp_dir(settings_dir)
        extra_env = {
            **_extra_env_for_tool(proposer_tool, ephemeral_home),
            **_tmp_env(run_tmp_dir),
        }
        srt_path = _require_srt_binary()
        srt_version = _get_srt_version(srt_path, runner=runner)
        _check_version_pin(config, srt_version)
        settings = build_srt_settings(
            view_dir=view_dir,
            main_root=main_root,
            config=config,
            ephemeral_home=ephemeral_home,
            proposer_tool=proposer_tool,
            run_tmp_dir=run_tmp_dir,
            runner=runner,
        )
        settings_path = write_srt_settings(settings, settings_dir)
        env = build_minimal_env(extra_env)
        _run_srt_canary_self_test(
            srt_path=srt_path,
            settings_path=settings_path,
            view_dir=view_dir,
            main_root=main_root,
            config=config,
            env=env,
            runner=runner,
        )
        metadata = build_isolation_metadata(
            backend_name=self.name,
            srt_version=srt_version,
            settings=settings,
        )
        return IsolationLaunch(
            backend_name=self.name,
            settings_path=settings_path,
            settings=settings,
            env=env,
            metadata=metadata,
        )


def resolve_isolation_backend(
    *,
    view_dir: Path,
    main_root: Path,
    config: dict,
    ephemeral_home: Path,
    settings_dir: Path | None = None,
    proposer_tool: str | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> IsolationLaunch:
    """設定済み isolation backend を解決し、起動前 self-test まで完了する。

    `settings_dir` 省略時は一時ディレクトリを作成し、成功時の所有権は呼び出し側へ移る。
    失敗時は本関数が一時ディレクトリを削除する。明示指定された `settings_dir` は削除しない。
    """
    proposer_cfg = config.get("proposer") or {}
    isolation_cfg = proposer_cfg.get("isolation") or {}
    backend_name = isolation_cfg.get("backend", "srt")
    if backend_name != "srt":
        raise IsolationError(f"unsupported proposer.isolation.backend: {backend_name!r}")
    owns_settings_dir = settings_dir is None
    launch_settings_dir = settings_dir or Path(tempfile.mkdtemp(prefix="meta-harness-srt-"))
    tool = proposer_tool or proposer_cfg.get("tool", "codex")
    try:
        launch = SrtBackend().prepare_launch(
            view_dir=view_dir,
            main_root=main_root,
            config=config,
            ephemeral_home=ephemeral_home,
            settings_dir=launch_settings_dir,
            proposer_tool=tool,
            runner=runner,
        )
        if not owns_settings_dir:
            return launch
        return replace(launch, owned_settings_dir=launch_settings_dir)
    except Exception:
        if owns_settings_dir:
            shutil.rmtree(launch_settings_dir, ignore_errors=True)
        raise


def build_srt_settings(
    *,
    view_dir: Path,
    main_root: Path,
    config: dict,
    ephemeral_home: Path,
    proposer_tool: str,
    run_tmp_dir: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> dict:
    """Sec11-3-5 のフル settings JSON を生成する。"""
    resolved_view = _realpath(view_dir)
    resolved_home = _realpath(ephemeral_home)
    allow_read = _dedupe_paths([resolved_view, *_configured_allow_read_extra(config)])
    forbidden = forbidden_read_paths(main_root, config)
    _assert_no_forbidden_allow_read(allow_read, forbidden)
    allow_write = _allow_write_paths(resolved_view, resolved_home, proposer_tool, run_tmp_dir)
    return {
        "network": {
            "allowedDomains": _allowed_domains_for_tool(proposer_tool),
            "deniedDomains": [],
            "strictAllowlist": True,
            "allowUnixSockets": [],
            "allowLocalBinding": proposer_tool == "codex",
            **_tls_terminate_settings_for_tool(proposer_tool),
        },
        "filesystem": {
            "denyRead": [str(path) for path in derive_deny_read_paths(main_root, runner=runner)],
            "allowRead": [str(path) for path in allow_read],
            "allowWrite": [str(path) for path in allow_write],
            "denyWrite": [],
        },
        "ignoreViolations": {},
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }


def write_srt_settings(settings: dict, settings_dir: Path) -> Path:
    """srt settings を 0600 で書き出す。"""
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_dir.chmod(0o700)
    path = settings_dir / SRT_SETTINGS_FILENAME
    payload = _canonical_json(settings)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.write("\n")
    return path


def build_isolation_metadata(*, backend_name: str, srt_version: str, settings: dict) -> dict:
    """run metadata へ埋め込む隔離情報を返す。"""
    settings_hash = _sha256_text(_canonical_json(settings))
    profile_input_material = _canonical_json(
        {
            "platform": sys.platform,
            "srt_version": srt_version,
            "settings_sha256": settings_hash,
            "settings": settings,
        }
    )
    return {
        "backend": backend_name,
        "srt_version": srt_version,
        "settings_sha256": settings_hash,
        "platform_profile_input_sha256": _sha256_text(profile_input_material),
    }


def build_minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """親環境を丸ごと継承しない proposer 用 env を作る。"""
    env = {key: os.environ[key] for key in BASE_ENV_ALLOWLIST if os.environ.get(key)}
    env.update(extra or {})
    return {key: value for key, value in env.items() if value is not None}


def verify_no_instruction_files(view_dir: Path) -> None:
    """view 内の自動注入対象 instruction file を fail-closed で拒否する。"""
    for path in view_dir.rglob("*"):
        if path.is_symlink():
            continue
        if path.name in INSTRUCTION_FILE_NAMES:
            raise IsolationError(f"instruction file must not be present in filtered view: {path}")


def ensure_empty_agents_file(ephemeral_home: Path) -> Path:
    """codex の親方向/ホーム探索を固定する空 AGENTS.md を作る。"""
    ephemeral_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    ephemeral_home.chmod(0o700)
    agents_path = ephemeral_home / "AGENTS.md"
    fd = os.open(agents_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8"):
        pass
    return agents_path


def _extra_env_for_tool(proposer_tool: str, ephemeral_home: Path) -> dict[str, str]:
    if proposer_tool == "codex":
        return {"CODEX_HOME": str(ephemeral_home)}
    if proposer_tool == "claude-bare":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise IsolationError("proposer.tool 'claude-bare' requires ANTHROPIC_API_KEY")
        return {"ANTHROPIC_API_KEY": api_key}
    raise IsolationError(f"unsupported proposer.tool: {proposer_tool!r}")


def derive_deny_read_paths(
    main_root: Path,
    *,
    runner: SubprocessRunner = subprocess.run,
    home: Path | None = None,
) -> list[Path]:
    """$HOME + main root + 全 worktree root を realpath 済みで返す。"""
    roots = [_realpath(home or Path.home()), _realpath(main_root)]
    roots.extend(_discover_git_worktree_roots(main_root, runner=runner))
    return _dedupe_paths(roots)


def forbidden_read_paths(
    main_root: Path,
    config: dict,
    *,
    home: Path | None = None,
) -> list[Path]:
    """allowRead と交差してはならない既知資産領域を返す。"""
    user_home = _realpath(home or Path.home())
    return _dedupe_paths(
        [
            _realpath(mh.store_dir(main_root, config)),
            _realpath(mh.holdout_runs_dir(main_root, config)),
            _realpath(main_root / "facets"),
            _realpath(user_home / ".claude" / "projects"),
            _realpath(user_home / ".codex"),
        ]
    )


def wrap_srt_command(srt_path: str, settings_path: Path, command: list[str]) -> list[str]:
    """srt CLI 経由のコマンド配列を作る。"""
    return [srt_path, "--settings", str(settings_path), *command]


def _require_srt_binary() -> str:
    srt_path = shutil.which("srt")
    if not srt_path:
        raise IsolationError("srt is not available on PATH")
    return srt_path


def _require_curl_binary() -> str:
    curl_path = shutil.which("curl")
    if not curl_path:
        raise IsolationError("curl is required for srt network canary self-test")
    return curl_path


def _get_srt_version(srt_path: str, *, runner: SubprocessRunner) -> str:
    try:
        completed = runner(
            [srt_path, "--version"],
            capture_output=True,
            text=True,
            timeout=SRT_VERSION_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationError(f"could not determine srt version: {exc}") from exc
    version_output = completed.stdout if (completed.stdout or "").strip() else completed.stderr
    first_line = (version_output or "").splitlines()[0:1]
    output = first_line[0] if first_line else ""
    match = re.search(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", output)
    if completed.returncode != 0 or match is None:
        raise IsolationError("could not determine srt version")
    return match.group(0)


def _check_version_pin(config: dict, srt_version: str) -> None:
    isolation_cfg = (config.get("proposer") or {}).get("isolation") or {}
    version_pin = isolation_cfg.get("srt_version_pin")
    if version_pin is not None and srt_version != version_pin:
        raise IsolationError(
            f"proposer.isolation.srt_version_pin mismatch: expected {version_pin!r}, "
            f"got {srt_version!r}"
        )


def _run_srt_canary_self_test(
    *,
    srt_path: str,
    settings_path: Path,
    view_dir: Path,
    main_root: Path,
    config: dict,
    env: dict[str, str],
    runner: SubprocessRunner,
) -> None:
    read_canary = _create_read_canary(main_root, config)
    try:
        read_cmd = ["/bin/cat", str(read_canary)]
        curl_path = _require_curl_binary()
        # 93.184.216.34 is example.com; update both canaries if that allocation changes.
        network_domain_cmd = [curl_path, "--fail", "--max-time", "3", "https://example.com"]
        network_direct_ip_cmd = [curl_path, "--fail", "--max-time", "3", "http://93.184.216.34"]
        _assert_sandbox_rejects(
            wrap_srt_command(srt_path, settings_path, read_cmd),
            view_dir=view_dir,
            env=env,
            runner=runner,
            label="view-external read canary",
            is_policy_denial=_read_canary_denied,
        )
        _assert_sandbox_rejects(
            wrap_srt_command(srt_path, settings_path, network_domain_cmd),
            view_dir=view_dir,
            env=env,
            runner=runner,
            label="non-allowlisted network canary",
            is_policy_denial=_network_canary_denied,
        )
        _assert_sandbox_rejects(
            wrap_srt_command(srt_path, settings_path, network_direct_ip_cmd),
            view_dir=view_dir,
            env=env,
            runner=runner,
            label="direct-IP network canary",
            is_policy_denial=_network_canary_denied,
        )
    finally:
        read_canary.unlink(missing_ok=True)


# read 遮断の実測シグナル: seatbelt は EPERM（macOS）、bubblewrap は EACCES（Linux）。
_READ_DENIAL_MARKERS = ("operation not permitted", "permission denied")
# curl の遮断実測（§11-3-5）: 56 = 接続遮断、22 = `--fail` による srt proxy の 403 応答。
_CURL_DENIAL_EXIT_CODES = frozenset({22, 56})


def _read_canary_denied(completed: subprocess.CompletedProcess) -> bool:
    output = f"{completed.stderr or ''}\n{completed.stdout or ''}".lower()
    return any(marker in output for marker in _READ_DENIAL_MARKERS)


def _network_canary_denied(completed: subprocess.CompletedProcess) -> bool:
    return completed.returncode in _CURL_DENIAL_EXIT_CODES


def _assert_sandbox_rejects(
    cmd: list[str],
    *,
    view_dir: Path,
    env: dict[str, str],
    runner: SubprocessRunner,
    label: str,
    is_policy_denial: Callable[[subprocess.CompletedProcess], bool],
) -> None:
    try:
        completed = runner(
            cmd,
            cwd=view_dir,
            capture_output=True,
            text=True,
            timeout=SRT_SELF_TEST_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationError(f"srt canary failed to run ({label}): {exc}") from exc
    if completed.returncode == 0:
        raise IsolationError(f"srt canary unexpectedly succeeded: {label}")
    if _looks_like_srt_startup_failure(completed):
        raise IsolationError(
            f"srt canary could not prove isolation because srt failed to start ({label}): "
            f"{completed.stderr.strip()[:500]}"
        )
    if not is_policy_denial(completed):
        # DNS 障害・タイムアウト・TLS エラー等の非ゼロ終了は隔離の証明にならない。
        raise IsolationError(
            f"srt canary failed without a sandbox-denial signal ({label}): "
            f"exit={completed.returncode}, {(completed.stderr or '').strip()[:300]}"
        )


def _looks_like_srt_startup_failure(completed: subprocess.CompletedProcess) -> bool:
    stderr = (completed.stderr or "").lower()
    stdout = (completed.stdout or "").lower()
    output = f"{stderr}\n{stdout}"
    return output.startswith("error:") or any(
        marker.lower() in output for marker in SRT_STARTUP_FAILURE_MARKERS
    )


def _create_read_canary(main_root: Path, config: dict) -> Path:
    parent = mh.tmp_dir(main_root, config) / "isolation-canaries"
    parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="deny-read-", dir=parent)
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("meta-harness isolation canary\n")
    return path


def _discover_git_worktree_roots(
    main_root: Path, *, runner: SubprocessRunner = subprocess.run
) -> list[Path]:
    try:
        completed = runner(
            ["git", "worktree", "list", "--porcelain"],
            cwd=main_root,
            capture_output=True,
            text=True,
            timeout=mh.GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationError(f"could not list git worktrees: {exc}") from exc
    if completed.returncode != 0:
        raise IsolationError("could not list git worktrees")
    roots: list[Path] = []
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            roots.append(_realpath(Path(line.removeprefix("worktree "))))
    return roots


def _configured_allow_read_extra(config: dict) -> list[Path]:
    """追加 allowRead を realpath 化する。

    ここで返す path は store/holdout/facet source/実 home 配下の機密領域と重なってはならない。
    呼び出し側の静的 guard が交差を fail-closed で拒否する。
    """
    isolation_cfg = (config.get("proposer") or {}).get("isolation") or {}
    return [
        _realpath(Path(path).expanduser()) for path in isolation_cfg.get("allow_read_extra", [])
    ]


def _assert_no_forbidden_allow_read(allow_read: list[Path], forbidden: list[Path]) -> None:
    for allowed in allow_read:
        for blocked in forbidden:
            if _paths_intersect(allowed, blocked):
                raise IsolationError(
                    f"proposer.isolation allowRead path intersects forbidden asset: "
                    f"{allowed} <-> {blocked}"
                )


def _allow_write_paths(
    view_dir: Path, ephemeral_home: Path, proposer_tool: str, run_tmp_dir: Path | None
) -> list[Path]:
    # 共有 /tmp・/private/tmp は許可しない: prompt injection された command が同一
    # ユーザーの他プロセスの一時ファイル・socket・別実行の settings を変更できるため、
    # per-run の専用 tmp（0700、TMPDIR/TMP/TEMP で固定）だけを許可する。
    paths = [view_dir]
    if proposer_tool == "codex":
        paths.append(ephemeral_home)
    if run_tmp_dir is not None:
        paths.append(run_tmp_dir)
    return _dedupe_paths(paths)


_RUN_TMP_DIR_NAME = "proposer-tmp"


def _create_run_tmp_dir(settings_dir: Path) -> Path:
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_dir.chmod(0o700)
    run_tmp = settings_dir / _RUN_TMP_DIR_NAME
    run_tmp.mkdir(mode=0o700, exist_ok=True)
    run_tmp.chmod(0o700)
    return _realpath(run_tmp)


def _tmp_env(run_tmp_dir: Path) -> dict[str, str]:
    value = str(run_tmp_dir)
    return {"TMPDIR": value, "TMP": value, "TEMP": value}


def _allowed_domains_for_tool(proposer_tool: str) -> list[str]:
    if proposer_tool == "codex":
        return list(CODEX_ALLOWED_DOMAINS)
    if proposer_tool == "claude-bare":
        return list(CLAUDE_BARE_ALLOWED_DOMAINS)
    raise IsolationError(f"unsupported proposer.tool: {proposer_tool!r}")


def _tls_terminate_settings_for_tool(proposer_tool: str) -> dict:
    if proposer_tool == "codex":
        return {"tlsTerminate": {"excludeDomains": list(CODEX_TLS_TERMINATE_EXCLUDE_DOMAINS)}}
    if proposer_tool == "claude-bare":
        return {}
    raise IsolationError(f"unsupported proposer.tool: {proposer_tool!r}")


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = _realpath(path)
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def _paths_intersect(left: Path, right: Path) -> bool:
    if left == right:
        return True
    return left in right.parents or right in left.parents


def _realpath(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _canonical_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
