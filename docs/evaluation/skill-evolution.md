# skill-evolution 評価セット

**パッケージ**: `packages/skill-evolution`
**類型**: hook 型（主: オンライン収集）+ CLI ツール型（副: オフライン反復ループ）
**作成日**: 2026-07-03
**最終レビュー日**: 評価保留（2026-07-04）— パッケージ実装が未完了のため、実装完了後に改めて人間レビューを行う。それまで本評価セットの観点は暫定（ドラフト）扱いとし、テスト改修時の突合基準としては未確定とする。
**情報源**: `docs/requirements/skill-evolution.md`（FT-01〜FT-12, NF-01〜NF-05, 受け入れ基準）, `docs/design/skill-evolution.md`（アーキテクチャ・データスキーマ・停止条件）。
補助参照（構成要素の列挙のみ）: `packages/skill-evolution/manifest.json`, `packages/skill-evolution/{hooks,scripts,lib,config}` のファイル名レベル構成。

## 1. 責務定義

skill-evolution は、スキル実行のたびに二軸テレメトリ（自己申告＋機械計測）を軽量に収集し、次回発火前に学び（lessons）をコンテキストへ還元するオンライン層と、蓄積した学びをもとに固定シナリオで並列評価し 1 反復 1 テーマの改善案を生成するオフライン層の二層で、スキル自体の実行品質を継続的に改善する。改善の反映は facet 製/非 facet 製で経路を分け、いずれも人間承認を経てのみファイルへ書き込む。停止条件と 3 つのガード（発散・過学習・コスト/反復上限）により、オフライン反復ループが無人で暴走・破壊的変更をしないことを保証する。

### Non-Goals

- 成果物（コード・ドキュメント等の生成物）セルフレビューの本格自動実行（`skill-review-policy` / `/review` の責務。本パッケージは FT-12 の最小追記のみ担当）
- facet 製スキルの完全自動昇格（無人反映）。当面は人間承認ゲートを必須とする
- スキル以外（エージェント本体・オーケストレーター等）の自己改善

## 2. 期待する入出力・副作用

| 構成要素                                                                                     | 入力                                                             | 期待する出力                                               | 副作用                                                                                                                                                                      |
| -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `inject-lessons.py`（PreToolUse, matcher: Skill）                                            | Skill 発火前の hook 入力 JSON（`tool_input` にスキル名）         | 対象スキルの `lessons/<skill>.md` 内容をコンテキストへ注入 | なし（読み取りのみ）                                                                                                                                                        |
| `capture-skill-telemetry.py`（PostToolUse, matcher: Skill）                                  | Skill 完了時の hook 入力 JSON（`tool_input`/`tool_response`）    | hook 応答（完了通知）                                      | `metrics/<skill>.jsonl` へ 1 行追記、`lessons/<skill>.md` の「学び」セクションへ要約追記                                                                                    |
| `capture-subagent-skill.py`（SubagentStop）                                                  | サブエージェント終了イベント（`context: fork` 実行スキルの完了） | hook 応答（完了通知）                                      | `metrics/<skill>.jsonl` への追記（メインループ外の完了経路）                                                                                                                |
| `capture-skill-stop.py`（Stop）                                                              | セッション区切りの hook 入力 JSON（`transcript_path`）           | hook 応答（完了通知）                                      | transcript 末尾から `[skill-self-report]` を抽出し `run_id` で pending と突合、正しい duration・自己申告で `metrics/<skill>.jsonl` へ確定記録（メインループ実行の完了経路） |
| `skill_evolution.py`（CLI: `status` / `check-trigger` / `evaluate` / `provenance` / `lock`） | サブコマンド引数（スキル名等）                                   | サブコマンドごとの状態表示・判定結果                       | `lock`: `<skill>.lock` の作成/解放。`evaluate`: オフライン反復の judge 結果・改善案テーマの生成                                                                             |

## 3. 評価観点

<!-- ID はファイル内一意の連番（欠番は再利用しない）。分類（正常/異常/境界）と優先度（must/should）、
     仕様根拠（参照ドキュメント。実装しか根拠がない場合は「実装挙動」と明示）を併記する。
     1 観点 = 1 振る舞い -->

