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

### 将来の解決策（v2）

mcp-proxy を使った HTTP 共有方式で単一プロセス化する予定。
