# meta-harness 評価セット

**パッケージ**: `packages/meta-harness`
**類型**: 主: CLI ツール型、副: hook 型（config-loading レイヤリングへの依存）
**作成日**: 2026-07-06
**最終レビュー日**: 未レビュー（draft、パッケージ実装前に作成）
**情報源**: docs/design/meta-harness.md（基本設計）, docs/design/meta-harness-detailed.md（詳細設計 §1〜§14）, docs/requirements/meta-harness.md, docs/adr/ADR-20260706-031.md

## 1. 責務定義

本パッケージは、facet/config で表現されるハーネス構成（候補）を宣言的オーバーレイとして登録し、
隔離された一時 worktree 上でシナリオ評価を実行し、その結果を改ざん不能な ledger に記録した上で
品質 vs コストの Pareto frontier を算出することを保証する。候補・実行結果は immutable に保存され、
状態（candidate/evaluated/promoted/retired）は ledger のイベント畳み込みからのみ導出される。

### Non-Goals

- 探索ループ（`loop`）や proposer（`propose`）による自動改善そのものの成否判定
  （本評価セットは Phase 1 計測基盤を主対象とし、Phase 2/3 の proposer/loop 品質は別途評価する）
- ハーネス候補が実際に品質を改善するかどうかの判定（それは evaluator が生成するスコアの対象であり、
  本パッケージ自体の責務は「正しく計測・記録・集計すること」に限定される）
- 実ファイル（facets/instructions 等）への直接反映（`promote` を経た PR ベースの反映のみが対象で
  あり、探索ループ自体は実ファイルを書き換えない）

## 2. 期待する入出力・副作用

