# CLI Language Policy

**外部 CLI（Codex CLI）と連携するスキルで守るべき共通ルール。**

## 言語プロトコル

| 対象                           | 言語       |
| ------------------------------ | ---------- |
| Codex への質問   | **英語**   |
| Codex からの回答 | **英語**   |
| ユーザーへの報告               | **日本語** |

## Config-Driven ルーティング

CLI ツールの利用可否と設定は `cli-tools.yaml` で一元管理する。

### 読み込み手順

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `{tool}.enabled` を確認する（`false` なら `claude-direct` にフォールバック）
4. `agents.{name}.tool` で実行先を決定する

### ルーティング規則

| `agents.{name}.tool` | 動作                                                                              |
| -------------------- | --------------------------------------------------------------------------------- |
| `codex`              | Codex CLI を使用                                                                  |
| `claude-direct`      | 外部 CLI を呼ばず Claude で処理                                                   |
| `auto`               | タスク種別に応じて選択（深い推論 → Codex、調査 → Claude、単純作業 → Claude） |

## サンドボックス実行

Codex CLI は sandbox 内で動作しないため、base + `.local.yaml` マージ後の実効値で
`codex.requires_sandbox_disable` が `true`（既定値）の場合に限り、呼び出し側で sandbox を
無効化して実行する。`false` に上書きされた環境では sandbox 内で実行する
（安全条件の詳細は `codex-delegation.md` 参照）。
エラー時は `claude-direct` にフォールバックする。
