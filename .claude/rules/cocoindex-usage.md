# cocoindex MCP サーバー

cocoindex パッケージは cocoindex-code MCP サーバーの設定を Claude Code / Codex CLI / Gemini CLI に自動プロビジョニングする。

## 仕組み

SessionStart hook が `config/cocoindex.yaml` を読み込み、以下の設定ファイルを reconcile する:

| CLI | 設定ファイル | フォーマット |
|-----|------------|-------------|
| Claude Code | `.mcp.json` | JSON (`mcpServers` キー) |
| Codex CLI | `.codex/config.toml` | TOML (`[mcp_servers.{name}]` セクション) |
| Gemini CLI | `.gemini/settings.json` | JSON (`mcpServers` キー) |

proxy モードでは、これらのエントリは **決定論的な proxy URL** を向く。

- `host/port` は `get_proxy_config()` から一意に導出する
- Claude Code / Gemini CLI は `/sse`
- Codex CLI は `/mcp`
- `proxy.enabled: true` のとき **stdio fallback は行わない**
- ただし target ごとの `force_stdio: true` は明示的 override として有効

## 設定変更

プロジェクト固有の上書きは `.claude/config/cocoindex/cocoindex.local.yaml` で行う。

### バージョン固定

```yaml
# .claude/config/cocoindex/cocoindex.local.yaml
args:
  - "--prerelease=explicit"
  - "--with"
  - "cocoindex==1.0.0a16"
  - "cocoindex-code==0.2.0"
```

### 特定 CLI を無効化

```yaml
# .claude/config/cocoindex/cocoindex.local.yaml
targets:
  codex:
    enabled: false
```

### 全体無効化

```yaml
# .claude/config/cocoindex/cocoindex.local.yaml
enabled: false
```

`enabled: false` を設定すると、各 CLI の設定ファイルから cocoindex-code のエントリが自動削除される（クリーンアップモード）。

## SQLite 競合について

cocoindex-code は内部で SQLite を使用する。複数の CLI が同時に同じ MCP サーバーインスタンスを起動すると SQLite のロック競合が発生する可能性がある。

### 現在の回避策（v1）

- 同一プロジェクトで複数 CLI を同時使用する場合は注意する
- 競合が頻発する場合は `targets` で一部 CLI を無効化する

### 解決策（v2: proxy モード）

mcp-proxy を使った HTTP 共有方式で単一プロセス化する。詳細は下記「proxy モード」を参照。

## proxy モード (v2)

### 有効化

```yaml
# .claude/config/cocoindex/cocoindex.local.yaml
proxy:
  enabled: true
```

### proxy ライフサイクル

proxy は supervisor 管理で起動し、外向き URL は固定のまま維持する。

- `SessionStart`
  - 各 CLI の設定を proxy URL に reconcile する
  - `.claude/state/cocoindex-sessions/<session_id>.json` を作成する
  - proxy が `stopped` / `failed` のときだけバックグラウンド warmup を開始する
- `UserPromptSubmit`
  - `proxy_state == ready` または `idle` になった後、その session に対して 1 回だけ reconnect を促す
- `SessionEnd`
  - session state のみ削除する
- 手動停止
  - `orchestra-manager.py proxy stop --project .`

global state は `.claude/state/cocoindex-proxy.json` に保存する。

#### supervisor と自動停止

- 外側の固定ポートは `proxy_supervisor.py` が listen する
- 実際の `mcp-proxy` は内側の一時ポートで起動し、supervisor が TCP 転送する
- `active_clients == 0` になると `idle_timeout` 秒のカウントダウンに入る
- タイムアウトまでに新しい接続が来なければ、supervisor が inner proxy と自分自身を停止する
- `proxy_state` は `starting` / `ready` / `idle` / `stopping` / `stopped` / `failed` を取る

#### なぜ current session を自動救済しないのか（実測: 2026-04-21）

2026-04-21 に Claude Code `v2.1.116` の `--print` 実行で確認した順序は次の通りだった。

```
1. MCP 設定読み込み / 接続試行
2. SessionStart hook 発火
3. InstructionsLoaded hook 発火
```

検証で判明した事実:
- `InstructionsLoaded` でも初回の MCP 接続には間に合わない
- cold start は 10 秒を超えることがある
- フック側から MCP reconnect を自動実行する手段はない

そのため、proxy mode は「**proxy-only で URL を先に固定し、proxy は裏で warmup する**」設計にしている。

#### 初回起動時

初回（proxy 未起動）のセッションでは MCP 接続が失敗しうる。

- SessionStart は stdio へ落とさず、proxy warmup を裏で始める
- proxy が `ready` または `idle` になった後、次の `UserPromptSubmit` で 1 回だけ `/mcp` reconnect を促す
- 以後の新しい session は、proxy が `ready` または `idle` なら自動接続される
