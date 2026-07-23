# image-generation 評価セット

**パッケージ**: `packages/image-generation`
**類型**: スキル型（`/image-gen` スキル + `image-generator` エージェント）
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-23（EV-18〜21 にスタイル切替機構を追加）
**情報源**: packages/image-generation/README.md, docs/adr/ADR-20260605-023.md, facets/instructions/image-gen.md（`/image-gen` スキル指示書）, packages/image-generation/agents/image-generator.md（エージェント指示書）, packages/image-generation/config/image-generation.yaml, packages/image-generation/manifest.json（補助参照: 構成要素の列挙のみ）

## 1. 責務定義

`image-generation` パッケージは、Claude Code のワークフロー内でテキストプロンプトから実際の AI 生成画像（Codex CLI 組み込み `image_gen`、ChatGPT 認証・API キー不要）を取得し、リポジトリ内の検証済みパスへ保存することを保証する。Pillow 等によるフォールバック描画や、古い生成物の誤流用を検知して明示的に失敗として扱い、成功したと偽らないことを保証する。`/image-gen` スキルと `image-generator` エージェントのみで完結する自己完結パッケージであり、`core` 以外への依存を持たない（ADR-023）。

### Non-Goals

- `OPENAI_API_KEY` を用いた OpenAI Images API の直接呼び出し（課金経路）はサポートしない（ADR-023 選択肢A、不採用）
- Adobe Firefly 等、他の画像生成手段との統合・代替提供は行わない
- 生成画像の品質評価に基づく自動リトライや複数バリエーション生成は行わない（1 リクエスト 1 回のみ）
- 通常の `codex-delegation`（`cli-tools.yaml` 経由の `agents.<name>.tool` ルーティング）には参加しない。エージェントが `codex exec` を直接呼ぶ設計を意図的に採用している（ADR-023「成果物の配置」節）。ただしグローバル無効化スイッチ `codex.enabled: false` は尊重する（EV-16）

## 2. 期待する入出力・副作用

