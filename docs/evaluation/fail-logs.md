# fail-logs 評価セット

**パッケージ**: `packages/fail-logs`
**類型**: hook 型（副類型: CLI ツール型。`scripts/lessons_cli.py` / `scripts/lessons_admin.py`）
**作成日**: 2026-07-03
**最終レビュー日**: 2026-11-03（教訓化と昇格に合わせて EV-22〜EV-88 を追加し、EV-04・EV-11・EV-12・EV-16・EV-17 を改訂。人間レビューで承認）。2026-07-28（価値フロー仕様確定・EV-17〜21 新設。裁定は docs/design/fail-logs.md / ADR-20260728-046 を正本とする）
**情報源**: docs/requirements/fail-logs.md（主、2026-11-03 追加分の FR / NF）, docs/design/fail-logs.md（主、価値フロー設計 §1〜§7 と 2026-11-03 追加の §3(f)〜(i)・§4 の 5〜8・§5.1・§7〜§12）, ADR-20260728-046（蓄積型ログの root worktree 解決と配置規約）, ADR-20261103-063（教訓化と昇格）, packages/fail-logs/README.md（主）。packages/fail-logs/manifest.json・hooks のファイル名/docstring 冒頭は構成要素の列挙にのみ使用し、期待値の導出には用いていない（README に記述がない振る舞いは「根拠: 実装挙動」と明示）。

## 1. 責務定義

fail-logs は AI エージェント（Claude Code 等）のツール実行失敗を検知する hook（`capture-failures.py`。PostToolUse / PostToolUseFailure / PermissionDenied）と、記録済みの失敗をセッション開始時にサマリーとしてコンテキストへ注入する SessionStart hook（`inject-failure-summary.py`）からなる、失敗知識蓄積の基盤である。失敗のみを記録し（成功は対象外、ノイズ抑制）、`core`（`hook_common`, `failure_detector`）にのみ依存する。記録処理自体の失敗が、記録対象であるツール実行やセッション開始そのものを妨げないことを保証する。

2026-11-03 から、貯めた失敗から教訓を昇格させる段を持つ。セッション終了時に合図のあったセッションから教訓候補を作り（教訓化。既定で無効）、候補を root worktree の台帳に蓄積・合流させ、基準を超えた候補をセッション開始時に提示し、人が `/lessons` スキルで確認・承認して昇格先へ書き込む。教訓化・提示・CLI のいずれも、失敗しても利用者のセッションを止めず、LLM が作った候補文を指示として扱わせない。

### Non-Goals

- 成功を含む実行統計・合格率の算出（`audit` の `quality_gate` が担う）
- 失敗の自動修正・自動リトライ（記録のみを行い、対応判断は下流に委ねる）
- `audit` への直接連携（`core` のみに依存し、`audit` には依存しない。伏せ字の強化版は audit のファイルを読み込むだけで、audit の導入を前提にしない）
- 人の承認なしの昇格、auto-memory の書き換え、Codex 直接実行・TAKT の失敗の収集（要件 §6.2）
- CI の失敗・revert の教訓化（FT-10）、作業直前の注入（FT-16）、auto-memory からの複製（FT-17）（Could。本評価セットの対象外）
- `/lessons` スキルの対話の手順そのもの（`docs/evaluation/skills/lessons.md` の責務。本評価セットは CLI と台帳の振る舞いを扱う）

## 2. 期待する入出力・副作用

| 構成要素                                                                     | 入力                                                                                                                                             | 期待する出力                                                                                                          | 副作用                                                                                                        |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `capture-failures.py`（PostToolUse / PostToolUseFailure / PermissionDenied） | hook 入力 JSON（`hook_event_name`, `tool_name`, `tool_input`, `tool_response` または `error` / `reason`, `tool_use_id`, `cwd`, `session_id` 等） | 失敗検知時のみ `.claude/logs/fail-logs/failures.jsonl` に schema v1 の 1 行を追記。非検知時・無効化時は無出力・無追記 | ログディレクトリ/ファイルの作成（所有者限定パーミッション）、記録前の機密情報マスキング                       |
| `inject-failure-summary.py`（SessionStart hook）                             | SessionStart hook 入力 JSON（`cwd`, `session_id` 等）                                                                                            | 再発している失敗のサマリーテキストを stdout に出力し、オーケストレーターの追加コンテキストとして注入される            | なし（読み取り専用。`failures.jsonl` への書き込みは行わない）                                                 |
| `distill-launch.py`（SessionEnd hook）                                       | SessionEnd hook 入力 JSON（`session_id`, `transcript_path`, `cwd` 等）と環境変数                                                                 | 無出力・終了コード 0。起動の条件を満たすときだけワーカーを切り離して起動する                                          | ワーカーの起動（標準入力でセッションの情報を渡す）                                                            |
| `lib/distill_worker.py`（ワーカー）                                          | 起動役から標準入力で受け取る JSON                                                                                                                | 教訓候補を台帳に追記・合流する。spool を取り込む                                                                      | `distill-runs.jsonl`・`distill-daily.json` の更新、教訓化セッション（`claude -p`）の呼び出し、台帳の更新      |
| `lib/lessons_store.py`（台帳）                                               | 出来事（候補の追加・合流・状態の変更など）                                                                                                       | `lessons.jsonl` への追記、`lessons-state.json`（写像）と `lessons.md` の更新                                          | `lessons.lock` によるロック                                                                                   |
| `present-lessons.py`（SessionStart hook）                                    | SessionStart hook 入力 JSON と写像                                                                                                               | 提示の基準を超えた候補を `<fail-logs-lessons>` 境界フレームで stdout に出力。無ければ無出力                           | `lessons-present.json` の更新、spool の取り込み                                                               |
| `scripts/lessons_cli.py`（エージェント向け CLI）                             | サブコマンド `list` / `show` / `add` / `check-text` / `render` / `path` と引数・標準入力                                                         | 無害化したテキストまたは JSON。終了コード 0 / 2 / 3 / 4 / 5                                                           | `add` は台帳に追記（root に書けないときは作業中の worktree の spool に退避）。`render` は `lessons.md` を更新 |
| `scripts/lessons_admin.py`（人向け CLI）                                     | サブコマンド `revise` / `set-state` / `merge` / `ack` / `verify` / `issue` / `rebuild` と引数・標準入力                                          | 結果のテキストまたは JSON。終了コードは `lessons_cli.py` と同じ                                                       | 台帳の更新。`verify` は `git fetch` / `git grep` / `gh pr list`、`issue` は `gh issue create` を実行          |

