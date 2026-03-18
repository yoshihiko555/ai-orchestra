# CLI Language Policy

**外部 CLI（Codex CLI / Gemini CLI）と連携するスキルで守るべき共通ルール。**

## 言語プロトコル

| 対象 | 言語 |
|------|------|
| Codex / Gemini への質問 | **英語** |
| Codex / Gemini からの回答 | **英語** |
| ユーザーへの報告 | **日本語** |

## Config-Driven ルーティング

CLI ツールの利用可否と設定は `cli-tools.yaml` で一元管理する。

### 読み込み手順

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `{tool}.enabled` を確認する（`false` なら `claude-direct` にフォールバック）
4. `agents.{name}.tool` で実行先を決定する

### ルーティング規則

| `agents.{name}.tool` | 動作 |
|----------------------|------|
| `codex` | Codex CLI を使用 |
| `gemini` | Gemini CLI を使用 |
| `claude-direct` | 外部 CLI を呼ばず Claude で処理 |
| `auto` | タスク種別に応じて選択（深い推論 → Codex、調査 → Gemini、単純作業 → Claude） |

## サンドボックス実行

外部 CLI（Codex / Gemini）は sandbox 内で直接実行する。

### 前提条件（ユーザー設定）

以下の設定が `~/.claude/settings.json` に必要:

```json
{
  "env": {
    "NODE_OPTIONS": "--use-env-proxy"
  },
  "sandbox": {
    "enableWeakerNetworkIsolation": true,
    "network": {
      "allowedDomains": ["generativelanguage.googleapis.com", "accounts.google.com"]
    },
    "filesystem": {
      "allowWrite": ["~/.gemini"]
    }
  }
}
```

### フォールバック

- sandbox 内で実行エラーが発生した場合は `claude-direct` にフォールバックする
- `dangerouslyDisableSandbox` は使用しない（サブエージェントでは承認プロンプトが自動拒否されるため）
