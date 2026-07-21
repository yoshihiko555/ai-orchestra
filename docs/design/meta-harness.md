---
codd:
  node_id: "design:meta-harness"
  kind: design
  status: draft
  depends_on:
    - id: "req:meta-harness"
      relation: derives_from
    - id: "design:skill-evolution"
      relation: references
  owner: ai-orchestra
---

# Meta-Harness（ハーネス最適化基盤）設計ドキュメント

**作成日**: 2026-07-06
**ステータス**: draft
**対象**: `feat/meta-harness` ブランチ
**関連**: `req:meta-harness`, `adr:ADR-20260706-032`（予定）, arXiv:2603.28052, `design:meta-harness-detailed`

> CODD 注記: 本書 → ADR は依存 edge を張らない（`design:skill-evolution` の慣行を踏襲。
> ADR 側が本書を `references` する形にすることで `req → design ← adr` の循環を避ける）。

---

## 1. 概要と論文マッピング

arXiv:2603.28052 "Meta-Harness: End-to-End Optimization of Model Harnesses" は、LLM ハーネス
（プロンプト足場・メモリ・検索・ツールオーケストレーション）を population ベースで探索する
メタ最適化プロセスを提案する。論文の 4 構成要素と、この repo での対応物は以下の通り。

| 論文の構成要素                                     | この repo での対応物                                                    |
| -------------------------------------------------- | ----------------------------------------------------------------------- |
| Population（候補ハーネス群）                       | `.claude/meta-harness/candidates/` に登録される宣言的オーバーレイの集合 |
| Filesystem store（ソース・スコア・生トレース保存） | `.claude/meta-harness/` 全体（candidates/ + runs/ + ledger.jsonl）      |
| Agentic proposer（選択的検査 + 新候補提案）        | Claude Code サブエージェント（store を read してオーバーレイを生成）    |
| Evaluator                                          | シナリオスイート実行 + 二軸 judge + コストベクトル計測                  |

論文は候補を「任意の Python プログラム」として扱い、コードそのものの自由な発明を許す。本設計では
これを **宣言的オーバーレイ**（facets/instructions, facets/policies, facets/compositions, rules の
facet ソース、config の allowlist されたキー）に制約する。理由は次の 3 点である。

1. **再現性** — オーバーレイは差分として保存・適用でき、`facet build` を通せば決定的に配布物へ反映できる。任意コードは実行環境依存の副作用を持ちうる。
2. **レビュー性** — オーバーレイは diff として人間がレビューできる。任意コード生成は静的レビューが困難。
3. **安全性** — facet/config はすでにこの repo の宣言的ハーネス面であり、既存の `facet build` / `context build` / `context sync` レールに乗せられる。任意コード実行は sandbox 逸脱・秘匿情報漏洩のリスクを増やす。

この制約により、論文が示す「自由なコード発明」による探索空間の広さは失われる。将来的には、
schema 付きの operator/adapter を allowlist に追加することで、この余地を段階的に回復する
（`## 4. 候補モデル` 参照）。

---

## 2. 用語定義

| 用語                      | 定義                                                                                                |
| ------------------------- | --------------------------------------------------------------------------------------------------- |
| メタハーネス              | ハーネス候補の生成・評価・選択・昇格を回す、ハーネス最適化プロセスそのもの                          |
| ハーネス候補（candidate） | 評価対象となる 1 つのハーネス構成。宣言的オーバーレイ + 来歴メタデータの組                          |
| オーバーレイ（overlay）   | baseline に対する facet ソース/config の差分パッチ。候補の実体                                      |
| 世代（generation）        | 候補が派生した探索ループの反復回数。`parent_id` から辿れる系譜の深さ                                |
| population                | ある時点で store に存在する候補集合（candidate/evaluated/promoted/retired の全状態を含む）          |
| ledger                    | 候補登録・評価完了・状態遷移・Pareto 更新を記録する append-only イベントログ                        |
| frontier                  | ledger から算出される、品質 vs コストの Pareto 最適集合                                             |
| シナリオ                  | evaluator が候補に対して実行する固定タスク定義（プロンプト・critical checklist・budget）            |
| oracle                    | シナリオ結果の合否を機械判定する手段（artifact_exists / json_schema / command_exit / rubric_judge） |

---

## 3. ストア設計