## 3. 評価観点

### 記録（既存）

- [ ] EV-01（正常 / must）: Bash の非ゼロ終了・他ツールの明示的エラーを `tool_error` として検知し `failures.jsonl` に追記する — 根拠: packages/fail-logs/README.md
- [ ] EV-02（正常 / must）: `exit_code` が 0 または欠落していても、test/lint 系コマンドの出力に失敗マーカーが含まれる場合は `test_failure`/`lint_failure` として検知する（2 段判定により `pytest ... | tail` のようなパイプでの終了コードマスクを回避する） — 根拠: packages/fail-logs/README.md
- [ ] EV-03（正常 / must）: 失敗イベントのみを記録し、成功したツール実行は記録しない（成功を含む実行統計は `audit` の `quality_gate` が別途担う） — 根拠: packages/fail-logs/README.md
- [ ] EV-04（正常 / must）: 記録するレコードは audit v1 互換スキーマ（`v`, `ts`, `sid`, `eid`, `type=failure`, `data{failure_type, error_type, detected_by, command_kind, tool, command, error_excerpt, exit_code, cwd}`）に従う。**2026-11-03 追加**: `PostToolUseFailure` / `PermissionDenied` から作る行は、これに加えて `data.tool_use_id` を持つ（項目の追加のみ。既存の項目の名前・型・意味は変えない） — 根拠: packages/fail-logs/README.md / docs/design/fail-logs.md §3(f)
- [ ] EV-05（正常 / should）: codex / agy 等の外部 CLI 呼び出し失敗を `cli_failure` として検知する — 根拠: packages/fail-logs/README.md
- [ ] EV-06（異常 / must）: エラー抜粋・コマンド文字列に含まれる機密情報（API キー・トークン等の既知パターン）は記録前に `[REDACTED]` へマスクする — 根拠: packages/fail-logs/README.md
- [ ] EV-07（境界 / must）: `config/fail-logs.yaml`（または `fail-logs.local.yaml`）の `enabled: false` で記録機能全体を無効化できる — 根拠: packages/fail-logs/README.md
- [ ] EV-08（境界 / should）: `targets.<failure_type>: false` で失敗種別ごとに記録可否を個別に無効化できる — 根拠: packages/fail-logs/README.md

### 記録（2026-11-03 追加: 合図の収集の拡張）

- [ ] EV-22（正常 / must）: `PostToolUseFailure` の Bash で `error` の 1 行目が `Exit code N` のとき、`exit_code: N`・`detected_by: "exit_code"` の行を 1 件記録する。`failure_type` は `classify_command` の結果で `test_failure` / `lint_failure` / `cli_failure`、それ以外は `tool_error`。`error_excerpt` は 2 行目以降から作る — 根拠: docs/requirements/fail-logs.md FR-04 / docs/design/fail-logs.md §3(f)
- [ ] EV-23（正常 / must）: `PostToolUseFailure` の Bash で `Exit code N` が無いとき、および Bash 以外のツールの失敗は、`failure_type: "tool_error"`・`detected_by: "tool_error"`・`exit_code: null` で記録する（Bash 以外は `command` が空・`command_kind: "none"`） — 根拠: docs/design/fail-logs.md §3(f)
- [ ] EV-24（境界 / must）: `PostToolUseFailure` の入力が `is_interrupt: true` のときは記録しない — 根拠: docs/design/fail-logs.md §3(f)
- [ ] EV-25（正常 / should）: `PermissionDenied` 1 件につき、`failure_type: "permission_denied"`・`detected_by: "permission"`・`error_type: "permission"` の行を 1 件記録する。`error_excerpt` は `reason` から作る — 根拠: docs/requirements/fail-logs.md FR-05 / docs/design/fail-logs.md §3(f)
- [ ] EV-26（境界 / must）: 1 回のツール呼び出しが 2 件の失敗イベントにならない。`PostToolUseFailure` / `PermissionDenied` の入力には `PostToolUse` の経路の検知（出力パターン）を重ねて適用しない — 根拠: docs/requirements/fail-logs.md FR-04 受け入れ条件 2 / docs/design/fail-logs.md §3(f)
- [ ] EV-27（異常 / must）: 新しい行の `command` と `error_excerpt` は、固定の上限（64 KiB）で切ってから強化版マスキングで伏せ、最後に `max_excerpt_chars` で切り詰める。`max_excerpt_chars` の境界をまたぐ位置に置いた機密情報も原文が残らない — 根拠: docs/requirements/fail-logs.md NF-03 / docs/design/fail-logs.md §3(h)
- [ ] EV-28（異常 / must）: 強化版マスキングを読み込めない・カナリアが伏せられないときは、新しい行の `command` と `error_excerpt` を空にして記録する（記録自体は止めない） — 根拠: docs/design/fail-logs.md §3(h)・§7.4
- [ ] EV-29（境界 / should）: `targets.permission_denied: false` で auto mode の拒否を記録しない — 根拠: docs/requirements/fail-logs.md NF-09 / docs/design/fail-logs.md §3(i)
- [ ] EV-30（境界 / must）: `PostToolUse` の経路の行（既存の失敗イベント）の形式と値は 2026-11-03 の変更前と同じ（`tool_use_id` を足さない・伏せ字の処理を変えない） — 根拠: docs/requirements/fail-logs.md NF-06 / docs/design/fail-logs.md §3(h)

