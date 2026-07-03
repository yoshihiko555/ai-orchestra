# image-generation 評価セット

**パッケージ**: `packages/image-generation`
**類型**: スキル型（`/image-gen` スキル + `image-generator` エージェント）
**作成日**: 2026-07-03
**最終レビュー日**: —（未レビュー）
**情報源**: packages/image-generation/README.md, docs/adr/ADR-20260605-023.md, facets/instructions/image-gen.md（`/image-gen` スキル指示書）, packages/image-generation/agents/image-generator.md（エージェント指示書）, packages/image-generation/config/image-generation.yaml, packages/image-generation/manifest.json（補助参照: 構成要素の列挙のみ）

## 1. 責務定義

`image-generation` パッケージは、Claude Code のワークフロー内でテキストプロンプトから実際の AI 生成画像（Codex CLI 組み込み `image_gen`、ChatGPT 認証・API キー不要）を取得し、リポジトリ内の検証済みパスへ保存することを保証する。Pillow 等によるフォールバック描画や、古い生成物の誤流用を検知して明示的に失敗として扱い、成功したと偽らないことを保証する。`/image-gen` スキルと `image-generator` エージェントのみで完結する自己完結パッケージであり、`core` 以外への依存を持たない（ADR-023）。

### Non-Goals

- `OPENAI_API_KEY` を用いた OpenAI Images API の直接呼び出し（課金経路）はサポートしない（ADR-023 選択肢A、不採用）
- Adobe Firefly 等、他の画像生成手段との統合・代替提供は行わない
- 生成画像の品質評価に基づく自動リトライや複数バリエーション生成は行わない（1 リクエスト 1 回のみ）
- 通常の `codex-delegation`（`cli-tools.yaml` 経由のルーティング）には参加しない。エージェントが `codex exec` を直接呼ぶ設計を意図的に採用している（ADR-023「成果物の配置」節）

## 2. 期待する入出力・副作用

