#!/usr/bin/env python3
"""SessionStart hook: cocoindex MCP 設定の reconcile と proxy warmup を行う。

対象:
  - Claude Code: {project_dir}/.mcp.json
  - Codex CLI:   {project_dir}/.codex/config.toml
  - Gemini CLI:  {project_dir}/.gemini/settings.json

冪等: 現在の状態と一致していれば書き込みをスキップする。
クリーンアップ: enabled=false またはパッケージ未インストール時にエントリを削除する。

v2: proxy.enabled=true 時は mcp-proxy 経由の HTTP エントリを生成する。
current session の接続を救済することはせず、必要に応じて proxy warmup を
バックグラウンドで開始する。
"""

from __future__ import annotations

import json
import os
import re
import sys

# hook_common を import するため core/hooks を sys.path に追加
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

# proxy_manager を import するため自ディレクトリを sys.path に追加
_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if _hooks_dir not in sys.path:
    sys.path.insert(0, _hooks_dir)

from hook_common import (
    load_package_config,
    read_hook_input,
    read_json_safe,
    safe_hook_execution,
    write_json,
)
from proxy_manager import (
    build_proxy_url,
    clear_session_state,
    get_proxy_state,
    start_proxy_background,
    write_session_state,
)

# ---------------------------------------------------------------------------
# Claude Code (.mcp.json)
# ---------------------------------------------------------------------------


def _build_claude_entry(config: dict, proxy_enabled: bool, project_dir: str) -> dict:
    """Claude Code 用の MCP サーバーエントリを構築する。"""
    target = config.get("targets", {}).get("claude", {})

    if proxy_enabled and not target.get("force_stdio", False):
        return {"type": "sse", "url": build_proxy_url("claude", config, project_dir)}

    entry: dict = {
        "command": config["command"],
        "args": list(config["args"]),
    }
    if target.get("type"):
        entry["type"] = target["type"]
    return entry


def provision_claude(
    project_dir: str, config: dict, server_name: str, proxy_enabled: bool = False
) -> str | None:
    """Claude Code の .mcp.json にエントリを追加/更新する。"""
    mcp_path = os.path.join(project_dir, ".mcp.json")
    data = read_json_safe(mcp_path)

    servers = data.get("mcpServers", {})
    new_entry = _build_claude_entry(config, proxy_enabled, project_dir)

    if servers.get(server_name) == new_entry:
        return None

    servers[server_name] = new_entry
    data["mcpServers"] = servers
    write_json(mcp_path, data)
    return "claude"


def cleanup_claude(project_dir: str, server_name: str) -> str | None:
    """Claude Code の .mcp.json からエントリを削除する。"""
    mcp_path = os.path.join(project_dir, ".mcp.json")
    if not os.path.isfile(mcp_path):
        return None

    data = read_json_safe(mcp_path)
    servers = data.get("mcpServers", {})

    if server_name not in servers:
        return None

    del servers[server_name]

    if not servers:
        del data["mcpServers"]

    if not data:
        os.remove(mcp_path)
    else:
        write_json(mcp_path, data)
    return "claude"


# ---------------------------------------------------------------------------
# Codex CLI (.codex/config.toml)
# ---------------------------------------------------------------------------

_TOML_HEADER_RE = re.compile(r"^\[([^\]]+)\]")


def _build_toml_section(
    server_name: str, config: dict, proxy_enabled: bool, project_dir: str
) -> str:
    """Codex 用の TOML セクション文字列を生成する。"""
    target = config.get("targets", {}).get("codex", {})
    lines = [f"[mcp_servers.{server_name}]"]

    if proxy_enabled and not target.get("force_stdio", False):
        lines.append(f'url = "{build_proxy_url("codex", config, project_dir)}"')
    else:
        lines.append(f'command = "{config["command"]}"')
        args_str = json.dumps(config["args"])
        lines.append(f"args = {args_str}")
        lines.append("enabled = true")
    return "\n".join(lines)


def _find_toml_section(content: str, section_name: str) -> tuple[int, int] | None:
    """TOML コンテンツから指定セクションの開始行・終了行（排他）を返す。

    行走査方式で検出し、次のセクションヘッダまたは EOF を終端とする。
    """
    lines = content.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = _TOML_HEADER_RE.match(line.strip())
        if m is None:
            continue
        header = m.group(1)
        if start is not None:
            return (start, i)
        if header == section_name:
            start = i
    if start is not None:
        return (start, len(lines))
    return None


def provision_codex(
    project_dir: str, config: dict, server_name: str, proxy_enabled: bool = False
) -> str | None:
    """Codex CLI の .codex/config.toml にセクションを追加/更新する。"""
    toml_path = os.path.join(project_dir, ".codex", "config.toml")
    if not os.path.isfile(toml_path):
        return None

    content = _read_text(toml_path)
    section_key = f"mcp_servers.{server_name}"
    new_section = _build_toml_section(server_name, config, proxy_enabled, project_dir)

    span = _find_toml_section(content, section_key)
    if span is not None:
        lines = content.splitlines()
        old_section = "\n".join(lines[span[0] : span[1]])
        if old_section.rstrip() == new_section.rstrip():
            return None
        new_lines = lines[: span[0]] + new_section.splitlines() + lines[span[1] :]
        _write_text(toml_path, "\n".join(new_lines) + "\n")
    else:
        separator = "\n" if content.endswith("\n") else "\n\n"
        _write_text(toml_path, content.rstrip("\n") + "\n" + separator + new_section + "\n")

    return "codex"