### 再発サマリー（2026-11-03 追加）

- [ ] EV-31（正常 / must）: 新しいイベントの行（`tool_use_id` を持つ）のうちテスト・lint 以外のもの（その他部）と、それ以外の行（従来部）を分けて集計し、それぞれに件数の上限と上位件数（その他部は `summary.top_signatures_new`）を適用する。その他部の行が大量にあっても、テスト・lint の再発が集計から押し出されない（2026-11-03 の実装で、分け方を `failure_type` から記録の出所に改めた。設計 §4 の 5） — 根拠: docs/requirements/fail-logs.md FR-04 受け入れ条件 4 / docs/design/fail-logs.md §4 の 5
- [ ] EV-32（境界 / must）: その他部の行が無いとき、注入文は 2026-11-03 の変更前と同じ（`tool_use_id` を持たない従来の行は、`failure_type` が `tool_error` でも従来どおり集計する）。ある場合は、従来部の後ろに別の見出しで足す — 根拠: docs/requirements/fail-logs.md NF-06 / docs/design/fail-logs.md §4 の 5
- [ ] EV-33（境界 / must）: その他部の行のうち、`detected_by: "exit_code"`・`exit_code == 1` で、最後の単純コマンドの（環境変数の代入を除いた）先頭 1〜2 トークンが `summary.expected_nonzero_commands` に当たる行は集計しない（例: `grep`・`test`・`git diff`。`a && grep x` も除外し、`grep x && a` は除外しない） — 根拠: docs/requirements/fail-logs.md FR-04 受け入れ条件 3 / docs/design/fail-logs.md §4 の 6
- [ ] EV-34（正常 / should）: その他部の `command_kind: "shell"` の行は、最後の単純コマンドの先頭 2 トークン（例 `git push`）をシグネチャにする — 根拠: docs/design/fail-logs.md §4 の 7
- [ ] EV-35（境界 / should）: その他部の、Bash 以外のツール（`tool` が `Bash` でない）の `tool_error` は、`summary.include_non_bash_tool_errors: true` のときだけ集計する — 根拠: docs/design/fail-logs.md §4 の 8
- [ ] EV-36（境界 / must）: ログの走査は末尾から `summary.max_scan_bytes` で止まり、注入するシグネチャは `top_signatures` + `top_signatures_new` 件が上限 — 根拠: docs/design/fail-logs.md §4 の有界性保証

### 教訓化（2026-11-03 追加。既定で無効）

