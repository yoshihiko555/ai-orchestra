# cocoindex MCP サーバー

cocoindex パッケージは cocoindex-code MCP サーバーの設定を Claude Code / Codex CLI / Antigravity CLI に自動プロビジョニングする。

## 仕組み

SessionStart hook が `config/cocoindex.yaml` を読み込み、以下の設定ファイルに MCP サーバー定義を書き出す:

| CLI                   | 設定ファイル            | フォーマット                             |
| --------------------- | ----------------------- | ---------------------------------------- |
| Claude Code           | `.mcp.json`             | JSON (`mcpServers` キー)                 |
| Codex CLI             | `.codex/config.toml`    | TOML (`[mcp_servers.{name}]` セクション) |
| Antigravity CLI (agy) | `.gemini/settings.json` | JSON (`mcpServers` キー)                 |

> **Note**: agy は Gemini CLI と同じ `.gemini/settings.json` を継続利用する（agy の仕様）。
> 旧キー `targets.gemini` の `enabled: false`（`.local.yaml` 残存分）は
> `targets.antigravity` に読み替えられる。

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

#### warmup は非同期（バックグラウンド）実行（仕様確定: 2026-07-15, Issue #127）

`provision-mcp-servers.py`（SessionStart）は proxy 未 ready のとき `start_proxy_background()` を
呼び出す。これはヘルパープロセスを `subprocess.Popen(..., start_new_session=True)` で起動して
即座に制御を返す **非同期処理**であり、proxy warmup の完了（`ready`/`idle` になるまで）を
同期的に待たない。そのため SessionStart hook 自体は warmup 完了を待たずに数秒以内に終了する。

- hook の返り値・終了は warmup 完了とは無関係（fire-and-forget）
- warmup の進捗は `.claude/state/` 配下の proxy state ファイルで追跡する
- 現在セッションの MCP 接続を warmup 完了後に救済する仕組みはない（上記「なぜ永続化するのか」参照）

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

## uninstall 時のクリーンアップ（仕様確定: 2026-07-15, Issue #127）

`orchestra-manager.py uninstall cocoindex` は `packages/cocoindex` 配下のパッケージファイル
（config / hooks 参照エントリ）と `orchestra.json` の `installed_packages` を対象としたパッケージ
共通のアンインストール処理のみを行う。**cocoindex 固有の以下の状態はクリーンアップの対象外**であり、
uninstall 実行後も残存する:

- 各 CLI 設定ファイルへ書き込み済みの cocoindex-code エントリ（`.mcp.json` / `.codex/config.toml` /
  `.gemini/settings.json`）
- 起動中の mcp-proxy プロセス（proxy モード使用時）
- `.claude/state/cocoindex-sessions/` 配下のセッション state ファイル

これは意図的な仕様であり、`uninstall` は「配布ファイルの削除」のみを責務とし、実行中プロセスの停止や
他 CLI 設定ファイルへの書き込み変更は行わない（`uninstall` 実行時に対象プロジェクトで Claude Code
セッションが起動していない可能性があり、hook 経由の reconcile ロジックを安全に再利用できないため）。

### 手動クリーンアップの推奨手順

cocoindex を完全に無効化したい場合は、uninstall の前後に以下を実行する:

1. `.claude/config/cocoindex/cocoindex.local.yaml` に `enabled: false` を設定し、
   一度セッションを起動して `provision-mcp-servers.py`（SessionStart hook）に
   3 CLI 設定ファイルからのエントリ削除（クリーンアップモード）を実行させる
2. proxy モードを使用していた場合は `orchestra-manager.py proxy stop --project .` で
   mcp-proxy プロセスを停止する
3. `orchestra-manager.py uninstall cocoindex --project .` でパッケージファイルを削除する
4. 必要であれば `.claude/state/cocoindex-sessions/` を手動で削除する

> **Note**: `uninstall` 自体に自動クリーンアップを統合することは将来の改善候補として残っている
> （既存の `uninstall` の挙動を変更しない、小さく安全な変更を優先する方針のため今回は見送り）。
