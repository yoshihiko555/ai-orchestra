# meta-harness 評価セット

**パッケージ**: `packages/meta-harness`
**類型**: 主: CLI ツール型、副: hook 型（config-loading レイヤリングへの依存）
**作成日**: 2026-07-06
**最終レビュー日**: 未レビュー（作成時はパッケージ実装前だったが、現在は Phase 1〜3（計測・評価 / propose・promote / 自動探索 loop）実装済み。評価観点自体の人間レビューは未了）
**情報源**: docs/design/meta-harness.md（基本設計）, docs/design/meta-harness-detailed.md（詳細設計 §1〜§14）, docs/design/meta-harness-proposer-routing-unlock.md, docs/requirements/meta-harness.md, docs/adr/ADR-20260706-032.md, docs/adr/ADR-20260716-041.md, docs/adr/ADR-20260717-042.md, docs/adr/ADR-20260726-045.md

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
| `evaluate`                           | `--candidate <id> [--scenario <id>...] [--repeat N]`           | own の `run_completed`、影響 skill の `regression_run_completed`、バッチ単位の `evaluation_completed` を ledger に追記 | attempt ごとの一時 worktree 作成・除去、`events.jsonl.gz` 等の成果物書き込み |
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
- [ ] EV-16（正常 / must）: routing-config 以外の Pareto 判定は `quality_mean(A) ≥ quality_mean(B) かつ cost_mean(A) ≤ cost_mean(B) かつ少なくとも一方が厳密` の場合に A が B を支配すると正しく判定する。`cost_mean` は `frontier.cost_axis` で選んだ run field から算出する — 根拠: 詳細設計 §3-5
- [ ] EV-17（境界 / must）: quality_mean・tokens_mean が完全に同率の候補同士は `quality_min` が高い方を優先するタイブレークが機能する — 根拠: 詳細設計 §3-5
- [ ] EV-18（正常 / must）: own non-holdout シナリオのいずれかで `verdict=fail` または `error` の候補は frontier から除外される。regression error も EV-54 の budget-latch-only 例外を除いて除外する。result event 欠落時に `extract_cost()` が `ZERO_COST` を返しても、headless outcome guard が `verdict=error` を強制し、`verdict=pass` / frontier eligible にはならない — 根拠: 詳細設計 §2-5、§3-5、§4-1
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
- [ ] EV-46（異常 / must）: candidate scenario の実行隔離は、対象 worktree・実行単位 runtime・必要 system/tool だけを許可し、実 HOME・store・実 repo・sibling worktree・global tmp 等のユーザー領域を読めず、親 env（`ANTHROPIC_API_KEY` 等の secret）を継承しない。Docker daemon 不在・イメージ pin 不一致・broker 起動失敗は worktree 作成前に fail-closed し、非隔離実行へ降格しない。scenario image は既定でもnon-rootで、preparation / oracle は read-only/no-network の別コンテナで独立 Git snapshot を参照する。artifact は `openat(O_NOFOLLOW)`・regular file・サイズ上限で検査する。container/network cleanup後の不在確認は明示的な missing-object 応答だけを成功扱いにする — 根拠: 詳細設計 §2-2、§8-2、ADR-20260712-035（ADR-20260711-033/033 を置換）
- [ ] EV-47（正常 / must）: 資格情報は候補コンテナに置かない。ephemeral broker が Claude Max OAuth を保持し、コンテナ内 Claude CLI は `ANTHROPIC_BASE_URL` 経由で broker に到達する。broker は dummy キーを実 Bearer に差し替え、`anthropic-beta` は broker 固定 OAuth feature と pin 済み CLI の既知 feature allowlist の和集合だけを中継する（未知・重複・不正値は拒否し、従量課金 API key へ fallback しない）。broker 自身の egress は api.anthropic.com に限定される。Claude CLI が送る `/v1/messages?beta=true` はqueryを保持して中継し、それ以外のqueryはpre-admissionで拒否してbudgetをラッチしない。scenario コンテナは internal network で直 egress を持たない。broker は run スコープで起動・破棄し、token はコンテナ tmpfs 注入後に即 unlink される。**broker は per-run 予算・累積token envelopeを独立に強制する**（候補 hooks/Bash が broker へ直接 `/v1/messages` を投げても、入力 token 上限を `ceil(request body bytes / input_bytes_per_token)` で安全側に見積もり、出力 token 上限との和が残余を超えれば上流送信前に拒否する。係数は layered config で変更でき、既定値は 3。scenario の `budget.max_total_tokens` 指定は当該 run の累積 envelope を上書きし、未指定時は全体既定 500000 を使う。実 `usage` 積算の上限超過もラッチして後続呼び出しを拒否する）。正常run中はhealth keepaliveとactive stream activityでidle誤停止を防ぎ、host消失時はidle/absolute lifetimeで自己終了する。budget/anomaly metrics は失敗経路でもcleanup前に保存し、いずれかがtrueならattempt全体をerrorにする — 根拠: 詳細設計 §2-2、§8-2（S1/S3/M0 実証）、ADR-20260712-035、Issue #356。runnable check は `meta-harness.checks.yaml` の EV-47 に登録する
- [ ] EV-48（正常 / must）: `target=skill:<slug>` は slug・suite path を検証し、candidate / run / ledger lineage を target 付きで登録する。frontier、parent 既定選定、status、promote 前提は target ごとに分離され、既存 `frontier.json` は `claude-harness` target にだけ移行される — 根拠: 詳細設計 §4
- [ ] EV-49（異常 / must）: skill target の baseline/overlay は composition から instructions / policies / output-contracts / knowledge / scripts への参照推移閉包だけを許可し、絶対 path・`..`・symlink・repo 外 realpath・directory・閉包外 path を拒否する。`regression.enabled=false` は shared facet 変更を拒否し、`true` は closure 全体を許可する。promote 時に baseline composition または closure 解決入力が source_commit から変化していれば拒否する — 根拠: 詳細設計 §4、§12-1
- [ ] EV-50（境界 / must）: `purge` は全 target の frontier 候補の和集合と promoted 候補を保護し、異なる target の同名/同世代候補や lineage を混同しない — 根拠: 詳細設計 §4、§6
- [ ] EV-51（境界 / must）: scenario の `allowed_tools` は presence semantics に従う。キーなしは global allowlist、空配列は tool 権限なし、値ありはその値を `--allowedTools` とモデル公開用 `--tools` に反映し、skill slash 起動のための `Skill` は権限 allowlist へ暗黙追加しない。`permission_mode`（`acceptEdits` / `bypassPermissions` の enum、schema-enforced）も同じ presence semantics に従う: scenario にキーがあればその値を `--permission-mode` に反映し、なければ `evaluate.permission_mode`（既定 `acceptEdits`）にフォールバックする。`.claude/` 等の protected path は allow ルール（`Edit(path)` / `Write(path)`）では書込み解除できないため（Claude Code protected-path チェックは allow ルール評価より先に実行される）、当該 path への書込みが必要な scenario だけが `bypassPermissions` を明示 opt-in する。`bypassPermissions` は安全関連設定であるため、実効値と決定根拠（`scenario` / `global`）は `allowed_tools_source` 等と同様に run metadata（`permission_mode` / `permission_mode_source`）へ監査痕跡として永続化される — 根拠: 詳細設計 §2-2、§4、Issue #261 PR6、PR #273 bot レビュー追撃対応
- [ ] EV-52（正常 / must）: skill target の scenario suite は train 1 本以上 + holdout 1 本以上を持ち、target skill の `[critical]` 正本を oracle へ明示写像し、固定 CLI 2.1.207 の headless run で slash skill を起動できる。各シナリオは `max_output_tokens` をシナリオ側で明示宣言し（上限 4096）、最小 tool 公開範囲で、複数 request を含む run 全体がシナリオ宣言の broker budget（handoff は $3 / issue-create・issue-fix 系は実測較正で $15 + `max_total_tokens` 1.5M）内に完了する — 根拠: 詳細設計 §4-1、PR #279/#360/#361 の較正実測
- [ ] EV-53（境界 / must）: `regression.enabled=true` は skill target の baseline closure 全体（shared facet を含む）を overlay allowlist とし、`false` は専有 facet allowlist に縮退する — 根拠: 詳細設計 §4、§4-1
- [ ] EV-54（異常 / must）: non-holdout evaluate は影響 skill の train critical を own critical と hard gate 合成する。ただし broker の `budget_rejected_count > 0` と budget latch に整合する metrics reason を持ち、失敗した critical check や独立した hard error がない error run だけで構成され、fail run を含まない regression suite は評価不能として中立化し、suite verdict は error のまま `budget_latched_suites` に記録する。latch-only なら frontier を通し、latch/non-latch error 混在・独立 hard error・回帰 fail・非 latch error・own run 後に中断した `evaluation_completed` 不在バッチは frontier に含めない。regression run は own quality/cost 軸へ混入しない — 根拠: 詳細設計 §4-1、ADR-20260720-044
- [ ] EV-55（異常 / must）: promote は同一 `evaluation_id` の holdout バッチで own と全影響 suite が pass し、skill suite の全 holdout scenario × `repeat_frontier` の run が揃う場合だけ通す。`budget_latched_suites` に記録された suite も per-suite verdict は error のため拒否し、同フィールド自体も holdout/train（non-holdout）の両 batch で明示検査して非空なら直接拒否し、suite A=fail 後の B=pass や `--scenario` / `--repeat 1` の部分評価を通さず、own/回帰 suite hash と evaluator hash は昇格時の現在値を照合する — 根拠: 詳細設計 §4-1、§12-1、ADR-20260720-044
- [ ] EV-56（境界 / must）: impact context は pre-overlay baseline の影響 skill 集合・逆写像入力 hash・base commit を記録し、promote が最新 `origin/main` で再計算した値と不一致なら再評価を要求する。suite 不在 skill は `unverified_impacts` として継続し PR 本文に警告するが、評価後に suite が追加された場合は stale として拒否する — 根拠: 詳細設計 §4-1、§12-2
- [ ] EV-57（異常 / must）: `regression.max_affected_suites` / `regression.max_budget_usd` 超過は evaluation error になり、train/holdout は同じ残予算を共有する。broker 経由 judge を含む保守的費用を回帰上限と loop `budget_usd` に算入し、`regression.*` の変更は evaluator hash を変えて旧評価を stale にする。同様に `judge.tool` / `judge.model` / `judge.effort`、`evaluate.isolation.broker.pricing_upper_bound_usd_per_million`、`evaluate.isolation.broker.input_bytes_per_token`、`evaluate.isolation.broker.max_total_tokens`、`scenario_run.max_budget_usd_default` の変更もコスト比較可能性に影響するため evaluator hash を変え、config だけの再較正（Issue #261 PR2、Issue #356）でも旧評価を stale にする。broker model allowlist については、hash に効くのは pin された実効 allowlist（`evaluate.model` / `judge.model`。両者は同一値に pin される必須制約があるため実質1値）の変更のみであり、`evaluate.isolation.broker.model_allowlist` のうち pin 外のメニュー余剰エントリ（価格未較正のため broker には配線されない）の追加/削除は evaluator hash に影響しない（テストで明示、Issue #261 PR2 review round 3-4） — 根拠: 詳細設計 §4-1、§13
- [ ] EV-58（正常 / must）: `regression_run_completed` と `evaluation_completed` は append 前に schema 検証され、回帰 attempt ごとに own run と独立した worktree を作成・破棄する。同名 scenario は `(suite_id, scenario_id)` で区別する — 根拠: 詳細設計 §1-2、§2-1、§4-1
- [ ] EV-59（正常 / must）: Docker CLI・security profile・broker/resource lifecycle を `docker-runtime` へ抽出した後も、meta-harness の既存 command、例外、cleanup、capability gate の振る舞いを変更しない。共有 runtime source は evaluator hash の入力へ含める — 根拠: `docs/design/loop-harness-isolation.md` §8 Phase 0
- [ ] EV-60（境界 / must）: `image_pin` の version token は Docker build-arg に渡す前に semver-only allowlist で検証し、injection payload と malformed version を拒否する — 根拠: `packages/meta-harness/lib/scenario_docker_image.py`（`ensure_scenario_image`、`ensure_images` 経由で呼び出される）が委譲する共有実装 `docker_runtime_cli.version_from_pin`（`packages/docker-runtime/lib/docker_runtime_cli.py`。loop-harness アダプタも同じ実装に委譲する — EV-114）
- [ ] EV-61（正常 / must）: meta-harness の `broker_env()` は移行期間中、同値の `DR_BROKER_*` / `DR_PRICE_*` と `MH_BROKER_*` / `MH_PRICE_*` を同時に送り、`DR_BROKER_NAMESPACE=meta-harness` を明示する。新しい共有 broker は `DR_*` を使用し、digest pin 済み旧 broker image は従来の `MH_*` を使用できる — 根拠: `docs/design/loop-harness-isolation.md` §6、ADR-20260715-039
- [ ] EV-62（境界 / must）: config patch allowlist は `"<file>#<key_path>"` を厳密に parse し、exact match と 1-segment `*` だけを許可する。allowlist 外 file/key、`**`、部分 wildcard、複数 `#`、空/危険セグメント、1 segment を超える wildcard 一致、同一実体 key の重複は拒否する — 根拠: 詳細設計 §1-8
- [ ] EV-63（異常 / must）: `.local.yaml` を含む実効 `config_patch.allowlist` はコード定数 ceiling の部分集合だけを許可し、3 種以外を追加した設定では候補内容に関係なく fail-closed する — 根拠: 詳細設計 §1-8、§5
- [ ] EV-64（異常 / must）: non-empty config patch の `created_by` は frozen per-key map で検証し、human は ceiling 3 kind、proposer は `agents.*.tool` / `antigravity.model` だけを通す。未知作成者と map 未定義 key は共通 validator で拒否する — 根拠: 詳細設計 §1-8、ADR-20260717-042
- [ ] EV-65（正常 / must）: `propose --target routing-config` と `loop --target routing-config` は Phase A guard 成立後に config-patch proposal 経路へ到達し、guard 不成立・引用可能な non-holdout run 不在・cooldown 中は proposer 起動前に exit 2 で fail-closed する — 根拠: 詳細設計 §4-3、§11、§13、ADR-20260717-042
- [ ] EV-66（境界 / must）: allowlisted value は `agents.*.tool` の 4 enum または charset / YAML round-trip 検証済みの非空 model slug だけを許可し、数値/bool/未知 tool/空文字/charset 違反を拒否する。`codex.model_allowlist` / `antigravity.model_allowlist` が未定義または空の場合も含め、対応 model の membership を常に必須とする（fail-closed） — 根拠: 詳細設計 §1-8
- [ ] EV-67（異常 / must）: `routing-config` ⇔ non-empty config patch の双方向排他と empty file overlay を human register・proposer register（第 5 entry point）・evaluate の worktree 変更前・promote preflight で同じ validator 契約として強制し、mixed overlay+patch や target 不一致を拒否する — 根拠: 詳細設計 §1-8、§2-1、§11、§12-1
- [ ] EV-68（異常 / must）: canonical `config-patch.json` の integrity hash は候補全体の `config_hash` chain に含まれ、register 後に sidecar を改ざん・欠落させると evaluate と promote の双方が worktree/PR 変更前に拒否する — 根拠: 詳細設計 §1-8、§12-1
- [ ] EV-69（正常 / must）: evaluate は検証済み patch を評価 worktree 内の `.claude/config/agent-routing/cli-tools.local.yaml` だけへ deterministic に deep merge し、`load_cli_tools_config()` が patch 値と未指定 base 値を解決する。developer checkout や worktree 外は変更しない — 根拠: 詳細設計 §2-1
- [ ] EV-70（境界 / must）: `routing-config` suite は `scenarios/routing-config/` に train 1 本以上 + holdout 1 本以上を必須とし、どちらかを欠く suite を拒否する — 根拠: 詳細設計 §4-3
- [ ] EV-71（正常 / must）: `routing-config` 候補は専用 `frontier-routing-config.json` を使い、他 target の frontier/cache/parent/status を汚染しない — 根拠: 詳細設計 §4-3
- [ ] EV-72（正常 / must）: promote は promotion worktree 作成後に `packages/agent-routing/config/cli-tools.yaml` と tracked mirror `.claude/config/agent-routing/cli-tools.yaml` の intended scalar だけを編集し、再 parse/deep-equality と最終 byte-equality を満たす。developer checkout と `*.local.yaml` は変更しない。tracked mirror 書き込み後、promotion worktree の `.claude/orchestra.json` に `file_hashes["agent-routing"]["config/agent-routing/cli-tools.yaml"]` エントリがあれば、それをパッチ後の実バイト列の hash へ更新し直す（`sync_engine.is_user_modified()` の誤判定防止、PR #244 と同種）— 根拠: 詳細設計 §12-2、ADR-20260716-041
- [ ] EV-73（異常 / must）: evaluate 時に記録した agent-routing SSOT content hash と promotion base の現在 hash が異なる routing-config 候補は stale evaluation として PR 作成前に拒否する — 根拠: 詳細設計 §12-1
- [ ] EV-74（異常 / must）: promote-time L3 secret scan / canary re-scan は全 lineage の manifest/overlay に加えて `config-patch.json` sidecar と適用後 YAML diff を走査し、secret/canary hit では commit/push/PR を行わない — 根拠: 詳細設計 §12-1、§11-3-6
- [ ] EV-75（正常 / must）: config-patch-only routing-config candidate の effective impact は全登録 `skill:*` target + `claude-harness` となり、own suite の mechanical / behavioral critical と suite-bearing impact の cross-skill regression critical を hard gate として合成する — 根拠: 詳細設計 §4-1、§4-3
- [ ] EV-76（境界 / must）: `evaluation_completed` の ledger event schema と run metadata schema は `target == "routing-config"` のときだけ `routing_config_base_hash` を必須とし、他 target では任意のままとする — 根拠: 詳細設計 §1-4、§4-3、ADR-20260716-041
- [ ] EV-77（異常 / must）: evaluate（overlay / patch 適用前）と promote preflight は lineage 内の各候補の `manifest.json` の `created_by` / `target` を、その候補の immutable `candidate_registered` ledger event と突合し、不一致または event 不在を拒否する — 根拠: 詳細設計 §1-2、§2-1、§12-1
- [ ] EV-78（正常 / must）: routing-config 候補の promote 鮮度チェックは、SSOT content hash と global impact context（全登録 skill + claude-harness、input hash、unverified 集合）を最新 `origin/main` で再計算する。いずれかの drift は再評価を要求し、suite 不在 target 自体は warning-only とする — 根拠: 詳細設計 §4-1、§12-1
- [ ] EV-79（異常 / must）: `meta-harness.yaml` / `meta-harness.local.yaml` が実在するのに読み込めない（YAML 破損等）場合、`config_patch.allowlist` はコード内蔵 DEFAULTS の 3 エントリへフォールバックせず空配列として扱う（config patch は fail-closed）。ファイル不在（設定なし）の場合は通常どおり DEFAULTS の 3 エントリが有効なままである — 根拠: 詳細設計 §1-8、§5
- [ ] EV-80（境界 / must）: per-key `allowed_created_by` map は frozen code constant であり runtime config から変更できない。proposer の `agents.*.tool` / `antigravity.model` は通過し、proposer の `codex.model`、未知 `created_by`、map に無い ceiling entry は fail-closed に拒否する — 根拠: proposer routing unlock 設計 C-7、詳細設計 §1-8
- [ ] EV-81（境界 / must）: `codex.model` patch は `codex.model_allowlist` membership を必須とし、allowlist 外値・未定義・空配列を全拒否する。初期 allowlist は現在設定中の model だけである — 根拠: proposer routing unlock 設計 C-1、詳細設計 §1-8
- [ ] EV-82（正常 / must）: routing-config の dominance は quality-strict で、equal quality + lower cost の候補は baseline を支配しない。quality が厳密に高く cost が非増加の場合だけ支配し、routing-config 以外は EV-16 の従来 semantics を維持する — 根拠: proposer routing unlock 設計 C-2、詳細設計 §3-5
- [ ] EV-83（異常 / must）: `frontier.cost_axis` の既定は全 target 共通の `total_cost_usd` である。選択 field を欠く run が 1 件でもあれば `MetaHarnessRootError` を raise し、0 補完・point 除外・旧 `total_tokens` への fallback を行わない — 根拠: proposer routing unlock 設計 C-3、詳細設計 §3-4〜§3-5
- [ ] EV-84（正常 / must）: routing-config の `candidate_impact_context` は overlay path に関係なく、登録済み composition から列挙した全 `skill:*` target と `claude-harness` を返す。低レベル `resolve_skill_impacts` の facets-only semantics は変更しない — 根拠: proposer routing unlock 設計 C-5、詳細設計 §4-1
- [ ] EV-85（異常 / must）: global impact の suite 不在 target は `unverified_impacts` として promote PR 本文へ全件 warning 表示する一方、suite-bearing target の resolution failure・run fail/error・run 不足・hash 不一致は評価または promotion の hard gate になる — 根拠: proposer routing unlock 設計 C-5（2026-07-17 修正）、詳細設計 §4-1、§12
- [ ] EV-86（正常 / must）: 現在の coverage（skill composition 22 件、skill suite 6 件、claude-harness を含む suite-bearing global impact 7 件）でも、own suite と suite-bearing global impact を通過し、残りを unverified warning とした現実的な routing-config 候補が frontier / holdout / freshness / integrity の全 precondition を満たして promotion path に到達できる — 根拠: proposer routing unlock 設計 §6 決定3、詳細設計 §4-1、§12
- [ ] EV-87（境界 / must）: proposer routing-config 候補は ceiling entry で定義した 1 key kind だけを含められ、同 kind の複数 item は許可し mixed kind は拒否する。human 候補は mixed kind を許可する — 根拠: proposer routing unlock 設計 C-6、詳細設計 §1-8
- [ ] EV-88（境界 / must）: loop は 1 iteration につき routing-config 候補を最大 1 件だけ生成し、直近候補の evaluation reject または overfit retire 後は既定 3 round の cooldown を ledger から再構築して強制する。resume や proposer retry で cap/cooldown を迂回できない — 根拠: proposer routing unlock 設計 C-6、詳細設計 §13
- [ ] EV-89（異常 / must）: proposer の第 5 entry point は config patch を canonical sidecar + integrity hash に変換した後、`created_by=proposer` で `register_candidate` と共通 validator を再利用する。proposal/候補の patch XOR overlay、allowlist/ceiling/value/menu/hash のいずれも迂回できない — 根拠: proposer routing unlock 設計「実装規律」、詳細設計 §1-9、§11
- [ ] EV-90（正常 / must）: routing-config suite は mechanical scenario を維持した上で behavioral train / holdout を各 1 本以上持ち、materialized routing 値の変更が deterministic oracle outcome を反転させる。scenario container 内で codex/agy を起動せず、internal network + Anthropic broker 制約を守る — 根拠: proposer routing unlock 設計 C-8、詳細設計 §4-3
- [ ] EV-91（異常 / must）: judge invariance は ceiling 全 entry が `agent-routing/cli-tools.yaml` のみに属して `meta-harness.yaml#judge.*` と交差しないこと、routing-config suite に `rubric_judge` が無いこと、judge config resolution が agent-routing config を読まないことを保証する — 根拠: proposer routing unlock 設計 C-4、詳細設計 §3-3
- [ ] EV-92（異常 / must）: adversarial dry-run で equal quality + lower cost の「全 agent を最安 tool へ切替」候補は baseline を支配せず、behavioral quality drop 時は frontier から除外される。mixed-kind patch と proposer `codex.model` patch は register 時点で拒否する — 根拠: proposer routing unlock 設計 Phase A entry criteria、詳細設計 §1-8、§3-5
- [ ] EV-93（異常 / must）: `permission_mode: bypassPermissions` を持つ scenario（task-state 4 本
  ［`mark-task-done` / `record-architecture-decision-holdout` / `add-phase-with-agreed-ac` /
  `add-phase-direct-call-confirms-ac-holdout`］・handoff 2 本の計 6 本）は、collateral-scope
  oracle（`assert-task-state-outcome.py collateral-scope`）を最低 1 件の critical check として
  必ず併設する（全 scenario yaml の機械的走査で強制）。この oracle は (a) tracked file の変更・
  追加・削除・rename/copy を `git diff --name-status -z` で検出し、`allowed_paths` に無いもの、
  または staged 追加（status `A`）以外で `allowed_new_prefixes` に一致しないものを fail にする、
  (b) untracked file の生成を `git status --porcelain -z --untracked-files=all` で検出し、
  `allowed_paths`/`allowed_new_prefixes` の scope 外を fail にする（scope 外の untracked を
  無視する早期 return は存在しない — 常時実行）、(c) 検出した全パス（tracked・untracked 双方）
  について symlink（`Path.is_symlink()`、追跡しない）を無条件で unexpected 扱いにし、許可
  path/prefix に一致していても fail にする（`ln -s <既存ファイル> <許可 prefix 配下>` による
  パスのみ許可・内容偽装の迂回を防止）。tracked/untracked いずれのパース（`-z` 出力）も空白・
  引用符付きパスと rename/copy の複数トークン record を正しく扱う。(d) opt-in の
  `--ignored-scan-prefix`（fixture 引数 `ignored_scan_prefixes`）を指定した場合、git-ignored
  file の新規出現もスキャン対象に加え、baseline snapshot 作成時（`_prepare_isolated_git`）に
  記録した ignored file 一覧（`runtime_state_dir/ignored-baseline.json` を oracle コンテナへ
  `/runtime/ignored-baseline.json` として read-only マウントし、`--ignored-baseline-file` で
  渡す）との差分で判定するベースライン差分方式を取る — baseline 記録済みの既存 ignored file
  （実リポジトリの tracked-but-ignored 由来等）は容認し、baseline 以降に新規出現した ignored
  file のみ fail にする（collapsed directory は baseline にその配下のパスがある場合のみ個別
  ファイルへ展開して差分判定するため、既存 ignored dir 配下への新規ファイル追加も検出される）。
  `--ignored-baseline-file` が未指定（省略）の場合は空集合扱いとして全 ignored file を fail に
  する従来の厳格判定へ fail-closed で戻る。一方、`--ignored-baseline-file` を明示指定した
  にもかかわらずそのファイルが不在の場合は同じ空集合フォールバックには落とさず、配線破損
  （bind mount 欠落・`_prepare_isolated_git` 書き出し失敗等）のシグナルとして明示的に
  `ValueError` で fail-loud にする（JSON 不正時の明示エラーと同系統。PR #351 bot レビュー
  指摘対応）。baseline JSON を書き出す `_prepare_isolated_git` 自身も、oracle コンテナが
  非 root `--user {uid}:{gid}` で read-only mount するファイルを読めるよう、書き込み直後に
  `ignored-baseline.json` のパーミッションを明示 chmod する（`container_paths` に応じ
  0o644/0o600 固定。同 PR #351 レビュー指摘、host umask 依存の読み取り不能を防止）。同様に
  snapshot repo 本体（`git-snapshot/` 配下の全ディレクトリ・ファイル・index）も、`container_paths`
  の場合は commands 実行と ignored-baseline 収集の完了後に再帰的に container-readable なモード
  （dir 0o755 / file 0o644）へ pin する。以前は `rev-parse [--short] HEAD` の完全一致引数だけが
  snapshot を経由しない fast path で `source_commit` を返していたため host umask に依存せず
  読めていたが、Issue #357 でその fast path を廃し常に snapshot 実体を読む形へ統一したことで、
  host が root かつ厳格な umask（例: 077）の場合に非 root コンテナから snapshot 自体が読めなく
  なる新規の regression が生じたため（Issue #357 bot レビュー指摘対応）。
  baseline 記録の有無に関わらず (c) の symlink 無条件拒否は優先されたまま変わらない —
  ディレクトリを指す symlink が丸ごと ignore された collapsed ディレクトリ配下にある場合も、
  ベースライン記録側・オラクル走査側いずれの collapsed directory 展開ヘルパーも symlink な
  サブディレクトリ自身のパスを記録し、リンク先へは決して降下しない
  — 根拠: 詳細設計 §2-2、§4、ADR-20260714-038「再検討記録（bypassPermissions 例外）」
  「再検討記録（Issue #297 / PR #326 — bypassPermissions 対象を 4→6 シナリオへ拡張）」、
  PR #273 全レビューラウンド、Issue #350「再検討記録（ベースライン差分方式への変更）」、
  PR #351 bot レビュー指摘対応、Issue #357 bot レビュー指摘対応（snapshot repo 本体の
  container-readable pin 追加）