```
.claude/meta-harness/            # gitignore 対象・SessionEnd クリーンアップ対象外
  candidates/<cand_id>/
    manifest.json                # cand_id, parent_id, generation, created_at, source_commit,
                                  # config_hash, model_versions, status(candidate/evaluated/promoted/retired)
    overlay/                     # repo 相対パスをミラーした差分ファイル群（facets/**, config allowlist キーのパッチ）
    notes.md                     # proposer の仮説・意図（人間可読）
  runs/<run_id>/                 # codex-harness artifact モデルの一般化
    metadata.json                # run_id, cand_id, scenario_id, project_root, ai_orchestra_dir,
                                  # source_commit, evaluator_hash, scenario_hash, model, started_at
    prompt.md
    events.jsonl.gz              # フルトレース（redaction 済み）
    result.json                  # schema 検証済み: スコア内訳・critical/must 判定・コスト・self_report
    report.md
  ledger.jsonl                   # append-only イベントログ（候補登録/評価完了/状態遷移/Pareto 更新）
  frontier.json                  # Pareto frontier サマリー（ledger から再生成可能なキャッシュ）
```

このレイアウトは `packages/codex-harness` の run artifact モデル（`.codex/runs/<run_id>/` に
prompt.md / metadata.json / events.jsonl / diff.patch / validation.log / final.json / report.md を
保存する設計）を一般化したものである。codex-harness が単一 CLI 実行の記録に特化しているのに対し、
meta-harness の `runs/` は候補（`cand_id`）に紐づく複数シナリオ実行を横断的に管理する点が異なる。

**immutability**: `candidates/<cand_id>/` と `runs/<run_id>/` は登録・保存後に内容を書き換えない。
再評価が必要な場合は新しい `run_id` を発行する。候補自体の改訂は新しい `cand_id`（`parent_id` で
系譜を保持）として登録する。

**retention/purge**: `orchex meta purge` で古い世代・`retired` 状態の候補を削除できる。既定では
直近 N 世代・frontier 上の候補・`promoted` 済み候補は purge 対象から除外する。

**圧縮**: `events.jsonl` はフルトレースであり肥大化しやすいため `.jsonl.gz` で保存する。

**`.claude/context/` との違い**: `context-sharing` の `.claude/context/` はセッションスコープで
`SessionEnd` に削除される（NF-05, `context-sharing.md`）。meta-harness のストアは複数セッション・
複数世代にまたがる永続的な比較を目的とするため、性質が根本的に異なる。skill-evolution が
`metrics/` / `lessons/` を `.claude/context/` と分離した保存先に置いたのと同じ理由で、
`.claude/meta-harness/` は独立ディレクトリとし、gitignore はするが SessionEnd クリーンアップの
対象には含めない。

### worktree 運用との関係

このリポジトリは main から `.worktrees/<name>` を切って機能開発する運用である。この前提のもと、
ストアと評価用 worktree の配置は次の通り確定する。

- store（`.claude/meta-harness/`）と評価用 worktree（`.worktrees/meta/`）は、実行元がどの
  worktree であっても**常にメイン（root）worktree のルート配下**に解決される
  （`git rev-parse --git-common-dir` ベースで導出。詳細は `design:meta-harness-detailed` §2-0）。
  feature worktree（例 `.worktrees/feat-x/`）内から `orchex meta` を実行しても、蓄積データは
  その feature worktree の寿命（削除タイミング）に依存しない。
- 評価用 worktree は候補の `source_commit`（コミット済み tree）から `--detach` で作成され、
  feature worktree の作業状態（未コミットの変更）を一切参照しない。そのため「feature 作業中に
  改善候補が見つかる → さらに worktree が切られる」という worktree の連鎖は発生しない。
- 改善候補（proposer が生成したもの、または手動登録したもの）は store への登録に留まり、実ファイル
  への反映は `promote` による PR 経由でのみ行われる。PR は通常の main → worktree → PR フローに
  合流するため、meta-harness 独自のマージ経路を新設しない。

---

## 4. 候補モデル（オーバーレイ仕様）

候補が変更できる対象面は次に限定する。

| 対象面                   | 内容                                                                                                                                      |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `facets/instructions/**` | 指示書ソース（`templates/context/*.md` 生成元を含む）                                                                                     |
| `facets/policies/**`     | ポリシー facet ソース                                                                                                                     |
| `facets/compositions/**` | facet 合成定義                                                                                                                            |
| rules の facet ソース    | `.claude/rules/*.md` の facet 由来ソース                                                                                                  |
| config の allowlist キー | 例: `cli-tools.yaml` の `agents.*.tool` / `*.model`（初期実装ではまず facet ソースのみを対象とし、config allowlist は例示に留めてもよい） |

