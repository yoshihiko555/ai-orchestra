# AI Orchestra

**マルチエージェント協調フレームワーク**

Claude Code + Codex CLI + Gemini CLI を統合したオーケストレーションシステム。

---

## このリポジトリについて

このリポジトリは AI Orchestra の設定ファイルを管理しています。

```
ai-orchestra/
├── agents/        # 25 専門エージェント定義
├── rules/         # 共通ルール（Codex/Gemini委譲、コーディング規約）
├── hooks/         # 自動ルーティングフック
├── skills/        # スキル定義（/review など）
└── templates/     # テンプレート
```

---

## セットアップ

### 1. シンボリックリンク設定

```bash
# ~/.claude/ に各ディレクトリをリンク
ln -s /path/to/ai-orchestra/agents ~/.claude/agents
ln -s /path/to/ai-orchestra/rules ~/.claude/rules
ln -s /path/to/ai-orchestra/hooks ~/.claude/hooks
ln -s /path/to/ai-orchestra/skills ~/.claude/skills
```

### 2. プロジェクトで有効化

```
/init-orchestra
```

---

## 使い方

→ `~/.claude/rules/orchestra-usage.md` を参照

または、Claude Code で以下を実行：

```
Task(subagent_type="planner", prompt="計画: {機能名}")
Task(subagent_type="frontend-dev", prompt="実装: {機能名}")
/review
```

---

## アーキテクチャ

```
Claude Code (Orchestrator)
    │
    ├── Codex CLI    # 深い推論・設計判断・デバッグ
    ├── Gemini CLI   # リサーチ・大規模分析・マルチモーダル
    │
    └── 25 Specialized Agents
        ├── Planning: planner, researcher, requirements
        ├── Design: architect, api-designer, data-modeler, auth-designer, spec-writer
        ├── Implementation: frontend-dev, backend-*-dev, ai-*, debugger, tester
        └── Review: code-reviewer, security-reviewer, performance-reviewer, ...
```

---

## 開発

このリポジトリへの変更は `~/.claude/` のグローバル設定に自動反映されます。
