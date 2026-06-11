# image-generation パッケージ

Codex CLI の組み込み `image_gen` スキル（OpenAI gpt-image、ChatGPT 認証・API キー不要）を
Claude Code から呼び出して画像を生成する。Adobe Firefly を使うまでもない簡単な画像を
`/image-gen <プロンプト>` で手軽に作るための仕組み。

設計判断は [ADR-023](../../docs/adr/ADR-20260605-023.md) を参照。

## トリガー

```
/image-gen <プロンプト>              # generated-images/ に保存
/image-gen <プロンプト> --out <path> # 出力先を指定
```

「画像生成して」「画像を作って」等の依頼は、`image-generator` エージェントの
description に基づく Claude Code のネイティブ subagent dispatch で起動される
（独自のルーティング登録は持たない）。

## 構成

このパッケージは **自己完結** している（skill + agent + config を同梱。`core` 以外に依存しない）。

| 要素                    | 配置                                                  | 役割                              |
| ----------------------- | ----------------------------------------------------- | --------------------------------- |
| `/image-gen` スキル     | `facets/instructions/image-gen.md`                    | プロンプト → 画像のワークフロー   |
| `image-generator` agent | `packages/image-generation/agents/image-generator.md` | Codex `image_gen` 呼び出し + 検証 |
| モデル設定              | `config/image-generation.yaml`                        | `image_model`（既定 `gpt-5.5`）   |

> **Note**: `cli-tools.yaml` への登録や `route_config.py` の AGENT_TRIGGERS は
> 持たない。エージェントは `codex exec` を直接呼ぶため通常の codex-delegation
> 経路に乗らず、ルーティング登録は冗長だったため廃止した。

## 呼び出しの仕組み（要点）

Codex の組み込み `image_gen` は ChatGPT 認証で動くため **`OPENAI_API_KEY` は不要**。
非対話 `codex exec` から呼ぶには **sandbox の二層構造**を解く必要がある。

| 層  | 対象                        | 設定                                            |
| --- | --------------------------- | ----------------------------------------------- |
| 層1 | Claude Code の Bash sandbox | **無効化**（`dangerouslyDisableSandbox: true`） |
| 層2 | Codex の `--sandbox`        | 通常の `workspace-write`（危険フラグ不要）      |

確定呼び出し:

```bash
codex exec --model gpt-5.5 --sandbox workspace-write --skip-git-repo-check --full-auto \
  "Generate <subject>. Save the file to <abs-path>. Use your built-in image generation tool. \
   Do NOT fall back to Pillow/ImageMagick on rate limit; report failure explicitly." < /dev/null
```

- モデルは既定 `gpt-5.5`（`gpt-5.3-codex` 等のコーディングモデルは image_gen 非対応）。
  `config/image-generation.yaml` の `image_model` で差し替え可能。
- レートリミットは連打由来。**1 タスク 1 回**で回避する。
- レートリミット時に Codex が Pillow で描く代替画像（非 AI）は**検知して失敗扱い**にする。

## 出力先

デフォルトは `generated-images/`（リポジトリ直下、`.gitignore` 管理）。
`--out <path>` で任意パスを指定できる。

## CLI 連携

エージェントは `codex exec` を直接呼ぶ:

- Codex CLI がインストール・認証済みなら有効
- 未インストール/未認証/実行エラー時は、本物の AI 画像生成は利用不可（その旨を報告）

## 依存

- `core`（hook 共通基盤・config ローダ）
