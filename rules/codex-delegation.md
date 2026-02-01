# Codex Delegation Rule

**Codex CLI は深い推論を担当する専門家。**

## いつ Codex を使うか

以下の場面で Codex に相談する：

| 場面 | トリガー（日本語） | トリガー（英語） |
|------|------------------|-----------------|
| 設計判断 | 「設計」「アーキテクチャ」「どう実装」 | "design", "architecture", "how to implement" |
| デバッグ | 「エラー」「バグ」「動かない」 | "error", "bug", "not working" |
| 比較検討 | 「どちらがいい」「比較」「トレードオフ」 | "compare", "trade-off", "which is better" |
| レビュー | 「レビュー」「見て」「チェック」 | "review", "check" |

## 呼び出し方法

### サブエージェント経由（推奨）

大きな出力が予想される場合、コンテキスト保護のためサブエージェント経由で呼び出す：

```
Task(subagent_type="general-purpose", prompt="""
Codex に以下を相談してください：

{質問内容}

Codex CLI コマンド:
codex exec --model gpt-5.2-codex --sandbox read-only --full-auto "{質問}" 2>/dev/null

結果を要約して返してください（5-7ポイント）。
""")
```

### 直接呼び出し（短い質問のみ）

```bash
# 分析（読み取り専用）
codex exec --model gpt-5.2-codex --sandbox read-only --full-auto "{質問}" 2>/dev/null

# 実装作業（書き込み可能）
codex exec --model gpt-5.2-codex --sandbox workspace-write --full-auto "{タスク}" 2>/dev/null
```

## Sandbox モード

| モード | 用途 |
|--------|------|
| `read-only` | 設計相談、デバッグ分析、レビュー |
| `workspace-write` | 実装、修正、リファクタリング |

## 言語プロトコル

1. Codex への質問: **英語**
2. Codex からの回答: **英語**
3. ユーザーへの報告: **日本語**

## 使わない場面

- 単純なファイル編集（typo修正など）
- 明確な指示に従う作業
- テスト・lint実行
