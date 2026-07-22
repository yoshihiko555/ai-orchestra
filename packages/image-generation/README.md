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

| 層  | 対象                        | 設定                                                                       |
| --- | --------------------------- | -------------------------------------------------------------------------- |
| 層1 | Claude Code の Bash sandbox | **無効化**（`dangerouslyDisableSandbox: true`）                            |
| 層2 | Codex の `--sandbox`        | `workspace-write` + `network_access=true`（FS は repo 内に OS 強制で限定） |

確定呼び出し（`$IMG_FEATURE` はフラグ名の実行時解決結果。次項参照）:

```bash
codex exec --model gpt-5.5 \
  --sandbox workspace-write \
  -c sandbox_workspace_write.network_access=true \
  --enable "$IMG_FEATURE" \
  -c model_reasoning_effort=low \
  --skip-git-repo-check \
  "Use your built-in image_gen tool to generate <subject>. Accept whatever it returns; \
   do NOT delete files. Print the saved path. Do NOT fall back to Pillow/ImageMagick on \
   rate limit; report failure explicitly." < /dev/null
```

- **image-generation feature の有効化が必須**: フラグ名は codex のバージョンで異なる
  （0.140.x は `imagegenext`、0.144.6 以降は `imagegenext` が deprecated になり
  `image_generation` に改称）。このフラグが無いと `exec` は `image_gen` の画像を
  **ディスクに保存しない**（`saved_path` が返らず base64 のみ）。固定名を使うとどちらかの
  バージョンで壊れるため、`image-generator` エージェント（Step 3）は `codex features list`
  （利用不可時は `codex --version` の minor 番号、`<=140` なら `imagegenext`、それ以外は
  `image_generation`）で `$IMG_FEATURE` を実行時に解決してから `--enable "$IMG_FEATURE"` を渡す。
  手動検証する場合は、まず `codex features list` で自分の環境のフラグ名を確認してから
  上記コマンド例の `$IMG_FEATURE` に代入すること。
- **`network_access=true` が必須**: `image_gen` の app-server が backend 通信を行うため、
  network 遮断のままだと app-server が `Operation not permitted` で起動しない。FS は
  `workspace-write` のまま repo 内に限定され、`danger-full-access` は使わない（OS 強制の
  境界を維持）。
- **`--full-auto` は廃止**: codex 0.140.0 で deprecated（`--sandbox` に統合）。
- **保存先**: `image_gen` は `~/.codex/generated_images/<session>/` に保存する
  （`imagegenext`/`image_generation` いずれの場合もファイル名は `call_*.png`、旧 codex は
  `ig_*.png`）。エージェントは生成直前のマーカー時刻より**新しい**ファイルだけを採用して
  出力先へコピーする（古い画像を誤って成功扱いしない鮮度ガード）。対象なし時に手動で
  ディレクトリを漁って最新ファイルを掴むのは**禁止**（虚偽成功の原因）。
- モデルは既定 `gpt-5.5`（`gpt-5.3-codex` 等のコーディングモデルは image_gen 非対応）。
  `config/image-generation.yaml` の `image_model` で差し替え可能。
- レートリミット/利用上限は連打由来。**1 タスク 1 回**で回避する。
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