- [ ] EV-01（正常 / must）: Skill ツール呼び出し（PreToolUse/PostToolUse, matcher: `Skill`）を検出し、`tool_input.skill` またはサブエージェント経由の `tool_input.subagent_type` から対象スキル名を一意に特定できる — 根拠: req FT-01; design §3.8
- [ ] EV-02（正常 / must）: スキル実行 1 回につき `metrics/<skill>.jsonl` へ `self_report`（自己申告）と `machine`（`tool_uses`/`duration_ms`/`critical_pass_rate`）を含む 1 行が追記される — 根拠: req FT-02; design §3.4
- [ ] EV-03（異常 / must）: 自己申告ブロックが出力されない実行でも `self_report` は `null` として記録され、機械計測のみで記録処理が完結する（異常終了しない） — 根拠: req FT-02; design §3.5（欠落時のフォールバック）
- [ ] EV-04（正常 / must）: スキル実行完了時、学びの要約が `lessons/<skill>.md` の「学び」セクションに追記される — 根拠: req FT-03
- [ ] EV-05（正常 / must）: 対象スキルの発火前（PreToolUse）に、当該スキルの `lessons/<skill>.md` 内容がコンテキストへ注入される — 根拠: req FT-04; 受け入れ基準
- [ ] EV-06（境界 / should）: `lessons/<skill>.md` の「学び」セクションが規定行数を超えると超過分が `lessons/<skill>.archive.md` へ退避され、発火前注入は規定文字数を超えない（コンテキスト肥大化を起こさない） — 根拠: NF-01; design §3.4
- [ ] EV-07（正常 / must）: `[critical]` チェックリストが全項目達成のときに限り `success=true` と記録される — 根拠: req FT-05; 受け入れ基準
- [ ] EV-08（異常 / must）: `[critical]` が 1 項目でも未達の場合、定量スコア（`critical_pass_rate` 等）が高くても `success=false` と記録される — 根拠: req FT-05; 受け入れ基準
- [ ] EV-09（境界 / must）: `lessons` の蓄積件数が閾値を超えると、オフライン反復ループの起動候補と判定される（`check-trigger`） — 根拠: req FT-06; 受け入れ基準
- [ ] EV-10（正常 / must）: オフライン反復の固定シナリオ評価は新規サブエージェントとして並列ディスパッチされ、直前セッションの実行履歴が評価実行のコンテキストに混入しない（学習バイアス防止） — 根拠: req FT-07; 受け入れ基準
- [ ] EV-11（正常 / must）: 1 回のオフライン反復で生成される改善案は単一テーマに限定され、複数観点を同時に変更しない — 根拠: req FT-08; 受け入れ基準
- [ ] EV-12（正常 / must）【暴走停止・停止条件】: 連続する規定回数の反復すべてで「新規不明瞭点 0・精度変化が規定 pt 以内・ステップ数変化が規定 % 以内・所要時間変化が規定 % 以内」を満たした場合、反復ループを正常停止する — 根拠: req FT-09; design §3.3
- [ ] EV-13（異常 / must）【暴走停止・ガード1: 発散】: 規定回数連続で改善が見られない場合、ループを自動では変更せず人間へ通知して停止する（自動構造変更はしない） — 根拠: req FT-09; NF-03; design §3.3
- [ ] EV-14（異常 / must）【暴走停止・ガード2: 過学習】: holdout スコアが規定 pt を超えて下落した場合、反復ループを停止する — 根拠: req FT-09; design §3.3
- [ ] EV-15（境界 / must）【暴走停止・ガード3: コスト/反復上限】: コスト上限または最大反復回数に達した場合、反復ループを強制終了する — 根拠: req FT-09; NF-02; design §3.3
- [ ] EV-16（境界 / should）: 同一スキルに対するオフライン反復の起動は同時に 1 インスタンスのみ許可され、スキル単位ロック（`<skill>.lock`）により多重起動が防止される — 根拠: design §3.3
- [ ] EV-17（異常 / must）【人間承認ゲート】: 改善案は、人間が diff を承認するまで、いかなるファイル（`lessons`/`SKILL.md`、facet ソース、facet 生成物）にも書き込まれない — 根拠: NF-03; 受け入れ基準
- [ ] EV-18（正常 / must）: 非 facet 製スキルの改善は、人間承認後に `lessons`/`SKILL.md` への diff として反映される — 根拠: req FT-10
- [ ] EV-19（正常 / must）: facet 製スキルの改善は、人間承認後に facet ソースを更新し `facet build` を経由して配布物へ反映される（生成物ファイルを直接編集しない） — 根拠: req FT-11; 受け入れ基準
- [ ] EV-20（正常 / must）: facet 製/非 facet 製の判別は `manifest.json` の `skills` リスト照合を正本とし、`facets/` ディレクトリの有無では判定しない — 根拠: design §3.7
- [ ] EV-21（異常 / must）: 判別不能な場合、facet ソース・生成物のいずれにも書き込まず、lessons 蓄積のみに留める（安全側フォールバック） — 根拠: NF-05; design §3.7
- [ ] EV-22（正常 / should）: `.claude/rules/skill-review-policy.md`（または対応するドキュメント）に Security/Perf/Quality/a11y の 4 視点網羅オプションが追記されている — 根拠: req FT-12
- [ ] EV-31（正常 / must）: メインループで実行されたスキルは、Stop hook（`capture-skill-stop.py`）が transcript から `[skill-self-report]` ブロックを抽出し、`run_id` で pending と突合して正しい duration・自己申告で記録する — 根拠: design §3.8（完了境界）; CHANGELOG（Unreleased）
- [ ] EV-32（正常 / must）: `capture-skill-telemetry.py`（PostToolUse）は自己申告が見つからない場合に記録を確定せず、pending を温存して Stop hook に委譲する — 根拠: CHANGELOG（Unreleased）
- [ ] EV-33（境界 / must）: 自己申告が見つからない stale pending は `pending.stale_after_seconds`（既定 600 秒）経過後、機械計測のみでフォールバック記録される — 根拠: CHANGELOG（Unreleased）
- [ ] EV-34（異常 / must）: stdin 由来の `transcript_path` は realpath で許可ルート（既定 `~/.claude`）配下か検証され、範囲外や symlink による脱出は空読み扱いとなる（任意ローカルファイル読み取りを防ぐ） — 根拠: 実装挙動（PR #140 セキュリティレビュー対応）

