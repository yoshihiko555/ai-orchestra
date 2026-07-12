"""cli-tools.yaml 駆動のルーティング共有モジュール。"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Collection

_hook_dir = os.path.dirname(os.path.abspath(__file__))

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import (  # noqa: E402, F401
    DEFAULT_ANTIGRAVITY_FLAGS,
    DEFAULT_ANTIGRAVITY_MODEL,
    DEFAULT_CODEX_FLAGS,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_SANDBOX_ANALYSIS,
    is_cli_enabled,
    load_package_config,
    normalize_cli_tools_config,
)

# エージェントルーティング設定（25エージェント分）
AGENT_TRIGGERS: dict[str, dict[str, list[str]]] = {
    # Planning & Research
    "planner": {
        "ja": ["計画", "タスク分解", "どう進める", "マイルストーン", "手順"],
        "en": ["plan", "break down", "how to proceed", "milestone", "steps"],
    },
    "researcher": {
        "ja": ["調べて", "リサーチ", "調査", "情報収集", "競合"],
        "en": ["research", "investigate", "look up", "gather info", "competitive"],
    },
    # Requirements & Spec
    "requirements": {
        "ja": ["要件", "要件定義", "スコープ", "NFR", "受け入れ条件"],
        "en": ["requirements", "scope", "acceptance criteria", "nfr"],
    },
    "spec-writer": {
        "ja": ["仕様書", "API仕様", "DB仕様", "画面仕様"],
        "en": ["specification", "api spec", "db spec", "screen spec"],
    },
    # Design
    "architect": {
        "ja": ["アーキテクチャ", "技術選定", "全体設計", "構成"],
        "en": ["architecture", "tech stack", "system design", "structure"],
    },
    "api-designer": {
        "ja": ["API設計", "エンドポイント", "インターフェース設計"],
        "en": ["api design", "endpoint", "interface design"],
    },
    "data-modeler": {
        "ja": ["データモデル", "DB設計", "スキーマ", "テーブル設計"],
        "en": ["data model", "database design", "schema", "table design"],
    },
    "auth-designer": {
        "ja": ["認証", "認可", "権限", "ログイン", "セキュリティ設計"],
        "en": ["auth", "authentication", "authorization", "permission", "login"],
    },
    # Implementation
    "frontend-dev": {
        "ja": ["フロントエンド", "React", "Next.js", "UI", "コンポーネント"],
        "en": ["frontend", "react", "next.js", "ui", "component"],
    },
    "backend-python-dev": {
        "ja": ["Python", "FastAPI", "Flask", "Pythonで"],
        "en": ["python", "fastapi", "flask", "in python"],
    },
    "backend-go-dev": {
        "ja": ["Go", "Golang", "Echo", "Gin", "Goで"],
        "en": ["go", "golang", "echo", "gin", "in go"],
    },
    # AI/ML
    "ai-architect": {
        "ja": ["AIアーキテクチャ", "モデル選定", "LLM設計", "AI設計"],
        "en": ["ai architecture", "model selection", "llm design", "ai design"],
    },
    "ai-dev": {
        "ja": ["AI実装", "LLM連携", "生成AI", "AI機能"],
        "en": ["ai implementation", "llm integration", "generative ai", "ai feature"],
    },
    "prompt-engineer": {
        "ja": ["プロンプト", "プロンプト設計", "テンプレート"],
        "en": ["prompt", "prompt design", "template"],
    },
    "rag-engineer": {
        "ja": ["RAG", "ベクトル検索", "埋め込み", "検索"],
        "en": ["rag", "vector search", "embedding", "retrieval"],
    },
    # Debug & Test
    "debugger": {
        "ja": ["デバッグ", "バグ", "エラー", "動かない", "原因"],
        "en": ["debug", "bug", "error", "not working", "cause"],
    },
    "tester": {
        "ja": ["テスト", "単体テスト", "結合テスト", "カバレッジ"],
        "en": ["test", "unit test", "integration test", "coverage"],
    },
    # Review - Implementation
    "code-reviewer": {
        "ja": ["コードレビュー", "レビュー"],
        "en": ["code review", "review code"],
    },
    "security-reviewer": {
        "ja": ["セキュリティレビュー", "脆弱性", "セキュリティチェック"],
        "en": ["security review", "vulnerability", "security check"],
    },
    "performance-reviewer": {
        "ja": ["パフォーマンスレビュー", "性能", "最適化"],
        "en": ["performance review", "performance", "optimization"],
    },
    # Review - Design
    "spec-reviewer": {
        "ja": ["仕様レビュー", "仕様確認", "設計書確認"],
        "en": ["spec review", "specification review", "design doc review"],
    },
    "architecture-reviewer": {
        "ja": ["アーキテクチャレビュー", "設計レビュー", "構造レビュー"],
        "en": ["architecture review", "design review", "structure review"],
    },
    "ux-reviewer": {
        "ja": ["UXレビュー", "アクセシビリティ", "ユーザビリティ"],
        "en": ["ux review", "accessibility", "usability"],
    },
    # Documentation
    "docs-writer": {
        "ja": ["ドキュメント", "README", "手順書", "マニュアル"],
        "en": ["documentation", "readme", "manual", "docs"],
    },
    # Specialized
    "specialized-mcp-builder": {
        "ja": ["MCP", "MCPサーバー", "ツール定義"],
        "en": ["mcp", "mcp server", "tool definition"],
    },
    "support-executive-summary-generator": {
        "ja": ["エグゼクティブサマリー", "要約", "経営報告"],
        "en": ["executive summary", "briefing", "c-suite"],
    },
    "testing-reality-checker": {
        "ja": ["リアリティチェック", "本番準備", "品質確認"],
        "en": ["reality check", "production ready", "quality gate"],
    },
}

# エージェント不一致時の researcher フォールバックトリガー
RESEARCHER_FALLBACK_TRIGGERS: dict[str, list[str]] = {
    "ja": ["PDF見て", "動画分析", "画像解析", "コードベース全体", "リポジトリ全体"],
    "en": ["analyze pdf", "analyze video", "analyze image", "entire codebase"],
}


def _project_dir_from_data(data: dict) -> str:
    """hook 入力データからプロジェクトディレクトリを取得する。"""
    return data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")


def load_config(data: dict) -> dict:
    """cli-tools.yaml を読み込む（load_package_config に委譲）。

    旧 gemini 系設定（.local.yaml 残存分）は antigravity に正規化される。
    """
    project_dir = _project_dir_from_data(data)
    config = load_package_config("agent-routing", "cli-tools.yaml", project_dir)
    return normalize_cli_tools_config(config)


def get_agent_tool(agent_name: str, config: dict) -> str:
    """config から指定エージェントの tool を取得。CLI 無効時は claude-direct にフォールバック。"""
    agents = config.get("agents", {})
    cfg = agents.get(agent_name, {})
    tool = cfg.get("tool", "claude-direct") if isinstance(cfg, dict) else "claude-direct"

    # 旧ツール値の読み替え（正規化前の config を直接渡された場合の保険）
    if tool == "gemini":
        tool = "antigravity"

    # CLI 無効時のフォールバック
    if tool == "codex" and not is_cli_enabled("codex", config):
        return "claude-direct"
    if tool == "antigravity" and not is_cli_enabled("antigravity", config):
        return "claude-direct"

    return tool


# トリガー用の単語境界正規表現のキャッシュ（UserPromptSubmit で毎回走るため）
_TRIGGER_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def _compile_trigger_pattern(trigger_lower: str) -> re.Pattern[str]:
    """ASCII トリガー用の単語境界付き正規表現を初回のみコンパイルしてキャッシュする。"""
    pattern = _TRIGGER_REGEX_CACHE.get(trigger_lower)
    if pattern is None:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(trigger_lower)}(?![A-Za-z0-9_])")
        _TRIGGER_REGEX_CACHE[trigger_lower] = pattern
    return pattern


def _trigger_matches(trigger: str, prompt_lower: str) -> bool:
    """トリガーがプロンプトにマッチするか判定する。

    ASCII のみで構成されるトリガー（"UI" や "test" など）は単語境界で厳密に
    判定し、"quick" への "UI" や "latest" への "test" のような誤検知を防ぐ。
    日本語トリガーは分かち書きが無いため、従来どおり部分一致を維持する。
    """
    trigger_lower = trigger.lower()
    if trigger_lower.isascii():
        return _compile_trigger_pattern(trigger_lower).search(prompt_lower) is not None
    return trigger_lower in prompt_lower


def detect_agent(
    prompt: str, allowed_agents: Collection[str] | None = None
) -> tuple[str | None, str]:
    """プロンプトから許可されたエージェントを検出。(agent_name, trigger) を返す。"""
    prompt_lower = prompt.lower()
    for agent, triggers in AGENT_TRIGGERS.items():
        if allowed_agents is not None and agent not in allowed_agents:
            continue
        for lang_triggers in triggers.values():
            for trigger in lang_triggers:
                if _trigger_matches(trigger, prompt_lower):
                    return agent, trigger
    return None, ""


def build_aliases(config: dict) -> dict[str, list[str]]:
    """cli-tools.yaml の agents セクションから動的 aliases を構築。"""
    codex_enabled = is_cli_enabled("codex", config)
    antigravity_enabled = is_cli_enabled("antigravity", config)

    aliases: dict[str, list[str]] = {
        "codex": ["bash:codex"] if codex_enabled else [],
        "antigravity": ["bash:agy"] if antigravity_enabled else [],
        "claude-direct": [],
        "auto": [],
    }

    # auto の bash エイリアスは有効な CLI のみ
    if codex_enabled:
        aliases["auto"].append("bash:codex")
    if antigravity_enabled:
        aliases["auto"].append("bash:agy")

    for name, cfg in config.get("agents", {}).items():
        tool = cfg.get("tool", "claude-direct") if isinstance(cfg, dict) else "claude-direct"

        # 旧ツール値の読み替え（正規化前の config を直接渡された場合の保険）
        if tool == "gemini":
            tool = "antigravity"

        # CLI 無効時は claude-direct に振り替え
        if tool == "codex" and not codex_enabled:
            tool = "claude-direct"
        elif tool == "antigravity" and not antigravity_enabled:
            tool = "claude-direct"

        task_alias = f"task:{name}"
        if tool not in aliases:
            aliases[tool] = []
        if task_alias not in aliases[tool]:
            aliases[tool].append(task_alias)
    return aliases


def build_cli_suggestion(tool: str, agent: str, trigger: str, config: dict) -> str | None:
    """CLI コマンド提案文字列を構築。CLI 無効または claude-direct の場合は None。"""
    if tool == "codex":
        if not is_cli_enabled("codex", config):
            return None
        c = config.get("codex", {})
        model = c.get("model", DEFAULT_CODEX_MODEL)
        sandbox = c.get("sandbox", {}).get("analysis", DEFAULT_CODEX_SANDBOX_ANALYSIS)
        flags = c.get("flags", DEFAULT_CODEX_FLAGS)
        return (
            f"[Codex CLI] Agent '{agent}' ('{trigger}') uses Codex:\n"
            f'`codex exec --model {model} --sandbox {sandbox} {flags} "..." < /dev/null 2>/dev/null`'
        )
    if tool in ("antigravity", "gemini"):
        if not is_cli_enabled("antigravity", config):
            return None
        a = config.get("antigravity", {})
        model = a.get("model", DEFAULT_ANTIGRAVITY_MODEL)
        flags = a.get("flags", DEFAULT_ANTIGRAVITY_FLAGS)
        parts = ["agy", '-p "..."']
        if model:
            parts.append(f"--model {model}")
        if flags:
            parts.append(flags)
        command = " ".join(parts)
        warn = ""
        allowlist = a.get("model_allowlist") or []
        if model and allowlist and model not in allowlist:
            warn = (
                f"\n[WARN] model '{model}' is not in antigravity.model_allowlist. "
                "agy silently falls back to its default model for unknown slugs."
            )
        return (
            f"[Antigravity CLI] Agent '{agent}' ('{trigger}') uses Antigravity:\n"
            f"`{command} 2>/dev/null`{warn}"
        )
    return None
