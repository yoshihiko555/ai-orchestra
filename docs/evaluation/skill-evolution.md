# skill-evolution 評価セット

**パッケージ**: `packages/skill-evolution`
**類型**: hook 型（主: スキルの実行範囲の記録）+ CLI ツール型（副: 改善候補の台帳・meta-harness への受け渡し）
**作成日**: 2026-07-03
**最終レビュー日**: 2026-11-08（v2（ADR-20261108-065）へ全面改訂し、人間レビューで承認。v1 の観点は欠番）。2026-07-28（v1）
**情報源**: `docs/requirements/skill-evolution.md`（v2。FT-01〜FT-22、NF-01〜NF-09、受け入れ基準）、`docs/design/skill-evolution.md`（v2）、
`docs/adr/ADR-20261108-065.md`、`docs/adr/ADR-20260728-046.md`（root への集約）。
補助参照（構成要素の列挙のみ）: `packages/skill-evolution/manifest.json`。

## 1. 責務定義

skill-evolution は、スキルの実行範囲（Skill ツール経由と `/<skill>` の直接起動）を記録し、fail-logs の失敗と教訓候補を
実行範囲に帰属させて改善候補の台帳に溜める。人が認めた候補について、proposer と別のセッションの LLM がシナリオを下書きし、
人が承認したものだけを meta-harness のスイートに入れ、評価・昇格 PR・マージまでの受け渡しを候補に紐付けて記録する。
昇格のマージ後は、マージ後の版での実行の回数と人が確定した同じ欠陥で再発を確かめる。hook は記録だけを行い、
コンテキストに何も注入せず、失敗しても Claude Code を止めない。

### Non-Goals

- 改善案の生成・評価・昇格 PR の作成（meta-harness の責務）
- 失敗の収集と、スキルに依らない教訓の昇格（fail-logs と `/lessons` の責務）
- スキルの直接編集、人の承認を経ない反映
- 成功率などの率による測定とダッシュボード
- 導入先のプロジェクトから ai-orchestra リポジトリへ候補を運ぶこと（要件 FT-19。MVP の対象外）
- 外部の確認による outcome の `success` / `failure`（要件 FT-20。MVP の対象外）

## 2. 期待する入出力・副作用

| 構成要素                                                                           | 入力                                                | 期待する出力                   | 副作用                                                                      |
| ---------------------------------------------------------------------------------- | --------------------------------------------------- | ------------------------------ | --------------------------------------------------------------------------- |
| `inject-lessons.py`（PreToolUse, matcher: Skill）                                  | hook 入力 JSON（`tool_input.skill`、`tool_use_id`） | なし（標準出力に何も出さない） | `runs-YYYYMM.jsonl` に `run.started` を 1 行追記                            |
| `capture-skill-telemetry.py`（PostToolUse, matcher: Skill）                        | hook 入力 JSON（`tool_use_id`）                     | なし                           | 同じ `tool_use_id` の `run.started` があれば `run.confirmed` を追記         |
| `record-skill-scope.py`（UserPromptExpansion / UserPromptSubmit / SubagentStart）  | hook 入力 JSON                                      | なし                           | `run.started`（直接起動）・`turn.started`・`agent.started` を追記           |
| `capture-subagent-skill.py`（SubagentStop）                                        | hook 入力 JSON（`agent_id`）                        | なし                           | `agent.stopped` を追記                                                      |
| `capture-skill-stop.py`（Stop）                                                    | hook 入力 JSON                                      | なし                           | `turn.stopped` を追記                                                       |
| `skill_candidates.py`（`list` / `show` / `runs`）                                  | サブコマンド引数                                    | 一覧・詳細（無害化した文字列） | なし（読むだけ）                                                            |
| `skill_candidates_admin.py`（`ingest` / `attribute` / … / `prune-runs`）           | サブコマンド引数、meta-harness の `--json` の出力   | 結果と理由                     | 台帳（`candidates.jsonl`・写像）・下書き・`--dest` の worktree への書き込み |
| `skill_evolution.py`（v1 のサブコマンド、`purge-v1`）                              | サブコマンド引数                                    | 非推奨の旨、削除対象の一覧     | `purge-v1 --apply` のときだけ v1 のデータを削除                             |
| `/skill-candidates`（スキル。観点は `docs/evaluation/skills/skill-candidates.md`） | 利用者の操作                                        | 一覧と確認                     | admin CLI の実行（利用者の承認の後）                                        |