| 構成要素                             | 入力                                                           | 期待する出力                                                                     | 副作用                                                                  |
| ------------------------------------ | -------------------------------------------------------------- | -------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `init`                               | なし                                                           | exit 0（初回・2回目とも）                                                        | `.claude/meta-harness/` 配下のディレクトリ・空ファイル作成（冪等）      |
| `register`                           | `--overlay <dir> --target <t> [--parent <id>] [--description]` | `candidates/<cand_id>/manifest.json` 生成、ledger に `candidate_registered` 追記 | overlay ファイルの immutable 配置                                       |
| `evaluate`                           | `--candidate <id> [--scenario <id>...] [--repeat N]`           | `runs/<run_id>/result.json` 一式、ledger に `run_completed` 追記                 | 一時 worktree 作成・除去、`events.jsonl.gz` 等の成果物書き込み          |
| `frontier`                           | `[--target <t>] [--rebuild]`                                   | target 別 Pareto frontier レポート                                                | `frontier-<target-slug>.json` の書き込み（`--rebuild` 時のみ）          |
| `status`                             | `[--target <t>] [--candidate <id>]`                            | target 別 population / frontier の状態表示（畳み込み済み状態）                   | なし                                                                    |
| `promote`                            | `<cand_id> [--confirm]`                                        | PR URL または confirm 結果                                                       | `promotion_reserved` / `promotion_opened` / `status_changed` 等の追記、promotion worktree 作成・PR 作成 |
| `purge`                              | `[--keep-generations N]`                                       | 削除件数                                                                         | 古い世代・`retired` 候補ディレクトリの削除（frontier・promoted は除外） |
| evaluator（worktree ライフサイクル） | `cand_id`, `scenario`                                          | `verdict` / `critical_pass_rate` / `quality_score` / `cost` を含む result        | 一時 worktree の作成・overlay 適用・config `.local.yaml` 実体化・除去   |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `init` は未初期化状態から `.claude/meta-harness/` の必要なディレクトリ（`candidates/` `runs/`）と `ledger.jsonl` / `frontier-claude-harness.json` を作成する — 根拠: 詳細設計 §6
- [ ] EV-02（境界 / must）: `init` を初期化済みの状態で再実行しても既存データを破壊せず exit 0 で完了する（冪等） — 根拠: 詳細設計 §6
- [ ] EV-03（異常 / must）: `register` は既存の `cand_id` に対する再登録を immutability 違反として拒否する（`candidates/<cand_id>/` は登録後書き換え不可） — 根拠: 詳細設計 §1-1 の immutability 原則、基本設計 §3
- [ ] EV-04（異常 / must）: `register` は overlay 内にパスエスケープ（`../` 等でリポジトリ外を指すパス）を含む場合、登録を拒否する — 根拠: 詳細設計 §7 テスト戦略（overlay 適用のパスエスケープ拒否）
- [ ] EV-05（異常 / must）: `register` の config patch（`overlay/config-patch.json`）が allowlist 外のキーを含む場合、登録を拒否する — 根拠: 詳細設計 §2-1 手順3（allowlist 検証は register 時と evaluate 時の両方で実施）
- [ ] EV-06（正常 / must）: ledger（`ledger.jsonl`）は既存行を書き換えず、新規イベントを追記のみで記録する（append-only） — 根拠: 基本設計 §2 用語定義「ledger」、詳細設計 §1-2
- [ ] EV-07（正常 / must）: 状態畳み込みは「registered → candidate」「最初の run_completed → evaluated」「status_changed(promoted|retired) は終端」の規則に従って正しく導出される — 根拠: 詳細設計 §1-2 状態畳み込み規則の表
- [ ] EV-08（境界 / should）: `promoted` / `retired` 到達後に届いた `run_completed` は状態を変化させず警告として扱われる — 根拠: 詳細設計 §1-2 状態畳み込み規則の表
- [ ] EV-09（正常 / must）: `evaluate` は run 成果物一式（result.json、events.jsonl.gz 等）を `runs/<run_id>/` に保存する — 根拠: 詳細設計 §2-1 手順8、基本設計 §3 ストア設計
- [ ] EV-10（正常 / must）: `evaluate` の成果物には redaction（OPENAI_API_KEY / AWS キー / GITHUB_TOKEN / ghp_ / github_pat_ / sk- / PEM 秘密鍵ブロック）が適用され、秘匿パターンが残らない — 根拠: 詳細設計 §2-6、codex-harness redaction パターンとの同値性
- [ ] EV-11（正常 / must）: `evaluate` の `events.jsonl` は `.gz` 圧縮されて保存される — 根拠: 基本設計 §3「圧縮」、詳細設計 §2-1 手順8
- [ ] EV-12（異常 / must）: `result.json` が schema（`result.schema.json`）を満たさない場合、書き込み前に検証エラーとして扱われる（exit 2 系） — 根拠: 詳細設計 §1-4、§6 exit code 表
- [ ] EV-13（境界 / must）: worktree 作成後のいずれの段階（overlay 適用・build・シナリオ実行・oracle 判定）でエラーが発生しても `verdict=error` の result.json と ledger 追記が必ず行われる（fail-safe） — 根拠: 詳細設計 §2-5
- [ ] EV-14（正常 / must）: 一時 worktree は評価の成功・失敗に関わらず `finally` で確実に除去される（`git worktree remove --force` + `prune`） — 根拠: 詳細設計 §2-1 手順9、§2-5
- [ ] EV-15（境界 / must）: `.claude/meta-harness/locks/evaluate.lock` が既に取得されている状態で `evaluate` を実行すると exit 3 で終了する — 根拠: 詳細設計 §2-3、§6 exit code 表
- [ ] EV-16（正常 / must）: Pareto 判定は `quality_mean(A) ≥ quality_mean(B) かつ tokens_mean(A) ≤ tokens_mean(B) かつ少なくとも一方が厳密` の場合に A が B を支配すると正しく判定する — 根拠: 詳細設計 §3-5
- [ ] EV-17（境界 / must）: quality_mean・tokens_mean が完全に同率の候補同士は `quality_min` が高い方を優先するタイブレークが機能する — 根拠: 詳細設計 §3-5
- [ ] EV-18（正常 / must）: non-holdout シナリオのいずれかで `verdict=fail` または `error` の候補は frontier から除外される — 根拠: 詳細設計 §3-5
- [ ] EV-19（境界 / must）: holdout シナリオの run 成果物は `.claude/meta-harness/holdout/runs/<run_id>/` に物理分離されて保存され、通常の `runs/` には現れない — 根拠: 詳細設計 §3-6
- [ ] EV-20（正常 / should）: `frontier --target <t>` は指定 target の ledger event から再計算し、`--rebuild` 指定時のみ `frontier-<target-slug>.json`（再生成可能キャッシュ）へ永続化する。`--rebuild` なしではキャッシュを書き換えない — 根拠: 詳細設計 §6
- [ ] EV-21（境界 / must）: `purge` は frontier 上の候補・`promoted` 済み候補を削除対象から除外する — 根拠: 基本設計 §3「retention/purge」、詳細設計 §6
- [ ] EV-22（正常 / must）: `.claude/config/meta-harness/meta-harness.local.yaml` に設定したキーはベース `config/meta-harness.yaml` の値を上書きし、未設定キーはベースの値が使われる — 根拠: `config-loading.md`、詳細設計 §5
- [ ] EV-23（境界 / must）: `result.json` の `self_report` が欠落またはパース不能の場合、`penalty = penalty_missing_report`（既定 6）が強制適用され `quality_score` のペナルティ項がゼロになる — 根拠: 詳細設計 §3-1
- [ ] EV-24（正常 / must）: `result.json` に `claude_version` フィールドが必須として記録される — 根拠: 詳細設計 §2-2 バージョン注意、§1-4 result schema

