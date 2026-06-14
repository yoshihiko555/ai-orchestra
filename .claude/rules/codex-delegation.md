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

| 条件                                    | 動作                                             |
| --------------------------------------- | ------------------------------------------------ |
| `codex.enabled == false`                | Codex は呼び出さない（フォールバック方針に従う） |
| `agents.<name>.tool == "codex"`         | Codex CLI を使用                                 |
| `agents.<name>.tool == "claude-direct"` | 外部 CLI を呼ばず Claude で処理                  |
| `agents.<name>.tool == "antigravity"`   | Antigravity CLI（`agy`）を使用                   |
| `agents.<name>.tool == "auto"`          | 以下の `auto` ヒューリスティクスで選択           |

## `tool: auto` ヒューリスティクス

`tool: auto` のときのみ、以下を目安に選択する:

| タスク種別                                         | 推奨          |
| -------------------------------------------------- | ------------- |
| 深い推論（設計判断、デバッグ、比較検討、レビュー） | Codex         |
| 外部調査、最新ドキュメント確認                     | Antigravity   |
| 単純編集、明確な単一解、テスト/lint実行            | Claude direct |

## 呼び出し方法

> **Bash サンドボックス制約**
> Codex CLI は OAuth 認証 + macOS システム API を使用するため、sandbox 内では動作しない場合がある。
> ただし `sandbox.excludedCommands` に `codex` が設定済みなら sandbox 内でも実行可能。

### サブエージェント経由（推奨）

```
Task(subagent_type="general-purpose", prompt="""
Resolve target agent/tool from cli-tools.yaml first.

If route resolves to codex:
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "{question}" < /dev/null 2>/dev/null

Return concise summary (recommendation + rationale).
""")
```

### 直接呼び出し（短い質問）

```bash
# analysis
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "{question}" < /dev/null 2>/dev/null

# implementation
codex exec --model <codex.model> --sandbox <codex.sandbox.implementation> <codex.flags> "{task}" < /dev/null 2>/dev/null
```

## Non-Interactive 実行（MUST）

Codex CLI はサブプロセスとして実行されるため、対話的な入力を受け付けられない。
以下を必ず守ること。

### 基本ルール

1. **stdin を封じる**: 全コマンドに `< /dev/null` を追加
   - stdin が開いたままだと `codex exec` は "Reading additional input from stdin..." で入力を待ち続け、無限ハングする（特にバックグラウンド実行・サブエージェント実行時）
2. **タイムアウトを設定**: Bash の timeout パラメータに `300000`（5分）を推奨
3. **exit code で判定**: `2>/dev/null` で stderr を破棄しているため、成否は exit code で判定する
   - **出力が空（0バイト）かつプロセス継続中**: ハングの疑い

### ハング調査プロトコル

`codex exec` が長時間無出力の場合、以下の順で調査する:

1. `< /dev/null` が付いているか確認する（stdin 待ちが最頻出の原因）
2. `2>/dev/null` を外して再実行し、stderr のエラーを確認する
   - 無効なモデル名（例: アカウントで未サポート）は **400 エラーをリトライし続けて無限ハングに見える**
3. `codex.model` の値が現在のアカウントで有効か、最小コマンドで疎通確認する:
   `codex exec --sandbox read-only "Reply with OK only" < /dev/null`

## Sandbox モード

| モード            | 用途                         |
| ----------------- | ---------------------------- |
| `read-only`       | 分析、レビュー、デバッグ助言 |
| `workspace-write` | 実装、修正、リファクタリング |

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