- [ ] EV-37（境界 / must）: 既定の設定（`distill.enabled: false`）では、SessionEnd の起動役はワーカーを起動せず、実行記録も書かない（spool も無いとき） — 根拠: docs/requirements/fail-logs.md FR-06 受け入れ条件 1・NF-09 / docs/design/fail-logs.md §7.1
- [ ] EV-38（境界 / must）: 環境変数 `FAIL_LOGS_DISTILL` があるとき、および `CLAUDE_CODE_ENTRYPOINT` が `sdk-` で始まり `distill.include_noninteractive` が false のときは、教訓化を起動せず、実行記録も書かない — 根拠: docs/requirements/fail-logs.md NF-05・NF-08 / docs/design/fail-logs.md §7.1
- [ ] EV-39（正常 / must）: 起動役は 1 秒以内に無出力・終了コード 0 で戻り、ワーカーを hook の標準入出力を引き継がずに切り離して起動し、セッションの記録のパスを引数ではなく標準入力で渡す — 根拠: docs/requirements/fail-logs.md NF-01 / docs/design/fail-logs.md §7.1・§7.7
- [ ] EV-40（正常 / must）: 前段判定で合図が無いときは LLM を呼ばず、`no_signal` で記録する — 根拠: docs/requirements/fail-logs.md FR-06 受け入れ条件 2 / docs/design/fail-logs.md §7.1・§7.6
- [ ] EV-41（正常 / must）: 手動の拒否（ツール結果の本文が `distill.reject_markers` で始まる）・中断（テキストが `distill.interrupt_markers` と一致する）のいずれかを含むセッションは、必ず「合図あり」になる（固定の評価用記録で 100%）。許可ルールの拒否・auto mode の分類器の拒否・セッションの終了による中断は人間の介入に数えない — 根拠: docs/requirements/fail-logs.md FR-06 受け入れ条件 3 / docs/design/fail-logs.md §7.3
- [ ] EV-42（正常 / must）: TDD の red → green だけのセッションと、意図した非ゼロ終了だけのセッションは「合図なし」になる（固定の評価用記録で 100%） — 根拠: docs/requirements/fail-logs.md FR-06 受け入れ条件 4 / docs/design/fail-logs.md §7.3
- [ ] EV-43（正常 / must）: 機械的な失敗の合図は、未解決のまま終わったもの・同じシグネチャで閾値（テスト・lint は `distill.repeat_threshold_test`、それ以外は `distill.repeat_threshold`）以上繰り返したもの、に限る。ツール結果のエラーと失敗イベントは `tool_use_id` で突き合わせて二重に数えない — 根拠: docs/requirements/fail-logs.md FR-06 受け入れ条件 4（2026-11-03 に設計に揃えて改訂） / docs/design/fail-logs.md §7.3
- [ ] EV-44（正常 / must）: 機械的な失敗がなく訂正だけを含むセッションの 8 割以上が「合図あり」になる（固定の評価用記録 5 件以上。`distill.correction_cues` の文字列の部分一致） — 根拠: docs/requirements/fail-logs.md FR-06 受け入れ条件 5 / docs/design/fail-logs.md §7.3
- [ ] EV-45（境界 / must）: 利用者の発言として数えるのは、`type: user` で `isSidechain` / `isMeta` / `isCompactSummary` が真でなく、本文がテキストで、`<` と英小文字のタグで始まらない行だけ（スキルの展開・hook の注入・会話の要約・`!` コマンドの出力の中の訂正の語彙で「合図あり」にしない） — 根拠: docs/design/fail-logs.md §7.3
- [ ] EV-46（境界 / must）: 同じセッションの同じ範囲を 2 回教訓化しない。再開したセッションは前回進めた `transcript_offset` から読み、フォークしたセッションは記録の中で最後に既知の `last_uuid` と一致した行の次から読む — 根拠: docs/requirements/fail-logs.md FR-06 受け入れ条件 6 / docs/design/fail-logs.md §7.3・§7.6
- [ ] EV-47（異常 / must）: 1 日の上限（`distill.daily_limit`）に達したら LLM を呼ばず `daily_limit` で終える（その日の最初の 1 回だけ記録する）。残りが `distill.reserve_for_intervention` 以下のときは人間の介入の合図があるセッションだけを通す。上限で落ちたセッションを後から教訓化しない — 根拠: docs/requirements/fail-logs.md NF-02 / docs/design/fail-logs.md §7.2
- [ ] EV-48（異常 / must）: 同じ root で同時に動く教訓化は 1 つまで。ロックを `distill.lock_wait_seconds` 待っても取れなければ `locked` で終える — 根拠: docs/requirements/fail-logs.md NF-02 / docs/design/fail-logs.md §7.2
- [ ] EV-49（異常 / must）: 教訓化セッションが `distill.timeout_seconds` を超えたら、そのプロセスグループを止めて `timeout` で記録する。`--max-budget-usd` に `distill.max_budget_usd` を渡す — 根拠: docs/requirements/fail-logs.md NF-02 / docs/design/fail-logs.md §7.2・§7.5
- [ ] EV-50（異常 / must）: 伏せ字が使えない（読み込み・カナリア・呼び出しのいずれかの失敗）ときは LLM を呼ばず `no_masking` で記録する — 根拠: docs/requirements/fail-logs.md NF-03 / docs/design/fail-logs.md §7.4
- [ ] EV-51（異常 / must）: LLM に渡す抜粋は、各メッセージを伏せてから切り詰め、全体を `distill.max_input_chars` に収める。強化版マスキングの評価に使う機密情報のサンプルの原文が、渡す内容に残らない。ホームディレクトリのパス・メールアドレス・IPv4 アドレス・URL のクエリと認証情報も伏せる — 根拠: docs/requirements/fail-logs.md NF-03 / docs/design/fail-logs.md §7.4
- [ ] EV-52（異常 / must）: 教訓化セッションは `--restricted --tools "" --strict-mcp-config --no-session-persistence`、`--settings '{"disableAllHooks": true}'`、環境変数の許可リスト + `FAIL_LOGS_DISTILL=1`、空の一時ディレクトリを作業ディレクトリとして、シェルを介さずに起動する。許可リストに無い環境変数（`GH_TOKEN` 等）を渡さない — 根拠: docs/requirements/fail-logs.md NF-05・NF-10 / docs/design/fail-logs.md §7.5
- [ ] EV-53（異常 / must）: 抜粋の各要素の役割はセッションの記録の構造で決め（本文の中身で決めない）、全体を実行ごとの乱数を含む区切りで囲み、本文中の区切りに似た文字列を中和し、データの中の `@` をすべて全角の `＠` に置き換える（行頭・空白の直後に限らない） — 根拠: docs/requirements/fail-logs.md NF-10 / docs/design/fail-logs.md §7.4
- [ ] EV-54（異常 / must）: 教訓化セッションの出力は `structured_output` だけを読み、形式検査（件数・列挙値・字数の上限・文字種）を通ったものだけを台帳に入れる。形式に合わない出力は `invalid_output` で記録して台帳に何も入れない。`merge_into` が台帳に無い ID なら新しい候補として扱う — 根拠: docs/requirements/fail-logs.md NF-10 / docs/design/fail-logs.md §7.5
- [ ] EV-55（正常 / must）: 起動・各スキップの理由・失敗の各分岐で、理由付きの実行記録が 1 件残る。LLM の出力そのもの・標準エラー・例外の文言は記録しない — 根拠: docs/requirements/fail-logs.md NF-08 / docs/design/fail-logs.md §7.6
- [ ] EV-56（異常 / must）: ワーカーは、受け取ったセッションの記録のパスが許可するルート（`$CLAUDE_CONFIG_DIR/projects/` または `~/.claude/projects/`）の配下の、自分の uid が所有する通常ファイルのときだけ読む（symlink をたどらない）。満たさなければ `no_transcript` で終える — 根拠: docs/design/fail-logs.md §7.7

