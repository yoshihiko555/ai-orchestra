# cocoindex MCP サーバー

cocoindex パッケージは cocoindex-code MCP サーバーの設定を Claude Code / Codex CLI / Gemini CLI に自動プロビジョニングする。

## 仕組み

SessionStart hook が `config/cocoindex.yaml` を読み込み、以下の設定ファイルに MCP サーバー定義を書き出す:

| CLI | 設定ファイル | フォーマット |
|-----|------------|-------------|
| Claude Code | `.mcp.json` | JSON (`mcpServers` キー) |
| Codex CLI | `.codex/config.toml` | TOML (`[mcp_servers.{name}]` セクション) |
| Gemini CLI | `.gemini/settings.json` | JSON (`mcpServers` キー) |

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

proxy はセッション間で**永続化**する運用を推奨する。

- `SessionStart` で `start_proxy()` が冪等に呼ばれる（起動済みならスキップ）
- `SessionEnd` では proxy を**停止しない**（次セッションで再利用）
- 手動停止: `orchestra-manager.py proxy stop --project .`

#### なぜ永続化するのか（検証結果: 2026-03-06）

Claude Code の起動シーケンスは以下の順序で行われる:

```
1. Instructions 読み込み → InstructionsLoaded hook 発火
2. .mcp.json 読み込み → MCP サーバー接続試行
3. SessionStart hook 発火
```

検証で判明した事実:
- `InstructionsLoaded` は `SessionStart` より約 580ms 先に発火する
- しかし proxy 起動には約 6 秒かかる（uvx + cocoindex のロード）
- MCP 接続は proxy ready より前に行われるため、どのフックで起動しても間に合わない
- フックから MCP リコネクトをプログラム的にトリガーする手段がない

そのため、proxy をセッション間で永続化し、次セッション起動時には既に proxy が稼働している状態にする。

#### 初回起動時

初回（proxy 未起動）のセッションでは MCP 接続が失敗する。`/mcp` でリコネクトが必要。
2 回目以降は proxy が永続化されているため自動接続される。