オーバーレイは `candidates/<cand_id>/overlay/` 配下に repo 相対パスをミラーしたディレクトリ構造で
保持する（例: `overlay/facets/policies/foo.md`）。適用手順は以下の通り。

1. 一時 worktree を作成する。
2. `overlay/` の内容を baseline の対応パスに上書き適用する。
3. `python scripts/orchestra-manager.py context build` 等、facet build 相当の生成コマンドを実行する。
4. 生成された実効設定に対してシナリオを実行する（`## 5. Evaluator 設計`）。

config allowlist キーは JSON Patch 相当（key path + value）で `overlay/config-patch.json` に記述し、
facet ソースと同じ worktree 適用フローに乗せる。

**将来拡張**: schema 付きの operator/adapter（例: 「特定 hook の閾値パラメータを変える」といった
型付き変更）を allowlist に追加することで、任意コード発明ほどの自由度は持たないまま探索空間を
広げる余地を残す。

---

## 5. Evaluator 設計

### シナリオスイート

`packages/meta-harness/scenarios/<target>/*.yaml` に固定シナリオを定義する。

```yaml
id: scenario-id
prompt: "..."
critical:
  - "..."
timeout_ms: 300000
budget:
  max_tokens: 200000
```

### 隔離実行

シナリオは新規サブエージェントで実行し、履歴汚染を避ける。これは skill-evolution の
オフライン評価（FT-07: 新規サブエージェントで並列ディスパッチし学習バイアスを避ける）と
同じ理由による。

### スコアリング

- **hard gate**: critical checklist を全達成しない候補は、他の指標に関わらず不合格として扱う。
- **品質スカラー**: `score = critical_pass_rate * 70 + max(0, 30 - penalty * 5)`（skill-evolution
  の `score_run` ロジックを流用。`penalty` は自己申告の減点項目数）。
- **コストベクトル**: `total_tokens` / `tool_uses` / `duration_ms` を別軸として記録する。
- 品質スカラーとコストベクトルの組で多目的評価とし、Pareto frontier 判定に用いる。

### 評価分散対策

重要な候補（frontier 候補・promotion 検討中の候補）は同一シナリオを N 回評価し、
`mean` / `var` / `min` を ledger に記録する。単発評価による偶然の高スコアを frontier 判定の
根拠にしない。

### EV checks sidecar

`docs/evaluation/<pkg>.md` の EV-NN は人間可読 Markdown であり機械可読ではない。これを補うため
`docs/evaluation/<pkg>.checks.yaml` sidecar を設け、must 観点から段階的に oracle 化する。

```yaml
- ev_id: EV-01
  oracle: artifact_exists
  path: "..."
- ev_id: EV-02
  oracle: json_schema
  schema: "..."
- ev_id: EV-03
  oracle: command_exit
  command: "..."
- ev_id: EV-04
  oracle: rubric_judge
  rubric: "..."
```

oracle 種別は `artifact_exists` / `json_schema` / `command_exit` / `rubric_judge` の 4 種とし、
Markdown 本体（`docs/evaluation/<pkg>.md`）を SSOT として維持したまま、機械判定できる観点のみ
sidecar に段階導入する。

### reward hacking 対策

evaluator（スコアリングロジック）とシナリオ定義は候補オーバーレイの適用範囲外に固定する。
候補が evaluator/シナリオ自体を改変してスコアを詐称することを防ぐため、evaluator と各シナリオの
hash を run metadata（`evaluator_hash` / `scenario_hash`）に記録し、実行時に照合する。

---

## 6. Proposer 設計

proposer は Claude Code サブエージェントとして実装する。store 全体（`candidates/` / `runs/` /
`ledger.jsonl`）への read アクセスを持つ。

- `escalation-strategy.md` の Glob → Grep → 部分 Read の原則に従い、失敗した run のトレースを
  選択的に検査する。これは論文の ablation が示す「生トレースへのフルアクセスが決定的」という
  知見に対応する（スコアのみでは失敗原因を診断できない）。
- 1 イテレーションにつき 1 候補のオーバーレイを生成し、`notes.md` に仮説（何を・なぜ変えたか）
  を記録する。