- [ ] EV-94（正常 / must）: 永続イメージ manifest 経由の image 再利用 — `ensure_scenario_image` / `ensure_broker_image` は `ImageRecipe`（context hash・build_args・docker_label・platform/target）から導出した recipe hash が manifest 記録済みで、かつ manifest 記録の `image_id` が現在 Docker に存在する image と一致する限り再ビルドを行わず、`last_used_at` のみを更新して同じ tag を返す。recipe が変化した場合、または manifest 記録と実際の Docker image が乖離（drift）している場合は再ビルドする — 根拠: `docker_runtime_image.ensure_recipe_image`（`packages/docker-runtime/lib/docker_runtime_image.py`）への委譲、`docs/design/loop-harness-isolation.md` §8 Phase 0
- [ ] EV-95（境界 / must）: namespace 分離 — meta-harness の scenario/broker image は Docker label `ai.orchestra.meta-harness`、manifest/lock パス `.claude/meta-harness/docker-image-cache.json` / `.claude/meta-harness/docker-image-build.lock`、専用 buildx builder `meta-harness-builder` を既定として使用し、loop-harness（`ai.orchestra.loop-harness` / `.claude/loop/*` / `loop-harness-builder`）と独立した名前空間を形成する。世代 prune は同一 family（Docker label + repository）の image だけを対象とし、他プロジェクトが所有する image を削除しない — 根拠: `scenario_docker_image.py`（`DOCKER_LABEL` / `DEFAULT_MANIFEST_PATH` / `DEFAULT_LOCK_PATH` / `DEFAULT_BUILDER_NAME`）、`docs/design/loop-harness-isolation.md` §8
- [ ] EV-96（異常 / must）: `auto_build_images: false` の fail-closed は永続イメージライフサイクル経由でも維持される — immutable な `@sha256:` digest 参照でない image 設定（タグのみ・digest 欠落等）は、共有 `ensure_recipe_image` の immutable-image 経路で拒否され、ビルドへフォールバックしない — 根拠: EV-27 の拡張、`docker_runtime_image._ensure_immutable_image`
- [ ] EV-97（異常 / must）: image ensure 完了後の `image_pin` 突合 — manifest 再利用・新規ビルドいずれの経路で確保された image でも、確保後に取得した実際の Claude CLI バージョンが設定済み `image_pin` と一致しない場合、`ensure_scenario_image` は capability gate（EV-27）到達前に fail-closed する — 根拠: `scenario_docker_image.ensure_scenario_image`（共有実装 `docker_runtime_cli.version_matches` 経由）、詳細設計 §2-7
- [ ] EV-98（正常 / must）[2026-07-26 追加（Issue #231 Phase 2）]: scenario/broker image の family（`ai.orchestra.meta-harness` label）は、共有 `ensure_recipe_image` の opportunistic stale cleanup（中断ビルドで manifest 未登録のまま残った pending タグの回収、owner label 付き dangling image の回収、pending journal に記録のない他プロジェクトのタグ付き image は削除しない）をそのまま経由する。meta-harness 側は独自の cleanup ロジックを持たず、共有実装の契約に委譲する — 根拠: `docker_runtime_image.ensure_recipe_image`（`packages/docker-runtime/lib/docker_runtime_image.py`）への委譲、ADR-20260726-045、`docs/design/loop-harness-isolation.md` §5.4。対応テスト（共有実装側）: `packages/docker-runtime/tests/test_docker_runtime_image.py::test_cleanup_reclaims_orphaned_tag_left_by_a_pending_build`, `::test_cleanup_removes_dangling_owner_labelled_image`, `::test_cleanup_leaves_other_projects_tagged_image_alone`, `::test_build_failure_triggers_best_effort_pending_tag_cleanup`
- [ ] EV-99（異常 / must）[2026-07-26 追加（Issue #231 Phase 2）、2026-07-26 契約修正（PR #320 レビュー指摘）]: `scenario_docker_cli.ensure_images_detailed()` は `ensure_recipe_image` が返した検証済み `EnsuredImage`（`image_id` を含む）をそのまま呼び出し元へ返す。`run_preparation_command`/`_start_broker` はこの `EnsuredImage.image_id` を直接使い、container 起動前にタグを再 inspect しない。共有タグへの並行 retag が起きても、ensure が確保した image と異なる image を誤って起動対象に解決しない（TOCTOU 解消）。旧 `ImageCache.trusted_image_ids` への登録による解消は #317 のリファクタで `ensure_images_detailed()` の戻り値直接使用に置き換えられ、当該 alias は削除済み — 根拠: `scenario_docker_cli.ensure_images_detailed`（`packages/meta-harness/lib/scenario_docker_cli.py`）、ADR-20260726-045。対応テスト: `packages/meta-harness/tests/test_scenario_docker.py::test_ensure_images_detailed_propagates_verified_image_id`
- [ ] EV-100（異常 / must）[2026-08-10 追加（Issue #362）]: rubric judge は host / Docker の両実行経路で Claude CLI の built-in tool 集合を空にし、信頼側 prompt でもファイルを読まず、rubric の参照先と候補の canonical final response を含む与えられた artifact 抜粋のみで判定するよう指示する。候補由来データの nonce delimiter 隔離と symlink 非追従は維持する — 根拠: Issue #362、詳細設計 §3-3
- [ ] EV-101（境界 / must）[2026-08-10 追加（Issue #362）]: 有効な judge backend（`claude-bare`）が設定されている場合、required-evidence 集合を rubric の正規表現抽出パス ∪ canonical `.claude/meta-harness-oracle/final-report.md` として重複排除する。canonical を含む全 required evidence が欠落または空なら backend を起動せず rubric check の `fail`（`error` ではない）として確定して `detail` に欠落パスを残し、一部でも取得できれば judge を起動して取得不能パスを prompt に明示する。`codex` または未知 backend は evidence 判定より先に `error` とする — 根拠: Issue #362、詳細設計 §3-3
- [ ] EV-102（境界 / must）[2026-08-10 追加（Issue #362）]: rubric judge の非ゼロ終了は、`error_max_turns`、認証・credential、budget・quota の決定論的失敗マーカーを deny-list で検出した場合は即座に `error` とし、それ以外は診断が空の場合も非空の一過性インフラ失敗の場合も同一 backend で最大 1 回リトライする。通常の rubric pass/fail と恒久的な backend 利用不能はリトライしない — 根拠: Issue #362、詳細設計 §3-3
- [ ] EV-103（撤回 / -）[2026-08-14 撤回（ADR-20260814-049）]: （撤回済み）gate/graded critical 分離は ADR-20260814-049 により実装中止。routing-config での品質分解能用途が消滅したため。skill target で必要性が再浮上した場合は新規 EV として再定義する — 根拠: ADR-20260814-049
- [ ] EV-104（境界 / must）[2026-08-14 追加（ADR-20260814-048、Issue #364）]: `loop` の収束判定（converged）は各候補を「当該候補を含む前の best_quality（best_quality(i-1) 相当。当該候補自身は算出に含めない）」と比較し、当該候補を含めた後の best（当該反復の best_quality(i) 自身）とは比較しない。これにより、best を更新した直後の候補が常に距離 0 と評価されて品質改善が連続している最中にも成立してしまう空虚な converged 停止を防ぐ。また収束判定の適格条件（`critical_pass` 集約）は verdict 基準（verdict=pass）で判定し、適格候補が収束判定から恒久的に除外されない — 根拠: ADR-20260814-048、ADR-20260814-049、詳細設計 §13-2。実装済み（Issue #364 実装 PR、2026-08-14）
- [ ] EV-105（境界 / must）[2026-08-14 追加（ADR-20260814-048、Issue #364）]: `target == "routing-config"` の quality-strict dominance 判定は `quality_mean(A) - quality_mean(B) > 1e-6` を厳密優越の条件とし、浮動小数誤差による誤った支配判定（本来同値の候補間で一方が他方を支配してしまう反転）を防ぐ数値衛生ガードを備える。これは分散を考慮した統計的 epsilon とは別物であり、統計的 epsilon の導入自体は本 ADR では見送る — 根拠: ADR-20260814-048、ADR-20260814-049 決定 3、詳細設計 §3-5。実装済み（Issue #364 実装 PR、2026-08-14）
- [ ] EV-106（境界 / must）[2026-08-14 追加（ADR-20260814-049）]: routing-config は検証専用 target であり、own suite は config patch の妥当性ゲート（materialization・layering・回帰非破壊・解決規則の検証）としてのみ機能する。C-2（quality 厳密優越 = コスト単独勝ちの禁止、1e-6 数値衛生ガード付き）によりコスト単独の自動世代交代は発生しない。品質差による dominance は機構上残るが、routing-config の own suite における適格候補間の品質差は自己申告 penalty 等の本番ルーティングと無関係な信号のみから生じるため、品質差 dominance による世代交代が観測された場合は改善ではなく異常（信号汚染）として扱い、human レビューで原因を確認する。promote は従来どおり human ゲートの判断（品質同点 frontier からの選択を含む）で行う。routing-config target への loop 運転は運用上行わない（最適化信号がなく divergence 停止しか起き得ないため）。実装は既存の C-2 分岐（EV-82 の quality-strict dominance、EV-105 の数値衛生ガード）が担保しており、loop 運転の禁止自体は運用ルールであり技術的な強制機構ではない — 根拠: ADR-20260814-049 決定 1・6
- N/A: hook 型の類型別観点（PreToolUse/PostToolUse ブロック挙動等）は本パッケージが hook を持たないため非該当。config-loading への依存のみが hook 型的性質であり、EV-22 でカバーする

### 運用メモ

- `evaluation_completed` は b92dd84 で新規追加されたイベント種別のため、それ以前に評価済みの既存候補は ledger にこのイベントを持たない。`loop_state.current_run_events`（loop 再開判定）はこの種の候補を旧来判定（attempt 完了性 + error verdict 無し）へ自動フォールバックするため壊れない。一方 `meta_harness_common.aggregate_run_points`（frontier 適格性）は意図的にフォールバックしない。own run 完了後・`evaluation_completed` 記録前に中断したバッチは、旧 ledger の未移行候補と ledger 上区別できないため、フォールバックすると EV-54 の中断バッチ排除ゲートが弱まってしまう。既存候補を frontier 対象に戻したい場合は、一度 `evaluate` を再実行して `evaluation_completed` を記録すること。

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