| 構成要素                                   | 入力                                                              | 期待する出力                                                                 | 副作用                                                                                         |
| ------------------------------------------ | ----------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `/image-gen` スキル Phase 1（引数解析）    | `<プロンプト>` [`--out <path>`] [`--style <name>`]                 | 絶対パスに解決された出力先、解決・検証済み style 名（または none）、または確認要求 | 空プロンプト・未知 style 時のみ AskUserQuestion 発火                                            |
| `/image-gen` スキル Phase 2（委譲）        | 解析済みプロンプト・出力先・style 名                               | `Task(subagent_type="image-generator", ...)` への委譲                        | なし（委譲のみ、CLI ログはメインコンテキストに出さない）                                       |
| `image-generator` Configuration            | `config/image-generation.yaml`（+ `.local.yaml`）の `image_model` / `output_language`、caller 指定 style 名 | 解決済みモデル名・画像内テキスト言語・style 定義（yaml 値優先。未設定時のみ各既定値 + フォールバック明示） | style 適用時のみ配布済み Markdown を読み取り                                                    |
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
- [ ] EV-11（境界 / should）: Codex 呼び出しコマンドが、実行時解決された image-generation feature を `--enable "$IMG_FEATURE"` で有効化していること（codex 0.140.0 時点の旧名は `imagegenext`。0.144.6 で `[features].image_generation` へ改称、既定 stable/true。固定名ではなく `codex features list`／`codex --version` の minor 番号で判定した名前を使う） / `-c sandbox_workspace_write.network_access=true` / `-c model_reasoning_effort=low` / `--skip-git-repo-check` が含まれる（欠落時は保存回帰・app-server起動失敗・自己検閲ハングが再発する） — 根拠: docs/adr/ADR-20260605-023.md（Update 2026-06-17, 2026-06-17 #2）, packages/image-generation/agents/image-generator.md（Step 3, Issue #291）
- [ ] EV-17（config 駆動 / 正常 / must）: `output_language` は `config/image-generation.yaml`（+ `.local.yaml`）の値を正とし、既定値 `ja` では画像内の見出し・ラベル・注釈・キャプションを日本語で描画する。技術用語・固有名詞は英語のままでもよく、ユーザープロンプトに画像内テキストの言語が明示されている場合はその指定を優先する。解決した値は言語コード形式 `^[a-z]{2}(-[A-Z]{2})?$`（例: `ja`, `en`, `en-US`）に一致しなければならず、一致しない値は invalid として `ja` にフォールバックし、その旨をレポートに記載する（config 由来の自由記述文字列をプロンプトへ混入させない defense-in-depth）。Output Format にも使用した画像内テキスト言語（fallback 時はその旨）を報告する — 根拠: packages/image-generation/config/image-generation.yaml, packages/image-generation/agents/image-generator.md（Configuration, Step 2, Output Format）
- [ ] EV-18（config 駆動 / 正常 / must）: style の解決順は明示指定 `--style <name>` > `image-generation.local.yaml` を含む config の `default_style` > style なしである。予約値 `--style none` はその 1 回だけ `default_style` を無効化し、style 定義ファイル名として扱わない。`image-generator` の Step 2 コード例は none 分岐を `if [ "$STYLE" = "none" ]` として明示し、その場合 `STYLE_TEXT`/`STYLE_BLOCK` を空文字列にして BEGIN/END ラッパーごと `FULL_PROMPT` へ何も混入させない — 根拠: facets/instructions/image-gen.md（Phase 1）, packages/image-generation/config/image-generation.yaml, packages/image-generation/agents/image-generator.md（Step 2）
- [ ] EV-19（対話規約 / 異常 / must）: 解決した style 名は画像生成への委譲前に `.claude/config/image-generation/styles/*.md` の列挙結果と照合する。未知の style は無視・無装飾 fallback・委譲をせず、利用可能な style と `none` を AskUserQuestion で提示して選び直させ、選択後に再検証する — 根拠: facets/instructions/image-gen.md（Phase 1-2）
- [ ] EV-20（配布 / 正常 / must）: style 定義の SSOT は `packages/image-generation/config/styles/<name>.md` で、manifest の config 同期により `.claude/config/image-generation/styles/<name>.md` へサブディレクトリを保持して配布される。bundled style `isometric` の内容は日本語のまま維持する。`packages/image-generation/config/styles/*.md` の全ファイルが `manifest.json` の `config` リストへ `config/styles/<name>.md` として列挙されていること（および逆方向: manifest 列挙分の実ファイル存在）は構造契約テストでドリフト検出する — 根拠: packages/image-generation/manifest.json, scripts/lib/sync_engine.py（config_target_relative_path）, packages/image-generation/config/styles/isometric.md
- [ ] EV-21（プロンプト構築 / 異常 / must）: caller から style 名が渡された場合、`image-generator` は Step 2 のコード内で `^[a-z0-9][a-z0-9-]*$` の正規表現一致と `.claude/config/image-generation/styles/<name>.md` の実在確認を機械的ゲート（heredoc 読み込み前の `grep`/`[ -f ]` チェック）として実行し、失敗時は caller bug として生成前に FAILURE を返す。存在する場合は、まず style ファイルが heredoc デリミタ `STYLE_EOF` と衝突する行を含んでいないか `grep -qx 'STYLE_EOF'` で機械検出して衝突時は FAILURE を報告し、衝突がなければ style 内容を quoted heredoc で literal に読み込み、`FULL_PROMPT` に style block として追加する。style 内容は翻訳せず、既存の shell injection guard と 1 回生成制約を維持し、Output Format に適用 style（または none）を報告する — 根拠: packages/image-generation/agents/image-generator.md（Configuration, Step 2, Output Format）

## 4. 類型別観点