- **過去トレースは untrusted input として扱う**。proposer プロンプトには prompt injection 警戒
  文を常設し、トレース内のテキストを指示として実行しないことを明記する。

### ガード

- **反復上限**: 10 回。
- **コスト上限**: config で設定（既定は保守的な値）。
- **発散**: 3 回連続で品質スカラーが改善しない場合、人間に通知して停止する。
- **過学習**: holdout シナリオでのスコアが 15pt 以上下落した場合、過学習とみなして候補を却下する。

これらのガードは skill-evolution 設計（`design:skill-evolution` の 3 ガード: 発散/過学習/コスト/
最大反復）から継承する。

---

## 7. 探索ループとライフサイクル

`orchex meta loop` の 1 イテレーションは以下の順で実行する。

```
proposer → register → evaluate → ledger 更新 → frontier 更新 → 停止判定
```

候補の状態遷移は `candidate → evaluated → promoted/retired` の一方向とする。

```
candidate --evaluate--> evaluated --promote(人間承認)--> promoted
                              \--(frontier 外/劣化)-----> retired
```

論文は 1 イテレーションあたり約 10M トークンを要するとしている。本 repo での実装は、これを
そのまま採用するとコストが過大なため、budget は config（`packages/meta-harness/config/
meta-harness.yaml`）で調整可能な形にし、既定値は保守的に設定する。

---

## 8. Promotion 設計

`orchex meta promote <cand_id>` は以下を行う。

1. 対象候補の `overlay/` を実際の facet ソース/config へ適用する PR ブランチを生成する。
2. `facet build` / `context build` / `context sync` を実行し、生成物の整合を取る。
3. 人間レビュー・マージを経て初めて反映が確定する。

探索ループ自体は実ファイルを直接編集しない。promotion のみが実ファイルへの書き込み経路であり、
かつ必ず PR を経由する。これにより **promotion drift**（ループの意図しない変更が無承認で本流に
混入すること）を防ぐ。

---

## 9. skill-evolution との関係（責務分界）

meta-harness と skill-evolution は **sibling package + delegation** の関係とする。

| 項目           | skill-evolution オンライン層                                     | skill-evolution オフライン層（未実装）     | meta-harness                                   |
| -------------- | ---------------------------------------------------------------- | ------------------------------------------ | ---------------------------------------------- |
| 実装状態       | 実装済み                                                         | 未実装（Issue #139）                       | 本設計で新設                                   |
| 実行頻度       | 毎回・軽量                                                       | 閾値/手動起動時                            | 手動起動 or 自動ループ（Phase 3）              |
| 役割           | テレメトリ収集（`metrics/<skill>.jsonl` / `lessons/<skill>.md`） | 単一系譜の漸進改善                         | population ベースの探索・比較・Pareto frontier |
| 本設計での扱い | 維持し、meta-harness のシグナル/シナリオ種として活用             | `meta-harness target=skill` への委譲で実現 | オフライン層の実装先を提供                     |

ADR-20260701-028 の決定 D1（二層構成）・D2（二軸 judge）・D3（改善反映先の出自による塩梅、
facet 自動昇格は当面しない）は、この統合後も **保存** される。D1 のオンライン層はそのまま
シグナル源として使い続け、D2 の二軸 judge ロジック（`score_run`）は meta-harness の evaluator に
流用し、D3 の人間承認ゲート必須という方針は meta-harness の promotion 設計（`## 8`）にそのまま
引き継がれる。

責務は 3 層で整理する。

| 層                         | 対象                                                             |
| -------------------------- | ---------------------------------------------------------------- |
| skill-evolution            | スキル実行そのものの品質（実行時の不明瞭点・裁量補完・再試行等） |
| skill-review-policy 4 視点 | 成果物の品質（Security/Perf/Quality/a11y）                       |
| meta-harness               | ハーネス構成（facet/config）自体の最適化                         |

「単一系譜の漸進改善」から「population ベースの探索」への一般化により、skill 対象の改善も
facet/config 対象の改善も同じ store・evaluator・proposer・promotion 基盤に載る。

---

## 10. パッケージ構成と CLI

```
packages/meta-harness/
  manifest.json          # depends: [core]（skill-evolution への依存は最小に留め、必須にしない）
  scripts/
  config/meta-harness.yaml
  scenarios/<target>/*.yaml
  schemas/
```

