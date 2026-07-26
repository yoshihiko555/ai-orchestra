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
> Codex CLI（0.145 系以降）は起動時の in-process app-server 初期化が sandbox の OS 制限で
> 失敗するため、sandbox 内では動作しない。`sandbox.excludedCommands` の `codex` 除外も
> 現行の Claude Code では効かない。
>
> sandbox 無効化（`dangerouslyDisableSandbox: true`）は、以下を全て満たす場合のみ行う
> （満たさない場合は無効化せず、実行失敗時は `claude-direct` にフォールバックする）:
>
> 1. **実効値の確認**: base + `.local.yaml` マージ後の `codex.requires_sandbox_disable` が
>    `true` であること（`.local.yaml` で `false` に上書きされた環境では sandbox 内で実行する）
> 2. **内側 sandbox の検証（fail-closed）**: 実効の `codex.sandbox.*` が `read-only` /
>    `workspace-write` のいずれかであり、`codex.flags` に
>    `--dangerously-bypass-approvals-and-sandbox` 等の bypass 系フラグが含まれないこと
>    （内外両方のファイルシステム境界を同時に失わないため）
> 3. **単体コマンド限定**: `codex exec ...` 単体のコマンドにのみ適用し、他のシェルコマンドと
>    `&&` / `;` / `|` 等で連結しない（インジェクション発生時の被害拡大を防ぐため）
> 4. **prompt の shell-safe 渡し**: Issue 本文・README・ログ等の信頼できない文字列を prompt に
>    含める場合は、シェル文字列へ直接埋め込まず一時ファイルへ書き出して
>    `"$(cat "$PROMPT_FILE")"` として渡す（`$(...)` やバッククォートがホスト側シェルで
>    評価されるのを防ぐ。コマンド置換の結果は再評価されない）
>
> 条件を満たして sandbox を無効化した場合も、`--sandbox read-only` / `workspace-write` による
> codex 側の保護は維持される。

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
4. stderr に `failed to initialize in-process app-server client: Operation not permitted` が
   出る場合は sandbox 起因の起動失敗（ハングではなく即時 exit 1）。Claude Code の sandbox 内で
   実行していないか確認し、sandbox を無効化して再実行する（`sandbox.excludedCommands` の
   `codex` 除外は現行 Claude Code では効かない。上記「Bash サンドボックス制約」参照）

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
