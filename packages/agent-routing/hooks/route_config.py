"""cli-tools.yaml 駆動のルーティング共有モジュール。"""

from __future__ import annotations

import json
import os
import sys

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

_hook_dir = os.path.dirname(os.path.abspath(__file__))

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
}

# エージェント不一致時の Gemini フォールバックトリガー
GEMINI_FALLBACK_TRIGGERS: dict[str, list[str]] = {
    "ja": ["PDF見て", "動画分析", "画像解析", "コードベース全体", "リポジトリ全体"],
    "en": ["analyze pdf", "analyze video", "analyze image", "entire codebase"],
}


def find_config_path(data: dict) -> str:
    """cli-tools.yaml パス解決。$AI_ORCHESTRA_DIR > cwd > hook相対。"""
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
    if orchestra_dir:
        p = os.path.join(orchestra_dir, "config", "cli-tools.yaml")
        if os.path.exists(p):
            return p
    cwd = data.get("cwd", "")
    if cwd:
        p = os.path.join(cwd, ".claude", "config", "cli-tools.yaml")
        if os.path.exists(p):
            return p
    p = os.path.abspath(os.path.join(_hook_dir, "..", "..", "..", "config", "cli-tools.yaml"))
    if os.path.exists(p):
        return p
    return ""


def load_config(data: dict) -> dict:
    """cli-tools.yaml を読み込む。PyYAML 未インストール時は空 dict を返す。"""
    if not yaml:
        return {}
    path = find_config_path(data)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def get_agent_tool(agent_name: str, config: dict) -> str:
    """config から指定エージェントの tool を取得。未定義なら claude-direct。"""
    agents = config.get("agents", {})
    cfg = agents.get(agent_name, {})
    return cfg.get("tool", "claude-direct") if isinstance(cfg, dict) else "claude-direct"


def detect_agent(prompt: str) -> tuple[str | None, str]:
    """プロンプトからエージェントを検出。(agent_name, trigger) を返す。"""
    prompt_lower = prompt.lower()
    for agent, triggers in AGENT_TRIGGERS.items():
        for lang_triggers in triggers.values():
            for trigger in lang_triggers:
                if trigger in prompt_lower:
                    return agent, trigger
    return None, ""


def build_aliases(config: dict) -> dict[str, list[str]]:
    """cli-tools.yaml の agents セクションから動的 aliases を構築。"""
    aliases: dict[str, list[str]] = {
        "codex": ["bash:codex"],
        "gemini": ["bash:gemini"],
        "claude-direct": [],
        "auto": ["bash:codex", "bash:gemini"],
    }
    for name, cfg in config.get("agents", {}).items():
        tool = cfg.get("tool", "claude-direct") if isinstance(cfg, dict) else "claude-direct"
        task_alias = f"task:{name}"
        if tool not in aliases:
            aliases[tool] = []
        if task_alias not in aliases[tool]:
            aliases[tool].append(task_alias)
    return aliases


def build_cli_suggestion(tool: str, agent: str, trigger: str, config: dict) -> str | None:
    """CLI コマンド提案文字列を構築。claude-direct の場合は None。"""
    if tool == "codex":
        c = config.get("codex", {})
        model = c.get("model", "gpt-5.3-codex")
        sandbox = c.get("sandbox", {}).get("analysis", "read-only")
        flags = c.get("flags", "--full-auto")
        return (
            f"[Codex CLI] Agent '{agent}' ('{trigger}') uses Codex:\n"
            f"`codex exec --model {model} --sandbox {sandbox} {flags} \"...\" 2>/dev/null`"
        )
    if tool == "gemini":
        g = config.get("gemini", {})
        model = g.get("model", "")
        mf = f"-m {model} " if model else ""
        return (
            f"[Gemini CLI] Agent '{agent}' ('{trigger}') uses Gemini:\n"
            f"`gemini {mf}-p \"...\" 2>/dev/null`"
        )
    return None