- [ ] EV-12（対話規約 / 正常 / should）: 対話（AskUserQuestion）はプロンプトが空の場合、または EV-19 の未知 style を選び直す場合に限り発生し、それ以外のフェーズ（出力先解決・委譲・報告）は非対話で完結する — 根拠: facets/instructions/image-gen.md（Phase 1-3）。なお本パッケージ独自の「Dialog Rules Policy」参照は `.claude/rules/` 配下に存在せず、対話条件はスキル指示書の記述のみが根拠。
- [ ] EV-13（非対話完結性 / 異常 / must）: `codex exec` 呼び出しは `< /dev/null` で stdin を封じ、Bash `timeout` を `180000` に設定し、exit code / stdout マーカーで成否を判定する（ハングしない） — 根拠: packages/image-generation/agents/image-generator.md（Step 3）
- [ ] EV-14（フォールバック / 異常 / must）: Codex 利用不能時は claude-direct 相当の代替画像生成を行わず、「利用不可」を明示報告して停止する（本パッケージには AI 画像生成の claude-direct 代替経路が存在しないため、フォールバック先は「機能停止の明示報告」であり「別ツールでの続行」ではない） — 根拠: packages/image-generation/agents/image-generator.md（Fallback）
- [ ] EV-16（config 駆動 / 異常 / must）: ルーティング尊重 — `cli-tools.yaml`（+ `.local.yaml`）の `codex.enabled: false` のとき、`/image-gen` スキル・`image-generator` エージェントは画像生成を実行せず「利用不可」を明示報告する。`image-generator` は `codex exec` を直接呼ぶ設計（`agents.<name>.tool` ルーティングには ADR-023 で不参加）だが、Codex CLI 依存機能であるためグローバル無効化スイッチ `codex.enabled` は尊重する — 根拠: 2026-07-04 人間レビュー裁定。`packages/image-generation/scripts/check_image_gen_enabled.py` による Step 0 kill-switch チェックとして実装済み（Issue #133）
- [ ] EV-15（成果物規約 / 正常 / must）: 生成画像の保存先は既定 `generated-images/<slug>.png`（`.gitignore` 管理）、または EV-04 で検証済みの `--out` パスに限られ、それ以外の場所へは書き込まない — 根拠: packages/image-generation/README.md（出力先）, facets/instructions/image-gen.md（トリガーと引数）

## 5. テストレビュー判断基準（パッケージ固有）

このパッケージには EV-16（`codex.enabled: false` 時の kill-switch 分岐）を検証するユニットテストが追加されている。EV-01/02/04/05/06/09/13/14 等その他の自動テスト可能な観点は、指示書（`image-generator.md`）に該当手順が記述されているかを検証する契約テスト（構造テスト）でカバーする。ただし EV-07/08/10/11/12/15、および EV-14 の実際の挙動面（本当に代替描画へ倒れていないか）は、実 Codex CLI・実ネットワーク・実ファイルシステム権限に依存するため引き続き手動確認に頼る。AI 生成テストをレビューする際は、検証手段の性質によって以下を区別する。

### 自動テスト可能な範囲（Codex 実 CLI 呼び出しを伴わない）

以下は Codex API・ChatGPT 認証をモック/スタブすれば決定的にユニット/結合テスト化できる。テストがこれらを謳う場合、実際に外部 `codex exec` を呼んでいないか（テストが遅い・環境依存で flaky にならないか）を確認する。