## 3. 評価観点

<!-- ID はファイル内一意の連番（欠番は再利用しない）。分類（正常/異常/境界）と優先度（must/should）、
     仕様根拠（参照ドキュメント。実装しか根拠がない場合は「実装挙動」と明示）を併記する。
     1 観点 = 1 振る舞い -->

> **v1 の観点の欠番**: EV-01〜EV-22、EV-24、EV-26、EV-27、EV-30〜EV-37 は v1（ADR-20260701-028）の観点で、
> v2 で機能ごと廃止したため欠番とする（再利用しない）。EV-23 は EV-45 に置き換えた。EV-25・EV-28・EV-29 は v2 でも同じ振る舞いとして残す。

### 3.1 実行範囲の記録

- [ ] EV-38（正常 / must）: PreToolUse:Skill の入力から、`run_id`・`sid`・`prompt_id`・`skill`・`trigger: skill_tool`・`tool_use_id`・`skill_hash`・`src` を持つ `run.started` が 1 行記録される — 根拠: req FT-01、FT-02; design §4.1、§4.2
- [ ] EV-39（正常 / must）: UserPromptExpansion（`expansion_type: slash_command`）でスキルとして解決できるコマンドは、`trigger: direct`・`tool_use_id: null`（`src: absent`）の `run.started` として記録される — 根拠: req FT-01; design §4.1
- [ ] EV-40（異常 / must）: UserPromptExpansion のうち、スキルとして解決できないもの（`.claude/commands/` の旧形式のコマンド、MCP のプロンプト）は記録されない — 根拠: design §4.1
- [ ] EV-41（正常 / must）: PostToolUse:Skill は、同じ `tool_use_id` の `run.started` があるときだけ `run.confirmed` を記録し、無いときは何も記録しない — 根拠: design §4.1
- [ ] EV-42（正常 / must）: UserPromptSubmit・Stop・SubagentStart・SubagentStop で、それぞれ `turn.started`・`turn.stopped`・`agent.started`・`agent.stopped` が記録され、サブエージェントの出来事は `agent_id` を持つ — 根拠: design §4.1、§4.2
- [ ] EV-43（正常 / must）: hook の入力に無い `tool_use_id`・`agent_id`・`model` は `null` で記録され、各値の `src` に入手元（`hook_input` / `absent`）が残る。`model` は常に `null` — 根拠: req FT-02; design §4.2
- [ ] EV-44（異常 / must）: 形の合わない値（改行・制御文字を含むスキル名、規定の形でない ID）を含む出来事は記録されない — 根拠: design §4.2
- [ ] EV-45（異常 / must）: 例外・不正な JSON の入力・書き込みの失敗・ロックの待ち切れのとき、hook は終了コード 0 で標準出力に何も出さずに終わる — 根拠: req NF-02; design §4.1、§6.1
- [ ] EV-46（正常 / must）: v2 の hook はどれも追加コンテキストを返さず、lessons・metrics・pending を読みも書きもしない — 根拠: req NF-01、FT-21; design §12.1
- [ ] EV-47（正常 / must）: `skill_hash` は同じ中身のスキルのディレクトリで同じ値になり、`.` で始まる名前と `__pycache__` を含めず、ディレクトリの中の symlink をたどらない — 根拠: req FT-01; design §4.3
- [ ] EV-48（境界 / must）: `skill_hash` は、ファイル数か大きさの上限を超えるとき、スキルが見つからないとき、plugin のスキルのとき `null`（`src: unresolved`）になる — 根拠: design §4.3
- [ ] EV-49（正常 / must）: 実行範囲の記録と台帳は root worktree の `storage.logs_dir` に書かれ、linked worktree で書いた記録も root に残る。`logs_dir` の `..` によるディレクトリの外への指定は拒否される — 根拠: req NF-08; ADR-20260728-046 の「決定」3; design §6.1
- [ ] EV-25（正常 / must）: `config/skill-evolution.yaml` の `enabled: false` のとき、hook は何も記録しない。`skill-evolution.local.yaml` による上書きは `config-loading` ルールどおりベースより優先される — 根拠: design §11; `.claude/rules/config-loading.md`