| 構成要素                                   | 入力                                                              | 期待する出力                                                                 | 副作用                                                                                         |
| ------------------------------------------ | ----------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `/image-gen` スキル Phase 1（引数解析）    | `<プロンプト>` [`--out <path>`]                                   | 絶対パスに解決された出力先、または空プロンプト時の確認要求                   | プロンプト空時のみ AskUserQuestion 発火                                                        |
| `/image-gen` スキル Phase 2（委譲）        | 解析済みプロンプト・出力先                                        | `Task(subagent_type="image-generator", ...)` への委譲                        | なし（委譲のみ、CLI ログはメインコンテキストに出さない）                                       |
| `image-generator` Configuration            | `config/image-generation.yaml`（+ `.local.yaml`）の `image_model` | 解決済みモデル名（yaml 値優先。未設定時のみ `gpt-5.5` + フォールバック明示） | なし（読み取りのみ）                                                                           |
| `image-generator` Step 1（パス解決）       | caller 指定パス or 既定 `generated-images/<slug>.png`             | リポジトリ内の絶対パス、またはパストラバーサル時の拒否                       | 出力先ディレクトリの `mkdir -p`                                                                |
| `image-generator` Step 3（Codex 呼び出し） | 検証済みプロンプト・モデル名                                      | `codex exec` の stdout（生成成否・保存パス相当のログ）                       | `~/.codex/generated_images/<session>/` への画像書き込み（Codex 側）、layer1 sandbox 一時無効化 |
| `image-generator` Step 3.5-4（回収・検証） | Step 3 の出力、フレッシュネスマーカー時刻                         | `$RESOLVED` への画像コピー成功、または honest FAILURE 報告                   | リポジトリ内ファイルへの `cp`                                                                  |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `/image-gen <プロンプト>` のみ指定時、出力先はリポジトリルート基準の `generated-images/<slug>.png` に解決される — 根拠: facets/instructions/image-gen.md（トリガーと引数 / Phase 1）
- [ ] EV-02（正常 / must）: `--out <path>` 指定時はそのパスが出力先として使われ、絶対パスに解決される — 根拠: facets/instructions/image-gen.md（Phase 1）
- [ ] EV-03（異常 / must）: プロンプトが空の場合、画像生成を実行せず AskUserQuestion で確認する — 根拠: facets/instructions/image-gen.md（Phase 1 手順3）
- [ ] EV-04（境界 / must）: 出力先パスが `git rev-parse --show-toplevel` で得たリポジトリルート外に解決される場合（パストラバーサル）、生成を実行せず拒否する — 根拠: packages/image-generation/agents/image-generator.md（Step 1）
- [ ] EV-05（異常 / must）: Codex 実行後、Step 3 で打ったフレッシュネスマーカーより新しい `call_*.png`/`ig_*.png` が存在しない場合、既存の古いファイルを流用せず FAILURE として報告する（`ls -t | head` 等での手動迂回は禁止） — 根拠: packages/image-generation/agents/image-generator.md（Step 3.5）, docs/adr/ADR-20260605-023.md（Update 2026-06-17 #2 点7）
- [ ] EV-06（異常 / must）: 出力ファイルが PNG マジックバイトを持たない、サイズが閾値未満、または Codex 出力ログにレートリミット/Pillow/PIL/ImageMagick/matplotlib 等のフォールバックマーカー（英日双方）を含む場合、成功として扱わず FAILURE 報告する — 根拠: packages/image-generation/agents/image-generator.md（Step 4）
- [ ] EV-07（異常 / must）: Codex CLI が未インストール・未認証・実行エラーの場合、画像生成は利用不可として報告し、Pillow 等での自前代替描画は行わない — 根拠: packages/image-generation/agents/image-generator.md（Fallback）, packages/image-generation/README.md（CLI連携）
- [ ] EV-08（境界 / should）: `image_model` は `config/image-generation.yaml`（+ `.local.yaml`）の値を正とし、キー自体が存在しない場合のみ `gpt-5.5` にフォールバックし、その旨を報告する — 根拠: packages/image-generation/agents/image-generator.md（Configuration）
- [ ] EV-09（境界 / must）: `image_model` に `gpt-5.3-codex` 等のコーディングモデルを使用しない（ChatGPT アカウントで image_gen 非対応） — 根拠: packages/image-generation/agents/image-generator.md（Configuration）, packages/image-generation/config/image-generation.yaml（コメント）
- [ ] EV-10（正常 / must）: 1 リクエストにつき Codex 呼び出しは 1 回のみで、レートリミット回避のためループ・リトライ・連打をしない — 根拠: packages/image-generation/agents/image-generator.md（Step 3）, docs/adr/ADR-20260605-023.md（決定4）
- [ ] EV-11（境界 / should）: Codex 呼び出しコマンドに `--enable imagegenext` / `-c sandbox_workspace_write.network_access=true` / `-c model_reasoning_effort=low` / `--skip-git-repo-check` が含まれる（欠落時は保存回帰・app-server起動失敗・自己検閲ハングが再発する） — 根拠: docs/adr/ADR-20260605-023.md（Update 2026-06-17, 2026-06-17 #2）, packages/image-generation/agents/image-generator.md（Step 3）

## 4. 類型別観点

- [ ] EV-12（対話規約 / 正常 / should）: 対話（AskUserQuestion）はプロンプトが空の場合のみ発生し、それ以外のフェーズ（出力先解決・委譲・報告）は非対話で完結する — 根拠: facets/instructions/image-gen.md（Phase 1-3）。なお本パッケージ独自の「Dialog Rules Policy」参照は `.claude/rules/` 配下に存在せず、対話条件はスキル指示書の記述のみが根拠。
- [ ] EV-13（非対話完結性 / 異常 / must）: `codex exec` 呼び出しは `< /dev/null` で stdin を封じ、Bash `timeout` を `180000` に設定し、exit code / stdout マーカーで成否を判定する（ハングしない） — 根拠: packages/image-generation/agents/image-generator.md（Step 3）
- [ ] EV-14（フォールバック / 異常 / must）: Codex 利用不能時は claude-direct 相当の代替画像生成を行わず、「利用不可」を明示報告して停止する（本パッケージには AI 画像生成の claude-direct 代替経路が存在しないため、フォールバック先は「機能停止の明示報告」であり「別ツールでの続行」ではない） — 根拠: packages/image-generation/agents/image-generator.md（Fallback）
- N/A: ルーティング尊重（`cli-tools.yaml` の enabled/tool 設定に従うか）— `image-generator` エージェントは `codex exec` を直接呼び出す設計で、`cli-tools.yaml` の `agents.<name>.tool` ルーティングを経由しない（ADR-023「成果物の配置」節で意図的に廃止）。そのため `codex.enabled: false` 等のグローバル無効化スイッチが本パッケージに伝播するかは未規定（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。
- [ ] EV-15（成果物規約 / 正常 / must）: 生成画像の保存先は既定 `generated-images/<slug>.png`（`.gitignore` 管理）、または EV-04 で検証済みの `--out` パスに限られ、それ以外の場所へは書き込まない — 根拠: packages/image-generation/README.md（出力先）, facets/instructions/image-gen.md（トリガーと引数）