- EV-01, EV-02, EV-03: 引数解析・デフォルトパス生成・空プロンプト検知のロジック
- EV-04: パストラバーサルガード（`realpath` 判定と repo root 比較）
- EV-05: フレッシュネスガード（`find -newer` 相当のマーカー比較ロジック。マーカーより新しいファイルが「ある/ない」の2ケースを固定ディレクトリでシミュレート可能）
- EV-06: 生成物検証（PNG magic bytes 判定、サイズ閾値判定、フォールバックマーカー文字列検知は固定 stdout 文字列 + 固定バイト列で完全に決定的）
- EV-09: モデル名のブロックリスト判定（`gpt-5.3-codex` 等を弾くロジック）
- EV-13: `codex exec` コマンドライン組み立て（`< /dev/null` / `timeout` / 必須フラグの有無）を文字列組み立てレベルで検証
- EV-16: `codex.enabled: false` を設定した config を読み込ませ、`codex exec` を呼ばず「利用不可」を報告する分岐（実 CLI を呼ばずに検証可能）。`packages/image-generation/tests/test_check_image_gen_enabled.py` でユニットテスト済み
- EV-17: base config の `output_language: ja`、`.local.yaml` 上書きの解決指示、Step 2 の `FULL_PROMPT` への反映、技術用語・固有名詞の例外、ユーザー明示指定の優先、言語コード形式検証（invalid 時 `ja` フォールバック + 報告）、Output Format での言語報告を構造契約テストで検証可能
- EV-18: `--style` / `default_style` / style なしの解決順、予約値 `none` の override、none 分岐が明示コードとして存在し `STYLE_BLOCK` が空になることをスキル/エージェント指示書の構造契約テストで検証可能
- EV-19: style 定義列挙・委譲前検証・AskUserQuestion による再選択をスキル指示書の構造契約テストで検証可能
- EV-20: manifest の nested config 宣言、package / `.claude` 双方の `isometric.md` 存在と内容一致、package styles/*.md 全件が manifest に列挙されていること（双方向ドリフト検出）を構造・配布契約テストで検証可能
- EV-21: style ファイル欠落時の生成前 FAILURE、style 名の正規表現/実在チェックを行う機械的ゲートが heredoc 読み込みより前にコードとして存在すること、heredoc デリミタ `STYLE_EOF` との衝突を `grep` で機械検出し FAILURE を報告すること、quoted heredoc による literal 読み込み、`FULL_PROMPT` への style block 反映、Output Format の style 報告をエージェント指示書の構造契約テストで検証可能

### 手動 E2E でしか検証できない範囲（実 Codex CLI・ChatGPT 認証・実ネットワークが必要）

- EV-07: Codex CLI 未インストール/未認証時の実際のエラー文言・終了コード（実環境の認証状態に依存し再現困難）
- EV-08: `image_model` 設定に基づく実際の image_gen 呼び出し成否（モデルの ChatGPT アカウント対応状況はテスト環境で制御不能）
- EV-10: レートリミット発生の実挙動（連打による `TooManyRequests` は実サービス依存）
- EV-11: `--enable image_generation`（旧 `imagegenext`）等のフラグが実際に `~/.codex/generated_images/` への保存を左右するかは、codex バイナリのバージョン挙動に依存し、モックでは検証できない（ADR-023 の複数回の「再改訂」自体が実機検証でしか判明しなかった事実）
- EV-14: Codex 実行エラー時に本当に代替描画へ倒れていないかの最終確認（生成される画像の実体を目視確認する必要がある）
- EV-15: 実際のファイルシステムへの書き込み場所（サンドボックス層1/層2の実際の権限境界での動作）

### 固有の判断基準

- レートリミット・自己検閲・保存回帰など ADR-023 に記録された「実機検証でしか判明しなかった不具合」は、モックテストで再現しようとせず、手動 E2E チェックリスト（README や PR テンプレート等）として明文化することを優先する。モックテストで「回帰しないこと」を保証したい場合は、コマンドライン組み立て（フラグの有無）レベルに留め、Codex 側の実際の保存挙動まで保証したと誤認しない。
- フレッシュネスガード（EV-05）のテストは、「マーカーより新しいファイルが存在しない」ケースを必ず独立ケースとして持つこと（存在するケースのみのテストは、EV-05 の核心である「古いファイルを誤って成功扱いしない」を担保しない）。