## 4. 類型別観点

<!-- docs/evaluation/README.md の類型別チェックリストを本パッケージの実情で具体化する。
     該当しない項目は「N/A: 理由」で明示する。ID は EV-NN の連番を継続する -->

### hook 型（主: オンライン収集）

- [ ] EV-23（境界 / should）: `inject-lessons.py`（PreToolUse）・`capture-skill-telemetry.py`（PostToolUse）・`capture-subagent-skill.py`（SubagentStop）は、正常時 exit 0 で完了し、lessons 注入やテレメトリ収集に失敗してもスキル実行・セッション自体は継続する（最低限「セッションを壊さない」ことのみを本評価セットの観点とする。fail-open/fail-closed の明示的な方針は要件・設計に記載がなく確定できない。情報源に明記なし（仕様確定・文書化はパッケージ別ギャップ Issue で追跡）） — 根拠: 実装挙動
- [ ] EV-24（境界 / should）: 同一 Skill 発火イベント（同一 `run_id`）に対し hook が重複して呼ばれても、`metrics/<skill>.jsonl` への二重追記や lessons への二重注入が発生しない — 根拠: 実装挙動
- [ ] EV-25（正常 / must）: `config/skill-evolution.yaml` の `enabled: false` 時、hook はテレメトリ収集・lessons 注入を行わない（no-op）。`skill-evolution.local.yaml` による上書きは `config-loading` ルール通りベースより優先される（hook 型・CLI 型の設定レイヤリングを兼ねる） — 根拠: `.claude/rules/config-loading.md`
- [ ] EV-26（境界 / should）: `metrics`/`lessons` に記録される自己申告・機械計測データにシークレット相当の文字列が含まれる場合、マスキングされた状態で保存される — 根拠: `.claude/rules/coding-principles.md`（セキュリティ）; 実装挙動
- [ ] EV-27（境界 / should）: PreToolUse/PostToolUse の同期 hook 処理は、スキル実行のレイテンシ・コンテキストを著しく増やさない（軽量・非同期追記中心） — 根拠: NF-01

### CLI ツール型（副: オフライン反復ループ）

- [ ] EV-28（正常 / must）: `skill_evolution.py` の `status`/`check-trigger`/`evaluate`/`provenance`/`lock` サブコマンドは、引数・オプション・exit code の契約を維持し、既存呼び出しとの後方互換性を壊さない — 根拠: `CLAUDE.md`（変更ガードレール）
- [ ] EV-29（異常 / must）: CLI に不正な引数（未知のスキル名・壊れた JSONL 入力等）を渡した場合、スタックトレースではなく分かりやすいエラーメッセージと non-zero exit code を返す — 根拠: 実装挙動
- [ ] EV-30（境界 / should）: `metrics/<skill>.jsonl` の 1 行の JSON スキーマ（`ts`/`skill`/`run_id`/`self_report`/`machine`/`success`）は既存フィールドの型・意味を変えずに後方互換で拡張される — 根拠: design §3.4
- N/A（破壊的操作の安全策）: EV-17（人間承認ゲート）と同一観点。CLI 経由（`evaluate`/`lock` 等）の反映操作も承認なしにファイル書き込みを行わない前提のため、重複 ID は起こさず EV-17 で代表する
- N/A（設定レイヤリング）: EV-25 と同一観点のため重複 ID は起こさず EV-25 で代表する

## 5. テストレビュー判断基準（パッケージ固有）

- 停止条件・3 ガード（EV-12〜EV-15）は、数値しきい値そのもの（pt / % / 回数/ USD）を実装からコピーしたテストになっていないか確認する。しきい値は `config/skill-evolution.yaml` が正本のため、テストは「config 値を変えると挙動が追従するか」を検証し、固定値のハードコードに終始していないかをレビューで重点確認する。
- 人間承認ゲート（EV-17〜EV-19）は、「承認前に書き込みが起きないこと」を独立した異常系テストとして持つか確認する（正常系の承認フローのテストのみで代替していないか）。
- facet/非 facet 判別（EV-20, EV-21）は、`manifest.json` の `skills` リストを実際に差し替えた境界テスト（判別不能ケース含む）があるかを確認する。
- EV-23（fail-safe）・EV-26（マスキング）は仕様未確定/根拠が実装挙動のみのため、テストが「あるべき仕様」でなく「現状のログをそのまま期待値化」していないか、人間レビューで重点確認する。