### 教訓候補の台帳（2026-11-03 追加）

- [ ] EV-57（正常 / must）: 教訓候補は、事象・原因・回避策・根拠（根拠ごとの日時を含む）・再発回数・重大度・対象範囲・状態（状態が変わった日時を含む）を持つ — 根拠: docs/requirements/fail-logs.md FR-11 受け入れ条件 1 / docs/design/fail-logs.md §8.1・§8.2
- [ ] EV-58（正常 / must）: linked worktree から追加しても root worktree の `.claude/logs/fail-logs/` の台帳に保存され、worktree を削除しても残る。台帳・写像・一覧はテキスト形式（JSON Lines / JSON / Markdown） — 根拠: docs/requirements/fail-logs.md FR-11 受け入れ条件 2・4 / docs/design/fail-logs.md §6
- [ ] EV-59（異常 / must）: 台帳のファイルは所有者だけが読み書きできる権限（ディレクトリ 0700・ファイル 0600）で作られ、symlink をたどらずに開く — 根拠: docs/requirements/fail-logs.md FR-11 受け入れ条件 3 / docs/design/fail-logs.md §6
- [ ] EV-60（異常 / must）: 台帳に入れる文字列は伏せ字にかけてから字数の上限で切り詰める。既知の機密情報のパターンが台帳・写像・一覧に原文で残らない — 根拠: docs/requirements/fail-logs.md FR-11 受け入れ条件 5 / docs/design/fail-logs.md §8.3
- [ ] EV-61（正常 / must）: 同じ教訓が異なるセッションから 2 回追加されたとき、候補は 1 件のままで再発回数が 2、根拠が 2 件になる — 根拠: docs/requirements/fail-logs.md FR-12 受け入れ条件 1 / docs/design/fail-logs.md §8.2
- [ ] EV-62（境界 / must）: 再発回数は根拠の `(kind, ref)` の異なる組の数で、同じセッション・同じ PR から何度合流しても増えない（同じ PR の複数の指摘も 1） — 根拠: docs/requirements/fail-logs.md FR-12 受け入れ条件 2・FR-09 受け入れ条件 2 / docs/design/fail-logs.md §8.2
- [ ] EV-63（正常 / must）: 状態が昇格中・昇格済み・却下の候補にも合流し、再発回数と根拠が足される。却下の候補は合流後も却下のまま — 根拠: docs/requirements/fail-logs.md FR-12 受け入れ条件 3・FR-15 受け入れ条件 2 / docs/design/fail-logs.md §10.3
- [ ] EV-64（境界 / must）: `kind: agent` の根拠（セッション ID の無いエージェントの追加）と、spool が持ち込んだ根拠のうち取り込み時に確かめられなかったものは、再発回数に数えない — 根拠: docs/design/fail-logs.md §8.2・§8.5
- [ ] EV-65（境界 / should）: 重大度は `normal` → `critical` へだけ、対象範囲は `claude` → `all` へだけ変わる。人が確定した値は合流で変わらず、人が `normal` と確定した候補に `critical` の根拠が合流したときは確認の印（`severity_alert`）が立つ — 根拠: docs/design/fail-logs.md §8.2
- [ ] EV-66（異常 / must）: 複数のプロセスが同時に候補を追加・更新しても、全候補が読めて、再発回数が追加した内容と一致する。追記の後・写像の置換の前に落ちても、次の書き手が追いつく — 根拠: docs/requirements/fail-logs.md NF-07 / docs/design/fail-logs.md §8.2・§8.3
- [ ] EV-67（異常 / should）: 写像が無い・壊れている・`ledger_offset` が台帳の大きさより大きいときは、台帳の全体から作り直す。台帳の末尾が改行で終わっていなければ、改行を足してから追記する — 根拠: docs/design/fail-logs.md §8.2・§8.3
- [ ] EV-68（異常 / must）: §10.3 の表に無い状態の遷移を拒否する — 根拠: docs/design/fail-logs.md §8.2・§10.3
- [ ] EV-69（異常 / must）: 文字種の検査（Cc（本文の改行を除く）・Cf・Cn・Zl・Zp・Co・Cs）と参照の形式の検査（`evidence.ref`・`target.ref`・`target.branch`・`note`）に通らない入力を拒否する — 根拠: docs/design/fail-logs.md §8.3
- [ ] EV-70（境界 / should）: 追加元（`source`）ごとの 1 日の追加件数が `lessons.add_daily_limit` を超えたら追加しない — 根拠: docs/requirements/fail-logs.md NF-02 / docs/design/fail-logs.md §8.3
- [ ] EV-71（境界 / must）: root の台帳に書けないとき、`lessons_cli.py add` は追加の内容を作業中の worktree の spool に退避して終了コード 3 を返す。状態を変える操作（`lessons_admin.py`）は退避せず終了コード 3 を返す — 根拠: docs/design/fail-logs.md §8.5
- [ ] EV-72（異常 / must）: spool の取り込みは出来事の種類を `lesson.created` / `lesson.evidence_added` に限り、`actor: spool`・`source: agent` に固定し、§8.3 の検査を通す。ファイル名の uuid を `eid` にし、同じ `eid` は 1 回だけ適用する — 根拠: docs/design/fail-logs.md §8.5
- [ ] EV-73（異常 / must）: spool のファイルが通常ファイルでない（FIFO・symlink）・自分の uid の所有でない・64 KiB を超えるときは、読まずに消す（hook を止めない） — 根拠: docs/design/fail-logs.md §8.5