## 4. 類型別観点

- [ ] EV-25（正常 / must）: CLI サブコマンド（`register` 等）は入力エラー時に exit code 2 を返す（実行時エラーの exit 1 と区別される） — 根拠: 詳細設計 §6 exit code 表
- [ ] EV-26（正常 / should）: 全サブコマンドは `--json` フラグ指定時に機械可読な JSON 出力を返す — 根拠: 詳細設計 §6
- [ ] EV-27（異常 / must）: capability gate — CLI バージョン pin 不一致または必須フラグ非対応時に `evaluate` が exit 2 で fail-closed する — 根拠: 詳細設計 §2-7
- [ ] EV-28（境界 / must）: store.lock — `register`/`evaluate`/`promote`/`frontier --rebuild`/`purge` の並行実行で ledger が破損しない（O_APPEND + fsync + 短期 lock） — 根拠: 詳細設計 §2-3
- [ ] EV-29（境界 / must）: run_id 一意性 — 同一秒・同一シナリオの並行 attempt でも run dir が衝突しない（nonce） — 根拠: 詳細設計 §2-4
- [ ] EV-30（正常 / must）: holdout filtered view — `propose` が構築する view に holdout run 成果物・ledger の holdout イベントが含まれない — 根拠: 詳細設計 §3-6
- [ ] EV-31（異常 / must）: overlay 拒否 — 絶対パス/`..`/symlink/禁止 prefix（packages/meta-harness 等）を含む overlay が `register` と `evaluate` の両方で拒否される — 根拠: 詳細設計 §1-7
- [ ] EV-32（正常 / must）: メインルート解決 — feature worktree 内から実行しても store・評価用 worktree がメイン worktree ルート配下に解決される（`git rev-parse --git-common-dir` ベース） — 根拠: 詳細設計 §2-0
- [ ] EV-33（異常 / must）: メインルートが導出できない環境（bare repo 等）で `storage.root` 未指定の場合、exit 2 で fail-closed する — 根拠: 詳細設計 §2-0
- [ ] EV-34（異常 / must）: proposer の proposal が overlay 安全制約違反（allowlist 外パス・サイズ超過・holdout run 参照）の場合、候補登録されず exit 2、`rejected/` に診断保存される。`based_on_runs` membership 照合・rejected 保存・exit 2 は propose CLI（M4）で実装する — 根拠: 詳細設計 §11-5
- [ ] EV-35（正常 / must）: proposer は srt 隔離 backend で fail-closed 起動され、version pin・最小 env allowlist・空 `AGENTS.md` により backend/環境/自動注入経路が固定される。各 canary は sandbox 外の成功と sandbox 内の拒否を対照検証し、origin HTTP エラー等を拒否成功と誤認しない — 根拠: 詳細設計 §11-3-2〜§11-3-5
- [ ] EV-36（正常 / must）: `loop` が 4 停止条件（budget_exhausted / max_iterations / divergence / converged）のそれぞれで `loop_stopped` を記録して停止する — 根拠: 詳細設計 §13-2
- [ ] EV-37（境界 / must）: `loop` が中断（SIGINT/エラー）されても `loop_stopped(interrupted/error)` が記録され、`--resume` で ledger から再開できる — 根拠: 詳細設計 §13-3
- [ ] EV-38（異常 / must）: `promote` の前提条件（状態が `evaluated` でない・frontier 外・最新 passing holdout 不在・non-holdout/holdout run hash 陳腐化・overlay hash 不一致・`source_commit` が main の ancestor でない・鮮度チェック失敗）のいずれかで exit 2 になり、PR は作られない — 根拠: 詳細設計 §12-1
- [ ] EV-39（正常 / must）: `promote` は auto-merge を付けず、`promoted` への状態遷移は `--confirm` 経由でのみ発生する。push 後の PR 作成失敗では remote branch を回収する — 根拠: 詳細設計 §12-2
- [ ] EV-40（異常 / must）: srt settings の allowRead が forbidden asset（実 store・holdout・facet ソース・実 `~/.codex` 等）と交差せず、view 外 read / `../` traversal / symlink escape / 非許可 network / env leak が到達不能である — 根拠: 詳細設計 §11-3-2〜§11-3-5
- [ ] EV-41（境界 / must）: loop 中断で `loop_iteration` 未記録の孤児候補が残った場合、`--resume` がその反復の完了から再開する — 根拠: 詳細設計 §13-1
- [ ] EV-42（異常 / must）: 未解放の `promotion_reserved` がある候補への二重 promote が exit 3 で拒否される — 根拠: 詳細設計 §12-2
- [ ] EV-43（異常 / must）: `--confirm` は PR が MERGED かつ main 到達済みの場合のみ `promoted` に遷移させて `promotion_released(promoted)` を記録し、closed-unmerged は `promotion_released(pr_closed_unmerged)` になる。subprocess 起動失敗・timeout は traceback ではなく exit 1 の runtime error になる — 根拠: 詳細設計 §12-2
- [ ] EV-44（正常 / must）: parent 候補から生成する proposer 子候補は、proposal で上書きした path に加えて parent overlay の未変更 path も累積 overlay として継承する — 根拠: 詳細設計 §11-1
- [ ] EV-45（異常 / must）: proposer 出力経路の資格情報検知（検知層。主対策は L1）— staged `auth.json` の canary（平文/base64/hex/URL 変形）を proposal・overlay に混入させた登録は L2 で拒否され `proposer_security_violation(L2_canary)` を記録する。`sk-` 系 API key・JWT 3 セグメント等の汎用 secret は L3 で登録時に拒否（`proposer_security_violation(L3_secret_scan)`）し、promote 前提条件でも同一スキャンを再実行して exit 2 で fail-closed する（スキャン導入前登録候補への遡及防御） — 根拠: 詳細設計 §11-3-6 L2/L3
- [ ] EV-46（異常 / must）: candidate scenario の実行隔離は、対象 worktree・実行単位 runtime・必要 system/tool だけを許可し、実 HOME・store・実 repo・sibling worktree・global tmp 等のユーザー領域を読めず、親 env（`ANTHROPIC_API_KEY` 等の secret）を継承しない。Docker daemon 不在・イメージ pin 不一致・broker 起動失敗は worktree 作成前に fail-closed し、非隔離実行へ降格しない。scenario image は既定でもnon-rootで、preparation / oracle は read-only/no-network の別コンテナで独立 Git snapshot を参照する。artifact は `openat(O_NOFOLLOW)`・regular file・サイズ上限で検査する。container/network cleanup後の不在確認は明示的な missing-object 応答だけを成功扱いにする — 根拠: 詳細設計 §2-2、§8-2、ADR-20260712-034（ADR-20260711-032/033 を置換）
- [ ] EV-47（正常 / must）: 資格情報は候補コンテナに置かない。ephemeral broker が Claude Max OAuth を保持し、コンテナ内 Claude CLI は `ANTHROPIC_BASE_URL` 経由で broker に到達する。broker は dummy キーを実 Bearer に差し替え、`anthropic-beta` は broker 固定 OAuth feature と pin 済み CLI の既知 feature allowlist の和集合だけを中継する（未知・重複・不正値は拒否し、従量課金 API key へ fallback しない）。broker 自身の egress は api.anthropic.com に限定される。Claude CLI が送る `/v1/messages?beta=true` はqueryを保持して中継し、それ以外のqueryはpre-admissionで拒否してbudgetをラッチしない。scenario コンテナは internal network で直 egress を持たない。broker は run スコープで起動・破棄し、token はコンテナ tmpfs 注入後に即 unlink される。**broker は per-run 予算・累積token envelopeを独立に強制する**（候補 hooks/Bash が broker へ直接 `/v1/messages` を投げても、上流送信前の保守的なrequest上限検査で単発超過を拒否し、実`usage`積算の上限超過もラッチして後続呼び出しを拒否する）。正常run中はhealth keepaliveとactive stream activityでidle誤停止を防ぎ、host消失時はidle/absolute lifetimeで自己終了する。budget/anomaly metrics は失敗経路でもcleanup前に保存し、いずれかがtrueならattempt全体をerrorにする — 根拠: 詳細設計 §2-2、§8-2（S1/S3/M0 実証）、ADR-20260712-034。runnable check は `meta-harness.checks.yaml` の EV-47 に登録する
- [ ] EV-48（正常 / must）: `target=skill:<slug>` は slug・suite path を検証し、candidate / run / ledger lineage を target 付きで登録する。frontier、parent 既定選定、status、promote 前提は target ごとに分離され、既存 `frontier.json` は `claude-harness` target にだけ移行される — 根拠: 詳細設計 §4
- [ ] EV-49（異常 / must）: skill target の baseline/overlay は composition から instructions / policies / output-contracts / knowledge / scripts への参照推移閉包だけを許可し、絶対 path・`..`・symlink・repo 外 realpath・directory・閉包外 path を拒否する。`regression.enabled=false` では shared facet 変更を拒否し、`true` は cross-skill 回帰実装前には fail-closed する。promote 時に baseline composition または closure 解決入力が source_commit から変化していれば拒否する — 根拠: 詳細設計 §4、§12-1
- [ ] EV-50（境界 / must）: `purge` は全 target の frontier 候補の和集合と promoted 候補を保護し、異なる target の同名/同世代候補や lineage を混同しない — 根拠: 詳細設計 §4、§6
- [ ] EV-51（境界 / must）: scenario の `allowed_tools` は presence semantics に従う。キーなしは global allowlist、空配列は tool 権限なし、値ありはその値を `--allowedTools` とモデル公開用 `--tools` に反映し、skill slash 起動のための `Skill` は権限 allowlist へ暗黙追加しない — 根拠: 詳細設計 §2-2、§4
- [ ] EV-52（正常 / must）: skill target の scenario suite は train 1 本以上 + holdout 1 本以上を持ち、target skill の `[critical]` 正本を oracle へ明示写像し、固定 CLI 2.1.207 の headless run で slash skill を起動できる。handoff / issue-create は `max_output_tokens=1024` と最小 tool 公開範囲で、複数 request を含む run 全体が broker の $3 budget 内に完了する — 根拠: 詳細設計 §4-1
- [ ] EV-53（異常 / must）: `regression.enabled=true` の実装後は shared facet を参照する全 cross-skill scenario が pass しなければ candidate を frontier/promote 対象にしない。PR1 ではこの設定を受理せず fail-closed する — 根拠: 詳細設計 §4
- N/A: hook 型の類型別観点（PreToolUse/PostToolUse ブロック挙動等）は本パッケージが hook を持たないため非該当。config-loading への依存のみが hook 型的性質であり、EV-22 でカバーする

## 5. テストレビュー判断基準（パッケージ固有）

- reward hacking 対策系のテスト（evaluator/シナリオの hash 照合、judge の `--bare` 隔離）は、
  「候補が evaluator 自体を改変してもスコアが変化しない」ことを実証するテストになっているかを
  確認する。単に hash が記録されることだけを確認するテストは不十分であり、hash 不一致時に
  評価が拒否される、または警告が出る挙動まで検証すること。
- evaluator のヘッドレス実行（`claude -p` 呼び出し）をモックするテストは、モックが実際の
  `stream-json` の `result` イベント形状（cost フィールド一式を含む）を模していることを確認する。
  モックが実 CLI の出力形状と乖離していないかは、詳細設計 §8 スパイクチェックリスト項目3の
  実測結果と突合すること。
- self-report 欠落時のペナルティ（EV-23）のテストは、「欠落時のスコアが完全な self-report を
  伴う低品質な結果のスコアを上回らない」ことまで確認し、reward hacking 耐性を実証する構成である
  ことが望ましい。
- lock 競合・run_id 衝突のテストは実時間 sleep に依存せず mtime/PID 注入で決定論的に行うこと。