### 3.2 実行範囲の組み立てと帰属

- [ ] EV-50（正常 / must）: メインのスレッドの実行範囲は、同じ `sid` の次の `turn.stopped`、または `prompt_id` が違う次の `turn.started` の早い方で区切られる。同じ `prompt_id` の `turn.started` では区切られない — 根拠: design §4.4
- [ ] EV-51（正常 / must）: サブエージェントの中の実行範囲は、同じ `agent_id` の `agent.stopped` で区切られ、メインのスレッドの区切りでは閉じない — 根拠: design §4.4
- [ ] EV-52（正常 / must）: 同じ `sid` で失敗の時刻を含む実行範囲がちょうど 1 つで、区切りが記録され、入れ子が無く、動いているサブエージェントが無いとき、失敗はその実行範囲に自動で帰属する（`attributed_by: auto`） — 根拠: req FT-05; design §5.2
- [ ] EV-53（異常 / must）: 時刻を含む実行範囲が 2 つ以上・入れ子（A → B → A の続きを含む）・動いているサブエージェントがある・区切り不明・`run.confirmed` の無い Skill ツールの実行範囲（権限で拒否されたもの）、のいずれかに当たる失敗は帰属 `unknown` になり、再発回数に数えない — 根拠: req FT-05、FT-06、NF-06; design §5.2
- [ ] EV-54（異常 / must）: 教訓候補は自動で帰属せず、帰属 `unknown` として取り込まれる — 根拠: design §5.1
- [ ] EV-55（正常 / must）: 人が実行範囲かスキルだけを指定して帰属を確定すると、その時点から観測として数えられる（`attributed_by: human`） — 根拠: req FT-04、FT-06; design §5.3
- [ ] EV-56（境界 / must）: 取り込みを 2 回しても出来事の件数は変わらず、失敗の記録と教訓候補の根拠の両方から来た同じ失敗は 1 件と数える — 根拠: req FT-07; design §6.3
- [ ] EV-57（異常 / must）: fail-logs が無い・無効、または伏せ字の処理が使えないとき、hook の記録は続き、取り込みと下書きは理由を示して止まる。1 件の壊れた記録（制御文字を含む抜粋、必須の項目が無い教訓候補など）では取り込み全体を止めず、その記録だけを飛ばして件数と理由を示す — 根拠: design §3.2、§5.1

### 3.3 台帳と状態

- [ ] EV-58（異常 / must）: 台帳の抜粋は fail-logs の `masking.py` を通してから `ledger.excerpt_max_chars` 字で切られ、伏せ字が使えないときは空になる — 根拠: req NF-05; design §6.3
- [ ] EV-59（正常 / must）: design §6.4 の表にある遷移はすべて行え、表にない遷移は書き込み時に拒否される — 根拠: req FT-08; design §6.4
- [ ] EV-60（異常 / must）: `scenario.approved` の無い候補は `scenario_approved` に、`handoff.merged` の無い候補は `promoted` に遷移できない — 根拠: req NF-03; design §6.4
- [ ] EV-61（異常 / must）: facet の composition（`facets/compositions/skills/<slug>.yaml`）の無いスキルの候補は「受け渡しの対象外」と示され、`drafting` に遷移できない — 根拠: req FT-18; design §6.5
- [ ] EV-62（境界 / must）: 状態を変える CLI は台帳のロックの中で書き込み、`ledger.lock_wait_seconds` を過ぎてもロックが取れなければ書き込まずに理由を示して終わる — 根拠: design §6.1
- [ ] EV-63（異常 / must）: 一覧と詳細の表示に出すすべての値（抜粋・仮説・`note`・スキル名・PR の URL・シナリオの本文）に無害化がかかる — 根拠: design §10.1

### 3.4 シナリオの下書きと承認