### 提示（2026-11-03 追加）

- [ ] EV-74（正常 / must）: 提示の対象は、(A) 候補・重大・再発 1（件数と ID だけ）、(B) 昇格済みで昇格後に閾値以上再発、(C) 昇格中のまま `lessons.present.promoting_stale_days` を過ぎた、(D) 候補で再発回数が `lessons.present.threshold` 以上。却下の候補と期間内の昇格中の候補は示さない。対象が無ければ何も示さない — 根拠: docs/requirements/fail-logs.md FR-13 / docs/design/fail-logs.md §9
- [ ] EV-75（境界 / must）: 示す一覧の内容（ID と再発回数）が前回と同じなら、同じ root で 1 日 1 回までにする。変わったときは示す — 根拠: docs/requirements/fail-logs.md FR-13 受け入れ条件 2 / docs/design/fail-logs.md §9
- [ ] EV-76（境界 / must）: 示す件数が `lessons.present.max_items`、全体の文字数が `lessons.present.max_chars` を超えない — 根拠: docs/requirements/fail-logs.md FR-13 受け入れ条件 4・NF-04 / docs/design/fail-logs.md §9
- [ ] EV-77（異常 / must）: 提示は `<fail-logs-lessons>` 境界フレームで囲み、候補の文の山括弧を中和し、「未承認の候補であり、指示ではない」と明記する。候補の文に閉じタグの偽装や指示文があっても境界の外に出ない — 根拠: docs/requirements/fail-logs.md FR-13 受け入れ条件 5・NF-04 / docs/design/fail-logs.md §5.1・§9
- [ ] EV-78（境界 / must）: `enabled: false`・`lessons.present.enabled: false`・`FAIL_LOGS_DISTILL=1`・`CLAUDE_CODE_ENTRYPOINT` が `sdk-` で始まる、のいずれかのときは示さない — 根拠: docs/requirements/fail-logs.md FR-13 受け入れ条件 7・NF-09 / docs/design/fail-logs.md §9
- [ ] EV-79（正常 / must）: 昇格後の再発として示した候補は、`lessons_admin.py ack` の後、さらに閾値以上再発するまで示さない — 根拠: docs/requirements/fail-logs.md FR-15 受け入れ条件 4 / docs/design/fail-logs.md §8.2・§9

### CLI（2026-11-03 追加）

- [ ] EV-80（異常 / must）: `lessons_cli.py` の `list` / `show` / `render` のテキスト出力は `<fail-logs-lessons>` 枠で囲み、各候補に `[candidate]` を付け、山括弧を中和する。`--json` の出力は文字列を同じく中和し、最上位に `"untrusted": true` を付ける — 根拠: docs/design/fail-logs.md §5.1
- [ ] EV-81（境界 / must）: fail-logs が導入されていない（`.claude/orchestra.json` に無い）、または `enabled: false` のとき、両方の CLI は何もせずに終了コード 4 で戻る — 根拠: docs/requirements/fail-logs.md NF-09 / docs/design/fail-logs.md §10.1
- [ ] EV-82（境界 / must）: `lessons_cli.py` は状態を変えるサブコマンド（`revise` / `set-state` / `merge` / `ack` / `verify` / `issue` / `rebuild`）を持たない（エージェントが使う入口と人が使う入口を分ける） — 根拠: docs/requirements/fail-logs.md §10 制約 / docs/design/fail-logs.md §10.1
- [ ] EV-83（境界 / must）: `lessons_cli.py add` の根拠は CLI が決める。`--actor agent` は `CLAUDE_CODE_SESSION_ID` があれば `kind: session`、無ければ `kind: agent`。`kind: pr` の根拠は `--actor review` のときだけ受け付ける — 根拠: docs/design/fail-logs.md §8.3
- [ ] EV-84（異常 / must）: `check-text` は、昇格する文面（見出し・回避策・マーカー）に伏せ字を再びかけ、URL・メールアドレス・ホームディレクトリのパス・IP アドレス・FQDN を検出し、文字種を検査し、公開範囲（取得に失敗したら公開として扱う）を返す。文面に根拠（セッション ID・PR）を含めない — 根拠: docs/requirements/fail-logs.md FR-14 受け入れ条件 6〜8 / docs/design/fail-logs.md §10.4
- [ ] EV-85（正常 / must）: `verify` は、統合ブランチにマーカーがあれば昇格済み、書き込みを含む PR がマージされずに閉じていれば候補、PR が開いている・無いときは昇格中のままにする。PR がマージ済みでマーカーが見つからないときは昇格中のまま理由を返す。`git fetch` に失敗したら状態を変えない — 根拠: docs/requirements/fail-logs.md FR-14 状態の遷移 / docs/design/fail-logs.md §10.5
- [ ] EV-86（異常 / must）: `issue` は、検出があるのに `--ack-findings` が無い、または公開（不明を含む）なのに `--ack-public` が無いとき、Issue を作らずに終了コード 5 で戻る。作る Issue にはラベルを付けない。成功したら昇格済み（Issue の URL を記録）、失敗したら状態を変えずに理由を返す — 根拠: docs/requirements/fail-logs.md FR-14 受け入れ条件 3・8 / docs/design/fail-logs.md §10.6
- [ ] EV-87（異常 / must）: CLI と hook は外部コマンド（`git` / `gh` / `claude`）を引数の配列で起動し、シェルを介さない — 根拠: docs/design/fail-logs.md §7.5・§10.1
- [ ] EV-88（正常 / should）: `merge` はまとめられた候補を写像から除き、その根拠をまとめ先に移す。`revise` で直した値は人が確定した値になる — 根拠: docs/design/fail-logs.md §8.1・§8.2

