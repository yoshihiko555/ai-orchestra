#!/usr/bin/env python3
"""
UserPromptSubmit hook: Route to appropriate agent based on user intent.

Analyzes user prompts and suggests the most appropriate agent
from the orchestra agent pool, including Codex/Gemini CLI recommendations.
"""

import json
import sys

# Codex CLI triggers (deep reasoning, design, debugging)
CODEX_TRIGGERS = {
    "ja": [
        "設計相談", "どう設計", "アーキテクチャ相談",
        "なぜ動かない", "原因分析", "深く考えて",
        "どちらがいい", "比較検討", "トレードオフ",
        "リファクタリング相談", "設計レビュー",
    ],
    "en": [
        "design consultation", "how to design",
        "root cause", "analyze deeply", "think deeply",
        "which is better", "compare options", "trade-off",
        "refactoring advice", "design review",
    ],
}

# Gemini CLI triggers (research, large context, multimodal)
GEMINI_TRIGGERS = {
    "ja": [
        "調べて", "リサーチして", "調査して",
        "PDF見て", "動画分析", "画像解析",
        "コードベース全体", "リポジトリ全体",
        "最新ドキュメント", "ライブラリ調査",
    ],
    "en": [
        "research", "investigate", "look up",
        "analyze pdf", "analyze video", "analyze image",
        "entire codebase", "whole repository",
        "latest docs", "library research",
    ],
}

# Agent routing configuration
AGENT_TRIGGERS = {
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


def detect_cli_tool(prompt: str) -> tuple[str | None, str]:
    """Detect if Codex or Gemini CLI should be suggested."""
    prompt_lower = prompt.lower()

    # Check Codex triggers
    for trigger in CODEX_TRIGGERS.get("ja", []) + CODEX_TRIGGERS.get("en", []):
        if trigger in prompt_lower:
            return "codex", trigger

    # Check Gemini triggers
    for trigger in GEMINI_TRIGGERS.get("ja", []) + GEMINI_TRIGGERS.get("en", []):
        if trigger in prompt_lower:
            return "gemini", trigger

    return None, ""


def detect_agent(prompt: str) -> tuple[str | None, str]:
    """Detect which agent should handle this prompt."""
    prompt_lower = prompt.lower()

    for agent, triggers in AGENT_TRIGGERS.items():
        for lang_triggers in triggers.values():
            for trigger in lang_triggers:
                if trigger in prompt_lower:
                    return agent, trigger

    return None, ""


def main():
    try:
        data = json.load(sys.stdin)
        prompt = data.get("prompt", "")

        # Skip short prompts
        if len(prompt) < 5:
            sys.exit(0)

        messages = []

        # Check for CLI tool suggestion
        cli_tool, cli_trigger = detect_cli_tool(prompt)
        if cli_tool == "codex":
            messages.append(
                f"[Codex CLI] Detected '{cli_trigger}' - Consider Codex for deep reasoning:\n"
                "`codex exec --model gpt-5.2-codex --sandbox read-only --full-auto \"...\" 2>/dev/null`"
            )
        elif cli_tool == "gemini":
            messages.append(
                f"[Gemini CLI] Detected '{cli_trigger}' - Consider Gemini for research:\n"
                "`gemini -p \"...\" 2>/dev/null`"
            )

        # Check for agent routing
        agent, trigger = detect_agent(prompt)
        if agent:
            messages.append(
                f"[Agent Routing] Detected '{trigger}' - Consider using `{agent}` agent:\n"
                f'Task(subagent_type="{agent}", prompt="...")'
            )

        if messages:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n\n".join(messages),
                }
            }
            print(json.dumps(output))

        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
