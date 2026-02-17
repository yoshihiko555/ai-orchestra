# Codex Delegation Rule

**Codex CLI は深い推論を担当する専門家。**

> **Note**: モデル名・オプションは `.claude/config/cli-tools.yaml` で一元管理。
> `.claude/config/cli-tools.local.yaml` が存在する場合はそちらの値を優先する（詳細は `config-loading.md` 参照）。
> 以下のコマンド例中の `<codex.model>` 等のプレースホルダーは、config ファイルの値で置換して使用する。

## いつ Codex を使うか

以下の場面で Codex に相談する：

| 場面     | トリガー（日本語）                       | トリガー（英語）                             |
| -------- | ---------------------------------------- | -------------------------------------------- |
| 設計判断 | 「設計」「アーキテクチャ」「どう実装」   | "design", "architecture", "how to implement" |
| デバッグ | 「エラー」「バグ」「動かない」           | "error", "bug", "not working"                |
| 比較検討 | 「どちらがいい」「比較」「トレードオフ」 | "compare", "trade-off", "which is better"    |
| レビュー | 「レビュー」「見て」「チェック」         | "review", "check"                            |

## 呼び出し方法

> **重要: Bash サンドボックスの制約**
> Codex CLI は OAuth 認証 + macOS システム API を使用するため、Bash のサンドボックス内では動作しない。
> Codex CLI を実行する際は **必ず `dangerouslyDisableSandbox: true`** を指定すること。

### サブエージェント経由（推奨）

大きな出力が予想される場合、コンテキスト保護のためサブエージェント経由で呼び出す：

```
Task(subagent_type="general-purpose", prompt="""
Codex に以下を相談してください：

{質問内容}

Codex CLI コマンド（dangerouslyDisableSandbox: true で実行すること）:
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "{質問}" 2>/dev/null

結果を要約して返してください（5-7ポイント）。
""")
```

### 直接呼び出し（短い質問のみ）

```bash
# dangerouslyDisableSandbox: true で実行すること

# 分析（読み取り専用）— config の codex.model, codex.sandbox.analysis, codex.flags を展開
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "{質問}" 2>/dev/null

# 実装作業（書き込み可能）— config の codex.sandbox.implementation を使用
codex exec --model <codex.model> --sandbox <codex.sandbox.implementation> <codex.flags> "{タスク}" 2>/dev/null
```

## Sandbox モード

| モード            | 用途                             |
| ----------------- | -------------------------------- |
| `read-only`       | 設計相談、デバッグ分析、レビュー |
| `workspace-write` | 実装、修正、リファクタリング     |

## 言語プロトコル

1. Codex への質問: **英語**
2. Codex からの回答: **英語**
3. ユーザーへの報告: **日本語**

## 無効化

`cli-tools.yaml`（または `.local.yaml`）で `codex.enabled: false` を設定すると、Codex CLI の呼び出しが全て無効化される。
無効時は Codex を使用するエージェントが自動的に `claude-direct`（Claude Code 自身の能力）にフォールバックする。

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
codex:
  enabled: false
```

## 使わない場面

- 単純なファイル編集（typo修正など）
- 明確な指示に従う作業
- テスト・lint実行