## 4. 類型別観点

<!-- docs/evaluation/README.md の hook 型チェックリストを本パッケージの実情で具体化する -->

- ~~EV-09~~（**欠番**, 2026-07-03 レビュー）: capture-failures の stdout 契約は「記録 → 集計 → 再発可視化」というパッケージの価値フローを捉えられておらず設計見直しが必要と判定（下記注記・Issue #131）。2026-07-28 裁定で後継 EV-17 を新設(欠番は再利用しない方針を維持)
- ~~EV-10~~（**欠番**, 2026-07-03 レビュー）: inject-failure-summary の注入・集計フローは「あるべき仕様」が未定義と判定。正しい集計軸・注入内容・活用フローの再定義が必要（下記注記・Issue #131）。2026-07-28 裁定で後継 EV-18/EV-19 を新設(欠番は再利用しない方針を維持)
- [ ] EV-11（境界 / should）: `capture-failures.py` は失敗を検知・記録した場合でも常に exit 0 を返し、PostToolUse をブロックしない（記録は非ブロッキングなメタデータ収集でありゲートではない）。**2026-11-03 追加**: `PostToolUseFailure` / `PermissionDenied` でも同じ。`distill-launch.py` と `present-lessons.py` も常に exit 0 で戻り、ツール実行・セッションの開始と終了をブロックしない — 根拠: 実装挙動 / docs/requirements/fail-logs.md NF-01
- [ ] EV-12（異常 / must）: `capture-failures.py` 内部で例外が発生しても exit 0 で終了し、記録対象であるツール実行フロー（PostToolUse）を阻害しない（fail-safe）。**2026-11-03 追加**: `distill-launch.py` と `present-lessons.py` も内部の例外で exit 0 で終了する。`present-lessons.py` は台帳のロックを `LOCK_NB` で取り、取れなければ待たずに写像のまま読む（取り込みと追いつきを飛ばす） — 根拠: 実装挙動 / docs/requirements/fail-logs.md NF-01 / docs/design/fail-logs.md §9
- [ ] EV-13（異常 / must）: `inject-failure-summary.py` は `failures.jsonl` 内の壊れた行（不正 JSON）や内部例外があってもクラッシュせず、当該行をスキップして SessionStart をブロックしない（fail-safe）。**加えて（2026-07-03 裁定）**、注入機能そのものが壊れていないこと（正常な失敗ログから期待どおりサマリーが生成されること）を、破損スキップだけでなく機能回帰テストで担保する — 根拠: 実装挙動 + 2026-07-03 人間レビュー裁定（機能回帰の担保。あるべき集計・注入フローは 2026-07-28 裁定で EV-18/EV-19 として確定済み。本機能回帰テストは実装状況に依存せず追加可能）
- [ ] EV-14（境界 / should）: `inject-failure-summary.py` のログ走査は上限を設けた範囲に限定され、ログが肥大してもファイル全体を毎回読み込まない（性能） — 根拠: 実装挙動
- ~~EV-15~~（**欠番**, 2026-07-03 レビュー）: 注入サマリーの無害化（間接プロンプトインジェクション対策）は、具体的な担保内容が未確定と判定。脅威モデルと保証範囲を明文化した設計が必要（現状の `<fail-logs-summary>` 境界フレーム＋`[log]` プレフィックス＋山括弧中和を出発点として Issue #131 で再設計）。2026-07-28 裁定で後継 EV-20 を新設(欠番は再利用しない方針を維持)
- 秘匿情報: EV-06（既存の経路）・EV-27・EV-28（新しい記録）・EV-50・EV-51（教訓化の入力）・EV-60（台帳）・EV-84（昇格先の文面）で担保
- config 駆動: EV-07（enabled 全体無効化）・EV-08・EV-29（targets 個別無効化）・EV-37（教訓化は既定で無効）・EV-78（提示の無効化）・EV-81（CLI の無効化）で担保
- [ ] EV-16（境界 / should）: 冪等性 — 同一失敗イベントが再入力されても重複排除は行わず、重複記録を許容する（`_append_secure_jsonl` は `O_APPEND` の純追記）。再発した失敗はそれ自体が「解決すべき重要な失敗」のシグナルであり、蓄積を許容して inject-failure-summary の再発集計で重要度として扱う。**2026-11-03 追加**: 新しいイベント（`PostToolUseFailure` / `PermissionDenied`）の記録も同じく重複排除しない。重複を排除するのは、教訓候補の再発回数（根拠の組で数える。EV-62）と spool の取り込み（`eid`。EV-72）だけで、失敗イベントの記録には適用しない — 根拠: 2026-07-03 人間レビュー裁定（現実装が重複排除しないことを確認済み。実装ギャップなし） / docs/design/fail-logs.md §3(d)
- [ ] EV-17（正常 / must）: capture の記録契約 — stdout 無出力・検知有無に関わらず常に exit 0、audit v1 互換スキーマ + data.branch フィールド（ADR-20260728-046）を含む記録が追記される。**2026-11-03 追加**: 同じスクリプトを PostToolUse / PostToolUseFailure / PermissionDenied に登録し、入力の `hook_event_name` で分岐する。どのイベントの行も `data.branch` を持つ — 根拠: docs/design/fail-logs.md §3
- [ ] EV-18（正常 / must）: 集計・注入の機能回帰 — 正常な失敗ログ（再発 min_occurrences 以上を含む）から、再発シグネチャ中心の期待どおりのサマリーが生成・注入される（シグネチャは command 先頭トークン + command_kind、非 Bash は failure_type + tool フォールバック） — 根拠: docs/design/fail-logs.md §4
- [ ] EV-19（境界 / must）: 注入の有界性 — ログが max_records を大きく超えて肥大しても、読み出しは末尾 max_records 件・注入は top_signatures 件・コマンド 120 字/抜粋 100 字上限で頭打ちになる — 根拠: docs/design/fail-logs.md §4
- [ ] EV-20（異常 / must）: 注入テキストの無害化 — ログ由来テキスト内の山括弧（</fail-logs-summary> 等の境界フレーム偽造を含む）が ‹ › へ中和され、境界フレームと [log] プレフィックスが維持される — 根拠: docs/design/fail-logs.md §5
- [ ] EV-21（正常 / must）: root worktree 解決 — worktree セッションからの書き込み・読み出しが root worktree の .claude/logs/fail-logs/ に集約され、git 解決不能時は project_dir へフォールバックする — 根拠: docs/adr/ADR-20260728-046.md / docs/design/fail-logs.md §6
- CLI ツール型（副類型）: コマンド契約は EV-80〜EV-88、入力バリデーションは EV-69・EV-83、破壊的操作の安全策は EV-82（入口の分離）・EV-86（Issue の作成の確認）、出力の安定性は EV-80（`--json`）、設定レイヤリングは EV-07 と同じ `config-loading` の規則で担保

