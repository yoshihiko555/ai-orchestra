# Codex Delegation Rule

**Codex CLI の利用可否と役割は config-driven で決定する。**

> **Note**: モデル名・オプションは `.claude/config/agent-routing/cli-tools.yaml` で一元管理。
> `.claude/config/agent-routing/cli-tools.local.yaml` が存在する場合はそちらの値を優先する（詳細は `config-loading.md` 参照）。
> 以下の `<codex.model>` 等は config ファイルから解決して使用する。

## 判定手順（MUST）

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `codex.enabled` を確認する
4. 対象エージェントの `agents.<name>.tool` で実行先を決定する
5. `tool == codex` のときだけ Codex CLI を呼び出す

## ルーティング規則

| 条件 | 動作 |
|------|------|
| `codex.enabled == false` | Codex は呼び出さない（フォールバック方針に従う） |
| `agents.<name>.tool == "codex"` | Codex CLI を使用 |
| `agents.<name>.tool == "claude-direct"` | 外部 CLI を呼ばず Claude で処理 |
| `agents.<name>.tool == "gemini"` | Gemini CLI を使用 |
| `agents.<name>.tool == "auto"` | 以下の `auto` ヒューリスティクスで選択 |

## `tool: auto` ヒューリスティクス

`tool: auto` のときのみ、以下を目安に選択する:

| タスク種別 | 推奨 |
|-----------|------|
| 深い推論（設計判断、デバッグ、比較検討、レビュー） | Codex |
| 外部調査、最新ドキュメント確認、マルチモーダル処理 | Gemini |
| 単純編集、明確な単一解、テスト/lint実行 | Claude direct |

## 呼び出し方法

> **重要: Bash サンドボックス制約**
> Codex CLI は OAuth 認証 + macOS システム API を使用するため、Bash サンドボックス内では動作しない。
> `requires_sandbox_disable: true` のときは **`dangerouslyDisableSandbox: true`** を指定すること。

### サブエージェント経由（推奨）

```
Task(subagent_type="general-purpose", prompt="""
Resolve target agent/tool from cli-tools.yaml first.

If route resolves to codex:
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "{question}" 2>/dev/null

Return concise summary (recommendation + rationale).
""")
```

### 直接呼び出し（短い質問）

```bash
# analysis
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "{question}" 2>/dev/null

# implementation
codex exec --model <codex.model> --sandbox <codex.sandbox.implementation> <codex.flags> "{task}" 2>/dev/null
```

## Sandbox モード

| モード | 用途 |
|-------|------|
| `read-only` | 分析、レビュー、デバッグ助言 |
| `workspace-write` | 実装、修正、リファクタリング |

## 言語プロトコル

1. Codex への質問: **英語**
2. Codex からの回答: **英語**
3. ユーザーへの報告: **日本語**

## 無効化

`codex.enabled: false` を設定すると Codex 呼び出しを停止できる。

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
codex:
  enabled: false
```

## 使わない場面

- `tool` 解決結果が `codex` でない場合
- 単純な typo 修正など、明らかに単一解で完結する作業
- テスト・lint 実行のみの作業