def cleanup_codex(project_dir: str, server_name: str) -> str | None:
    """Codex CLI の .codex/config.toml からセクションを削除する。"""
    toml_path = os.path.join(project_dir, ".codex", "config.toml")
    if not os.path.isfile(toml_path):
        return None

    content = _read_text(toml_path)
    section_key = f"mcp_servers.{server_name}"

    span = _find_toml_section(content, section_key)
    if span is None:
        return None

    lines = content.splitlines()
    # セクション前後の空行を 1 行に圧縮
    new_lines = lines[: span[0]] + lines[span[1] :]
    result = "\n".join(new_lines).strip("\n") + "\n"
    _write_text(toml_path, result)
    return "codex"


# ---------------------------------------------------------------------------
# Gemini CLI (.gemini/settings.json)
# ---------------------------------------------------------------------------


def _build_gemini_entry(config: dict, proxy_enabled: bool, project_dir: str) -> dict:
    """Gemini CLI 用の MCP サーバーエントリを構築する。"""
    target = config.get("targets", {}).get("gemini", {})

    if proxy_enabled and not target.get("force_stdio", False):
        return {"url": build_proxy_url("gemini", config, project_dir)}

    return {
        "command": config["command"],
        "args": list(config["args"]),
    }


def provision_gemini(
    project_dir: str, config: dict, server_name: str, proxy_enabled: bool = False
) -> str | None:
    """Gemini CLI の .gemini/settings.json にエントリを追加/更新する。"""
    settings_path = os.path.join(project_dir, ".gemini", "settings.json")
    if not os.path.isfile(settings_path):
        return None

    data = read_json_safe(settings_path)
    servers = data.get("mcpServers", {})
    new_entry = _build_gemini_entry(config, proxy_enabled, project_dir)

    if servers.get(server_name) == new_entry:
        return None

    servers[server_name] = new_entry
    data["mcpServers"] = servers
    write_json(settings_path, data)
    return "gemini"


def cleanup_gemini(project_dir: str, server_name: str) -> str | None:
    """Gemini CLI の .gemini/settings.json からエントリを削除する。"""
    settings_path = os.path.join(project_dir, ".gemini", "settings.json")
    if not os.path.isfile(settings_path):
        return None

    data = read_json_safe(settings_path)
    servers = data.get("mcpServers", {})

    if server_name not in servers:
        return None

    del servers[server_name]

    if not servers:
        del data["mcpServers"]

    write_json(settings_path, data)
    return "gemini"


# ---------------------------------------------------------------------------
# テキストファイル I/O
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str:
    """テキストファイルを読み込む。存在しなければ空文字を返す。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _write_text(path: str, content: str) -> None:
    """テキストファイルに書き出す。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

TARGET_HANDLERS = {
    "claude": (provision_claude, cleanup_claude),
    "codex": (provision_codex, cleanup_codex),
    "gemini": (provision_gemini, cleanup_gemini),
}


def _session_uses_proxy(config: dict, proxy_enabled: bool) -> bool:
    """現在の Claude session が proxy reconnect 対象かを返す。"""
    if not proxy_enabled:
        return False
    target = (config.get("targets", {}) or {}).get("claude", {}) or {}
    return bool(target.get("enabled", False) and not target.get("force_stdio", False))


@safe_hook_execution
def main() -> None:
    data = read_hook_input()
    project_dir = data.get("cwd", "")
    if not project_dir:
        return
    session_id = str(data.get("session_id") or "")

    config = load_package_config("cocoindex", "cocoindex.yaml", project_dir)
    enabled = config.get("enabled", False) if config else False
    server_name = config.get("server_name", "cocoindex-code") if config else "cocoindex-code"

    proxy_enabled = bool(enabled and config and (config.get("proxy", {}) or {}).get("enabled", False))
    warmup_started = False
    session_uses_proxy = _session_uses_proxy(config, proxy_enabled) if config else False
    if proxy_enabled and config:
        proxy_state = get_proxy_state(config, project_dir)
        proxy_ready = proxy_state.get("proxy_state") in {"ready", "idle"}
        if session_id:
            if session_uses_proxy:
                write_session_state(project_dir, session_id, reconnect_required=not proxy_ready)
            else:
                clear_session_state(project_dir, session_id)
        if not proxy_ready:
            warmup_started = start_proxy_background(config, project_dir)
    elif session_id:
        clear_session_state(project_dir, session_id)

    changed: list[str] = []

    if enabled:
        targets = config.get("targets", {})
        for target_name, (provision_fn, cleanup_fn) in TARGET_HANDLERS.items():
            target_config = targets.get(target_name, {})
            if target_config.get("enabled", False):
                result = provision_fn(project_dir, config, server_name, proxy_enabled)
            else:
                result = cleanup_fn(project_dir, server_name)
            if result:
                changed.append(result)
    else:
        for target_name, (_provision_fn, cleanup_fn) in TARGET_HANDLERS.items():
            result = cleanup_fn(project_dir, server_name)
            if result:
                changed.append(result)

    messages: list[str] = []
    if changed:
        mode = "proxy" if proxy_enabled else "stdio"
        messages.append(f"[cocoindex] MCP server provisioned ({mode}): {', '.join(changed)}")
    if warmup_started:
        messages.append("[cocoindex] mcp-proxy warmup started in background")
    if messages:
        print("\n".join(messages))


if __name__ == "__main__":
    main()