> **fail-logs の価値フロー再設計（EV-09・EV-10・EV-15 欠番, 2026-07-03）**: 本パッケージの「記録 → 集計 → 再発サマリー注入」という価値フローと注入テキストの無害化要件は、現状「根拠: 実装挙動」に留まり「あるべき仕様」が未確定。(1) capture の記録契約、(2) inject の集計軸・注入内容・活用フロー、(3) 注入テキストの無害化（脅威モデルと保証範囲）を設計として明文化し、確定後に新 ID で観点を追加する（Issue #131）。再設計が決まるまで、現状実装を「正」とする回帰テストは追加しない。
>
> **2026-07-28 追記**: 上記の再設計は EV-17〜EV-21 の新設により解消済み。回帰テスト追加の抑制は解除された（テスト追加は evaluation-set-policy の手順に従う）。
>
> **2026-11-03 追記**: 教訓化と昇格（要件 `docs/requirements/fail-logs.md`、ADR-20261103-063）に合わせて EV-22〜EV-88 を追加した。LLM の判断に依存する受け入れ条件（要件の「判定」。FR-07 の 3 項目、FR-08 の 2 項目、FR-12 の合流の判定）は CI のテストでは確かめず、`scripts/eval_distill.py` の手動評価で確かめる（設計 §7.8。plan の Phase 4 の受け入れ条件）。本評価セットの観点は、LLM を呼ばずに確かめられる部分だけを対象にする。FR-18（memory-layers ルールと ADR）はパッケージの振る舞いではないため本評価セットの対象外とし、文書の受け入れ条件で確かめる。

## 5. テストレビュー判断基準（パッケージ固有）

- EV-09・EV-10・EV-15 は 2026-07-03 レビューで欠番化された（記録・集計・注入フロー、無害化要件が仕様未確定）。現状実装を「正」とするテストは追加せず、設計確定（Issue #131）を待つ。EV-14（性能・上限読み）は実装のみが根拠のため、テストが切り詰め値の丸写しになっていないか確認する。
- EV-06（機密情報マスキング）のテストは、実際のシークレットパターン（`sk-...`, `ghp_...`, `AKIA...` 等）を用いたケースを正常系のついでではなく独立した異常系として検証しているか確認する。EV-27・EV-51・EV-60 も同じ基準で、強化版マスキングの評価に使うサンプルを使い、切り詰めの境界をまたぐ位置に機密情報を置いたケースを含むか確認する。
- EV-12・EV-13（fail-safe）のテストは、例外を強制的に発生させた上で exit code と「記録対象の本体処理（ツール実行・セッション開始）が継続すること」の両方を検証しているか確認する。
- EV-30・EV-32（既存の出力の互換）のテストは、変更前のテストの期待値を書き換えずに通すことで確かめる。期待値を書き換えた場合は互換性が壊れたことを意味するので、評価セットとの突合をやり直す。
- 教訓化のテスト（EV-37〜EV-56）は `claude` をスタブにし、スタブに渡された引数・環境変数・標準入力を検証する。スタブの戻り値を固定して「台帳に入った」ことだけを見るテストは、形式検査（EV-54）を空洞化していないか確認する。
- 前段判定のテスト（EV-41〜EV-46）は、合成したセッションの記録（`tests/fixtures/`）を使う。記録の形（項目名・定型文）は設計 §7.3・§15 で実機に合わせたものを正とし、実装の都合で作った形の記録でテストしていないか確認する。
- 並行更新のテスト（EV-66）は、複数のプロセスを実際に起動して追加し、再発回数の合計を検証しているか確認する（スレッドや逐次実行だけでは flock の競合を確かめられない）。
