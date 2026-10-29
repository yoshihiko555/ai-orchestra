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
    TASK_NOTIFICATION_TAG,
    get_agent_tool,
    has_project_config,
    is_cli_enabled,
    is_harness_prompt,
    is_task_notification,
    load_cli_tools_config,
    load_package_config,
    resolve_agent_routing,
    resolve_agent_sandbox,
)

# エージェントルーティング設定（catch-all の general-purpose を除く 29 エージェント分）
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
    "adversarial-reviewer": {
        "ja": ["敵対的検証", "敵対的レビュー", "堅牢性レビュー", "壊れる入力"],
        "en": ["adversarial review", "adversarial verification", "robustness review"],
    },
    "code-reviewer": {
        "ja": ["コードレビュー", "レビュー"],
        "en": ["code review", "review code"],
    },
    "finding-verifier": {
        "ja": ["指摘検証", "指摘の反証", "レビュー指摘の検証", "ファインディング検証"],
        "en": ["finding verification", "verify findings", "refute finding"],
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
    # agency-agents（先に定義したエージェントが優先されるため末尾に置く）
    "engineering-privacy-engineer": {
        "ja": ["個人情報", "プライバシー", "データ最小化"],
        "en": ["privacy", "pii", "gdpr"],
    },
    "engineering-identity-access-engineer": {
        "ja": ["シングルサインオン", "ID連携", "SCIM"],
        "en": ["sso", "oidc", "saml", "scim"],
    },
    "engineering-payments-billing-engineer": {
        "ja": ["決済", "課金", "請求処理"],
        "en": ["payment", "billing", "stripe"],
    },
    "engineering-database-reliability-engineer": {
        "ja": ["レプリケーション", "フェイルオーバー", "ポイントインタイムリカバリ"],
        "en": ["replication", "failover", "point-in-time recovery"],
    },
    "engineering-minimal-change-engineer": {
        "ja": ["最小差分", "最小限の修正", "最小変更"],
        "en": ["minimal change", "minimal diff", "smallest fix"],
    },
}

# エージェント不一致時の researcher フォールバックトリガー
RESEARCHER_FALLBACK_TRIGGERS: dict[str, list[str]] = {
    "ja": ["PDF見て", "動画分析", "画像解析", "コードベース全体", "リポジトリ全体"],
    "en": ["analyze pdf", "analyze video", "analyze image", "entire codebase"],
}

# 依頼の形（ADR-20261029-062）。キーワード判定はこの形を含む節だけでトリガーを照合する
_REQUEST_VERBS_EN = (
    r"implement|investigate|research|debug|review|write|design|compare|fix|run|add|create|"
    r"build|plan|refactor|optimize|test|document|analyze|update"
)
REQUEST_PATTERN = re.compile(
    r"(?:して|作って|書いて|直して|調べて|深掘りして|見直して|比べて|"
    r"決めて|確認して|探して|挙げて|考えて|分解して|見て)"
    r"(?:ください|ほしい|欲しい|いただきたい|もらいたい|もらえますか|"
    r"いただけますか|いきましょう|[。.!！?？\n]|$)"
    r"|(?:調査|実装|設計|比較|検討|作成|修正|レビュー)(?:を)?したい"
    r"|比較(?:も|が)(?:ほしい|欲しい)"
    r"|(?:^\s*|\bplease\s+|\b(?:can|could|would)\s+you\s+|"
    r"\bI\s+(?:want|need)\s+to\s+)"
    rf"(?:{_REQUEST_VERBS_EN})\b",
    re.I,
)
# 依頼の形でも、今すぐの依頼ではない節（完了・先送り・否定）
NONCURRENT_PATTERN = re.compile(
    r"後ほど|(?<![一-鿿])後で|そのうち|済み|してもらった|しておきました|"
    r"してほしいわけ.{0,5}ない|しなくて(?:も)?いい|"
    r"資料.{0,12}ありますか|未実装かな|なかったっけ|"
    r"\b(?:later|already)\b|"
    rf"\b(?:do not|don.t)\s+(?:{_REQUEST_VERBS_EN})\b",
    re.I,
)
_FENCED_CODE_RE = re.compile(r"(?ms)^[ \t]*(\x60{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)")
_PASTED_OPEN_TAG_RE = re.compile(r"<pasted_content\b[^>]{0,256}>", re.I)
_PASTED_CLOSE_TAG_RE = re.compile(r"</pasted_content[^>]{0,256}>", re.I)
_PASTED_TAG_RE = re.compile(r"</?pasted_content[^>]{0,256}>")
# 行頭の空白・タブの後の `>`。改行はまたがない（空行が続く入力で二乗時間にならないため）
_QUOTED_LINE_RE = re.compile(r"(?m)^[ \t]*>.*$")
_URL_RE = re.compile(r"https?://\S+")
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?\n]|\.(?=\s)|(?=後ほど|(?<![一-鿿])後で|そのうち)")


def _remove_pasted_blocks(prompt: str) -> str:
    """閉じタグがない場合は再探索せず、残りの入力をそのまま残す。"""
    parts: list[str] = []
    cursor = 0
    while opening := _PASTED_OPEN_TAG_RE.search(prompt, cursor):
        closing = _PASTED_CLOSE_TAG_RE.search(prompt, opening.end())
        if closing is None:
            break
        parts.append(prompt[cursor : opening.start()])
        cursor = closing.end()
    parts.append(prompt[cursor:])
    return "".join(parts)


def _strip_quoted_text(prompt: str) -> str:
    """コードブロック・引用行・URL と、依頼が外側にあるときの貼り付けを取り除く。"""
    prompt = _FENCED_CODE_RE.sub("", prompt)
    outer = _remove_pasted_blocks(prompt)
    if REQUEST_PATTERN.search(outer):
        prompt = outer
    else:
        # 貼り付けだけの入力は、貼り付けた本文が依頼そのものであることがある
        prompt = _PASTED_TAG_RE.sub("", prompt)
    prompt = _QUOTED_LINE_RE.sub("", prompt)
    return _URL_RE.sub("", prompt)


def _routing_request_clauses(prompt: str) -> list[str]:
    """入力から、今すぐの依頼の形をした節だけを返す。

    `detect_agent` 自体は節に分けない。loop-harness は依頼の形をしていない Issue の
    タイトルとラベルを渡すため、ユーザー入力を扱う呼び出し側がこの節を渡す。
    """
    clauses = _CLAUSE_SPLIT_RE.split(_strip_quoted_text(prompt))
    return [
        clause
        for clause in clauses
        if REQUEST_PATTERN.search(clause) and not NONCURRENT_PATTERN.search(clause)
    ]


def _project_dir_from_data(data: dict) -> str:
    """hook 入力データからプロジェクトディレクトリを取得する。"""
    return data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")


def load_config(data: dict) -> dict:
    """cli-tools.yaml を読み込む（load_cli_tools_config に委譲）。

    旧 gemini 系設定（.local.yaml 残存分）は base/local レイヤーごとに
    antigravity へ正規化されてからマージされる（Issue #125）。
    """
    project_dir = _project_dir_from_data(data)
    return load_cli_tools_config(project_dir)


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
        flags_part = f"{flags} " if flags else ""
        return (
            f"[Codex CLI] Agent '{agent}' ('{trigger}') uses Codex:\n"
            f'`codex exec --model {model} --sandbox {sandbox} {flags_part}"..." < /dev/null 2>/dev/null`'
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
