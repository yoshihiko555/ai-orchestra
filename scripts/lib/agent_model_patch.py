"""サブエージェント .md の frontmatter model パッチ。"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


def _deep_merge(base: dict, override: dict) -> dict:
    """override の値で base を再帰的に上書きする。"""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml_safe(path: Path) -> dict:
    """YAML ファイルを読み込み、失敗時は空辞書を返す。"""
    if yaml is None or not path.is_file():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError:
        return {}
    except yaml.YAMLError:
        return {}

    if isinstance(data, dict):
        return data
    return {}


def load_cli_tools_config(project_dir: Path) -> dict:
    """cli-tools.yaml と cli-tools.local.yaml を読み込み、上書きをマージする。"""
    config_dir = project_dir / ".claude" / "config" / "agent-routing"
    base = _read_yaml_safe(config_dir / "cli-tools.yaml")
    local = _read_yaml_safe(config_dir / "cli-tools.local.yaml")

    if local:
        return _deep_merge(base, local)
    return base


def resolve_agent_model(agent_name: str, config: dict) -> str | None:
    """agent の model を解決する（agents.model > subagent.default_model > None）。"""
    agents = config.get("agents", {})
    if isinstance(agents, dict):
        agent_cfg = agents.get(agent_name, {})
        if isinstance(agent_cfg, dict):
            if "model" in agent_cfg:
                model = agent_cfg.get("model")
                if isinstance(model, str) and model.strip():
                    return model.strip()

    subagent = config.get("subagent", {})
    if isinstance(subagent, dict):
        default_model = subagent.get("default_model")
        if isinstance(default_model, str) and default_model.strip():
            return default_model.strip()

    return None


def patch_agent_model(file_path: Path, model: str) -> bool:
    """agent .md の frontmatter model 行を置換する。"""
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return False

    frontmatter_match = re.match(r"(?s)\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", content)
    if not frontmatter_match:
        return False

    frontmatter = frontmatter_match.group(1)
    model_pattern = re.compile(r"(?m)^model:\s*.*$")
    if not model_pattern.search(frontmatter):
        return False

    new_frontmatter = model_pattern.sub(f"model: {model}", frontmatter, count=1)
    if new_frontmatter == frontmatter:
        return False

    new_content = (
        content[: frontmatter_match.start(1)]
        + new_frontmatter
        + content[frontmatter_match.end(1) :]
    )
    try:
        file_path.write_text(new_content, encoding="utf-8")
    except OSError:
        return False

    return True


def patch_all_agents(project_dir: Path) -> int:
    """全エージェント .md の model をパッチし、変更数を返す。"""
    cli_tools_config = load_cli_tools_config(project_dir)
    agents_dir = project_dir / ".claude" / "agents"
    if not agents_dir.is_dir():
        return 0

    patched_count = 0
    for agent_file in sorted(agents_dir.glob("*.md")):
        model = resolve_agent_model(agent_file.stem, cli_tools_config)
        if not model:
            continue
        if patch_agent_model(agent_file, model):
            patched_count += 1

    return patched_count
