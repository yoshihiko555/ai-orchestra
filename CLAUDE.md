# AI Orchestra

**マルチエージェント開発フレームワーク**

Claude Code をオーケストレーターとして、専門エージェントを並列実行し、開発を加速する。

---

## Architecture

```
Claude Code (Orchestrator)
    │
    ├── Planning Phase
    │   ├── planner        # タスク分解・計画
    │   ├── researcher     # 調査・情報収集
    │   └── requirements   # 要件整理
    │
    ├── Design Phase
    │   ├── architect      # システム設計
    │   ├── api-designer   # API設計
    │   ├── data-modeler   # データモデリング
    │   ├── auth-designer  # 認証設計
    │   └── spec-writer    # 仕様書作成
    │
    ├── Implementation Phase
    │   ├── frontend-dev       # フロントエンド実装
    │   ├── backend-python-dev # Python バックエンド
    │   ├── backend-go-dev     # Go バックエンド
    │   ├── ai-architect       # AI システム設計
    │   ├── ai-dev             # AI 機能実装
    │   ├── prompt-engineer    # プロンプト設計
    │   ├── rag-engineer       # RAG 実装
    │   ├── debugger           # デバッグ
    │   └── tester             # テスト作成
    │
    └── Review Phase (parallel)
        ├── code-reviewer        # コードレビュー
        ├── security-reviewer    # セキュリティレビュー
        ├── performance-reviewer # パフォーマンスレビュー
        ├── spec-reviewer        # 仕様整合性
        ├── architecture-reviewer # アーキテクチャ
        ├── ux-reviewer          # UX/アクセシビリティ
        └── docs-writer          # ドキュメント作成
```

---

## Quick Reference

### エージェント呼び出し

```
Task(subagent_type="planner", prompt="タスク分解: ユーザー認証機能")
Task(subagent_type="frontend-dev", prompt="実装: ログインフォーム")
```

### レビュー実行

```
/review              # 全レビュアー並列実行
/review code         # コードレビューのみ
/review security     # セキュリティレビューのみ
/review impl         # 実装系（code + security + performance）
/review design       # 設計系（spec + architecture）
```

---

## Context Management

メインオーケストレーターのコンテキストを節約するため、サブエージェント経由で実行する。

| 状況 | 推奨方法 |
|------|----------|
| 大きな出力が予想される | サブエージェント経由 |
| 複数の分析が必要 | 並列サブエージェント |
| 詳細なレビュー | `/review` スキル使用 |

---

## Directory Structure

```
.
├── CLAUDE.md              # このファイル
├── agents/                # エージェント定義
│   ├── planner.md
│   ├── researcher.md
│   ├── architect.md
│   ├── frontend-dev.md
│   ├── backend-python-dev.md
│   ├── code-reviewer.md
│   └── ...
├── hooks/
│   └── agent-router.py    # エージェントルーティング
├── skills/
│   └── review/            # レビュースキル
└── templates/             # テンプレート
```

---

## Agents

### Planning
| Agent | Role |
|-------|------|
| `planner` | タスク分解・マイルストーン計画 |
| `researcher` | 調査・情報収集 |
| `requirements` | 要件整理・明確化 |

### Design
| Agent | Role |
|-------|------|
| `architect` | システムアーキテクチャ設計 |
| `api-designer` | REST/GraphQL API設計 |
| `data-modeler` | データベース・スキーマ設計 |
| `auth-designer` | 認証・認可設計 |
| `spec-writer` | 仕様書作成 |

### Implementation
| Agent | Role |
|-------|------|
| `frontend-dev` | React/Vue/Next.js 実装 |
| `backend-python-dev` | Python バックエンド |
| `backend-go-dev` | Go バックエンド |
| `ai-architect` | AI/ML システム設計 |
| `ai-dev` | AI 機能実装 |
| `prompt-engineer` | プロンプト設計・最適化 |
| `rag-engineer` | RAG パイプライン実装 |
| `debugger` | バグ特定・修正 |
| `tester` | テストコード作成 |

### Review
| Agent | Role |
|-------|------|
| `code-reviewer` | 可読性・保守性・バグ検出 |
| `security-reviewer` | 脆弱性・権限・情報漏洩 |
| `performance-reviewer` | 計算量・I/O・最適化 |
| `spec-reviewer` | 仕様との整合性 |
| `architecture-reviewer` | アーキテクチャ妥当性 |
| `ux-reviewer` | UX・アクセシビリティ |
| `docs-writer` | ドキュメント作成 |

---

## Workflow Example

### 新機能開発

1. **計画フェーズ**
   ```
   Task(subagent_type="planner", prompt="計画: {機能名}")
   Task(subagent_type="researcher", prompt="調査: {関連技術}")
   ```

2. **設計フェーズ**
   ```
   Task(subagent_type="architect", prompt="設計: {機能名}")
   Task(subagent_type="api-designer", prompt="API設計: {機能名}")
   ```

3. **実装フェーズ**
   ```
   Task(subagent_type="backend-python-dev", prompt="実装: {機能名}")
   Task(subagent_type="tester", prompt="テスト作成: {機能名}")
   ```

4. **レビューフェーズ**
   ```
   /review  # 全レビュアー並列実行
   ```

---

## Language Protocol

- **思考・コード**: 英語
- **ユーザー対話**: 日本語
- **エージェント出力**: 日本語

---

## Tips

- 独立したタスクは並列実行で効率化
- レビューは `/review` スキルで一括実行
- 大きなタスクは `planner` で分解してから実行