- [ ] EV-64（正常 / must）: 下書きは、道具・hook・MCP を持たない別のセッション（`claude -p`）で作られ、学習用と holdout を各 1 本以上含み、スイートの外（`drafts/<cid>/<n>/`）に置かれる — 根拠: req FT-10; design §7.1
- [ ] EV-65（境界 / must）: `draft.*` の上限（本数・1 本の大きさ・費用・時間・作り直しの回数）を超える出力は捨てられ、下書きは作られない — 根拠: req NF-09; design §7.2
- [ ] EV-66（異常 / must）: 実行される行が design §7.4 の形に合わない下書き（コマンド置換・パイプ・`;`・環境変数の展開、許可されないスクリプトやフラグ、`bypassPermissions`、`/` で始まる・`..` を含むパス、`.meta-harness/` の外への準備の書き込み（`.claude/`・`.git` など）、許可の外の `printf` の文字、`allowed_tools` の省略、既存より大きい予算・回数・時間）は不合格になる — 根拠: design §7.4
- [ ] EV-67（異常 / must）: 公開してはいけない文字列（伏せ字で変わる文字列、絶対パス、`~` で始まるパス、先頭の `/` の無いホームのパス、メールアドレス、URL、IP アドレス、ホスト名、リポジトリの参照、根拠の抜粋との正規化した 12 字以上の一致）を含む下書きは、全角などで書き換えても不合格になる — 根拠: req NF-09; design §7.3
- [ ] EV-68（異常 / must）: スキーマに合わない、`target` が違う、`id` が規定の接頭辞でない・既存と重なる、学習用の `graded` の有無が既存とそろわない、YAML のアンカー・エイリアスを使う下書きは不合格になる — 根拠: design §7.3
- [ ] EV-69（異常 / must）: 承認に添えた sha256 が承認の時点のファイルと違えば承認されず、承認の後に下書きのファイルが変わると `install` も `verify-suite` も通らない。LLM が出した元の行を「人が書いた行」として挙げても受け付けない — 根拠: req FT-11; design §7.5

### 3.5 スイートへの導入と受け渡し

- [ ] EV-70（異常 / must）: `install` は、`--dest` がこのリポジトリの worktree でないとき、書き込み先の途中に symlink があるとき、同じ名前のファイルが既にあるときに書き込まずに止まる — 根拠: design §7.6
- [ ] EV-71（正常 / must）: `verify-suite` が計算する `suite_hash` は、同じシナリオ置き場について meta-harness の `compute_suite_hash` と同じ値になる — 根拠: req FT-11; design §7.6
- [ ] EV-72（異常 / must）: `verify-suite` は、シナリオ置き場に未コミットの変更がある・既定のブランチでない・承認したファイルが HEAD に無いかハッシュが違う・承認していない `sevo-` のファイルがある・コミットが origin/main から辿れない・meta-harness の実行の backend が `docker` でない、のいずれかで止まり、`scenario.suite_verified` を記録しない — 根拠: req FT-11; design §7.6
- [ ] EV-73（異常 / must）: `own_suite_hash` が記録した `suite_hash` と違う評価は、`link` で受け渡しの記録に結び付けられない — 根拠: req FT-11、FT-12; design §8.1
- [ ] EV-74（正常 / must）: 1 つの候補から、承認したシナリオのハッシュ・スイートのハッシュ・meta-harness の評価の ID と候補の ID・昇格 PR・マージのコミットを辿れる — 根拠: req FT-12; design §6.2、§8
- [ ] EV-75（異常 / must）: `confirm-merge` は、PR が MERGED でない・URL のリポジトリが記録と違う・マージのコミットが origin/main から辿れない、のいずれかで `handoff.merged` を記録しない — 根拠: design §8.3

### 3.6 昇格後の確認

- [ ] EV-76（正常 / must）: 該当する機会は、マージの時刻より後に始まり、対象のスキルで、`skill_hash` がマージ後の版と同じで、実際に走った（直接起動か `run.confirmed` がある）実行範囲だけを数える — 根拠: req FT-13; design §9.1
- [ ] EV-77（正常 / must）: 同じ欠陥の確定が無い昇格済みの候補は「N 回の該当する機会で、確認された再発なし（帰属 unknown の証拠 M 件）」と示され、率は出ない — 根拠: req FT-13; design §9.1
- [ ] EV-78（正常 / must）: 人がマージ後の版の観測を同じ欠陥と確定すると、候補は `promoted` から `candidate` に戻る（再開） — 根拠: req FT-13; design §9.2
- [ ] EV-79（正常 / should）: 同じスキル・同じ失敗の種類で抜粋の語の重なりが多い観測が、別々のセッションか PR で 2 件以上あると、候補にするかの提案として一覧の先頭に出る — 根拠: req FT-09; design §10.1
- [ ] EV-80（正常 / should）: マージ後の版で同じスキルに帰属した観測が、候補の根拠との語の重なりの多い順に、確認を促す一覧に出る — 根拠: req FT-14; design §9.3

