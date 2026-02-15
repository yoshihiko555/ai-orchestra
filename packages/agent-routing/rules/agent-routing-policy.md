# Agent Routing Policy

**agent-router hook がエージェントを提案した場合、サブエージェント経由で実行すること。**

## 原則

UserPromptSubmit hook の `[Agent Routing]` 提案が出た場合、オーケストレーターは以下に従う：

1. **提案されたエージェントをサブエージェントとして呼び出す**（`Task(subagent_type=...)` を使用）
2. **Codex/Gemini CLI を直接 Bash で実行しない**（サブエージェント内で実行させる）

## 理由

- メインコンテキストを保護する（CLI 出力がコンテキストを圧迫しない）
- ルーティングの一貫性を担保する（判断のブレを排除）
- サブエージェントが結果を要約して返すため、効率的

## 実行パターン

### hook が提案を出した場合（従う）

```
# hook 出力例:
# [Agent Routing] 'Python' → `backend-python-dev` (tool: codex):
# Task(subagent_type="backend-python-dev", prompt="...")

# → サブエージェント経由で実行
Task(subagent_type="backend-python-dev", prompt="Pythonで足し算ロジックを実装して...")
```

### hook が提案を出さなかった場合（自由裁量）

hook がルーティングしなかった場合は、オーケストレーターの判断で直接実行してよい。

## 例外

以下の場合のみ、直接実行を許可する：

- **明らかに1行で完結する CLI 呼び出し**（例: `codex exec ... "What is 2+2?"` のような動作確認）
- **ユーザーが明示的に直接実行を指示した場合**
