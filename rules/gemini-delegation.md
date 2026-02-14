# Gemini Delegation Rule

**Gemini CLI は大規模リサーチを担当する専門家。**

> **Note**: モデル名・オプションは `.claude/config/cli-tools.yaml` で一元管理。
> 以下のコマンド例中の `<gemini.model>` 等のプレースホルダーは、config ファイルの値で置換して使用する。

## いつ Gemini を使うか

以下の場面で Gemini に相談する：

| 場面 | トリガー（日本語） | トリガー（英語） |
|------|------------------|-----------------|
| リサーチ | 「調べて」「リサーチ」「調査」 | "research", "investigate", "look up" |
| ドキュメント | 「ドキュメント」「最新」「API」 | "documentation", "latest", "API" |
| コードベース分析 | 「全体を理解」「構造」 | "entire codebase", "structure" |
| マルチモーダル | 「PDF」「動画」「画像」 | "PDF", "video", "image" |

## 呼び出し方法

### サブエージェント経由（推奨）

大きな出力が予想される場合、コンテキスト保護のためサブエージェント経由で呼び出す：

```
Task(subagent_type="general-purpose", prompt="""
Gemini でリサーチしてください：

{リサーチ内容}

Gemini CLI コマンド:
gemini -m <gemini.model> -p "{質問}" 2>/dev/null

結果を .claude/docs/research/{topic}.md に保存し、
要約を返してください（5-7ポイント）。
""")
```

### 直接呼び出し（短い質問のみ）

```bash
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

## Codex vs Gemini 使い分け

| タスク | Codex | Gemini |
|--------|-------|--------|
| 設計判断 | ✓ | |
| デバッグ | ✓ | |
| コード実装 | ✓ | |
| ライブラリ調査 | | ✓ |
| コードベース理解 | | ✓ |
| ドキュメント検索 | | ✓ |
| マルチモーダル | | ✓ |