### 3.7 v1 の廃止と移行

- [ ] EV-81（正常 / must）: `check-trigger` は lessons の件数によらず起動を勧めず、`suggested_command` を返さず、非推奨の旨を返す — 根拠: req FT-21; design §12.1
- [ ] EV-82（異常 / must）: `purge-v1` は `--apply` が無ければ削除せずに絶対パスの一覧だけを出し、`--apply` でも worktree の中に収まらないパスと symlink は削除しない — 根拠: req FT-22; design §12.3
- [ ] EV-83（異常 / must）: hook と、`purge-v1` 以外の CLI の操作は、v1 のデータ（lessons・metrics・pending・locks）を削除しない — 根拠: req FT-22; design §12.3
- [ ] EV-84（正常 / must）: manifest は v1 の hook のエントリ（PreToolUse:Skill・PostToolUse:Skill・SubagentStop・Stop）を同じファイル名で持ち、v1 の設定のキーを `.local.yaml` に残した導入先で `orchex sync` と `orchex uninstall skill-evolution` が成功する — 根拠: req FT-22、NF-07; design §12.2

## 4. 類型別観点

<!-- docs/evaluation/README.md の類型別チェックリストを本パッケージの実情で具体化する。
     該当しない項目は「N/A: 理由」で明示する。ID は EV-NN の連番を継続する -->

### hook 型（主: スキルの実行範囲の記録）

- N/A（fail-safe）: EV-45 で代表する
- N/A（設定レイヤリング）: EV-25 で代表する
- N/A（重複の発火）: 同じ出来事が 2 回記録されても、実行範囲の組み立て（EV-50・EV-51）と帰属の件数（EV-56）は `run_id`・`tool_use_id`・証拠の鍵で 1 つにまとまるため、記録の側では重複を防がない

### CLI ツール型（副: 台帳・受け渡し）

- [ ] EV-28（正常 / must）: `skill_evolution.py` の `status` / `check-trigger` / `evaluate` / `provenance` / `lock` サブコマンドは、引数・オプション・exit code の契約を維持し、既存呼び出しとの後方互換性を壊さない（v2 では非推奨の旨を出す） — 根拠: `AGENTS.md`（変更ガードレール）; design §12.2
- [ ] EV-29（異常 / must）: CLI に不正な引数（未知の ID・壊れた JSONL・壊れた `--from-json` の入力等）を渡した場合、スタックトレースではなく分かりやすいエラーメッセージと non-zero exit code を返す — 根拠: 実装挙動
- N/A（破壊的操作の安全策）: EV-82・EV-70 で代表する

## 5. テストレビュー判断基準（パッケージ固有）

- 帰属（EV-52〜EV-55）は、自動で帰属する正常系だけでなく、`unknown` になる各条件（2 つ以上・入れ子・サブエージェント・区切り不明）を独立したテストで持つか確認する。誤帰属を防ぐ側の観点のため、正常系だけのテストでは足りない。
- `suite_hash` の一致（EV-71）は、skill-evolution の計算を meta-harness の `compute_suite_hash` と同じ入力で比べる契約テストにする。計算方法をテストにコピーしただけのテストでは、meta-harness 側の変更を検出できない。
- 下書きの形の検査（EV-66〜EV-68）は、通る例だけでなく、すり抜けを狙う入力（フラグを足す、`printf` の文字列に `$` や `\` を入れる、パスの途中に `..` を入れる、言い換えた抜粋）を含むか確認する。
- hook のテスト（EV-38〜EV-46）は、標準出力が空であることを毎回確かめているか確認する（NF-01）。
- `install`・`purge-v1`（EV-70、EV-82）は、symlink と範囲外のパスを実際に作った異常系テストを持つか確認する。