## 5. テストレビュー判断基準（パッケージ固有）

このパッケージは現状テストが 1 つも存在しない。AI 生成テストをレビューする際は、検証手段の性質によって以下を区別する。

### 自動テスト可能な範囲（Codex 実 CLI 呼び出しを伴わない）

以下は Codex API・ChatGPT 認証をモック/スタブすれば決定的にユニット/結合テスト化できる。テストがこれらを謳う場合、実際に外部 `codex exec` を呼んでいないか（テストが遅い・環境依存で flaky にならないか）を確認する。

- EV-01, EV-02, EV-03: 引数解析・デフォルトパス生成・空プロンプト検知のロジック
- EV-04: パストラバーサルガード（`realpath` 判定と repo root 比較）
- EV-05: フレッシュネスガード（`find -newer` 相当のマーカー比較ロジック。マーカーより新しいファイルが「ある/ない」の2ケースを固定ディレクトリでシミュレート可能）
- EV-06: 生成物検証（PNG magic bytes 判定、サイズ閾値判定、フォールバックマーカー文字列検知は固定 stdout 文字列 + 固定バイト列で完全に決定的）
- EV-09: モデル名のブロックリスト判定（`gpt-5.3-codex` 等を弾くロジック）
- EV-13: `codex exec` コマンドライン組み立て（`< /dev/null` / `timeout` / 必須フラグの有無）を文字列組み立てレベルで検証

### 手動 E2E でしか検証できない範囲（実 Codex CLI・ChatGPT 認証・実ネットワークが必要）

- EV-07: Codex CLI 未インストール/未認証時の実際のエラー文言・終了コード（実環境の認証状態に依存し再現困難）
- EV-08: `image_model` 設定に基づく実際の image_gen 呼び出し成否（モデルの ChatGPT アカウント対応状況はテスト環境で制御不能）
- EV-10: レートリミット発生の実挙動（連打による `TooManyRequests` は実サービス依存）
- EV-11: `--enable imagegenext` 等のフラグが実際に `~/.codex/generated_images/` への保存を左右するかは、codex バイナリのバージョン挙動に依存し、モックでは検証できない（ADR-023 の複数回の「再改訂」自体が実機検証でしか判明しなかった事実）
- EV-14: Codex 実行エラー時に本当に代替描画へ倒れていないかの最終確認（生成される画像の実体を目視確認する必要がある）
- EV-15: 実際のファイルシステムへの書き込み場所（サンドボックス層1/層2の実際の権限境界での動作）

### 固有の判断基準

- レートリミット・自己検閲・保存回帰など ADR-023 に記録された「実機検証でしか判明しなかった不具合」は、モックテストで再現しようとせず、手動 E2E チェックリスト（README や PR テンプレート等）として明文化することを優先する。モックテストで「回帰しないこと」を保証したい場合は、コマンドライン組み立て（フラグの有無）レベルに留め、Codex 側の実際の保存挙動まで保証したと誤認しない。
- フレッシュネスガード（EV-05）のテストは、「マーカーより新しいファイルが存在しない」ケースを必ず独立ケースとして持つこと（存在するケースのみのテストは、EV-05 の核心である「古いファイルを誤って成功扱いしない」を担保しない）。
