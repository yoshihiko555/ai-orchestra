# Gemini Delegation Rule

**Gemini CLI は大規模リサーチを担当する専門家。**

> **Note**: モデル名・オプションは `.claude/config/agent-routing/cli-tools.yaml` で一元管理。
> `.claude/config/agent-routing/cli-tools.local.yaml` が存在する場合はそちらの値を優先する（詳細は `config-loading.md` 参照）。
> 以下のコマンド例中の `<gemini.model>` 等のプレースホルダーは、config ファイルの値で置換して使用する。

## 判定手順（MUST）

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `gemini.enabled` を確認する
4. 対象エージェントの `agents.<name>.tool` で実行先を決定する
5. `tool == gemini` のときだけ Gemini CLI を呼び出す

## いつ Gemini を使うか

- ルーティング解決結果が `tool: gemini` のとき
- `tool: auto` で調査・外部情報取得が必要なとき

### `tool: auto` のトリガー目安

| 場面 | トリガー（日本語） | トリガー（英語） |
|------|------------------|-----------------|
| リサーチ | 「調べて」「リサーチ」「調査」 | "research", "investigate", "look up" |
| ドキュメント | 「ドキュメント」「最新」「API」 | "documentation", "latest", "API" |
| コードベース分析 | 「全体を理解」「構造」 | "entire codebase", "structure" |
| マルチモーダル | 「PDF」「動画」「画像」 | "PDF", "video", "image" |

## 呼び出し方法

> **重要: Bash サンドボックスの制約**
> Gemini CLI は認証 + macOS システム API を使用するため、Bash のサンドボックス内では動作しない場合がある。
> Gemini CLI を実行する際は **必ず `dangerouslyDisableSandbox: true`** を指定すること。

### サブエージェント経由（推奨）

大きな出力が予想される場合、コンテキスト保護のためサブエージェント経由で呼び出す：

```
Task(subagent_type="general-purpose", prompt="""
Gemini でリサーチしてください：

{リサーチ内容}

Gemini CLI コマンド（dangerouslyDisableSandbox: true で実行すること）:
gemini -m <gemini.model> -p "{質問}" 2>/dev/null

結果を .claude/docs/research/{topic}.md に保存し、
要約を返してください（5-7ポイント）。
""")
```

### 直接呼び出し（短い質問のみ）

```bash
# dangerouslyDisableSandbox: true で実行すること

# config の gemini.model を -m フラグに展開して使う

# リサーチ
gemini -m <gemini.model> -p "{質問}" 2>/dev/null

# コードベース分析
gemini -m <gemini.model> -p "{質問}" --include-directories . 2>/dev/null

# マルチモーダル（PDF/動画/音声）
gemini -m <gemini.model> -p "{抽出プロンプト}" < /path/to/file 2>/dev/null
```

## Gemini の強み

| 機能 | 説明 |
|------|------|
| 1Mトークン | コードベース全体を一度に分析可能 |
| Google検索 | 最新情報へのアクセス |
| マルチモーダル | PDF、動画、音声、画像の処理 |

## 言語プロトコル

1. Gemini への質問: **英語**
2. Gemini からの回答: **英語**
3. ユーザーへの報告: **日本語**

## 無効化

`.claude/config/agent-routing/cli-tools.yaml`（または `.local.yaml`）で `gemini.enabled: false` を設定すると、Gemini CLI の呼び出しが全て無効化される。
無効時は Gemini を使用するエージェントが自動的に `claude-direct`（Claude Code 自身の能力）にフォールバックする。

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
gemini:
  enabled: false
```

## `tool: auto` 時の使い分け目安

| タスク | 推奨 |
|--------|------|
| 設計判断 | Codex 候補 |
| デバッグ | Codex 候補 |
| コード実装 | `agents.<target>.tool` で解決 |
| ライブラリ調査 | Gemini 候補 |
| コードベース理解 | Gemini 候補 |
| ドキュメント検索 | Gemini 候補 |
| マルチモーダル | Gemini 候補 |