`manifest.json` の依存は `core` のみを推奨とする。skill-evolution への依存は `target=skill` の
委譲を使う場合にのみ必要になるため、必須依存にはしない。

`orchex meta` サブコマンド群:

| サブコマンド | 役割                                     |
| ------------ | ---------------------------------------- |
| `init`       | `.claude/meta-harness/` の初期化         |
| `register`   | 候補（オーバーレイ + メタデータ）の登録  |
| `evaluate`   | 候補に対するシナリオ実行と評価           |
| `frontier`   | ledger から Pareto frontier レポート生成 |
| `propose`    | proposer を 1 回起動（人間起動）         |
| `loop`       | 探索ループの自動反復（Phase 3）          |
| `promote`    | 勝者候補の PR ベース昇格                 |
| `status`     | population / frontier の状態表示         |
| `purge`      | 古い世代・retired 候補の削除             |

`orchestra-manager.py` への追加位置は、既存の複合サブコマンドパターン（`context build/check/sync`
等）を踏襲し、`facet` サブコマンド群と `setup` サブコマンド群の間に配置する。

---

## 11. セキュリティ・リスクと対策

| リスク                                                          | 対策                                                                                    |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| reward hacking（候補が evaluator/シナリオを改変してスコア詐称） | evaluator・シナリオは overlay 適用範囲外に固定し、hash を run metadata に記録・照合する |
| 過去トレースへの prompt injection                               | トレースは untrusted input として扱い、proposer プロンプトに警戒文を常設する            |
| worktree 混線（複数セッション/プロジェクトの取り違え）          | `ai_orchestra_dir` / `project_root` / `source_commit` を run metadata に必須記録する    |
| LLM 評価の分散                                                  | frontier 候補・重要候補は N 回評価し `mean`/`var`/`min` を記録する                      |
| promotion drift（無承認の本流混入）                             | 昇格は必ず PR diff + `facet build` + `context build`/`sync` を経由させる                |
| store の肥大化                                                  | retention/purge（`orchex meta purge`）+ `events.jsonl.gz` 圧縮                          |
| トークンコストの膨張                                            | budget・反復上限・コストガードをハード制約として実装する                                |

---

## 12. 段階導入計画

### Phase 1: 計測基盤（proposer/promotion なし）

- store scaffold、`register`、baseline 候補の登録
- 1 target 分のシナリオスイート
- `evaluate` / ledger / `frontier`
- **完了条件**: 現行ハーネス（baseline 候補）を登録し、シナリオスイートで評価し、ledger と
  frontier レポートが再現可能に得られる。これ単体で独立した価値（現行ハーネスの再現可能な計測）
  を持つ。

### Phase 2: 提案と昇格（人間起動）

- `propose`（人間起動、1 イテレーション）
- EV checks sidecar（`docs/evaluation/<pkg>.checks.yaml`）
- `promote`
- **完了条件**: proposer が生成した候補が baseline を上回る場合、人間承認を経て PR ベースで
  facet/config に反映できる。

### Phase 3: 自動探索とスコープ拡大

- `loop`（自動反復、ガード付き）
- 対象拡大（routing config、将来的に外部 CLI ハーネス）
- **完了条件**: 停止条件・3 ガード（発散/過学習/コスト/反復上限）付きの自動ループが、人間の介入
  なしに population を探索し frontier を更新できる（promotion は引き続き人間承認必須）。

---

## 13. 未解決事項

- 隔離実行の具体機構 → 解決済み（詳細設計 `design:meta-harness-detailed` §2-1 参照）。一時 git worktree 方式に確定。
- シナリオ実行時の Claude Code 呼び出し方式 → 解決済み（詳細設計 `design:meta-harness-detailed` §2-2 参照）。`claude -p` + `stream-json` に確定。CLI 2.1.201 で実機検証済み。
- 評価コスト（1 候補あたりのトークン消費・実時間）の実測値がなく、Phase 1 の budget 既定値は仮置きである（Phase 1b スパイクで実施予定、詳細設計 `design:meta-harness-detailed` §8）。
- `docs/evaluation/skills/`（スキルフロー評価セット）との整合方法は未検討。EV checks sidecar の対象をパッケージ評価セットのみとするかスキルフロー評価セットにも広げるか要判断。
- config allowlist キーの具体的な初期セット（`cli-tools.yaml` 以外に何を含めるか）は Phase 1 実装時に確定する。
