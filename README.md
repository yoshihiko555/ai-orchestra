# AI Orchestra

Claude Code用のマルチエージェントオーケストレーションシステム

## 構成

```
ai-orchestra/
├── agents/           # 専門エージェント定義（24種）
├── hooks/            # 自動ルーティング用Hooks
├── rules/            # コーディングルール（将来用）
└── skills/           # ワークフロースキル
    └── review/       # レビュー一括実行スキル
```

## エージェント一覧

### コア
- `planner` - タスク分解・マイルストーン策定
- `researcher` - リサーチ・ドキュメント解析

### 要件・仕様
- `requirements` - 要件抽出・NFR整理
- `spec-writer` - 仕様書生成

### 設計
- `architect` - アーキテクチャ設計・技術選定
- `api-designer` - API/IF設計
- `data-modeler` - データモデリング・スキーマ設計
- `auth-designer` - 認証認可設計

### 実装
- `frontend-dev` - React/Next.js/TypeScript
- `backend-python-dev` - Python API
- `backend-go-dev` - Go API

### AI/ML
- `ai-architect` - AIアーキテクチャ・モデル選定
- `ai-dev` - AI機能実装
- `prompt-engineer` - プロンプト設計
- `rag-engineer` - RAG実装

### テスト・デバッグ
- `debugger` - バグ原因分析
- `tester` - テスト戦略・実装

### レビュー（実装）
- `code-reviewer` - コード品質
- `security-reviewer` - セキュリティ
- `performance-reviewer` - パフォーマンス

### レビュー（設計・仕様）
- `spec-reviewer` - 仕様整合性
- `architecture-reviewer` - アーキテクチャ妥当性
- `ux-reviewer` - UX・アクセシビリティ

### ドキュメント
- `docs-writer` - 技術文書・手順書

---

## プロジェクトでオーケストラを有効化

オーケストラはプロジェクト単位で有効化します。

### 方法1: taskコマンド

```bash
cd /path/to/your/project
task -d ~/ghq/github.com/yoshihiko555/ai-orchestra init:project
```

### 方法2: 手動

プロジェクトに `.claude/settings.json` を作成：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/agent-router.py\"",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

---

## セットアップ手順

### 初回セットアップ

1. dotfilesにディレクトリ作成（初回のみ）
```bash
mkdir -p ~/ghq/github.com/yoshihiko555/dotfiles/claude/.claude/{agents,hooks,rules}
```

2. シンボリックリンク作成
```bash
# agents
cd ~/ghq/github.com/yoshihiko555/dotfiles/claude/.claude/agents
for f in ~/ghq/github.com/yoshihiko555/ai-orchestra/agents/*.md; do
  ln -sf "$f" "$(basename "$f")"
done

# hooks
cd ~/ghq/github.com/yoshihiko555/dotfiles/claude/.claude/hooks
for f in ~/ghq/github.com/yoshihiko555/ai-orchestra/hooks/*.py; do
  ln -sf "$f" "$(basename "$f")"
done
```

3. stowで展開
```bash
cd ~/ghq/github.com/yoshihiko555/dotfiles && stow -R claude
```

---

## ファイル追加時の手順

### エージェント追加

1. ai-orchestraにエージェントファイルを作成
```bash
# 例: 新しいエージェント my-agent.md を追加
vim ~/ghq/github.com/yoshihiko555/ai-orchestra/agents/my-agent.md
```

2. dotfilesにシンボリックリンクを追加
```bash
cd ~/ghq/github.com/yoshihiko555/dotfiles/claude/.claude/agents
ln -sf ~/ghq/github.com/yoshihiko555/ai-orchestra/agents/my-agent.md my-agent.md
```

3. stowで展開
```bash
cd ~/ghq/github.com/yoshihiko555/dotfiles && stow -R claude
```

### Hook追加

1. ai-orchestraにhookファイルを作成
```bash
vim ~/ghq/github.com/yoshihiko555/ai-orchestra/hooks/my-hook.py
```

2. dotfilesにシンボリックリンクを追加
```bash
cd ~/ghq/github.com/yoshihiko555/dotfiles/claude/.claude/hooks
ln -sf ~/ghq/github.com/yoshihiko555/ai-orchestra/hooks/my-hook.py my-hook.py
```

3. settings.jsonにhook設定を追加（必要な場合）

4. stowで展開
```bash
cd ~/ghq/github.com/yoshihiko555/dotfiles && stow -R claude
```

### スキル追加

1. ai-orchestraにスキルディレクトリを作成
```bash
mkdir -p ~/ghq/github.com/yoshihiko555/ai-orchestra/skills/my-skill
vim ~/ghq/github.com/yoshihiko555/ai-orchestra/skills/my-skill/SKILL.md
```

2. shared/skills/claude-onlyにシンボリックリンクを追加
```bash
cd ~/ghq/github.com/yoshihiko555/dotfiles/shared/skills/claude-only
ln -sf ~/ghq/github.com/yoshihiko555/ai-orchestra/skills/my-skill my-skill
```

3. dotfiles/claude/.claude/skillsにシンボリックリンクを追加
```bash
cd ~/ghq/github.com/yoshihiko555/dotfiles/claude/.claude/skills
ln -sf ../../../shared/skills/claude-only/my-skill my-skill
```

4. stowで展開
```bash
cd ~/ghq/github.com/yoshihiko555/dotfiles && stow -R claude
```

---

## Taskコマンド

[go-task](https://taskfile.dev/) を使用してタスクを管理しています。

### 利用可能なタスク

```bash
task              # タスク一覧を表示
task sync         # 全ファイルを同期（agents + hooks + rules + stow）
task sync:agents  # agentsのみ同期
task sync:hooks   # hooksのみ同期
task sync:rules   # rulesのみ同期
task stow         # stowを実行
task list:agents  # エージェント一覧
task list:hooks   # hook一覧
task list:skills  # スキル一覧
task add:agent -- name  # 新しいエージェントのテンプレート作成
task clean        # 壊れたシンボリックリンクを削除
```

### ファイル追加後の同期

```bash
# ai-orchestraにファイルを追加した後
cd ~/ghq/github.com/yoshihiko555/ai-orchestra
task sync
```

---

## 使い方

### エージェントの呼び出し

```
Task(subagent_type="planner", prompt="このタスクを分解して")
Task(subagent_type="code-reviewer", prompt="このコードをレビューして")
```

### レビュースキル

```
/review              # 全レビュアー並列実行
/review code         # コードレビューのみ
/review security     # セキュリティレビューのみ
/review impl         # 実装系レビュー
/review design       # 設計系レビュー
```
