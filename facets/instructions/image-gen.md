# Image Gen

**テキストプロンプトから画像を生成するスキル。Codex CLI の組み込み `image_gen`（OpenAI gpt-image、ChatGPT 認証・API キー不要）を `image-generator` サブエージェント経由で呼び出す。**

Adobe Firefly などを使うまでもない簡単な画像を、Claude Code のワークフロー内で手軽に生成するためのスキル。設計の背景は [ADR-023](../../docs/adr/ADR-20260605-023.md) を参照。

## トリガーと引数

```
/image-gen <プロンプト>               # generated-images/ に保存
/image-gen <プロンプト> --out <path>  # 出力先を指定
```

| 指定           | 解釈                                                 |
| -------------- | ---------------------------------------------------- |
| プロンプト     | 生成したい画像の説明（日本語/英語どちらでも可）      |
| `--out <path>` | 出力先パス。未指定なら `generated-images/<slug>.png` |

`<slug>` はプロンプトを短い kebab-case に要約したもの。

## ワークフロー

### Phase 1: 引数の解析

1. プロンプト本文と `--out` を分離する。
2. 出力先を決める:
   - `--out` 指定があればそれを使う。
   - なければ `generated-images/<slug>.png`（リポジトリルート基準）。
   - **絶対パス**に解決する（Codex の作業ディレクトリ差異を避けるため）。
3. プロンプトが空なら AskUserQuestion で「何の画像を生成するか」を確認する。

### Phase 2: image-generator エージェントへ委譲

`cli-tools.yaml` の `agents.image-generator.tool` を確認し、`codex`（既定）なら
`image-generator` サブエージェントに委譲する。

```
Task(subagent_type="image-generator", prompt="""
次の画像を生成してください:

プロンプト: {ユーザーのプロンプト}
出力先（絶対パス）: {解決した出力パス}

cli-tools.yaml の agents.image-generator 設定（image_model 等）に従うこと。
sandbox の扱い・出力パス検証・フォールバック検知は image-generator エージェント定義
（Sandbox Policy / Implementation Method）に従うこと（スキル側からは sandbox を指示しない）。
1 回だけ生成を試みること（連打しない）。

結果（成功/失敗・出力パス・モデル）を簡潔に返してください。
""")
```

> **Note**: 大きな出力（CLI ログ）でメインコンテキストを圧迫しないよう、必ずサブエージェント経由で実行する。

### Phase 3: 結果の確認と報告

1. サブエージェントの結果を受け取る。
2. **成功**: 出力パス・解像度・使用モデルを日本語で報告し、画像を確認するよう促す。
3. **フォールバック検知（失敗）**: AI 生成ではなく Pillow 等の代替描画が疑われる場合、
   その旨と推定原因（直近の連打によるレートリミット）を報告し、少し時間を置いての
   再実行を提案する。代替画像ファイルのパスも示し、削除可能であることを伝える。
4. **利用不可**: `codex.enabled: false` 等で Codex が使えない場合、その旨を報告する
   （Pillow 等での代替描画は行わない）。

## 注意事項

- **API キー不要**: 組み込み `image_gen` は Codex の ChatGPT 認証で動く。`OPENAI_API_KEY` は使わない。
- **モデル**: `gpt-5.5` 固定（`codex.model` の `gpt-5.3-codex` は image_gen 非対応）。
  変更は `cli-tools.yaml` の `agents.image-generator.image_model` で行う。
- **sandbox**: 画像生成コマンドのみ Claude Code 側 Bash を `dangerouslyDisableSandbox: true` で実行する
  （Codex の app-server 起動が層1 sandbox に阻害されるため）。Codex 側は通常の `workspace-write`。
- **レートリミット**: 連打で発生する。1 リクエストにつき 1 回だけ生成を試みる。
- **出力先**: デフォルト `generated-images/` は `.gitignore` 管理。成果物はユーザーが目視確認する前提。
